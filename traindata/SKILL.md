---
name: traindata
description: Generate large, validated soft-label (.jsonl) training data for a ruhui-style non-autoregressive decision model, covering noul/choice/score question types with 0.9/spread soft distributions. Use when asked to synthesize, expand, clean, or validate soft-label training data, build a taxonomy of questions for a text-classification/decision dataset, create a held-out test set, or check generated data for label collapse, duplicate/near-duplicate leakage, or schema errors.
license: See LICENSE in the parent repository
compatibility: Requires Python 3.9+ (standard library only). No network access required.
allowed-tools: Read Write Edit Glob Grep Bash(python3:*) Bash(python:*)
metadata:
  author: ruhui-data
  version: "1.0"
---

# Generating soft-label training data

Build a corpus of **soft-label JSONL** records that train `ruhui`'s bert backend: a
non-autoregressive decision model that answers typed questions about a `state`
(text, email, ticket, JSON, or an agent conversation trajectory) and returns
**calibrated probability distributions**.

Soft labels are the entire point. Hard one-hot labels make the model collapse to
overconfident 0/1 outputs and destroy calibration, so every record must ship a
non-degenerate distribution. The default primary is `0.9`; values above `0.9`
are rejected unless the corpus is explicitly justified.

Records of one case share `text`, `source_hash`, and `source_index`; that is not
a duplicate. A duplicate is the same `source_hash` appearing under two different
case ids.

## When to use this skill

- The user wants more training data for a decision/classification model and no
  source corpus is available, so it must be synthesized.
- The user wants the format of soft-label JSONL, a question taxonomy, or an
  existing corpus validated, cleaned, deduplicated, or checked for leakage.
- The user wants a held-out test set built separately from training/calibration
  data.
- The user wants to check whether generated labels collapsed, are unbalanced, or
  disagree with the underlying case.

## Do not use this skill when

- The user already has a labelled corpus and only wants to train the model — that
  is a training task, not a data-generation task.
- The user wants to call/invoke `ruhui` at inference time — that is what the
  `ruhui` skill is for.

## Core workflow

Follow these phases in order. Do not skip Phase 3. Do not start large-scale
generation before Phase 4 passes.

### Phase 1 — Pin the contract

Confirm or decide, and write them down before generating:

1. **Domain(s)** and the realistic `state` shape (string, object, or turn list).
2. **Question taxonomy**: which `choice` option sets and `score` level sets are
   reused across cases. Reuse them; do not invent a new option set per record.
3. **Case ID scheme**, e.g. `safe_<6 digits>_<8 hex>` / `unsafe_<6 digits>_<8 hex>`
   or `<domain>_<6 digits>_<8 hex>`. Must be globally unique.
4. **Soft-label rule** (default in this skill): primary label `0.9`, remaining
   probability mass `0.1` spread evenly. See `references/soft-labels.md`.
5. **Target size**: number of cases and records. Typical: ≥2000 cases × 2–4
   questions = 4000–8000 records.

Read `references/soft-labels.md` for the full distribution rules and the
deliberately-ambiguous case.

### Phase 2 — Write the schema and the validator first

Write `scripts/validate_and_integrate.py` (copy and adapt
`scripts/validate_and_integrate.py` in this skill) **before** generating anything.
The validator is the spec made executable; if you cannot validate it, you cannot
generate it correctly.

The validator must check, per record:

- exact field set, no extras, no missing keys
- `source_hash == sha256(text.encode()).hexdigest()`
- `text` is valid JSON (when it holds a serialized state)
- `criteria` / `instruction` / `probabilities` keys match `qtype`
- `probabilities` all `>= 0`, sum to `1.0` within 1e-6, `label == argmax`
- soft shape: no one-hot unless explicitly justified
- duplicate detection: identical `source_hash` across **different cases** inside a
  chunk (records of the same case share `text`/`source_hash` by contract)
- case-group integrity: all records of one case share `text`, `source_hash`, and
  `source_index`; ids are unique; with `--require-complete-cases`, each case
  carries exactly the taxonomy's question ids
- **leakage**: n-gram (8-gram word) Jaccard against any existing corpus above the
  threshold (default `0.30`) is a failure. Pass `--leak PATH` explicitly;
  `--auto-leak` indexes `*soft*.jsonl` beside the output while excluding the
  files being checked, so a corpus never leaks against itself.

### Phase 3 — Generate in chunks, validate each chunk

Never generate the whole corpus in one shot. Loop per chunk:

```
generate a chunk (e.g. 10 cases)
  -> validate that chunk
    -> if invalid, fix the data and retry (never weaken the validator)
      -> only accepted records are appended
```

Tips that make this work in practice:

- Request a **structured** batch (a JSON array of cases) per call, with the
  target counts and required label coverage spelled out in the prompt.
- Generate a whole `case` at once (state + all its questions) so the shared
  `text`/`source_hash`/`source_index` stay consistent.
