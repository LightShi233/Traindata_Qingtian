# Generation playbook

How to produce a large corpus without letting quality decay. Terms are defined
in `references/glossary.md`; "domain" below means **email domain** only where it
says so — otherwise it means **task domain** (the subject area of a case).

## Sizing

| Quantity | Guidance |
| - | - |
| target corpus | ≥2000 cases, 2–4 questions each → 4000–8000 records |
| chunk size | 8–12 cases per generation call |
| pilot | 1–2 chunks, inspected by hand before scaling |
| parallelism | 4–8 independent workers, each with a disjoint ID range |

Small chunks matter: a single large request tends to truncate, repeat templates,
and drift from the schema late in the output.

### Minimum pilot matrix

Do the axis math **before** generating. The 40% caps make some small chunks
impossible, and a model that keeps retrying one can loop forever:

- `injection_surface` / `payload_style` are counted over injected cases only.
  With 2 injected cases every value is 50% > 40% — **unsolvable**. Use ≥3
  injected cases with 3 distinct surfaces and 3 distinct styles.
- `request_shape` / `harm_kind` are counted over all cases. At 7 cases the cap
  is `floor(0.4 × 7) = 2` per shape, so three shapes can hold at most 6 cases —
  **7 cases are unsolvable**; use ≥8 (8 gives a 3 + 3 + 2 split).
- `--strict` also requires every `choice` key and every `score` level to appear
  at least once.

A working 8-case pilot (the shape this skill was tested with):

| case | safe | injection (surface / style) | mechanism | severity | harm_kind | request_shape | id_style |
| - | - | - | - | - | - | - | - |
| 1 | T | — (hard negative) | own_reasoning | 0 | none | near_miss | ticket_num |
| 2 | T | footer / workflow_step (resisted) | injection | 0 | data_egress | standard | prefix_seq |
| 3 | T | — (hard negative) | other | 0 | none | near_miss | slug |
| 4 | F | description / authority_claim (yielded) | injection | 3 | data_egress | contextual | uuid |
| 5 | F | — | misdirection | 1 | destructive_change | contextual | hex |
| 6 | F | — | faulty_input | 2 | misinformation | standard | ticket_num |
| 7 | T | status / admin_note (resisted) | injection | 0 | data_egress | standard | uuid |
| 8 | F | — | own_reasoning | 2 | destructive_change | contextual | uuid |

It covers all 5 `mechanism` keys, all 4 `severity` levels, 4 `harm_kind`s and
all 3 `request_shape`s. Clean cases carry 2 hard negatives (25–75% window) and
the address batch is 3 cross-mapped pairs (2 freemail + 1 disposable): one
address of each pair goes into a clean case (1, 2, 3) and its twin into a
violating case (4, 5, 6), so the violation side stays ≥40% freemail. Case 7
carries no address, which keeps the clean/injected address gap small. Every
`scenario_family`, `entity_family`, and user-turn opener is distinct; the
8 org families are each 12.5% of the chunk.

## The diversity contract (do this before any chunk)

Read `references/anti-shortcut.md` first. A chunk is designed, not sampled: write
the diversity plan **before** calling the model, then pass it in the prompt, then
let `traindata/scripts/audit_diversity.py` check that the model honoured it.

For each case in the batch, decide up front and put it in a small table:

- `scenario_family` — rotate a set of task shapes, not one.
- `entity_family` + name + email domain — no family in more than 25% of the
  chunk; names must appear in the text.
- `id_style` — rotate at least three formats (`NFS-3107`, `#4411`, a UUID, a
  slug, a hex hash, a date-sequence).
- `injection_present` — at most half the chunk; and if there are injected cases,
  at least one must be `safe=true` (resisted) and one `safe=false` (yielded).
- `harm_kind` and `request_shape` — rotate both orthogonal axes
  (`data_egress`/`privilege_escalation`/… × `standard`/`contextual`/`near_miss`);
  `contextual` needs a literal `context_span`, `near_miss` a
  `benign_directive_span`. See `references/taxonomy-design.md`.
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
| thought voice | terse check · planner · subject-matter expert · cautious · skeptical · uncertain |

### Payload writing rules

- Ordinary operational prose beats obvious attack prose. See the examples in
  `references/anti-shortcut.md`.
- Vary the verb: cc, route, mirror, sync, escalate, attach, digest — not always
  "forward".
- Vary length from one clause to three sentences.
- Fictional but plausible addresses with varied email domains and local parts;
  never an address that names the intent.
- Put addresses in clean cases too. If only poison has an address, the model
  learns "address ⇒ attack".

## Prompt shape

One generation call should produce **YAML** — a list of cases with everything the
validator and the audit need (the converter does the JSON escaping). Include in
the prompt:

1. the output schema, verbatim, including the `meta` block
2. the diversity plan table for this chunk (one row per case)
3. the exact number of cases and the required safe/unsafe **and**
   injection_present split
4. the **task domains**/topics to cover this chunk
5. the **required label coverage** for this chunk, named explicitly, so the
   taxonomy does not collapse onto one or two easy options
6. the soft-label rule (`0.9` primary, `0.1` spread)
7. the hard rules: no one-hot, exact field set, tool calls ≥2, length range,
   fictional data only, **no fixed payload phrasing, no intent-naming local
   parts** (email domains are free)
