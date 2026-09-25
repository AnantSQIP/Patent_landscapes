# ADR 0003: Object storage image for local development and tests

* Status: accepted (Phase 0)
* Date: 2026-09-25

## Context
The build prompt (§5) names MinIO for on-prem S3-compatible storage. As of this ADR,
MinIO upstream is discontinued:
* The `minio/minio` and `minio/mc` GitHub repositories are archived.
* The official Docker Hub images have been removed.
* Quay requires authentication.
* `dl.min.io` returns *410 Gone*.

The final server release is `RELEASE.2025-10-15T17-29-55Z`.

## Decision
* **Local dev and tests** use `alpine/minio`, pinned by digest to the final release:
  `sha256:cf23643a…e84b4`. `github.com/alpine-docker/minio` builds this image from the
  official source in public CI.
* **Bucket creation** uses the official `amazon/aws-cli` image (pinned version) instead
  of the discontinued `mc`.
* **Permissions:** the image runs as uid 100 while volume mount points are root-owned, so
  the dev/test container runs as root.
* **Single source of truth:** image references live in `tests/support.py`, and
  `tests/unit/test_compose.py` asserts that `docker-compose.yml` uses the same ones. The
  same test forbids `:latest` tags.

## Consequences
* The application only speaks the S3 API through boto3, so the storage backend can be
  swapped by configuration alone.
* MinIO receives no further security fixes, so **it must not be used in production.** Use
  AWS S3, or re-evaluate a maintained S3-compatible server (SeaweedFS, Garage) in Phase 12.
