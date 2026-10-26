# Generation playbook

How to produce a large corpus without letting quality decay.

## Sizing

| Quantity | Guidance |
| - | - |
| target corpus | ≥2000 cases, 2–4 questions each → 4000–8000 records |
| chunk size | 8–12 cases per generation call |
| pilot | 1–2 chunks, inspected by hand before scaling |
| parallelism | 4–8 independent workers, each with a disjoint ID range |

Small chunks matter: a single large request tends to truncate, repeat templates,
and drift from the schema late in the output.

## Prompt shape

One generation call should produce a JSON array of cases with everything the
validator needs. Include in the prompt:

1. the output schema, verbatim
2. the exact number of cases and the required safe/unsafe (or class) split
3. the domains/topics to cover this chunk
4. the **required label coverage** for this chunk, named explicitly, so the
   taxonomy does not collapse onto one or two easy options
5. the soft-label rule (`0.9` primary, `0.1` spread)
6. the hard rules: no one-hot, exact field set, tool calls ≥2, length range,
   fictional data only
7. an instruction to output JSON only, no prose and no code fences

Ask for a whole case at once so all its questions share one `text`.

## The accept loop

```python
accepted = []
while len(accepted) < target:
    batch = call_model(prompt_for_remaining(target - len(accepted)))
    for case in parse(batch):
        assign_unique_id(case)          # from this worker's ID range
        errors = validate(case)         # same checks as the CLI validator
        if errors:
            log_and_drop(case, errors)  # regenerate rather than hand-patch
            continue
        accepted.append(case)
write(accepted)
run_cli_validator(output_path)          # must exit 0
```

Rules:

- **Never** weaken the validator to make a batch pass. Fix the data or the prompt.
- Track a rejection count and its reasons; a rising rate means the prompt drifted.
- Keep a per-worker ID prefix so parallel workers cannot collide.
- Write only accepted records to the chunk file; keep rejects in a side log.

## Retry and repair

- schema violation → regenerate the case
- length out of range → regenerate or resplit the source text; do not truncate a
  turn list mid-`action`
- near-duplicate of another case → regenerate with a different template
- label/argmax mismatch → regenerate; do not silently reorder probabilities
- JSON parse failure → regenerate; if the model keeps emitting fences, tighten
  the instruction

## Parallel workers

- one ID range per worker (e.g. worker `k` owns `<prefix>_k<NNNN>_<hex>`)
- one output file per worker: `chunk_0001.jsonl`, `chunk_0002.jsonl`, ...
- each worker validates its own chunk before finishing
- integration re-checks duplicates and leakage across all chunks

## Scaling checklist before a big run

- [ ] pilot chunk passed the validator
- [ ] pilot chunk was inspected by hand and labels looked right
- [ ] taxonomy coverage is explicit per chunk, not left to chance
- [ ] leakage check is wired against all existing corpora
- [ ] train / calibration / test ID ranges are disjoint
- [ ] a rejection-rate alarm exists (e.g. warn above 30%)

## Tooling

The bundled validator covers the whole loop:

- `--check` validates records; add `--expand` to validate case-form chunks
  without writing anything.
- `--integrate --expand` validates and expands accepted chunks into one file.
- `--split-dir` writes deterministic, case-disjoint `train` / `calibration` /
  `test` files plus `split_manifest.json`, then prints the leakage command to
  run between splits.
- `--card` writes the Markdown data card from the validated rows.
- `--require-complete-cases` rejects a case that is missing a taxonomy question.

## Cost and time

Cheap models are fine for volume, but verify the pilot: a weak generator produces
schema-valid, semantically wrong labels, which is worse than no data. Reserve the
stronger model for the pilot and for any chunk whose rejection rate spikes.