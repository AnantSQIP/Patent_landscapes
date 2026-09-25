# ADR 0008: Datasets, patent families and applicant names

* Status: proposed (Phase 4)
* Date: 2026-09-25

## Decision
* **A dataset is an immutable snapshot** built from complete ingest batches (six append-only
  tables, migration 0006). It records its configuration (copy rule, family evidence, name
  rules version, alias file hash) and a `dataset_built` audit event.
* **One copy per publication:** the most recently retrieved copy is kept, and the others are
  excluded as `superseded_copy`. Every field on which the copies differ is stored as a
  conflict and shown in the report, never silently resolved.
* **Families (simple families)** are found by union-find over three kinds of evidence:
  * stated family members (e.g. Google's "Also Published As" / `docdbFamily` table);
  * equal source family IDs;
  * the same application (office-scoped, formatting-free number).

  The key is the smallest publication number of the family inside the dataset. Stated
  members that were not retrieved are kept, marked as not in the dataset, so family size
  and office counts stay complete. Grouping follows what the sources state. Google's list
  also includes continuations, which a strict DOCDB definition would place in a separate
  family. A source that supplies DOCDB family IDs (EPO OPS, Lens) makes this exact.
* **Applicant names:**
  * Formatting-only rules: NFKC, case, "&", punctuation, and trailing legal-form suffixes,
    stripped until none remain, so the rules are idempotent.
  * A human-curated alias file for ownership relations. Every group needs a reason, and the
    file ships empty.
  * Look-alike keys are listed for review, never merged automatically.
  * The display name is the alias canonical name, or else the most common spelling.
* **Conservation (Layer 2)** is checked before anything is stored:
  * every input document has exactly one decision;
  * every publication is selected once and belongs to exactly one family;
  * applicant rows match applicants.
* **Proportional parsing:** one unparseable number in a family or citation list marks only
  that field `unparseable`, with a warning on the ingest item. Singapore numbers with a
  trailing check letter (`SG10201707936TA`) are accepted.
* **Backfill:** documents stored before `family_members` existed are marked
  `not_requested` in migration 0005, the one controlled exception to append-only.

## Source behaviour noted
* Google Patents harmonises assignee names itself. Even a 2010 patent originally assigned to
  "Google Inc." shows "Google LLC" as its original assignee. On Google-only data few
  spelling variants remain; the rules matter most once several sources are combined.
