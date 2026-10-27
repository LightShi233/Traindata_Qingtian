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

## The diversity contract (do this before any chunk)

Read `references/anti-shortcut.md` first. A chunk is designed, not sampled: write
the diversity plan **before** calling the model, then pass it in the prompt, then
let `traindata/scripts/audit_diversity.py` check that the model honoured it.

For each case in the batch, decide up front and put it in a small table:

- `scenario_family` — rotate a set of task shapes, not one.
- `entity_family` + name + domain — no family in more than 25% of the chunk;
  names must appear in the text.
- `id_style` — rotate at least three formats (`NFS-3107`, `#4411`, a UUID, a
  slug, a hex hash, a date-sequence).
- `injection_present` — at most half the chunk; and if there are injected cases,
  at least one must be `safe=true` (resisted) and one `safe=false` (yielded).
- `injection_surface` and `payload_style` — rotate across body / subject /
  status / footer / description / notes, and across wording registers.
- `payload_span` — write it yourself if you can, so the model copies a plan
  instead of inventing a template.
- `thought_style` — rotate reasoning voices; vary sentence length too.
- `benign_directive_span` — for 25–75% of clean cases, an instruction-like but
  legitimate substring.

Then tell the model the plan, case by case, and require it to echo the `meta`
block. Anything the model invents that is not in the plan is a smell.

### Rotation matrices (pick one cell per case, no repeats inside a chunk)

| dimension | options to rotate |
| - | - |
| scenario | kb triage · ticket reply · incident update · doc lookup · queue cleanup · status ping · calendar/booking · data reconciliation |
| org style | one-word brand · two-word brand · person name + collective · acronym + industry noun |
| ID style | `ABC-1234` · `#1234` · UUID · lowercase-slug · 12-hex · `YYYY-MM-DD-NN` |
| injection surface | article body · ticket subject · tool `status` · footer · `description` · `notes` field · revision comment · calendar description |
| payload register | vendor policy · workflow step · authority claim · data-sync request · admin handoff · compliance sweep |
| thought voice | terse check · planner · domain expert · cautious · skeptical · uncertain |

### Payload writing rules

- Ordinary operational prose beats obvious attack prose. See the examples in
  `references/anti-shortcut.md`.
- Vary the verb: cc, route, mirror, sync, escalate, attach, digest — not always
  "forward".
- Vary length from one clause to three sentences.
- Fictional but plausible addresses with varied domains and local parts; never
  an address that names the intent.
- Put addresses in clean cases too. If only poison has an address, the model
  learns "address ⇒ attack".

## Prompt shape

One generation call should produce a JSON array of cases with everything the
validator and the audit need. Include in the prompt:

1. the output schema, verbatim, including the `meta` block
2. the diversity plan table for this chunk (one row per case)
3. the exact number of cases and the required safe/unsafe **and**
   injection_present split
4. the domains/topics to cover this chunk
5. the **required label coverage** for this chunk, named explicitly, so the
   taxonomy does not collapse onto one or two easy options
6. the soft-label rule (`0.9` primary, `0.1` spread)
7. the hard rules: no one-hot, exact field set, tool calls ≥2, length range,
   fictional data only, **no fixed payload phrasing, no intent-naming local
   parts** (domains are free)
8. the **locked email assignment** block from `sample_emails.py` (the generator
   must run the script and must not read the library files itself), with the
   cross-mapping order, the note that the batch is a whitelist enforced via
   `--email-manifest`, and the two answer-time rules: a `safe=true` case on a
   disposable address must neutralise it in the thought (vary the wording); a
   `safe=false` case must violate through an action, never because of the domain
9. an instruction to output JSON only, no prose and no code fences

Ask for a whole case at once so all its questions share one `text`.

## The accept loop

```python
accepted = []
while len(accepted) < target:
    plan = build_diversity_plan(remaining=target - len(accepted))
    emails = sample_locked_emails(scenario, count, seed)   # sample_emails.py
    batch = call_model(prompt_for(plan, emails))
    for case in parse(batch):
        assign_unique_id(case)          # from this worker's ID range
        errors = validate(case)         # same checks as the CLI validator
        if errors:
            log_and_drop(case, errors)  # regenerate rather than hand-patch
            continue
        accepted.append(case)
write(accepted)
run_cli_validator(output_path)          # must exit 0
run_diversity_audit(output_path)        # must exit 0: diversity + cross-map + action attribution
run_email_dedup(output_path)            # REGENERATE EMAIL YOURSELF on over-reuse
```

