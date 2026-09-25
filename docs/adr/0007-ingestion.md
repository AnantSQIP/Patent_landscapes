# ADR 0007: Ingestion pipeline and the Google Patents page adapter

* Status: proposed (Phase 3)
* Date: 2026-09-25

## Decision
* **Raw first, content-addressed.** Every fetched payload goes to object storage under
  `raw/<source>/<sha256[:2]>/<sha256>` before anything is derived from it. Re-storing the
  same bytes verifies the stored copy instead of overwriting it, and every read checks the
  hash.
* **One outcome per requested key** (`ingest_item`, append-only): `stored`, `quarantined`,
  `not_found`, `invalid_request` or `failed`.
  * An item's raw record, documents or quarantine entry, and its outcome commit in one
    transaction.
  * A batch is `complete` only when every key has a final outcome and the row counts match
    the outcomes (Layer 2). Otherwise it is `failed`, and `plr ingest resume` retries only
    the failed keys.
  * Start and finish are written to the hash-chained audit log.
* **The batch records its source:** the adapter version and the provider's format version
  (`source_api_version`), plus the exact list of requested keys (duplicates removed and
  counted).
* **First adapter: Google Patents pages, lookup by number.** It needs no credentials, and
  robots.txt allows `/patent/` pages. It uses the original-language page (no language
  suffix). Title, abstract and claims are stored only when the page marks them as the
  patent office's text:
  * text marked as OCR (`WIPO-OCR`) or as a machine translation is recorded as `unparseable`;
  * family IDs, priority claims and IPC codes are not on the page, so they are recorded as
    `not_provided_by_source`;
  * Google's legal-status wording is mapped by a fixed table (unknown wording becomes `other`)
    and kept verbatim, and a new `active` category (granted and in force) was added in
    migration 0003.
* **Contract tests** run on six real recorded pages (US grant, US application, EP, WO, CN,
  1992 US). If the page structure changes, the tests fail.

## Not yet done
* Search-capable sources (patent APIs, Google BigQuery) and local-file adapters (XML, CSV,
  JSON, PDF). They wait for the owner's credentials and sample files.
* Rate limiting is per process. A multi-worker limiter comes with the workflow engine.
