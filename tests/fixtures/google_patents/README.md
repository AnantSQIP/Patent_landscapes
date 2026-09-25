# Google Patents page fixtures (TEST-ONLY)

Real pages from `https://patents.google.com/patent/<number>/` (original-language pages; the
path is allowed by the site's robots.txt), fetched 2026-09-25 and stored gzipped with
`<script>`/`<style>` removed. Used by the contract tests in
`tests/unit/test_google_patents.py`. Refresh them when Google changes the page structure;
the contract tests are designed to fail loudly when it does.