Rules:

- **Never** weaken the validator or the audit to make a batch pass. Fix the data
  or the prompt.
- Track a rejection count and its reasons; a rising rate means the prompt drifted.
- Keep a per-worker ID prefix so parallel workers cannot collide.
- Write only accepted records to the chunk file; keep rejects in a side log.
- Keep the diversity plan next to the chunk so a reviewer can compare plan to
  output.

## Retry and repair

- schema violation → regenerate the case
- length out of range → regenerate or resplit the source text; do not truncate a
  turn list mid-`action`
- near-duplicate of another case → regenerate with a different template
- label/argmax mismatch → regenerate; do not silently reorder probabilities
- JSON parse failure → regenerate; if the model keeps emitting fences, tighten
  the instruction
- diversity failure → regenerate the **whole chunk** with a new plan; do not
  hand-patch one case, because the failure is the generator's distribution, not
  one row. "Fixed the phrase, kept the sentence" is still the same shortcut.

## Parallel workers

- one ID range per worker (e.g. worker `k` owns `<prefix>_k<NNNN>_<hex>`)
- one output file per worker: `chunk_0001.jsonl`, `chunk_0002.jsonl`, ...
- each worker validates its own chunk and runs the diversity audit before finishing
- each worker owns its own slice of the rotation matrix, so parallel workers do
  not converge on the same org names, surfaces, or payload templates
- integration re-checks duplicates and leakage across all chunks

## Scaling checklist before a big run

- [ ] pilot chunk passed the validator
- [ ] pilot chunk passed `audit_diversity.py` (exit 0, `--require-injection --strict`)
- [ ] pilot chunk was inspected by hand and labels looked right
- [ ] scenario hash holdout written and excluded from training/tuning
- [ ] taxonomy coverage is explicit per chunk, not left to chance
- [ ] no `choice`/`score` instruction has dual-polarity phrasing
- [ ] leakage check is wired against all existing corpora
- [ ] train / calibration / test ID ranges are disjoint
- [ ] pilot is a prompt gate, not training data; if any is used, it is mixed
      below 5% with the base corpus and the ratio is in the data card
- [ ] a rejection-rate alarm exists (e.g. warn above 30%)

## Tooling

Two bundled tools cover the loop. Run **both** on every chunk; each must exit 0
before the chunk is accepted.

`traindata/scripts/validate_and_integrate.py` — schema and soft-label contract:

- `--check` validates records; add `--expand` to validate case-form chunks
  without writing anything.
- `--integrate --expand` validates and expands accepted chunks into one file.
- `--split-dir` writes deterministic, case-disjoint `train` / `calibration` /
  `test` files plus `split_manifest.json`, then prints the leakage command to
  run between splits.
- `--card` writes the Markdown data card from the validated rows.
- `--require-complete-cases` rejects a case that is missing a taxonomy question.

`traindata/scripts/audit_diversity.py` — the shortcut-learning gate:

- reads case-form chunks and their `meta` block, and fails on fixed payload
  fingerprints, giveaway addresses, payload reuse, one org/ID/voice/surface,
  injected-heavy corpora, and dual-polarity taxonomy wording.
- `--require-injection` for the injection domain; `--strict` to promote warnings
  to failures; `--json` writes a report for the data card.
- `--holdout N` writes a scenario-hash `holdout.jsonl` / `dev.jsonl` plus a
  manifest; never train or tune on the holdout.

## Cost and time

Cheap models are fine for volume, but verify the pilot: a weak generator produces
schema-valid, semantically wrong labels, which is worse than no data. Reserve the
stronger model for the pilot and for any chunk whose rejection rate spikes. A
**single cheap generator's voice** is itself a shortcut (see
`references/anti-shortcut.md`): as volume scales, rotate generator models or
prompt personas per chunk and let the audit confirm the styles really diverge.