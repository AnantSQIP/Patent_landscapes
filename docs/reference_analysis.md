# Reference PLR analysis

Phase 1 deliverable (build prompt §12). This is a study of how five professionally produced
Patent Landscape Reports are *built*: their structure, chart types, metrics and methodology.
It feeds the template specification in [`template/plr_template.yaml`](../template/plr_template.yaml).

All descriptions here are our own. No text from the reports is reproduced, and nothing in
this document may be copied into templates or generated reports (principle 9).

## 1. Method

| Step | How |
|---|---|
| Structure | `plr reference extract` reads PDF bookmarks (4 of 5 reports) or, for the hydrogen report, which has none, headings found by font size. It also records every figure/table/box caption with its page. Output goes to `reference/extracted/*.json` (git-ignored). |
| Reading | Each report was read in full, apart from the non-patent chapters 4–8 of the quantum report, which were skimmed for methods only. |
| Charts | `plr reference render` rendered pages whose chart type could not be read from text: the wind maturity map (p26), the GenAI model→mode Sankey (p58), the hydrogen applicant×segment bubble matrix (p33) and the Royal Society ThemeScape map (p69). Each was inspected visually. |
| Identity | Every report is identified by file name and SHA-256 in the extract JSON. |

Short names used below:

| ID | Report | Publisher, date | Pages |
|---|---|---|---|
| `genai` | Generative AI patent landscape | WIPO, 2024 | 116 |
| `wind` | Offshore wind energy patent insight report | EPO & IRENA, Nov 2023 | 59 |
| `hydrogen` | Hydrogen patents for a clean energy future | EPO & IEA, Jan 2023 | 70 |
| `quantum` | Mapping the global quantum ecosystem | EPO & OECD (joint), Dec 2025 | 163 |
| `ai_science` | AI-related inventions: AI patent landscape | IP Pragmatics for the Royal Society, May 2023 | 206 |

## 2. Report by report

### 2.1 `genai` (WIPO)
- **Structure.**
  - Key findings come first, then an introduction and a plain-language technology primer.
  - Next is a global chapter: trend, top owners, inventor locations, filing jurisdictions.
  - Then three segmentation chapters, one per axis: *models*, *modes*, *applications*.
    Every one of them repeats the same blocks: trend and growth, then top companies and top
    universities (separately), then inventor countries.
  - Cross-axis connections are shown as Sankey diagrams, with the numbers in appendix tables.
  - It closes with "further considerations" (concerns, limitations of patent analysis) and
    appendices: methodology, indicators, full search queries, the semantic-search concept
    list, and one example patent per application.
- **Counting.**
  - Unit: simple patent families.
  - Time basis: the family's **first publication year**.
  - Owners are consolidated to the ultimate owner. Each inventor country counts once per family.
- **Search and filtering.** Two stages.
  - Recall stage: keyword+classification queries unioned with ~100 semantic-search concepts.
  - Precision stage: a fine-tuned BERT classifier trained on seed positives and "near-miss"
    negatives. On a test sample it scored precision 0.8, recall 0.9, F1 0.85. This is the
    only report of the five that discloses filter accuracy.
- **Segments.**
  - Multi-label assignment by keyword/class rules.
  - Large residual "other/unassigned" buckets are disclosed: only about a quarter of families
    map to a model.
  - Every segment chart carries a note that segments don't sum to the total.
- **Charts.**
  - Multi-series trend lines.
  - Top-20 owner bars comparing the whole period with the latest two years.
  - Share-vs-growth charts.
  - Paired growth bars for two windows.
  - Owner×segment and country×segment heat tables.
  - Sankey flows.
  - Every chart has a one-sentence takeaway title and a standard source line.
- **Adopt:**
  - The identical per-segment block.
  - Multi-axis segmentation with cross-matrices.
  - Disclosed classifier metrics.
  - Near-miss negatives.
  - Disclosed residual buckets.
  - Takeaway titles and source lines.
- **Avoid:** its internal numeric inconsistencies. The same top-country share appears with two
  different values, and a category total differs between a figure and a table. These are
  exactly what automated claim verification (Layer 5) must prevent.

