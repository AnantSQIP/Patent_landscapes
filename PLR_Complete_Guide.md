# Patent Landscape Reports (PLR): Complete Guide

*Compiled 23 Sep 2026 from our chats and the 5 reference reports in this folder. Written for someone new to the patent industry.*

---

## Table of Contents

1. [The Project in One Paragraph](#1-the-project-in-one-paragraph)
2. [Patent Basics You Need to Know](#2-patent-basics-you-need-to-know)
3. [What Is a Patent Landscape Report?](#3-what-is-a-patent-landscape-report)
4. [What Goes Inside a PLR (Components)](#4-what-goes-inside-a-plr-components)
5. [Reference Reports in This Folder: How They Are Structured](#5-reference-reports-in-this-folder-how-they-are-structured)
6. [Other Useful References](#6-other-useful-references)
7. [How a PLR Is Built (Manual Method)](#7-how-a-plr-is-built-manual-method)
8. [The Patsquire Auto-Landscape System: The Flow](#8-the-patsquire-auto-landscape-system-the-flow)
9. [System Components Explained](#9-system-components-explained)
10. [Technical Approach: Stage by Stage](#10-technical-approach-stage-by-stage)
11. [Architecture, Data Sources and Build Plan](#11-architecture-data-sources-and-build-plan)
12. [Risks and How to Handle Them](#12-risks-and-how-to-handle-them)
13. [Proposed Patsquire PLR Template](#13-proposed-patsquire-plr-template)
14. [Cross-Questions Your Manager May Ask (with Answers)](#14-cross-questions-your-manager-may-ask-with-answers)
15. [Open Questions and Next Steps](#15-open-questions-and-next-steps)

---

## 1. The Project in One Paragraph

The manager wants Patsquire to take a topic such as **"LLM"**, find and analyse the relevant patents automatically, and turn that analysis into a professional **Patent Landscape Report** almost at one click.

The manager's instructions, decoded:

| Manager said | What it means |
|---|---|
| "Find some good patent landscape reports" | Study real, professional PLRs to learn their structure, charts and writing style before designing our own. |
| "Key string preparer for use with API needs to be developed" | Build the **search-query layer** that converts a human topic ("LLM") into proper patent search strings for the API. |
| "Fetch from patent API, use Patsquire to further filter" | The **API gets raw data**; **Patsquire cleans, filters and analyses** it. |
| "Use your reporting module to give templates landscape output with nice text and graphics" | Build a **standard report template** that fills itself with charts, tables and written insights from the analysed data. |
| "One-click landscape report on any topic" | The user types a topic, clicks Generate, and the whole pipeline runs behind the button. |

**The one flow to remember:**

```
Topic → Key String / Search Strategy → Patent API → Patent Records → Cleaning & Deduplication
→ Patsquire Filtering → Patent Family / Technology Analysis → Charts & Insights
→ Report Template → Final Patent Landscape Report
```

---

## 2. Patent Basics You Need to Know

### What is a patent?
A **patent** is a legal right given for an invention. It is a legal document describing and protecting that invention. For example, a company that invents a way to make batteries charge faster may file a patent on it.

### What a patent record contains

| Field | Meaning |
|---|---|
| Patent / Application / Publication number | Unique ID of the document |
| Title, Abstract | Short name and summary of the invention |
| **Claims** | The exact legal statements of what is protected (the most important part legally) |
| **Inventor** | The person(s) who made the invention |
| **Applicant / Assignee** | The organisation that owns or applied for the patent (e.g. inventor John Smith, assignee ABC Corp) |
| **Filing date** | When the application was filed |
| **Priority date** | The earliest filing date for the invention (best date for measuring when an invention happened) |
| Publication date | When it became public (usually ~18 months after filing) |
| Country / Jurisdiction | Which patent office it was filed at (US, CN, EP, IN, JP, WO...) |
| **Classification codes (CPC / IPC)** | Standard technology category codes assigned by patent offices |
| Citations | Other patents/papers it refers to (backward) or that refer to it (forward) |
| **Legal status** | Pending, granted, active, expired, abandoned, withdrawn, etc. |
| Family ID | Links it to related filings of the same invention |

### Key terms (glossary)

| Term | Simple meaning |
|---|---|
| **Patent family** | Filings in several countries for the *same* invention. Count families, not documents, or you count one invention many times. Example: "Company A has 120 families containing 400 documents." |
| **International patent family (IPF)** | A family filed at 2+ patent offices. Used by EPO/OECD as a sign of higher-value inventions. |
| **Assignee normalisation** | Merging different spellings of the same owner (Google Inc., Google LLC) into one entity, carefully (Alphabet vs Google is an ownership question, not a spelling one). |
| **Legal status** | Whether the patent is currently alive. A patent in a database is not automatically enforceable. |
| **CPC / IPC** | Cooperative / International Patent Classification: hierarchical codes for technology areas (e.g. G06N = AI/computing models). |
| **Technology segmentation / taxonomy** | Splitting a topic into sub-areas (LLM → training, fine-tuning, inference, ...). |
| **White space** | Areas with *relatively less* patent activity within our search scope. It does **not** mean "nobody has patented it". |
| **Recall** | Did we find all the relevant patents? (don't miss things) |
| **Precision** | Are the patents we found actually relevant? (don't include junk) |
| **FTO (Freedom to Operate)** | A legal analysis of whether a specific product may infringe enforceable patents. Much narrower and more legal than a landscape. |
| **Seed set** | A small set of patents known to be relevant, used to test whether our searches find them. |
| **Publication lag** | Patents publish ~18 months after filing, so the last ~2 years always look like a (fake) decline. |
| **RTA (Revealed Technological Advantage)** | Country's share in a field ÷ its share in all fields. >1 means the country specialises in that field. |
| **NPL** | Non-patent literature (scientific papers) cited by patents. |

---

## 3. What Is a Patent Landscape Report?

A **Patent Landscape** is a **map of patent activity around a technology**. A **Patent Landscape Report (PLR)** is the document that presents that map.

```
                    LLM PATENT LANDSCAPE
                           LLM
          ┌─────────────────┼─────────────────┐
       Training          Inference        Applications
      Fine-tuning       Optimization       Chatbots
      Data training     Hardware           Search
      RLHF              Compression        Coding
```

It answers:
- **Who** is filing? (companies, universities, countries)
- **What** are they patenting? (technology sub-areas)
- **Where** are they filing / inventing? (jurisdictions, inventor locations)
- **When**? (filing trends over time, growth)
- **How strong / alive** is it? (legal status, citations, international families)
- **What's next**? (fast-growing areas, new entrants, white space)

### A PLR is NOT just a list of patents

```
Patent data + filtering + grouping + analysis + visualisation + explanation = PLR
```

### Comparisons

| | Patent Search | Patent Landscape | FTO |
|---|---|---|---|
| Goal | Find relevant patents | Understand the whole patent environment | Check whether a specific product risks infringing |
| Output | List of documents | Statistics, charts, trends, insights | Legal opinion on specific claims |
| Scope | Narrow or broad | Broad, topic-level | Narrow, product-level, jurisdiction-specific |
| Legal weight | None | Informational (not legal advice) | Legal advice |

### Why companies/governments use PLRs
R&D planning, competitor intelligence, finding partners or acquisition targets, spotting white space, investment decisions, policy-making, and as a starting point for FTO or licensing work.

---

## 4. What Goes Inside a PLR (Components)

Typical sections (exact set varies by purpose):

1. **Executive Summary / Key Findings**: the top insights in plain language, up front.
2. **Introduction / Technology Primer**: what the technology is, explained for non-experts.
3. **Scope & Methodology**: what's in/out, data source, date range, jurisdictions, extraction date.
4. **Search Strategy**: keywords, boolean queries, classification codes (full list usually in appendix).
5. **Dataset overview**: number of documents vs families, cleaning steps.
6. **Filing Trends**: patents per year (by priority year), growth rate.
7. **Top Applicants / Assignees**: ranked by number of families.
8. **Top Inventors**.
9. **Geographic Distribution**: where inventors are (origin) vs where protection is sought (filing offices).
10. **Technology Segmentation**: breakdown by sub-area, trend per segment.
11. **Patent Family Analysis**: family sizes, international families.
12. **Legal Status Analysis**: pending / granted / expired / abandoned.
13. **Key Patents**: highly cited or strategically important patents.
14. **Competitive Landscape**: company × technology matrix, company profiles/case studies.
15. **White-space / Opportunity Analysis**.
16. **Further Considerations / Limitations**.
17. **Conclusion**.
18. **Appendices**: search strings, classification codes, methodology details, indicator definitions, list of patents.

### Standard charts and visuals

| Visual | Shows |
|---|---|
| Line / bar over years | Filing trend (mark last 2 years as incomplete) |
| Horizontal bar | Top assignees, top inventors, top countries |
| World map / bar | Geographic distribution |
| Pie / stacked bar / treemap | Technology segment shares |
| **Heatmap / matrix** | Assignee × technology segment, country × segment |
| Stacked bar | Legal status breakdown |
| Bubble / "maturity map" | Growth vs volume per segment |
| Landscape (topic) map | Clusters of similar patents (text-similarity map) |
| Citation network | Influential patents and flows between countries |

Example of an assignee × technology matrix:

| Company | Training | Inference | Fine-tuning | Applications |
|---|---|---|---|---|
| Company A | ✓ | ✓ | ✓ | |
| Company B | ✓ | | ✓ | ✓ |
| Company C | | ✓ | | ✓ |

---

## 5. Reference Reports in This Folder: How They Are Structured

The five PDFs in this folder, with their real tables of contents summarised.

### 5.1 `Generative AI - PLR.pdf`: WIPO, 2024 (116 pages). **Closest match to our LLM example**

- Key findings and insights → Introduction
- 1. GenAI main concepts (plain-language primer: models, modes)
- 2. Global patenting & research: global development, **top patent owners, inventor locations, filing jurisdictions**
- 3. Trends by GenAI **model** (LLM, GAN, VAE, diffusion...)
- 4. Trends by GenAI **mode** (text, image, video, molecules...), plus a model × mode connection
- 5. Trends by GenAI **application** (21 areas), plus model × application and mode × application connections
- Further considerations: concerns; **limitations of patent analysis**
- Appendices: **methodology, patent indicators, patent searches, LLM prompts used**, publication query (The Lens), example patents per application

**Lesson:** each segmentation chapter repeats the *same 3 blocks* (global trend → top owners → inventor locations). That repetition is exactly what a template engine can generate automatically. Segmentation along 3 axes (models / modes / applications) plus cross-matrices is a reusable pattern. Notably, WIPO lists the **LLM prompts** it used in the appendix.

### 5.2 `Wind Energy - PLR.pdf`: EPO–IRENA Offshore Wind, 2023 (59 pages). **Best model for the Key String Preparer**

- Executive summary → Introduction
- 2. Methodology: using patent information; **patent search**
- 3.1 Overall trends: filings, top applicant countries, top patent offices, top applicants, **maturity map**, citations
- 3.2 Technology concept grouping: **one query per segment** (QA–QL): fixed & floating foundations (QA, QB), towers (QH), power transmission (QC), blades/rotors (QI), hybrid systems (QE), energy storage (QD), grid & cables (QJ, QL)
- Conclusion, Glossary

**Lesson:** each technology segment = its own saved search query (with an ID). Trends shown for all families and for international families. Short and clean: a good MVP-size target.

### 5.3 `Hydrogenpatentsforacleanenergyfuture.pdf`: EPO–IEA, 2023 (70 pages)

- Forewords, lists of tables/figures/abbreviations/countries
- Executive summary, Key findings
- 1. Introduction (why hydrogen, why this report, structure)
- 2. Overview: geography of innovation (RTA, top innovation clusters), established vs emerging technologies
- 3. Production · 4. Storage, distribution & transformation · 5. End-use applications (**segments follow the value chain**)
- References

**Lesson:** segment by **value chain** (produce → store → use). Uses **IPFs** as the counting unit and **RTA** for country specialisation. Combines patents with business context (start-up investment, manufacturing capacity).

### 5.4 `Quantum ecosystem - PLR.pdf`: OECD (with EPO data), 2025 (163 pages). **Advanced / ecosystem style**

- Executive summary, Key findings
- 2. Mapping quantum technologies (primer)
- 3. **Quantum patent landscaping**: mapping patents, trends, geography, main actors, **citation-flow diffusion analysis**
- 4–8. Beyond patents: start-ups, investment, skills (job postings), trade, government strategy
- Annex: subdomain definitions, **keyword lists**, firm identification, etc.

**Lesson:** shows advanced metrics for later versions: RTA, internationalisation rate, NPL citation rate, citation flows. Chapter 3 alone is a solid pure-patent landscape.

### 5.5 `science-ai-related-inventions-PLR.pdf`: Royal Society / IP Pragmatics (206 pages). **Consultancy style**

- Executive summary (per sector)
- 1. Methodology: **search strategy, landscape maps, forward-citation analysis**
- 2. Filing landscape: filing rate, **priority filing countries vs protection countries**
- 3. Trends: by AI technology, applications, top classifications, key concepts, **grant success**, top assignees
- 4–7. **Four sector chapters with an identical sub-structure**: filing rate → classifications → landscape concepts → top assignees → landscape map
- 8. Key patents per sector · 9. **Company case studies** (Illumina, Siemens, Alphabet, Toyota)
- 10. Other considerations (patentability, enforcement)
- 11. Appendix: **search strings, classification codes**

**Lesson:** the clearest demonstration of a **repeatable per-segment chapter template**. Adds key patents, case studies and grant-success rate.

### 5.6 Side-by-side comparison

| Feature | GenAI (WIPO) | Wind (EPO-IRENA) | Hydrogen (EPO-IEA) | Quantum (OECD) | AI inventions (RS) |
|---|---|---|---|---|---|
| Key findings up front | ✓ | ✓ | ✓ | ✓ | ✓ |
| Technology primer | ✓ | ✓ | ✓ | ✓ | light |
| Methodology section | appendix | ✓ | light | annex | ✓ |
| Search strings published | ✓ | ✓ (per segment) | – | keywords | ✓ |
| Filing trend | ✓ | ✓ | ✓ | ✓ | ✓ |
| Top applicants | ✓ | ✓ | ✓ | ✓ | ✓ |
| Inventor location vs filing office | ✓ | ✓ | ✓ | ✓ | ✓ |
| Segment chapters (repeating structure) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cross-matrices | ✓ | – | ✓ | ✓ | – |
| Citations / key patents | – | ✓ | – | ✓ | ✓ |
| Legal status / grant rate | – | – | – | – | ✓ |
| Business context (investment etc.) | – | – | ✓ | ✓ | case studies |
| Limitations section | ✓ | – | – | ✓ | ✓ |

### Common backbone across all of them
1. Key findings first
2. Plain-language technology primer
3. Scope & search strategy (queries + classification codes in appendix)
4. Filing trends over time
5. Top applicants
6. Geography: inventor locations **and** filing jurisdictions
7. Segment-by-segment chapters with an identical internal structure
8. Further considerations / limitations
9. Appendices

---

## 6. Other Useful References

| Resource | Why it's useful |
|---|---|
| **WIPO Guidelines for Preparing Patent Landscape Reports (2015)** | The "how-to manual": step-by-step PLR preparation. Treat it as the spec for our pipeline. |
| **WIPO Manual on Open Source Tools for Patent Analytics** | Software/tools side of patent analytics. |
| **WIPO Patent Landscape Report series** | Same WIPO template applied to many topics (hydrogen fuel cells, agrifood, graphite, COVID-19 vaccines, heavy-duty transport decarbonisation...). Useful for designing a topic-agnostic template. |
| **UKIPO: Artificial Intelligence – A worldwide overview of AI patents** | Good methodology section. Openly flags that "Microsoft Corporation" and "Microsoft Technology Licensing" appear as separate entities: proof that we need assignee normalisation. |
| **USPTO OCE AI patent landscape + AI Patent Dataset (AIPD)** | Shows keyword/classification queries miss many AI patents; uses a **BERT-for-Patents ML classifier** to identify AI patents. Model for Patsquire's ML filtering. |
| **"Patent landscape analysis — contributing to the identification of technology trends…"** (open-access paper) | 3-part method: systematic search, **statistical check of recall**, **PRISMA-style** data cleaning workflow. Model for our "Search Strategy & Validation" section. |

Data sources mentioned: **PATSTAT** (EPO, statistical, SQL), **The Lens** (links patents with scholarly papers), **EPO OPS**, **PatentsView** (US only, free), **Google Patents on BigQuery**, commercial: **IFI Claims, PatSnap, Derwent**.

---

## 7. How a PLR Is Built (Manual Method)

How analysts do it today; our system automates these steps:

1. **Define objective & audience**: why is the report needed (R&D, competitor watch, policy...)?
2. **Define scope**: technology definition, inclusions/exclusions, date range, jurisdictions.
3. **Build a taxonomy**: split the technology into segments, usually with domain experts.
4. **Design the search**: keywords + synonyms + classification codes → boolean queries (one per segment + one overall).
5. **Test & refine the search**: check against known patents (recall), inspect samples (precision), iterate.
6. **Extract data** from the patent database.
7. **Clean the data**: deduplicate, group into families, normalise assignee names, standardise dates/countries/status.
8. **Screen for relevance**: remove false hits (manually or with ML).
9. **Categorise** each family into segments.
10. **Analyse**: compute indicators (trends, rankings, geography, status, citations, matrices).
11. **Visualise**: charts, maps, matrices.
12. **Interpret & write**: key findings, commentary, limitations.
13. **Review & publish**: expert validation, then the final report with appendices.

---

## 8. The Patsquire Auto-Landscape System: The Flow

### High-level flow

```
             USER  ("LLM patents")
               │
               ↓
      Scoping & Taxonomy (with user approval)
               │
               ↓
       Key String Preparer  (queries per segment)
               │
               ↓
          Patent API
               │
               ↓
       Raw Patent Records
               │
               ↓
   Cleaning, Deduplication, Family Grouping, Name Normalisation
               │
               ↓
       Patsquire Relevance Filtering
               │
               ↓
       Segment Classification
               │
               ↓
       Analytics Engine (plain code)
               │
               ↓
       Charts  +  Narrative (LLM, number-checked)
               │
               ↓
       Report Template Assembly
               │
               ↓
      Patent Landscape Report (PDF / Web)
```

### What happens behind the "Generate" button (17 steps)

1. Understand topic
2. Generate search terms
3. Build API queries
4. Call patent API
5. Collect patent records
6. Clean duplicate records
7. Group patent families
8. Filter irrelevant patents
9. Determine technology categories
10. Analyse companies
11. Analyse filing trends
12. Analyse countries
13. Analyse legal status
14. Generate charts
15. Generate written insights
16. Put everything into the report template
17. Generate the final PLR

### The filtering funnel (example numbers)

```
API returns          100,000 records
      ↓ dedup + family grouping
                      40,000 families
      ↓ Patsquire filtering
                      25,000 potentially relevant
      ↓ further analysis
                       5,000 highly relevant
```

### Proposed one-click UI

```
──────────────────────────────────────
        CREATE PATENT LANDSCAPE
Topic:       [ Large Language Models  ]
Date Range:  [ 2015 ] → [ 2026 ]
Countries:   ☑ US ☑ China ☑ Europe ☑ India ☐ Japan
                    [ GENERATE ]
──────────────────────────────────────
```

Realistically the product is **"one click to a strong draft, with optional review checkpoints"**: approve the taxonomy, then review the draft before publishing.

---

## 9. System Components Explained

| Component | Job | Input → Output |
|---|---|---|
| **Scoping & Taxonomy** | Define what's in/out; propose technology segments | Topic → scope definition + segment list |
| **Key String Preparer** | Turn the topic/segments into search queries | Segments → keywords, synonyms, CPC/IPC codes, boolean strings in API syntax |
| **Patent API (data acquisition)** | Fetch patent records | Queries → raw records (saved untouched) |
| **Cleaning / Normalisation** | Make data consistent | Raw records → deduplicated families, normalised assignees/dates/countries/status |
| **Patsquire Filtering** | Keep only relevant patents | Families → relevant families (+ precision/recall metrics) |
| **Segment Classifier** | Tag each family with segment(s) | Relevant families → labelled families |
| **Analytics Engine** | Compute all statistics (no LLM) | Labelled families → structured stats JSON |
| **Chart Generator** | Build standard visuals | Stats → PNG/SVG/interactive charts |
| **Narrative Generator** | Write text from stats only (LLM) | Stats JSON → section text, number-checked |
| **Reporting Module / Template** | Assemble final document | Charts + text + tables → PDF / HTML / DOCX |

### Key String Preparer: example for "LLM"

The user's word "LLM" alone is too narrow (patents may not use the abbreviation) and too noisy ("LLM" also means a law degree). Expand it:

```
Concepts: "large language model*", "language model*", "generative pre-trained transformer",
          "foundation model*", "transformer-based", "natural language generation" ...
Segment example (Fine-tuning):
  ("large language model*" OR "LLM" OR "foundation model*")
  AND ("fine-tun*" OR "instruction tuning" OR "RLHF" OR "reinforcement learning from human feedback")
  AND CPC=(G06N3/* OR G06F40/*)
```
*(Illustrative only: the real terms and codes must be generated, tested and refined per topic and per API syntax.)*

Good practice: generate automatically, but **allow manual editing**; keep **one query per segment + one broad query**; **test against a seed set**; **store all final queries** for the appendix.

### Patent API: what we get back
Patent/application number, title, abstract, claims, inventors, assignees, filing/priority/publication dates, jurisdiction, classifications, citations, legal status, family ID. **The exact fields depend on the API**; check this before choosing a provider.

### Patsquire vs API: why both?
**API = gets the data. Patsquire = processes, filters, groups and analyses it.** Search results are not the final relevant dataset.

### Reporting module: example input it receives

```
Total patent families: 4,820
Top applicants: Company A 620, Company B 410, Company C 280
Countries: US 2,100, China 1,800, Europe 900
Technology areas: Training 35%, Inference 25%, Applications 20%, Hardware 10%, Other 10%
```
...and turns it into tables, charts, commentary and a formatted report. The **template** keeps the same structure for any topic (LLM, EVs, solid-state batteries, quantum computing); only the data and segments change.

---

## 10. Technical Approach: Stage by Stage

**Two core principles:**
1. **Pipeline of separate stages, each saving its output**, so any stage can be inspected, fixed or re-run.
2. **Code does the counting, the LLM does the language.** Every number comes from ordinary code. The LLM helps with topic understanding, keyword expansion, relevance judgement, categorisation and writing, but **never produces statistics**.

### Stage 1: Topic input & scoping
User gives topic, date range, jurisdictions. LLM drafts a scope definition and a **taxonomy** (for LLM: training, fine-tuning, inference, architecture, retrieval, agents, hardware, applications). **Show it to the user for approve/edit**: this single checkpoint greatly improves quality.

### Stage 2: Key String Preparer
Per segment: synonyms, related terms, CPC/IPC codes, boolean strings in the API's syntax. Follow the EPO wind pattern (**one query per segment + one broad query**). Validate with a **seed set**: if queries find most seed patents, recall is good. Store every final query.

### Stage 3: Retrieval
Run queries, handle pagination & rate limits, **save raw responses untouched** for reprocessing. Choosing the data source is the most important early decision (see §11).

### Stage 4: Cleaning & family grouping
- Remove duplicates.
- Group into families using the database's family ID.
- Normalise assignee names ("Microsoft Corp" + "Microsoft Technology Licensing LLC" → Microsoft).
- Standardise dates, countries, legal status.
- **From here on, the family is the default counting unit.**

### Stage 5: Relevance filtering (Patsquire layer): a cheap-to-expensive funnel
1. Rule-based exclusions (wrong CPC, excluded terms)
2. **Embedding similarity** of abstract/claims vs topic definition and seed patents
3. **LLM judgement only for borderline cases**

Have a person label a random sample (a few hundred) to measure **precision & recall**; tune thresholds; **publish these metrics in the methodology section**.

### Stage 6: Segment classification
Assign each family to one or more segments using per-segment query hits, an LLM classifier with segment definitions, or both. Validate on a labelled sample.

### Stage 7: Analytics engine (plain code, no LLM)
Standard metrics every time:
- Filing trend by **priority year** + growth rate
- Top assignees by number of families
- Geography: **inventor location vs protection sought**
- Share of international families (2+ offices)
- Legal status breakdown
- Assignee × segment matrix
- Fastest-growing segments
- New entrants
- Highly cited key patents
- *(later)* RTA, grant rate, citation flows, NPL share

**Trap:** publication lag. Mark the last ~2 years as **incomplete** in charts and text.

### Stage 8: Chart generation
Fixed chart library tied to template sections (trend line, top-assignee bar, country map/bar, segment breakdown, assignee × segment heatmap, legal status). Plotly or Matplotlib with a consistent Patsquire style.

### Stage 9: Narrative generation
Give the LLM **structured JSON of computed stats, one section at a time**. Ask it to write key findings and commentary using only those numbers. Then **automatically verify every number in the text exists in the stats**. That gives "nice text" without invented facts.

### Stage 10: Template assembly
Fixed skeleton filled with data: e.g. **Jinja2 HTML templates → PDF via WeasyPrint**, or Word generation. Always include an appendix with data source & extraction date, all queries & codes, filtering accuracy metrics, list of included families.

### Stage 11: Review
Analyst opens the draft, checks taxonomy and a sample of patents, edits, then publishes.

---

## 11. Architecture, Data Sources and Build Plan

### Architecture
- Reports take minutes to hours, so run them as **background jobs** (Celery / Prefect / Airflow).
- **PostgreSQL** for raw and cleaned patent records.
- **Vector store** for embeddings.
- **Versioned artifacts** per stage, making reports reproducible (you can show exactly which data and queries produced a report).
- **Keep layers separate**: data acquisition ≠ analysis ≠ reporting. The reporting module should *not* call the API directly. This lets us swap the API or output format without rewriting everything.

```
User Topic → Query Generator → Patent API → Raw Data → Normalisation → Deduplication
→ Patsquire Filtering → Patent Analysis → Structured Landscape Dataset → Report Generator → PDF / Web
```

### Data source options

| Source | Coverage | Notes |
|---|---|---|
| Lens.org API | Global | Relatively accessible; links to scholarly works |
| EPO OPS | Global | Official EPO web service; family & legal data (INPADOC) |
| PatentsView | US only | Free; good for prototyping |
| Google Patents (BigQuery) | Global | SQL-based; pay per query |
| PATSTAT | Global | EPO statistical DB; SQL; licensed |
| IFI Claims / PatSnap / Derwent | Global | Commercial; normalised assignees, legal status; licensing cost |

**Check for each:** family IDs, legal status, full claims, CPC/IPC codes, normalised assignee names, rate limits, cost, **commercial licensing terms**.

### Build phases
| Phase | Deliverable |
|---|---|
| **0: Template** | Study reference reports; finalise section list, chart list, metric definitions |
| **1: MVP on one topic** | One data source, hand-written queries, basic cleaning, analytics engine, charts, template output: proves the end-to-end flow |
| **2: Search & filtering** | Key String Preparer + Patsquire relevance filter + labelled evaluation set |
| **3: Segmentation & narrative** | LLM taxonomy generation, classification, number-checked text generation |
| **4: Product** | One-click UI, review checkpoints, more data sources, assignee normalisation at scale |

---

## 12. Risks and How to Handle Them

| Risk | Effect | Control |
|---|---|---|
| **Low search recall** | Missing patents silently skews every result | Seed-set testing, broad + per-segment queries, recall measurement |
| **Low precision** | Irrelevant patents inflate counts | Filtering funnel, labelled samples |
| **Messy assignee names** | Distorted rankings | Normalisation dictionary / entity resolution |
| **Double counting** | Same invention counted many times | Count families, not documents |
| **Publication lag** | Last 2 years look like a decline | Mark years as incomplete |
| **Invented narrative** | LLM states unsupported facts/numbers | Stats-only prompts + automatic number check |
| **Missing data fields** | Wrong or empty analysis | Never invent; mark unavailable or pull from another source |
| **Data licensing / API cost** | Limits which source we can use | Decide early; check terms |
| **Legal misinterpretation** | Report read as legal advice | Disclaimer: analytical, not a legal opinion or FTO |

---

## 13. Proposed Patsquire PLR Template

Built from the common backbone of the reference reports:

```
PATSQUIRE PATENT LANDSCAPE REPORT: <TOPIC>
Cover: title, date, data source, extraction date

1.  Key Findings                      (LLM text from stats, number-checked)
2.  Executive Summary
3.  Technology Overview               (plain-language primer + taxonomy diagram)
4.  Scope & Methodology               (scope, sources, date range, jurisdictions,
                                        counting unit, filtering precision/recall)
5.  Global Overview
      5.1 Filing trends (priority year, growth, incomplete-years marker)
      5.2 Top assignees
      5.3 Top inventors
      5.4 Geography: inventor origin vs filing offices
      5.5 International families
      5.6 Legal status
6.  Technology Segmentation           (share per segment, fastest-growing segments)
7.  Segment Chapters  [repeat per segment, same sub-structure]
      7.x.1 Filing trend
      7.x.2 Top assignees
      7.x.3 Geography
      7.x.4 Key patents
8.  Competitive Landscape             (assignee × segment heatmap, new entrants, profiles)
9.  Key Patents                       (highly cited / broad families)
10. White-space & Opportunities       (with "relative, within scope" caveat)
11. Limitations & Further Considerations
12. Conclusion
Appendices
  A. Search strings per segment (query IDs, like EPO's QA–QL)
  B. Classification codes used
  C. Indicator definitions
  D. Validation metrics (precision / recall, sample sizes)
  E. List of included patent families
  F. Disclaimer (not legal advice / not an FTO)
```

---

## 14. Cross-Questions Your Manager May Ask (with Answers)

### A. Basics
**Q1. What is a Patent Landscape Report?**
A structured analysis of patents related to a technology, showing relevant patents, families, assignees, inventors, filing trends, countries, legal status and technology areas.

**Q2. Is a landscape just a list of patents?**
No. The list is raw data; a landscape adds filtering, grouping, analysis, visualisation and insights.

**Q3. Why do we need a patent landscape?**
To understand who is working in an area, what they patent, where, and how activity develops over time.

**Q4. Patent search vs landscape?**
Search finds relevant patents; a landscape analyses and organises them to understand the overall environment.

**Q5. PLR vs FTO?**
A landscape is a broad view of the patent environment. FTO checks whether a specific product may infringe specific enforceable claims. A landscape can support FTO but is not FTO.

### B. The Auto-Landscape system
**Q6. What are we automating?**
Topic input → search queries → API retrieval → Patsquire filtering and analysis → charts, insights and a formatted report.

**Q7. What happens if I enter "LLM"?**
The system builds a search strategy from related terms and concepts (not just "LLM"), queries the API, retrieves records, then filters them for relevance.

**Q8. Why not just search "LLM"?**
Patents use different wording (many won't use "LLM"), and some that mention it are irrelevant. So: broader search, then filtering.

### C. Key strings / queries
**Q9. What is the Key String Preparer?**
The component that converts a topic into patent-search keywords and query strings for the API.

**Q10. Example?**
For LLM: "large language model", "language model", "transformer", "foundation model", etc., combined into queries in the API's syntax. These are generated and refined, not fixed.

**Q11. Manual or automatic?**
Automatic by default, with configuration/manual editing, since search quality is critical and each technology differs.

**Q12. Query too broad?**
Too many irrelevant results. Refine with more concepts, classification codes, fields, dates, jurisdictions or filters.

**Q13. Query too narrow?**
We miss relevant patents. We must balance **recall** (don't miss relevant patents) and **precision** (don't include irrelevant patents).

### D. Patent API
**Q14. Role of the API?**
It's the data source: we send queries and receive patent records.

**Q15. What data do we get?**
Numbers, title, abstract, inventors, assignee, dates, jurisdictions, classifications, claims, citations, legal status. The exact fields depend on the API.

**Q16. Trust the API results directly?**
Not blindly. We need validation, deduplication, relevance filtering and normalisation.

### E. Patent families
**Q17. What is a patent family?**
Related applications from the same invention, often filed in several countries.

**Q18. Why group families?**
To avoid counting one invention multiple times.

**Q19. Count patents or families?**
Both, depending on the analysis. Documents suit jurisdiction and legal-status analysis; families suit counting distinct inventions.

### F. Filtering
**Q20. Why Patsquire if the API already searches?**
Search results aren't the final relevant dataset. Patsquire adds filtering, normalisation, classification, grouping and analysis.

**Q21. How do you decide relevance?**
Search terms and fields, claims, abstracts, classifications, metadata, and rules or ML models. The methodology depends on the landscape type.

**Q22. Is keyword matching enough?**
Probably not. Add classifications, claims, semantic similarity, families and metadata. (The USPTO showed that keyword/class queries miss many AI patents.)

### G. Technology categorisation
**Q23. What is technology segmentation?**
Dividing the technology into meaningful sub-areas (for LLM: training, fine-tuning, inference, architecture, data, retrieval, applications).

**Q24. Who decides the categories?**
Initially the methodology or template, with the system assisting later. Categories need validation because they drive report quality.

**Q25. Same categories for every technology?**
No. The section structure can be standard; segmentation must adapt (LLM and batteries differ completely).

### H. Reporting module
**Q26. What should the final report contain?**
Executive summary, methodology, search strategy, trends, top assignees, inventors, geography, segmentation, family analysis, legal status, charts and key findings.

**Q27. Why templates?**
Consistent structure and presentation. The engine fills sections from analysed data instead of designing each report by hand.

**Q28. Does one template fit all?**
A common base template with configurable sections per technology and purpose.

**Q29. Where does the text come from?**
Some is template text; analytical text is generated from structured data and must stay grounded in it (number-checked).

**Q30. Where do graphs come from?**
The reporting module converts structured datasets (counts by year, by assignee...) into charts.

### I. Data quality
**Q31. Duplicate patents?**
A deduplication/normalisation layer using identifiers, family relationships and metadata.

**Q32. Missing information?**
Never invent it. Get it from another source, mark it unavailable, or exclude it from that analysis.

**Q33. Irrelevant patents?**
Multiple stages: query generation → retrieval → filtering → relevance analysis → human review if needed.

**Q34. Same company under different names (Google, Google LLC, Google Inc., Alphabet)?**
Assignee-name normalisation, with ownership relationships handled carefully rather than merging every similar name.

### J. Legal status
**Q35. Why does legal status matter?**
A patent in a database isn't necessarily enforceable. Distinguish pending, granted, active, expired and abandoned.

**Q36. Does an active patent mean infringement risk?**
No. That needs claim analysis against the product, jurisdiction and other legal factors.

### K. White space
**Q37. What is white-space analysis?**
Finding technical areas with relatively less patent activity compared with others.

**Q38. Does white space mean nobody patented it?**
No. It means relatively less activity *within our search scope and methodology*.

### L. Architecture
**Q39. How would you design the pipeline?**
Topic → Query Generator → API → Raw Data → Normalisation → Deduplication → Patsquire Filtering → Analysis → Structured Dataset → Report Generator → PDF/Web.

**Q40. Should the reporting module call the API directly?**
No. Keep acquisition, analysis and reporting separate.

**Q41. Why separate modules?**
Maintainability and reuse: swap the API without touching reporting, or produce new formats from the same dataset.

### M. Gotcha questions
**Q42. Can you guarantee we find every relevant patent?**
No. It depends on scope, terminology, databases, classifications, jurisdictions, dates and filtering. We define the methodology and report its limitations.

**Q43. Can AI generate the entire landscape?**
It can automate much of the workflow, but search strategy, relevance, categories, legal status and conclusions may need validation.

**Q44. Can we call the report legally accurate?**
Be careful. It is an analytical landscape, not a legal opinion or infringement conclusion unless legally reviewed.

**Q45. What is the biggest challenge?**
Not the PDF. It's a **reliable, reproducible dataset**: search strategy, relevant retrieval, filtering, family grouping, entity normalisation, then analysis on clean data.

### Top 10 to prepare best
1. What is a PLR? · 2. PLR vs search · 3. PLR vs FTO · 4. What happens when the user enters "LLM"? · 5. What is key string preparation? · 6. What does the API provide? · 7. Why Patsquire after the API? · 8. What is a patent family and why does it matter? · 9. What goes in the report? · 10. How would you automate the whole process?

---

## 15. Open Questions and Next Steps

**Open questions to raise with the manager**
- Which **patent database/API** will we use (and what's the budget/licence)? This shapes stages 2–4 more than anything.
- Who is the **audience** (internal analysts, clients, investors)? It affects depth and tone.
- Output format: **PDF, web dashboard, Word**, or all three?
- How much **human review** is acceptable in the "one-click" flow?
- Which LLM / embedding models are allowed (cost, data privacy)?
- Which topic should be the **MVP pilot** (e.g. LLM)?

**Next steps**
1. Read the WIPO Guidelines and go through the 5 PDFs in this folder in detail (charts used, metric definitions).
2. Finalise the template (§13): sections, chart list, metric definitions.
3. Pick the data source; confirm available fields (family ID, legal status, CPC, claims).
4. Build the Phase 1 MVP on one topic with hand-written queries.
5. Build a labelled seed/evaluation set for that topic.
6. Add the Key String Preparer, Patsquire filtering, then LLM segmentation and narrative.
