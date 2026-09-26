# ADR 0007: Ingestion pipeline and the Google Patents page adapter

* Status: proposed (Phase 3)
* Date: 2026-09-25

## Decision
* **Raw first, content-addressed.** Every fetched payload goes to object storage under
  `raw/<source>/<sha256[:2]>/<sha256>` before anything is derived from it. Re-storing the
  same bytes verifies the stored copy instead of overwriting it, and every read checks the
  hash.
* **One outcome per requested key** (`ingest_item`, append-only): `stored`, `quarantined`,
  `duplicate`, `not_found`, `invalid_request` or `failed`.
  * Keys are de-duplicated on the normalised publication number, so spelling variants
    count once.
  * The served publication must match the request (the kind code only if one was
    requested); otherwise the item is quarantined. The one exception is the kind-code
    fallback of ADR 0009: after it, only the kind's letter must match (E and E1, B1 and B2).
  * A key that resolves to a publication already stored in the batch (e.g. `EP3123456` and
    `EP3123456A1`) is recorded as `duplicate` and no second document is stored.
  * A session advisory lock stops two runs of one batch from interleaving.
  * An unexpected error marks the batch `failed` (audit event `batch_aborted`). The CLI
    prints the batch ID before starting, so the run can always be resumed.
  * Resume refuses a batch created with a different adapter version or source format.
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
  * the office's text is `load-source="patent-office"`, or `load-source="docdb"` with
    `source="national office"`. The DOCDB form is how about half of all abstracts are
    delivered; it has been accepted since adapter version 5. It was checked over the 2,001
    stored pages on 2026-09-26: abstracts come as docdb/national office (1,011) or
    patent-office (984), and claims and descriptions as patent-office or WIPO-OCR;
  * text marked as OCR (`WIPO-OCR`) or as a machine translation is recorded as `unparseable`;
  * family IDs, priority claims and IPC codes are not on the page, so they are recorded as
    `not_provided_by_source`;
  * Google's legal-status wording is mapped by a fixed table (unknown wording becomes `other`)
    and kept verbatim, and a new `active` category (granted and in force) was added in
    migration 0003.
* **Citations** come from the page's root-level lists, which are complete per publication:
  every citing publication (e.g. 22 for US10000000B2). The Family section's tables show one
  publication per family and are not used. Examiner and third-party markers are kept for
  patent and non-patent citations.
* **Grant date** is set only when this publication is the grant, meaning the application's
  grant date equals this publication's own publication date. Application publications (A1,
  WO) record `not_applicable`.
* **Contract tests** run on six real recorded pages (US grant, US application, EP, WO, CN,
  1992 US). Stored citation counts are cross-checked against the page's own "Patent
  Citations (N)" and "Non-Patent Citations (N)" headings. If the page structure changes,
  the tests fail.
* **Migration note:** downgrading below 0003 fails once any document has status `active`,
  because documents are append-only. That is intended: history is never rewritten to fit
  an older schema.

## Re-normalising stored pages
`plr ingest renormalize BATCH` re-reads a batch's stored pages with the current adapter
into a new batch. Nothing is fetched.
* The original retrieval times and raw objects are kept, and the batch reconciles as usual.
* Keys that were never fetched are counted in the provenance, not replayed.
* A kind fallback from before adapter 4 recorded it is inferred from the page: a page
  requested with a kind code is served as exactly that kind, so another kind of the same
  number can only come from the bare-number lookup.

## Not yet done
* Search-capable sources (patent APIs, Google BigQuery) and local-file adapters (XML, CSV,
  JSON, PDF). They wait for the owner's credentials and sample files.
* Rate limiting is per process. A multi-worker limiter comes with the workflow engine.