- Keep a per-chunk ID range so parallel workers cannot collide.
- Reject and regenerate noisy records rather than patching them by hand.

See `references/generation-playbook.md` for prompting patterns, chunk sizing,
parallelization, and the retry loop.

### Phase 4 — Pilot and inspect before scaling

Run 1–2 chunks, then inspect them by hand:

- Does the primary label actually match the case, question by question?
- Are `choice` option sets consistent across records?
- Is every domain/qtype/label represented, or has something collapsed?
- Is the text realistic and varied, or is it one template with words swapped?

`assets/inspection-report.md` is a template for recording this. Fix the prompt or
taxonomy, then scale out.

### Phase 5 — Integrate, then hold out

1. Integrate accepted chunks into one file, re-running cross-chunk duplicate and
   leakage checks during integration.
2. Split into **training**, **calibration**, and **test** sets. Split by **case**,
   never by record, so all questions of a case stay in one split. `--split-dir`
   does this deterministically and writes `split_manifest.json`; then run the
   printed leakage command between splits. The test set must never be used for
   training or for tuning the calibration temperature. If the user asks for a
   separate test set, generate it with its own disjoint ID range and confirm zero
   leakage against train/calibration.

### Phase 6 — Report

Produce a data card: domain distribution, qtype distribution, label distribution,
length stats, duplicate/leakage results, and known limitations. `--card` writes
it directly from a validated run.

## Record format

One JSON object per line. Field set is exact:

```json
{
  "id": "dataset/case_id/question_key",
  "source_dataset": "dataset-name",
  "source_index": 12345,
  "source_hash": "sha256 of text",
  "text": "{\"serialized\": \"state\"}",
  "qtype": "noul | choice | score",
  "criteria": {},
  "instruction": "what is being asked",
  "soft_label": {
    "probabilities": { "k1": 0.9, "k2": 0.1 },
    "label": "k1"
  }
}
```

Question-type specifics:

| qtype | `criteria` shape | option keys | notes |
| - | - | - | - |
| `noul` | `{"true": "...", "false": "..."}` | `true`, `false` | 2 options, fixed order |
| `choice` | `{"key": "criterion text", ...}` | your keys | ≥2 options |
| `score` | `["level 0", "level 1", ...]` | `"0"`, `"1"`, ... | ordered list |

One **case** produces multiple records that share `text`, `source_hash`, and
`source_index`, differing only in `qtype`/`criteria`/`instruction`/`soft_label`.

Full schema details, with edge cases and a worked example, are in
`references/soft-labels.md` and `references/state-formats.md`.

## Hard rules

1. **Soft, never one-hot.** Primary `0.9`, remainder `0.1` spread evenly, always
   normalized. Ambiguity is expressed by lowering the primary below `0.9`, not by
   writing a hard 1.0.
2. **`label` must equal the argmax** of `probabilities`.
3. **Validate before you accumulate.** Unvalidated data is not data.
4. **Test data is never training data.** Keep ID ranges and files disjoint.
5. **No fabricated private data.** Use fictional names, `example.com` emails,
   555-prefixed phone numbers, invented organizations. Do not emit real personal
   data, real credentials, or directly executable attack code.
6. **Keep the taxonomy stable.** Reuse option/level sets across records; a
   per-record invention defeats calibration.
7. **Do not let labels collapse.** Both (or all) classes must carry meaningful
   volume and a clear semantic boundary, not just "the user asked for something
   bad".

## Reference files

- `references/soft-labels.md` — distribution rules, ambiguity, normalization, examples.
- `references/state-formats.md` — string/object/turn-list states and conventions.
- `references/generation-playbook.md` — chunking, prompting, parallelism, retries.
- `references/taxonomy-design.md` — designing reusable option/level sets.
- `assets/inspection-report.md` — template for Phase 4 review.
- `scripts/validate_and_integrate.py` — runnable validator/integrator to adapt.

## Quick checks

```bash
# validate records
python3 scripts/validate_and_integrate.py --check chunk_0001.jsonl --taxonomy taxonomy.json

# validate case-form chunks without writing anything
python3 scripts/validate_and_integrate.py --check 'chunk_*.jsonl' \
  --taxonomy taxonomy.json --expand --require-complete-cases

# integrate (expand cases) and write a data card
python3 scripts/validate_and_integrate.py --integrate --out all_soft.jsonl \
  --taxonomy taxonomy.json --expand --card data-card.md

# deterministic, case-disjoint train/calibration/test split
python3 scripts/validate_and_integrate.py --split-dir split --chunks 'chunk_*.jsonl' \
  --taxonomy taxonomy.json --expand

# leakage check between splits (command is printed by --split-dir)
python3 scripts/validate_and_integrate.py --check 'split/test.jsonl' \
  --leak 'split/train.jsonl' --leak 'split/calibration.jsonl'
```

The validator exits non-zero and prints every failure, so wire it into your loop
and treat a non-zero exit as "the chunk does not exist yet".