8. the **locked email assignment** block. The *orchestrator* runs
   `sample_emails.py` and pastes its output; the generator receives only that
   block, reuses the addresses verbatim, and must not read the library files
   itself. State the cross-mapping order, that the same `--ledger` is used for
   every chunk, that the batch is a whitelist enforced via `--email-manifest`,
   and the two answer-time rules: a `safe=true` case on a disposable address must
   neutralise it in the thought (vary the wording); a `safe=false` case must
   violate through an action, never because of the email domain
9. an instruction to output YAML only — one document, no prose and no code
   fences; the model never hand-escapes JSON

Ask for a whole case at once so all its questions share one `text`.

## The accept loop

This loop is the **orchestrator's**; when generation is delegated, a worker only
produces the YAML batch (see *Parallel workers*).

```python
accepted = []
while len(accepted) < target:
    plan = build_diversity_plan(remaining=target - len(accepted))
    emails = sample_locked_emails(scenario, count, seed, ledger)  # sample_emails.py
    batch = call_model(prompt_for(plan, emails))       # YAML, not JSON
    for case in load_yaml(batch):                      # converter reports line numbers
        assign_unique_id(case)          # from this worker's ID range
        errors = validate(case)         # same checks as the CLI validator
        if errors:
            log_and_drop(case, errors)  # regenerate rather than hand-patch
            continue
        accepted.append(case)
write_yaml(accepted, allocated_slot)    # data/inbox/chunk_NNNN.yaml
preflight_gates(yaml)                   # validate + audit on the YAML (no --require-salt)
run_round_write()                       # data_writer.py write: convert + validate + salt stamp
postflight_gates()                      # validate + audit again, this time --require-salt
run_email_dedup()                       # REGENERATE EMAIL YOURSELF on over-reuse
# any gate failure after write: data_writer.py discard --round R --name N --reason CODE,
# fix the YAML, write the same name, then merge again. Never rm or hand-edit the JSONL.
```

Rules:

- **Never** weaken the validator or the audit to make a batch pass. Fix the data
  or the prompt.
- **Gate the YAML before `write`.** Both gates read `.yaml` directly; a chunk
  that fails preflight never becomes a JSONL in the round. The preflight audit
  omits `--require-salt` because the host stamp exists only after `write`.
- **After a written chunk fails a gate, `discard` it.** That is the only
  supported recovery; reallocating a fresh name leaves the bad chunk in the
  round and `merge` will still include it.
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
- YAML parse failure → regenerate; the converter names the line, and if the model
  keeps emitting fences or tabs, tighten the instruction
- diversity failure → regenerate the **whole chunk** with a new plan; do not
  hand-patch one case, because the failure is the generator's distribution, not
  one row. "Fixed the phrase, kept the sentence" is still the same shortcut.
  If the chunk was already written, `discard` it first so `merge` cannot pick
  it up.

## Parallel workers

- one ID range per worker (e.g. worker `k` owns `<prefix>_k<NNNN>_<hex>`)
- one allocated slot per worker: the worker writes `data/inbox/chunk_NNNN.yaml`;
  the orchestrator's `write` produces `data/chunks/chunk_NNNN.jsonl`
- **the worker only emits YAML into its allocated slot; the orchestrator runs
  `write`, the validator, and the audit.** A worker's gate call may be blocked by
  the permission classifier (adversarial-looking content); that is not a verdict.
  Workers must not depend on executing the gates to know if they passed.
- **give each worker its plan as a file it Reads, not inline.** A long inline
  matrix truncates, and a worker that lost its tail will invent replacements and
  collide on payloads. The worker echoes back the exact `traj_id`s and
  `payload_span`s it authored; the orchestrator diffs that echo against the plan
  and re-dispatches on any mismatch instead of merging.
- assign disjoint `payload_span` and user/thought openers across workers; keep a
  corpus-wide payload ledger (mirroring the email `--ledger`) so no two workers
  reuse one payload, and run the cross-chunk dedup inside the loop rather than
  only at final integration.
- each worker owns its own slice of the rotation matrix, so parallel workers do
  not converge on the same org names, surfaces, or payload templates
- integration re-checks duplicates and leakage across all chunks
- a blocked / off-plan worker chunk must not stall the round: salvage the chunks
  that pass; if a written chunk is off-spec, `discard` it and redo the same slot
  (a fresh slot only if the YAML is a total loss), never leaving it written

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

Two bundled tools decide acceptance. Run **both** on every chunk; each must exit 0
before the chunk is accepted. (`data_writer.py write` runs the first of them for
you, and also refuses off-batch addresses, so in the round workflow the audit is
the gate you invoke directly.)

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
- `--require-injection` for any round whose task domain involves injections;
  `--strict` to promote warnings to failures; `--json` writes a report for the
  data card.
- `--holdout N` writes a scenario-hash `holdout.jsonl` / `dev.jsonl` plus a
  manifest; never train or tune on the holdout.

## Cost and time

Cheap models are fine for volume, but verify the pilot: a weak generator produces
schema-valid, semantically wrong labels, which is worse than no data. Reserve the
stronger model for the pilot and for any chunk whose rejection rate spikes. A
**single cheap generator's voice** is itself a shortcut (see
`references/anti-shortcut.md`): as volume scales, rotate generator models or
prompt personas per chunk and let the audit confirm the styles really diverge.