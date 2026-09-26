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
  * where a PDF has a text layer, the first two pages must contain the number's digits as
    a whole number (digit-group separators such as "10,521,786" are ignored);
  * a mismatch is reported in the scan summary.

  This check confirms that the number appears on the first pages, not where. A cover page
  listing cited patents can still let a misnamed file pass. `--scan-only` prints the summary
  and fetches nothing, without needing database settings. A missing folder, an empty folder
  and an unreadable file are errors. The folder is recorded as an absolute path.
* **Kind-code fallback** (Google Patents adapter version 4): if a number with a kind code is
  not found, the adapter retries once with the bare country and number. Google lists some
  reissues as `E1`, not `E`.
  * The served document is accepted only if its kind has the **same letter** as the
    requested one (E and E1, B1 and B2). An application (A) is never stored for a
    requested grant (B), or the other way round; that item is quarantined.
  * The item's detail always records what was requested, what was looked up and what was
    served, whatever the outcome (stored, duplicate or quarantined).
  * A retry that fails (e.g. HTTP 503) leaves the item `failed`, so it can be resumed. It
    is never recorded as `not_found`.

## Consequences
* Patents too recent for the source are reported as `not_found` and can be looked up again
  later. On 2026-09-26, 21 US grants from 2026 were not yet on Google Patents.
* Two files naming the same publication (e.g. `US9288556` and `US9288556B2`) end up as one
  stored document and one `duplicate` outcome.
* Search-capable sources and parsing of structured local files (XML, CSV, JSON) remain
  open; see ADR 0007.