### 2.2 `wind` (EPO–IRENA)
- **Structure.**
  - Executive summary with policy insights, then a data-trend summary.
  - Methodology chapter.
  - Overall-trends chapter: filings, applicant countries, filing offices, applicants,
    maturity map, citations.
  - One section per technology grouping, each with a fixed a/b/c figure panel (trend by
    sub-query, top countries, top applicants).
  - Themed side "boxes" (standards, newcomers, recycling, …).
  - Conclusion and glossary.
- **Counting.**
  - Unit: families, with **international patent families (IPFs)** as the headline unit.
  - Time basis: earliest publication year within the family.
  - The last year is flagged as incomplete.
  - A family counts as an IPF if it has filings at more than one office, or its applicants or
    inventors come from different countries. Regional (EP) and PCT filings count as
    international by default.
- **Search.**
  - A broad base query plus a hierarchy of query IDs: groupings → queries → sub-queries, with an
    "other" suffix for residuals.
  - Queries are written by examiners.
  - Overlaps and double counting are explicitly accepted and disclosed.
  - The query text is published only as a separate spreadsheet, not in the PDF. No recall or
    precision validation is reported.
- **Metrics:**
  - Granted share (at least one grant in the family).
  - Newcomers (first filing on or after a cut-off year).
  - Applicant sector (company / public body / individual / university).
  - Forward citations in fixed bins, with self-citations removed.
  - Cited×citing country matrix.
  - Co-operation pairs above a threshold.
- **Signature chart: the maturity map.** One point per year, x = number of applicants,
  y = number of IPFs, bubble size = granted patents, coloured by fixed five-year phases.
- **Adopt:**
  - The hierarchical query-ID scheme.
  - The "all families vs IPFs" toggle on every chart.
  - Fixed period buckets reused across charts.
  - The maturity map, the newcomer rule and granted share.
  - A disclosed exclusion count (records removed as out of scope).
- **Avoid:** publishing query text only outside the report. Our appendix must contain it.

### 2.3 `hydrogen` (EPO–IEA)
- **Structure.**
  - Forewords, then an executive summary and numbered key findings.
  - An introduction with a taxonomy diagram.
  - An overview chapter: geography and specialisation, innovation clusters, established vs
    emerging actors, start-ups.
  - Three chapters that follow the **value chain**: production, storage/distribution/
    transformation, end use.
  - No methodology chapter and no annex. Method notes are scattered through footnotes and
    figure notes.
- **Counting.**
  - Unit: IPFs, by publication year.
  - Countries are attributed by applicant, with **fractional counting** for co-applicants.
  - Segment membership can overlap, and that is stated.
- **Taxonomy.** Two axes: value-chain stage × "established" vs "motivated by climate". The
  second axis uses explicit rules (for example, capture of CO₂ must be stated in the patent).
- **Metrics:**
  - **Revealed technological advantage (RTA):** a country's share in a field divided by its
    share across all fields.
  - Growth rates over a fixed window.
  - Top-10 concentration.
  - University/public-research share.
  - Innovation clusters by hierarchical clustering of geocoded inventor addresses.
- **Context data.** Start-up and venture funding records are matched to applicants by
  automated name matching plus manual curation. Funding rounds are compared with the first
  patent priority date.
- **Charts:**
  - Share bars with RTA markers.
  - Year×technology heat tables.
  - Indexed growth lines.
  - 100% stacked origin bars with a regional-aggregate callout.
  - Applicant×segment bubble matrices, with the numbers printed in the cells.
  - A cluster map.
- **Adopt:**
  - Fractional counting (as an option, stated on every chart).
  - RTA.
  - Rule-based secondary tags.
  - Applicant×segment bubble matrices.
  - Concentration measures.
- **Avoid:** an undisclosed data source and search strategy. Our methodology section is
  mandatory, not optional.

### 2.4 `quantum` (EPO–OECD)
- **Structure.**
  - Executive summary and key findings.
  - A technology-mapping primer.
  - A patent chapter with a methodology box, trends, geography, main actors and citation
    diffusion.
  - Five non-patent chapters: start-ups, investment, skills, trade, policy.
  - Concluding remarks and annexes.
- **Counting.**
  - DOCDB families split into IPF / non-IPF. An IPF is a family filed with at least two
    authorities, or at the EPO, or via PCT.
  - Time basis: earliest publication year.
  - Fractional counting across concepts and countries, but whole counts for top-applicant
    rankings. The switch is stated.
