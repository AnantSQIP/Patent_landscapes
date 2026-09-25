"""Dataset build and report on real Postgres + MinIO, from the recorded Google pages."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from patsquire_plr.cli import app
from patsquire_plr.config import ObjectStorageSettings
from patsquire_plr.db.datasets import (
    DatasetError,
    build_dataset,
    render_report_markdown,
    report_dataset,
)
from patsquire_plr.db.models import AuditEvent, DatasetFamilyMember
from patsquire_plr.ingest.rawstore import RawStore
from patsquire_plr.ingest.runner import run_lookup_batch, start_lookup_batch
from tests.integration.test_ingestion import _pages, _serve, _source
from tests.support import TEST_DB_PASSWORD, base_config, base_secrets

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALIASES = REPO_ROOT / "config" / "applicant_aliases.yaml"
NUMBERS = ["US10000000B2", "US20160266243A1", "CN112345678A", "EP3123456A1"]


def _ingest(engine: Engine, store: ObjectStorageSettings, numbers: list[str]) -> str:
    source = _source(_serve(_pages()))
    batch = start_lookup_batch(engine, source, numbers)
    assert run_lookup_batch(engine, RawStore(store), source, batch).status == "complete"
    return str(batch)


def test_dataset_groups_families_and_accounts_for_every_document(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:
    first = _ingest(db_engine, object_storage, NUMBERS)
    second = _ingest(db_engine, object_storage, ["US10000000B2"])  # a re-fetched copy

    dataset_id = build_dataset(
        db_engine, name="test", batch_ids=[uuid.UUID(first), uuid.UUID(second)], alias_file=ALIASES
    )
    report = report_dataset(db_engine, dataset_id)

    assert (report.input_documents, report.selected, report.publications) == (5, 4, 4)
    assert report.excluded == {"superseded_copy": 1}
    assert report.families == 3  # the US A1 and B2 are one family
    with Session(db_engine) as session:
        members = session.scalars(
            select(DatasetFamilyMember).where(
                DatasetFamilyMember.dataset_id == dataset_id, DatasetFamilyMember.in_dataset
            )
        ).all()
        family_of = {m.publication: m.family_key for m in members}
        assert family_of["US20160266243A1"] == family_of["US10000000B2"] == "US10000000B2"
        assert family_of["CN112345678A"] != family_of["EP3123456A1"]
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "dataset_built")
        ).one()
        assert event.payload["publications"] == 4
    assert report.config["name_rules_version"] == "2"
    assert report.conflicts == []  # identical re-fetch of the same page


def test_dataset_rows_are_immutable(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:

    batch = uuid.UUID(_ingest(db_engine, object_storage, ["US10000000B2"]))
    build_dataset(db_engine, name="frozen", batch_ids=[batch], alias_file=ALIASES)
    with db_engine.connect() as conn, pytest.raises(DBAPIError, match="append-only"):
        conn.execute(text("UPDATE dataset_family SET family_key = 'x'"))


def test_report_markdown_names_every_section(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:

    batch = uuid.UUID(_ingest(db_engine, object_storage, NUMBERS))
    markdown = render_report_markdown(
        report_dataset(
            db_engine, build_dataset(db_engine, name="md", batch_ids=[batch], alias_file=ALIASES)
        )
    )
    for heading in (
        "## Conservation",
        "## Applicant name merges",
        "## Look-alike names to review",
        "## Conflicts between copies",
    ):
        assert heading in markdown


def test_incomplete_or_unknown_batches_are_refused(
    db_engine: Engine, object_storage: ObjectStorageSettings
) -> None:

    source = _source(_serve(_pages(), fail={"US10000000B2"}))
    failed = start_lookup_batch(db_engine, source, ["US10000000B2"])
    run_lookup_batch(db_engine, RawStore(object_storage), source, failed)
    with pytest.raises(DatasetError, match="not complete"):
        build_dataset(db_engine, name="x", batch_ids=[failed], alias_file=ALIASES)
    with pytest.raises(DatasetError, match="unknown ingest batches"):
        build_dataset(db_engine, name="x", batch_ids=[uuid.uuid4()], alias_file=ALIASES)
    with pytest.raises(DatasetError, match="at least one"):
        build_dataset(db_engine, name="x", batch_ids=[], alias_file=ALIASES)


def test_cli_build_and_report(
    db_engine: Engine,
    object_storage: ObjectStorageSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _ingest(db_engine, object_storage, NUMBERS)
    url = db_engine.url
    data = base_config()
    data["database"].update(host=url.host, port=url.port, name=url.database)
    config = tmp_path / "settings.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    for name, value in {**base_secrets(), "PLR__DATABASE__PASSWORD": TEST_DB_PASSWORD}.items():
        monkeypatch.setenv(name, value)

    built = CliRunner().invoke(
        app,
        [
            "dataset",
            "build",
            "--batch",
            batch,
            "--name",
            "cli",
            "--aliases",
            str(ALIASES),
            "--config",
            str(config),
        ],
    )
    assert built.exit_code == 0, built.output
    assert "# Dataset report: cli" in built.stdout
    dataset_id = built.stdout.split("Dataset `")[1].split("`")[0]
    as_json = CliRunner().invoke(
        app, ["dataset", "report", dataset_id, "--output", "json", "--config", str(config)]
    )
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout)["publications"] == 4
    bad = CliRunner().invoke(
        app, ["dataset", "report", dataset_id, "--output", "xml", "--config", str(config)]
    )
    assert bad.exit_code == 2


def test_report_shows_alias_merges_and_unknown_family_members(
    db_engine: Engine, object_storage: ObjectStorageSettings, tmp_path: Path
) -> None:
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text(
        "version: 1\ngroups:\n  - canonical: Raytheon Technologies\n"
        "    variants: [Raytheon Co, Hughes Aircraft Co]\n"
        "    reason: test-only alias group for the report\n",
        encoding="utf-8",
    )
    batch = uuid.UUID(_ingest(db_engine, object_storage, ["US10000000B2", "US5093563A"]))
    report = report_dataset(
        db_engine, build_dataset(db_engine, name="alias", batch_ids=[batch], alias_file=aliases)
    )

    [merge] = report.name_merges
    assert merge.entity == "Raytheon Technologies"
    assert merge.spellings == ["Hughes Aircraft Co", "Raytheon Co"]
    assert merge.keys == ["hughes aircraft", "raytheon"]
    assert merge.alias_reason == "test-only alias group for the report"
    # the 1992 page has no "Also Published As" table: shown as unknown, not as a family of one
    assert report.family_members_unknown == {"not_provided_by_source": 1}
    assert "source_stated_family" in str(report.config["family_definition"])
    markdown = render_report_markdown(report)
    assert "Raytheon Technologies" in markdown
    assert "## Family statements to check" in markdown
