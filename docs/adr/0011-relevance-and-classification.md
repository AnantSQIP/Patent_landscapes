# ADR 0011: Relevance filtering and segment classification (pipeline steps 6–7)

* Status: proposed (Phase 6)
* Date: 2026-09-26

## Decision

**Unit and text**
* The unit is the dataset **family** (ADR 0008).
* Each family is judged on one representative publication, chosen by fixed rules:
  abstract first, then English, then the earliest publication, then the lowest number.
* The text is title plus abstract.
* A family without text is `no_text`, and is counted, never guessed.

**Embeddings**
* The `embedding` role (pgvector, `text_embedding`, one row per model and text hash)
  compares each family with prototypes: the topic, each segment's definition, and the
  texts of the person-confirmed patents.
* Scores are cosine similarities, rounded half-even to 4 decimals as `Decimal`. This
  makes threshold decisions exact and repeatable.

**Relevance, with two methods**
* Bands:
  * `high` (at or above the high threshold) is relevant;
  * `low` (at or below the low threshold) is not relevant;
  * `middle` is borderline.
* Borderline families always go to the judge (`bulk_classifier` role, prompt
  `relevance.judge` v1).
* A fixed sample of confident families (seeded hash, `qa_sample_rate`) also goes to the
  judge, which measures agreement.
* A judge answer is used only if both hold:
  * its evidence is quoted verbatim from the text;
  * its confidence meets the minimum.
* A usable disagreement with a confident band makes the family `uncertain`. So does a
  borderline family with no usable answer. Nothing is forced.

**Segments (multi-label, relevant families)**
* The embedding votes on similarity to each segment prototype. The judge (prompt
  `segments.classify` v1) assigns segments, each with quoted evidence.
* Agreement assigns the segment; disagreement is `uncertain`. Unknown segment ids and
  invented evidence are recorded as problems.
* A relevant family with no segment is `unclassified`.

**Review queue**
* Every `uncertain` relevance or segment decision appears in `plr classify review`, with
  its reason.

**Gold set**
* Every file handed to people is recorded as a `label_export`, and imports must name their
  export. There are two kinds:
  * `plr label export` is a seeded, uniform random **sample**. These are the only labels
    used to measure accuracy.
  * `plr label review` is the **review queue**. These labels resolve uncertain decisions
    and never enter the figures, since they are the hardest cases.
* Neither file shows the system's decisions.
* A "not relevant" label also marks every segment "no", so old segment labels cannot
  outlive a correction.
* Excel files separated by ";" or tab are read. A file that is not UTF-8 is rejected with
  instructions. A file with any invalid row is rejected whole.
* Labels belong to dataset families, so they survive new runs.
* Each labeller's newest label counts. Labellers who disagree are reported and block
  publication until reconciled.

**Evaluation** (`plr classify evaluate`)
* It reports precision, recall and F1, with Wilson 95% intervals, for relevance and for
  each segment.
* `uncertain` counts as not found.
* A run is **publishable** only if all of these hold:
  * enough sampled labels, and enough of them relevant;
  * precision and recall meet their minimums. They are compared unrounded against the 95%
    Wilson lower bound by default (`gate_on: lower_bound`), so a tiny sample cannot pass
    by luck;
  * enough sampled families have segment labels, and every segment with any hit or error
    meets the minimum F1;
  * no labeller conflicts;
  * no review item is open.

**Failures**
* A judge call whose output never validates is recorded against that family, with its
  call key. If more than `max_judge_failure_rate` of the calls fail, the run stops and
  nothing is stored: a broken model must not fill the review queue.
* A failed segment call makes every segment of that family `uncertain`.
* A quote must be at least 15 characters to count as evidence.

**Embeddings** are stored per (backend, model, text), inserted once, so every stored score
can be recomputed.

**Atomicity**
* A run and all its decisions are written in one transaction.
* Model calls are cached, so repeating a failed run is cheap.

## Threshold calibration (2026-09-26)
The model was all-minilm and the taxonomy was the LLM one.
* The nine owner-confirmed LLM patents scored 0.4505–0.6774.
* The owner's 1,131 unrelated patents had a median of 0.1894, a 99th percentile of 0.4080
  and a maximum of 0.4955.
* Chosen thresholds: high 0.50 (no unrelated patent reached it; 8 of 9 confirmed did) and
  low 0.35 (all 9 confirmed are above it).

This is a calibration check, not a gold set. Accuracy is measured only against people's
labels.

## Consequences
* **Memory:** a run holds all family vectors in memory, about 150 MB per 100,000 families
  with 384 dimensions (as Python floats, about 1 GB). Landscapes of that size need chunked
  scoring, which is planned when a real run needs it.
* On a CPU, the local judge (qwen2.5 3B) takes seconds per family. Large landscapes need a
  GPU or a hosted model; the gateway makes that a configuration change.
* Until someone labels a sample, every run is reported as not publishable. The
  methodology prints the measured figures once they exist.