- **Search.**
  - Examiner-built strategies combining keywords with classification symbols, refined
    iteratively for recall vs noise.
  - The strategies themselves are **not published**. The published keyword lists identify
    *firms* in company databases, not patents.
  - A hand-built three-level taxonomy, with examiners assigning families to concepts.
- **Metrics:**
  - RTA.
  - Relative technology internationalisation: a country's IPF share within a sub-field
    divided by its IPF share overall.
  - Internationalisation rate (IPFs ÷ all families).
  - Share of backward citations that are to non-patent literature (NPL), benchmarked against
    general fields.
  - Forward citations within a fixed five-year window, in bands.
  - Herfindahl–Hirschman concentration index (HHI).
  - Citation flows from the cited field to the citing field.
- **Entity resolution.**
  - Harmonised applicant names, linked to company databases.
  - Fuzzy matching (several string-similarity measures, exact country match, stop-word lists).
  - Manual review above a similarity threshold, plus an LLM plausibility check followed by
    human review.
- **Charts:**
  - Taxonomy tree.
  - Stacked IPF/non-IPF bars.
  - Indexed lines with compound annual growth rate (CAGR) callouts.
  - Small-multiple trend panels.
  - RTA/RTI heatmap tables.
  - Chord diagrams (co-applicants, inventor→applicant country).
  - Sankey citation flows.
  - A university bubble map.
- **Adopt:**
  - The internationalisation rate, RTA/RTI and HHI.
  - Fixed-window forward citations.
  - The NPL share.
  - The co-applicant network.
  - The documented entity-resolution recipe (deterministic first, human confirmation).
- **Avoid:** unpublished search strategies and scattered limitations.

### 2.5 `ai_science` (Royal Society / IP Pragmatics)
- **Structure.**
  - Executive summary, with one page per sector.
  - Methodology: search strategy, landscape maps, citation analysis.
  - A filing landscape: priority-filing countries vs protection countries, with a UK deep dive.
  - Trends: sub-technologies, applications, top classification codes, grant success, top
    assignees.
  - **Four sector chapters with an identical sub-structure.**
  - Key patents per sector, company case studies, and patentability and enforcement
    considerations.
  - An appendix with every search string and code.
- **Counting.**
  - Unit: INPADOC (extended) families.
  - Time basis: **earliest priority date**, over the last 10 years.
  - The three most recent years are flagged incomplete and removed from trend charts.
  - Domestic-only filings from one large office were excluded, a choice that more than halved
    the dataset and is disclosed with before/after counts.
- **Search.**
  - A core string (keywords in title/abstract/claims OR classification codes, NOT excluded
    classes, limited to science/engineering sections).
  - Sector sub-searches layered on the core string by adding class filters.
  - Every string is published with its hit count.
- **Metrics:**
  - Legal-status distribution (granted / pending / revoked / expired / lapsed).
  - Filing velocity (families per year over the last five years).
  - Portfolio strength (non-self forward citations vs average portfolio age).
  - A portfolio-value index (weighted by the market size of the protected countries).
  - Top classification codes with their full hierarchy definitions.
- **Key patents.** A normalised citation-impact score (age- and field-corrected) followed by
  manual review.
- **Charts:**
  - Trend lines.
  - Horizontal bars.
  - Clustered legal-status bars.
  - A citation-vs-age bubble chart.
  - Top-code tables.
  - Assignee×class focus matrices.
  - Keyword concept maps.
  - A proprietary density landscape map with top assignees overlaid.
- **Adopt:**
  - Published strings with hit counts.
  - Layered sub-searches.
  - Priority vs protection country.
  - Legal-status distribution.
  - Top codes with definitions.
  - Velocity.
  - The portfolio strength chart.
  - The case-study template.
  - Patentability caveats.
- **Avoid:**
  - Its section introduction says one exclusion was applied, while a later chapter uses the
    unfiltered set. Every chart must state its exact dataset.
  - Its landscape map is a proprietary black box. Ours must be built from documented
    embeddings with a stated projection method.

## 3. Cross-report comparison

✓ = present, ◐ = partial, – = absent.

