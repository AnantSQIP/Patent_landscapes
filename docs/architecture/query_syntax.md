# Provider query syntax (verified 2026-09-26)

Phase 5 generates search strings by code (`src/patsquire_plr/landscape/queries.py`). This
page records where each piece of syntax comes from. Anything the official documentation
does not show is marked **unverified**. It must be confirmed with a live call once
credentials exist, before counts from that provider are used in a report.

Each segment has one logical query:
(keywords in title/abstract) AND (CPC codes, including everything below them) AND
(publication date in range) AND (offices).

## EPO Open Patent Services (OPS) v3.2: `epo_ops_cql`

Source: *OPS RESTful Web Services Reference Guide* v1.3.20 (June 2024),
https://link.epo.org/web/searching-for-patents/data/en-ops-v3.2-documentation-version-1.3.20.pdf
(linked from https://www.epo.org/en/searching-for-patents/data/web-services/ops).

**Endpoint and authentication**
- Endpoint: `GET|POST https://ops.epo.org/3.2/rest-services/published-data/search`.
- Authentication is OAuth2 client credentials:
  - `POST https://ops.epo.org/3.2/auth/accesstoken` with `Authorization: Basic base64(key:secret)`;
  - the token lasts about 20 minutes (p. 36–37).

**Query language (CQL, appendix 4.2, p. 142–148)**
- `ta`: title or abstract. There is **no claims or full-text index** in the search.
- Phrases: `ta="green energy technology"`.
- Operators: `and`, `or`, `not`, with parentheses.
- CPC: `cpc=/low A01B` means "classified with A01B and all subclasses".
  - We generate `cpc=/low <group>`, e.g. `cpc=/low G06N3/045`.
  - The documented examples show `/low` with a subclass and inside a `prox` example with a
    group. **Unverified:** a stand-alone `/low` at subgroup level.
- Dates: `pd within "20170101 20241231"`.
  - There is **no priority-date index**, which is why the scope's dates are publication
    dates.
- Office: there is no country field. Offices are given as a publication-number prefix:
  `pn=US` ("published by").
- Result counts: `@total-result-count` on `ops:biblio-search`.
  - It is **capped at 10,000**.
  - At most 2,000 results can be paged, 100 per request (p. 59–61).
- Query length and term limits are **unverified**, since the guide does not state them. The
  guide's error for an invalid query is `400 CLIENT.CQL`.

## Lens.org Patent API: `lens_json`

Sources:
- https://docs.api.lens.org/getting-started.html
- https://docs.api.lens.org/request-patent.html
- https://docs.api.lens.org/response-patent.html
- https://docs.api.lens.org/examples-patent.html

**Endpoint and authentication**
- `POST https://api.lens.org/patent/search` with `Authorization: Bearer <token>`.

**Query language:** Elasticsearch-style DSL
- `bool` with `must`/`should`/`must_not`.
- `match_phrase` on `title` and `abstract`.
- `terms` on `class_cpc.symbol`.
- `range` on `date_published` (`YYYY-MM-DD`).
- `terms` on `jurisdiction`.

**CPC hierarchy**
- Lens documents no "and below" operator. We therefore list every group below each chosen
  code explicitly, taken from the CPC scheme.

**Counts**
- Counts come from `total` in the response. We send `size: 0`. **Unverified:** the docs
  imply `size: 0` is accepted (`max_score` is 0 when size is 0) but do not document it
  as a way to count.
- **Unverified:** a `bool` holding only `should` clauses requires at least one match
  (Elasticsearch semantics; Lens calls its DSL "modified").

**Limits**
- `from`/`size` paging stops at 10,000 results; beyond that, use `scroll`.
- The rate limit depends on the plan.

## Google BigQuery `patents-public-data.patents.publications`: `bigquery_sql`

Sources:
- Schema: https://github.com/google/patents-public-data/blob/master/tables/dataset_Google%20Patents%20Public%20Datasets.md
- Example: https://github.com/google/patents-public-data/blob/master/examples/claim-text/claim_text_extraction.ipynb

**Fields**
- `publication_number`, `country_code`, and `publication_date` (INT64 `YYYYMMDD`).
- `title_localized[].text` and `abstract_localized[].text`.
- `cpc[].code`.

**What we generate**
- `COUNT(DISTINCT publication_number)`.
- Text is matched with `REGEXP_CONTAINS(LOWER(text), r'\b(?:...)\b')`.
- CPC matching:
  - a subclass uses `SUBSTR(c.code, 1, 4) = 'G06F'`, as in the official example;
  - group codes are listed explicitly with `IN (...)`.

**Unverified**
- The date format is evidenced by official notebooks, not stated in the schema.
- Query cost (billed by bytes scanned) was not checked.

## USPTO PatentsView PatentSearch API: not generated

On 2026-09-26, `search.patentsview.org` did not resolve, and patentsview.org redirects to
https://data.uspto.gov/support/transition-guide/patentsview. That page says the
PatentSearch API is interrupted, gives no launch date for its replacement, and says old
keys will not work.

Queries for it are therefore not generated. Its documented syntax is kept here for when the
replacement ships:
- JSON operators `_and`, `_or`, `_text_phrase`, `_begins` and `_gte`/`_lte`.
- Fields `patent_title`, `patent_abstract`, `cpc_current.cpc_group_id` and `patent_date`.
- Header `X-Api-Key`.
- The total count is returned as `total_hits`.

Source: https://github.com/PatentsView/PatentSearch-API.

## Google Patents: no search

Google Patents has no documented search API. Its robots.txt
(https://patents.google.com/robots.txt) allows only `/`, `/advanced`, `/patent/` and
`/sitemap/`; everything else is disallowed:

```
User-agent: *
Disallow: /*
Allow: /$
Allow: /advanced$
Allow: /patent/
Allow: /sitemap/
```

The system therefore uses only `/patent/<number>/` pages. Key-less discovery follows the
citations those pages state (citation expansion); it never uses the search pages.

## Local evaluation: `local`

The same logical query is applied in code to records that are already stored. This is how
counts are made for the citation-expansion universe.

**Text matching** (the same pattern is used for BigQuery):
- Words are letters and digits of any script; matching is case-insensitive.
- Hyphens and spaces are treated alike.
- The final word of a term may be plural ("s"/"es"): "language model" finds "language
  models", and "LLM" finds "LLMs". There is no other stemming.
- Title and abstract are matched separately.
- A term without letters or digits is refused.

**CPC matching**
- CPC matching uses the scheme's hierarchy.
- A code missing from the loaded version is decided from its symbol where that settles
  it: a different subclass or main group is outside, and a code inside a queried subclass
  is inside.

**Records that cannot be evaluated**
- A record is "not evaluable" when there is no date, when the text did not match and the
  title or abstract is missing, when it has no codes, or when an unknown code in the same
  main group leaves the CPC condition unsettled.
- Such records are counted separately, and the count is marked as a lower bound.
