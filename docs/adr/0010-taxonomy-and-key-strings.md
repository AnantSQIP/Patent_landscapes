# ADR 0010: Scope, taxonomy and key strings (pipeline steps 1–3)

* Status: proposed (Phase 5)
* Date: 2026-09-26

## Decision

**Scope (step 1)**
* A landscape starts from a scope YAML file, stored with its SHA-256. It holds:
  * the topic and optional notes;
  * the publication date range;
  * offices (WIPO ST.3);
  * optional CPC hints;
  * seed patents;
  * `known_relevant` patents, confirmed by a named person.
* Search dates are **publication** dates, because EPO OPS has no priority-date index
  (docs/architecture/query_syntax.md).

**Official classification**
* The official source is the EPO/USPTO **CPC Title List** bulk file. It is loaded once per
  version (`plr cpc load`) and stored byte-for-byte, content-addressed and hash-verified.
* It is parsed exactly, and anything unexpected is an error. Parents are computed from dot
  levels in file order.
* Every CPC code the system uses must exist in the loaded version.
* IPC is not loaded yet. Queries use CPC only, and IPC can be added the same way (WIPO's
  IPC scheme file) if needed.

**Taxonomy (step 2), split between model and code**
1. The `reasoner` model drafts the segments (prompt `taxonomy.draft` v1): name,
   definition, includes and excludes, keywords and synonyms.
2. Code checks the draft:
   * terms that cannot be searched (query-syntax characters, wrong length) are set aside;
   * repeated segment names are dropped.
   Every such case is recorded under `rejected` with its reason.
3. Code searches the official CPC titles with each segment's terms, limited to the
   subclasses of the scope's CPC hints. It works on whole words and ignores
   parenthesised references. A plural matches its singular only when the singular has four
   or more letters. Matched phrases rank above single words.
4. The model picks codes **only from that candidate list** (`taxonomy.cpc_select` v1).
   Picks outside the list, repeats and picks over the limit are rejected and recorded. The
   unchosen candidates stay visible as suggestions.

**Editing and versions**
* A person edits the exported YAML, and `plr taxonomy import` stores it as a new version.
  Invalid CPC codes fail the import.
* Versions are append-only and hash-checked when read.

**Approval gate**
* `landscape.approval_mode` is `human` by default: query generation needs an approved
  taxonomy version, and discovery needs an approved query set.
* `automatic` is the minimum-interaction mode, and records its approvals as
  `mode=automatic`.
* The newest decision counts. Every decision is also an audit event.

**Key strings (step 3), built by code**
* One logical query per segment, in three parts (keywords, cpc, combined). The combined
  part is the search; the other two show what each condition contributes.
* It is rendered to EPO OPS CQL, Lens JSON and BigQuery SQL, with syntax sources and gaps
  listed in docs/architecture/query_syntax.md.
* PatentsView is not generated while its API is offline.
* Values placed in SQL are validated first.

**Counts and recall**
* Counts always name their universe. Today that is `batches:<ids>` (stored records,
  evaluated locally). Provider counts go in the same table once credentials exist.
* Records that cannot be evaluated make a count a lower bound.
* The recall check reports which `known_relevant` patents the search finds, and why each
  missed one was missed: not retrieved, or retrieved but not matched.

**Key-less discovery: citation expansion**
* Hop 0 fetches the seeds.
* Each later hop follows the backward and forward citations of the previous hop's frontier.
  After hop 1, the frontier is only the records that match the approved search.
* Candidates are ranked by number of links, then by number. A cap applies, and every
  excluded candidate is counted with a reason.
* Each hop is an ordinary lookup batch, so re-running resumes it.

## Consequences
* **Recall is limited:** citation expansion finds only what is linked to the seeds within
  the hop limit. The methodology section must say so, and counts describe this universe,
  not the whole patent literature.
* **Draft quality depends on the model.** With the local 3B model on CPU, drafts need real
  human editing: generic keywords, and codes of doubtful relevance chosen from the
  candidates. The checks make sure nothing invented survives, but they cannot make weak
  choices good. A hosted model is expected to do much better.
* **Keys still needed:** provider execution (EPO OPS, Lens, BigQuery) waits for
  credentials. Until then, only local counts exist.