| Practice | genai | wind | hydrogen | quantum | ai_science |
|---|---|---|---|---|---|
| Key findings up front | ✓ | ✓ | ✓ | ✓ | ✓ |
| Technology primer | ✓ | ✓ | ✓ | ✓ | ◐ |
| Dedicated methodology section | appendix | ✓ | – | box in ch.3 | ✓ |
| Search strings in the report itself | ✓ | – (external file) | – | – | ✓ |
| Hit counts per query | ◐ | ◐ | – | – | ✓ |
| Filter accuracy disclosed | ✓ (P/R/F1) | – | – | – | – |
| Counting unit | simple family | family + IPF | IPF | family + IPF | extended family |
| Time basis | publication | publication | publication | publication | **priority** |
| Incomplete recent years flagged | ✓ | ✓ | ◐ | ✓ | ✓ |
| Fractional counting | – | – | ✓ | ✓ | – |
| Inventor country | ✓ | – | clusters only | ◐ | – |
| Applicant country | – | ✓ | ✓ | ✓ | ✓ |
| Filing office / protection country | ✓ | ✓ | – | ◐ | ✓ |
| Identical per-segment chapter block | ✓ | ✓ | ✓ | ◐ | ✓ |
| Cross-segment matrices | ✓ | ◐ | ✓ | ✓ | ◐ |
| Legal status / grant rate | ◐ (active share) | ✓ | – | – | ✓ |
| Forward citations / key patents | ◐ (examples) | ✓ | – | ✓ | ✓ |
| Specialisation (RTA) | – | – | ✓ | ✓ | – |
| Concentration (top-N share / HHI) | – | – | ✓ | ✓ | – |
| Newcomers | – | ✓ | ◐ | – | – |
| Business context | – | – | ✓ | ✓ | case studies |
| Dedicated limitations section | ✓ | – | – | – (scattered) | ◐ (patentability) |

## 4. What the template takes from this

Each decision below is implemented in `template/plr_template.yaml`.

1. **Priority year is the default time basis.** Four reports use publication year, which is
   quicker to compute but places inventions 18+ months late. The build prompt (§7) requires
   earliest priority year by default. The basis is configurable and printed on every trend
   chart.
2. **Families are the default unit, and IPFs are a first-class toggle.** Every ranking and
   trend is available for all families and for international families. The IPF rule
   (whether EP and PCT filings count as "international") is a stated, configurable definition
   because the references disagree.
3. **Whole counting is the default, and fractional counting is optional.** When fractional
   counting is used for country attribution, it is labelled on the chart.
4. **Identical per-segment block** (fully in four reports, partly in `quantum`): trend and
   growth → top applicants (companies and academia separately) → geography → key patents.
5. **Cross-matrices:** applicant×segment and country×segment. Segment×segment Sankeys are
   used where there are several taxonomy axes.
6. **Full disclosure in the report itself**, going beyond every reference (the `limitations`
   metric and table in the methodology section carry the last item):
   * the search strings with hit counts per query;
   * the exclusions with reason codes;
   * the measured precision/recall of the AI filter and classifier;
   * the residual "unclassified" share;
   * a dedicated limitations section.
7. **Signature analytics adopted:**
   * maturity map;
   * newcomers;
   * granted share and legal-status distribution;
   * RTA;
   * internationalisation rate;
   * concentration (top-N share, HHI);
   * forward citations over a fixed window, with self-citations removed;
   * filing velocity;
   * portfolio strength (citations vs age);
   * co-applicant network.
8. **Presentation conventions:**
   * a takeaway sentence as the chart title (verified like any other claim);
   * a standard source line (data source + retrieval date);
   * "segments overlap" notes wherever labels are multi-label.
9. **Deferred to later phases:**
   * **Business context** (funding, start-ups): it needs non-patent sources, so it is only
     allowed under Layer 7 (cited sources).
   * **Landscape (topic) maps:** the template includes one (white-space section), but its
     coordinates come from our own embeddings (Phase 6) with a documented projection, never a
     proprietary black box.

## 5. Owner decisions (resolved 2026-09-25)

The owner approved the template with its default options:

* **International family:** two or more distinct granting offices. The EPO counts as one
  office, and a PCT application alone does not count.
* **Incomplete period:** 18 months before the retrieval date.
* **Key-patent weights:** 0.5 citations, 0.3 offices, 0.2 granted.

All three remain configurable. The family definition (simple or extended) is still open,
because it depends on which data sources a report uses (Phase 3).
