# CPC codes for large language models (research, 2026-09-26)

This page records why `examples/taxonomies/llm_taxonomy.yaml` uses the codes it does.

**Evidence comes from three places:**
1. Official CPC sources.
2. Published landscape methods.
3. The codes actually carried by patents: nine owner-confirmed LLM patents, the patents
   found by citation expansion, and a background set. The background is the owner's 1,131
   unrelated patents.

## Key facts

**No CPC code is specific to LLMs, transformers or attention.**
- CPC 2026.08 has no title containing "transformer", "attention" or "large language".
- The notices of changes for 2023–2026 add only:
  - the rebuilt G06N3 groups (NOC 1380, 2023.01), which created G06N3/045, /0455,
    /0475, /0442, /0499, /0895, /09, /092, /096 and /0985, and deleted G06N3/0454 and
    /0445;
  - G06F16/33295 "in dialogue systems" (NOC 1679, 2025.01).
- Sources:
  - https://www.uspto.gov/web/patents/classification/cpc/html/cpc-notices-of-changes.html
  - https://www.uspto.gov/web/patents/classification/cpc/pdf/CPCNOC1380RP11914G06N.pdf
  - https://www.uspto.gov/web/patents/classification/cpc/pdf/CPCNOC1679RP12338G06F.pdf

**G06N3/045 "Combinations of networks" is where transformers are classified.**
- The CPC definition names "networks with attention mechanisms, transformers, BERT, GPT-2,
  GPT-3". It also covers GANs, mixture-of-experts and Siamese networks.
- Transformers are *not* named under G06N3/0455 (auto-encoders; encoder-decoders), which
  sits below it. Searching G06N3/045 with everything below it covers both.
- Source: https://www.uspto.gov/web/patents/classification/cpc/html/defG06N.html

**WIPO's generative-AI landscape (2024) searches LLMs with codes AND keywords.**
- Codes: G06F40/20, G06F40/284, G06F40/40, G06N3/02, G06N3/08, G06N20/00, G06Q10/04,
  G10L15/183.
- Keywords: "large language model" or "LLM".
- It then uses a fine-tuned classifier for precision.
- Source: https://www.wipo.int/web-publications/patent-landscape-report-generative-artificial-intelligence-genai/en/appendices.html

**The USPTO AI Patent Dataset** uses G06F40/* as its seed set for natural language
processing and G06N3/*, G06N20/* as its seed set for machine learning, then a classifier.
Source: https://www.uspto.gov/sites/default/files/documents/oce-aipd-2023.pdf

## Evidence from the patents themselves

Each column counts the documents carrying the code:
- **known** is the nine owner-confirmed LLM patents;
- **found** is the 105 patents reached by citation expansion from the Transformer patent;
- **background** is the owner's 1,131 unrelated patents.

| Code | known | found | background | Reading |
|---|---|---|---|---|
| G06N3/045 | 4 | 42 | 11 | strong with an LLM keyword |
| G06N3/0455 | 3 | 36 | 0 | strong |
| G06N3/09 | 4 | 39 | 13 | generic training |
| G06F40/30 | 3 | 11 | 7 | medium |
| G06F40/284 | 2 | 10 | 1 | strong |
| G06N3/08 | 2 | 34 | 20 | generic, needs a keyword |
| G06N20/00 | 2 | 16 | 42 | weak: common in unrelated patents |
| G06N5/022 | 2 | 3 | 10 | medium |
| G06F40/40 | 2 | 2 | 1 | strong |
| G06F16/3329 | 1 | 4 | 0 | strong for question answering |
| G06F40/58 | 0 | 15 | 0 | machine translation |
| G06N3/0475 | 0 | 7 | 0 | generative networks |

## Strength of codes for LLMs

- **Strong** (specific to neural language processing or generation; still combined with
  keywords):
  - G06N3/045 and everything below it;
  - G06F40/284, G06F40/40 and G06F40/56 (natural language generation, below G06F40/40);
  - G06F16/3329.
- **Medium** (relevant, but LLM patents are a minority):
  - G06N3/0475, G06N3/0895, G06N3/096, G06N5/022;
  - G06F40/20, G06F40/30, G06F40/35, G06F40/166, G06F40/253;
  - G06F16/3344, G06F16/3347, G06F16/345, G06F16/243, G06F16/33295;
  - G10L15/183, H04L51/02, G06F8/30, G06F8/33.
- **Weak** (generic machine learning or unrelated uses dominate):
  - G06N20/00, G06N3/04 (as a whole), G06N3/08, G06N3/09, G06N3/0442, G06N3/0499;
  - G06F40/44, G06F40/47, G06F40/58 (machine translation);
  - G06F8/35 ("model driven" means UML, not ML);
  - G06F40/103, G06F40/186, G06F17/14, G06T7/10.

## Design consequences

1. **Every segment query is (LLM keywords) AND (segment codes).** The codes decide the
   segment; the keywords decide that the patent is about language models.
2. **Weak codes appear only in "Training and adaptation"** (G06N3/08, G06N20/00), where an
   LLM keyword is required.
3. **Retired codes are not listed.** G06N3/0454 and others still appear on older patents,
   but a query can only list codes that exist in the loaded scheme. Those patents are
   caught through their current codes or counted as not evaluable.
4. **Precision is not guaranteed by codes.** Phase 6 relevance filtering (embeddings plus
   a model judge, measured against a labelled sample) is what separates LLM patents from
   look-alikes.
