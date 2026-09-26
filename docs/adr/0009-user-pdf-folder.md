# ADR 0009: Ingesting a folder of user-supplied patent PDFs

* Status: proposed (between Phases 4 and 5)
* Date: 2026-09-26

## Context
The owner keeps patents as PDF files named by publication number. Most of them are scanned
images with no text layer (in the first sample, 169 of 193 had none), and OCR is out of
scope. The PDFs therefore cannot be the data source themselves.

## Decision
* **The file name is the request, and a data source is the record.** `plr ingest folder
  FOLDER` scans `*.pdf` recursively (case-insensitive), reads each file stem as a
  publication number, and runs an ordinary lookup batch against a configured source
  (`--source`, default `google_patents`). The data comes only from the source's
  response, never from the PDF.
* **Provenance:** the batch's `query_text` records `{"type": "user_folder", "folder": ...,
  "files": [...]}`, with each file's name, size and sha256. That way every stored record
  traces back to the exact file that asked for it.
* **Checks before fetching:**
  * a file stem that is not a publication number is reported, not guessed;
  * where a PDF has a text layer, the first two pages are checked for the number (digits
    only, separators ignored);
  * a mismatch is reported in the scan summary.

  `--scan-only` prints this summary and fetches nothing. A missing folder, an empty
  folder and an unreadable PDF are errors.
* **Kind-code fallback:** if a number with a kind code is not found, the adapter retries
  once with the bare country and number (Google lists some reissues as `E1`, not `E`).
  The stored document keeps the publication the source actually served, and the item's
  detail says what was requested and what was stored. Matching ignores only the kind in
  this case.

## Consequences
* Patents too recent for the source are reported as `not_found` and can be looked up again
  later. On 2026-09-26, 21 US grants from 2026 were not yet on Google Patents.
* Two files naming the same publication (e.g. `US9288556` and `US9288556B2`) end up as one
  stored document and one `duplicate` outcome.
* Search-capable sources and parsing of structured local files (XML, CSV, JSON) remain
  open; see ADR 0007.
