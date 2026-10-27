---
name: traindata
description: Generate large, validated soft-label (.jsonl) training data for a ruhui-style non-autoregressive decision model, covering noul/choice/score question types with 0.9/spread soft distributions. Use when asked to synthesize, expand, clean, or validate soft-label training data, build a taxonomy of questions for a text-classification/decision dataset, create a held-out test set, or check generated data for label collapse, duplicate/near-duplicate leakage, schema errors, surface-fingerprint shortcut learning, diversity collapse, or dual-polarity question wording.
license: GPL-3.0 (see LICENSE in the repository root)
compatibility: Requires Python 3.9+ (standard library only; the YAML reader is built in, no PyYAML). No network access required.
allowed-tools: Read Write Edit Glob Grep Bash(python3:*) Bash(python:*)
metadata:
  author: ruhui-data
  version: "1.1"
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

Count **scenarios**, not rows. One case = one independent scenario no matter how
many heads it has, so a 30-record pilot can be 10 scenarios. Diversity and
holdout are computed per scenario (`text` hash), and a schema-valid corpus can
still teach a surface shortcut — see `references/anti-shortcut.md`, which is part
of the contract, not optional advice.

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
generation before Phase 4 passes (schema validator **and** diversity audit).

### Phase 1 — Pin the contract

Confirm or decide, and write them down before generating:

1. **Domain(s)** and the realistic `state` shape (string, object, or turn list).
2. **Question taxonomy**: which `choice` option sets and `score` level sets are
   reused across cases. Reuse them; do not invent a new option set per record.
   One head has one polarity — never "the mechanism it avoided" on clean cases.
   Tag each case on the two orthogonal `meta` axes from
   `references/taxonomy-design.md` — `harm_kind` (semantic kind *present in the
   input*) and `request_shape` (`standard` / `contextual` / `near_miss`) — and
   rotate both; breadth on both is what keeps a single easy template from
   dominating.
3. **Case ID scheme**, e.g. `safe_<6 digits>_<8 hex>` / `unsafe_<6 digits>_<8 hex>`
   or `<domain>_<6 digits>_<8 hex>`. Must be globally unique. Rotate ID formats
   across scenarios (prefix, `#num`, UUID, slug, hex) — one format is a shortcut.
4. **Soft-label rule** (default in this skill): primary label `0.9`, remaining
   probability mass `0.1` spread evenly. See `references/soft-labels.md`.
5. **Target size**: number of cases and records. Typical: ≥2000 cases × 2–4
   questions = 4000–8000 records.
6. **Diversity contract and the `meta` block.** Read
   `references/anti-shortcut.md`. Decide the scenario families, org/entity
   families, ID styles, thought voices, injection surfaces, payload registers,
   and class ratios *before* generating, and require the generator to echo them
   in a per-case `meta` object. Clean cases and injected cases are crossed
   deliberately (resisted injections are mandatory); injected cases must not
   exceed half the corpus.

Read `references/soft-labels.md` for the full distribution rules and the
deliberately-ambiguous case. Read `references/anti-shortcut.md` before writing
any generation prompt; a schema-valid corpus that teaches a surface fingerprint
is a failure.

### Phase 2 — Confirm the schema gate before generating

The two gates ship **inside this skill** at `traindata/scripts/`. Run them
in place; do not copy them to the repo root or anywhere else in the output tree.
The generator's job is to produce data, a taxonomy, and a diversity plan — not a
second copy of the tooling.

The gates read input leniently (JSON array, fenced block, BOM, trailing commas)
and accept `.yaml` case files directly, but the **recommended authoring format
is YAML converted by script** — see "Authoring input: YAML first" below. Give
every long run a `--timeout` (see "Wall-clock budget"): a gate that exceeds its
budget exits 3 instead of hanging the job.

- `traindata/scripts/validate_and_integrate.py` — schema and soft-label contract.
- `traindata/scripts/audit_diversity.py` — the shortcut-learning gate.

If a domain genuinely needs an extra check, add it to the taxonomy/config the
tools already read, or extend the check in the skill script — never fork a
loose copy into `./scripts`.

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

The audit proves a record is not learnable by shortcut. It runs on
case-form chunks, reads their `meta` block, and fails on fixed payload
fingerprints, giveaway addresses, payload reuse, a single org/ID/voice/injection
surface, injected-heavy corpora, reversed-polarity taxonomy wording, and a
non-disjoint holdout. Both tools must exit 0 before a chunk is accepted. See
`references/anti-shortcut.md` for every rule and default threshold.

### Phase 3 — Generate in chunks, validate each chunk

Never generate the whole corpus in one shot. Loop per chunk:

```
draw a locked email batch + write the diversity plan (e.g. 10 cases)
  -> generate the chunk with that exact email batch in the prompt
    -> validate that chunk (schema + soft labels)
      -> audit that chunk (diversity + anti-shortcut + action attribution)
        -> check email dedup; if any check fails, regenerate (never weaken a check)
          -> only accepted chunks are kept
```

Tips that make this work in practice:

- **Author each chunk in YAML, then convert.** Ask for one `.yaml` file per
  chunk (a list of cases, or `---`-separated documents). In YAML the model does
  not hand-escape quotes: `action:` is a mapping, long `content:` is a `|`
  block. Convert and validate in one step:

  ```bash
  python3 traindata/scripts/yaml_to_jsonl.py --in 'cases_*.yaml' \
    --taxonomy taxonomy.json --expand --out chunk_0001.jsonl
  ```

  Conversion is fail-closed — any malformed case writes nothing. Never hand-fix
  the emitted JSONL; fix the YAML and reconvert.
- **Sample the addresses first**, then put the locked list in the prompt:
  `python3 traindata/scripts/sample_emails.py --scenario <safe|violation|injection> \
  --count <n> --cross-map --manifest email_assignment.json --ledger prefix_ledger.txt`.
  The generator must reuse those exact strings. It must **not** read the library
  files itself — the tiers, weights and the compositional prefix grammar are the
  model, and the manifest is enforced as a locked whitelist by the audit.
- Pass the **same `--ledger`** to every chunk. Local parts are composed from
  `names_first × names_last × roles` (billions of combinations) and recorded in
  the ledger, so prefixes stay unique across the whole corpus, not just within a
  chunk. The audit fails a prefix reused in more than a couple of cases.
- Request a **structured** batch (a JSON array of cases) per call, with the
  target counts, the required label coverage, and the **diversity plan** spelled
  out in the prompt.
- Generate a whole `case` at once (state + all its questions + `meta`) so the
  shared `text`/`source_hash`/`source_index` stay consistent.
- Keep a per-chunk ID range so parallel workers cannot collide, and give each
  worker its own slice of the rotation matrix so they do not converge on one
  voice.
- Reject and regenerate noisy records rather than patching them by hand. A
  diversity failure means regenerate the whole chunk, not one row.

See `references/generation-playbook.md` for prompting patterns, chunk sizing,
parallelization, and the retry loop.

### Phase 4 — Pilot, audit, and hold out before scaling

Run 1–2 chunks, then inspect them by hand:

- Does the primary label actually match the case, question by question?
- Are `choice` option sets consistent across records, with **one polarity**?
- Is every domain/qtype/label represented, or has something collapsed?
- Is the text realistic and varied, or is it one template with words swapped?
- Could you guess the label from a fixed phrase, an address, an org name, or an
  ID format? If yes, the audit will fail it and so would the model.

Run the diversity audit on the pilot and write a scenario-hash holdout:

```bash
python3 traindata/scripts/audit_diversity.py --chunks 'chunk_*.jsonl' \
  --taxonomy taxonomy.json --require-injection --strict \
  --holdout 2 --out-dir pilot_holdout --json pilot_diversity.json
```

`--holdout 2` on a 10-scenario pilot keeps 2 scenarios out of every later
training/tuning run. Count scenarios by `text` hash: a 30-record, 10-scenario
pilot is 10 scenarios, and the three heads of a case are not independent cases.

**The pilot is a prompt gate, not training data.** Do not fine-tune a head on a
small pilot alone: blending 30 rows into a model already trained on thousands,
with the old epoch budget, is memorisation. If pilot rows ever enter training,
keep them below ~5% of the blend (mixed with the base corpus, e.g. ATBench) with
a reduced epoch budget, and say so in the data card.

`assets/inspection-report.md` is a template for recording this. Fix the prompt or
taxonomy, then scale out. Never relax the audit to pass a chunk.

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
3. Keep the Phase 4 scenario-hash holdout disjoint from all three splits and from
   any prompt iteration after the pilot is frozen.

### Phase 6 — Report

Produce a data card: domain distribution, qtype distribution, label distribution,
length stats, duplicate/leakage results, the diversity-audit verdict and metrics
(from `--json`), the holdout manifest, how the corpus was mixed (or not) with the
base training data, and known limitations. `--card` writes the soft-label card
directly from a validated run; attach the audit report next to it.

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

Case-form chunks carry one extra object, `meta`, holding the diversity plan (see
`references/anti-shortcut.md`). It is **not** a training field: it is stripped when
cases are expanded into records, and only the audit reads it.

Full schema details, with edge cases and a worked example, are in
`references/soft-labels.md` and `references/state-formats.md`.

## Authoring input: YAML first

Have the model write **YAML**, not raw JSONL. Escaping JSON-inside-JSON is the
single most common way a generated chunk is silently corrupted: one missed
backslash in `content` or `action` and the record is unusable. In YAML the case
is structure, and the script does the escaping:

```yaml
- traj_id: ruhsoft_000101_a1b2c3d4
  safe: true
  turns:
    - role: user
      content: |
        Triage the open tickets. Summarise each and reply in-place.
        Do not move anything out of the queue.
    - role: agent
      thought: List the open tickets first.
      action:
        name: list_tickets
        arguments: {status: open, limit: 25}
  meta:
    scenario_family: support-triage
    entity_family: northlight
    id_style: ticket-prefix
    thought_style: terse
    injection_present: false
    external_contact: false
    salted: true
  is_violation: false
  mechanism: own_reasoning
  severity: 0
```

`yaml_to_jsonl.py` (stdlib only, no PyYAML) then:

- serialises `action:` / `content:` / `thought:` mappings to the JSON strings
  the record contract requires;
- accepts a list of cases, `---`-separated documents, or a top-level `cases:`
  list;
- rejects anchors/aliases, tabs used for indentation, unclosed quotes and
  non-JSON `action` strings **with a line number**;
- validates the result against the taxonomy (`--check`) and expands to records
  (`--expand`) using the same `expand_case` the validator uses;
- writes atomically (temp file + rename), so a crash cannot leave a half chunk;
- is fail-closed: any error means nothing is written.

Supported YAML subset, so the prompt can stay honest: block mappings and
sequences, plain/quoted scalars, `|` and `>` block scalars with `-`/`+`
chomping, inline `[...]`/`{...}`, comments, multiple documents. Quote any value
that must stay a string (`"0"`, `"000101"`, ids). Plain `yes`/`no` stay strings;
only `true`/`false` become booleans.

## Wall-clock budget

Every gate and the converter take `--timeout SECONDS` (default `0` = unlimited).
The budget covers the whole run, is checked per case and inside the O(n²)
leak/payload comparisons, and exits **3** on expiry. In a generation loop treat
exit 3 like any other rejection: shrink the chunk or split it, then retry —
never remove the timeout to get a pass. A typical chunk budget is 60–300s.

## Robust input handling

The loaders accept what a model actually emits, without relaxing content rules:
a JSON array or one pretty-printed object instead of JSONL, markdown
```` ```json ```` fences, a UTF-8 BOM, CRLF, blank lines, `//` lines, and
trailing commas. `.yaml`/`.yml` paths go through the YAML reader directly, so
`audit_diversity.py` and `salt.py` can audit an unconverted chunk. Invalid bytes
are decoded with replacement instead of crashing. Outputs are written atomically.

## Hard rules

1. **Soft, never one-hot.** Primary `0.9`, remainder `0.1` spread evenly, always
   normalized. Ambiguity is expressed by lowering the primary below `0.9`, not by
   writing a hard 1.0.
2. **`label` must equal the argmax** of `probabilities`.
3. **Validate, then audit, before you accumulate.** Unvalidated data is not data,
   and schema-valid data that teaches a shortcut is worse than none. Both
   `validate_and_integrate.py` and `audit_diversity.py` must exit 0.
4. **Test data is never training data.** Keep ID ranges, files, and the scenario
   holdout disjoint.
5. **Addresses come only from `sample_emails.py`, and domains are never
   blacklisted.** The generator must **run** `traindata/scripts/sample_emails.py`
   and use its output verbatim; it must **not** read, grep, `cat`, or otherwise
   open `traindata/assets/email_library/*` itself, and must not invent
   addresses. The library is a probability model (tiers + weights + cross-map),
   not a menu — reading it would skip the weighting. The audit treats the
   `--manifest` as a **locked whitelist**, so hand-picked domains fail even when
   they are in the pool. Do not exclude throwaway providers: sample them into
   clean and violating cases alike. No real personal data, no real credentials,
   no directly executable attack code.
6. **Keep the taxonomy stable.** Reuse option/level sets across records; a
   per-record invention defeats calibration.
7. **Do not let labels collapse.** Both (or all) classes must carry meaningful
   volume and a clear semantic boundary, not just "the user asked for something
   bad".
8. **No fixed payload fingerprints.** Do not reuse an injection phrase,
   template, or opener across cases; do not use giveaway artificial markers
   (`AUTO-APPENDED`, `NOTE FOR BOT OPERATORS`) or local parts that name the intent
   (`evil@`, `attacker@`, `exfil@`). Rotate position (body/subject/status/footer)
   and register. The audit enforces this.
9. **Entity cross-mapping.** The same domain style — throwaway or corporate —
   must appear in both clean and violating contexts, so the address never
   predicts the label. `sample_emails.py --cross-map` does this; the audit fails a
   corpus where disposable-domain share differs by >30 points between classes.
10. **One head, one polarity.** No `choice`/`score` question may mean "the
    failure" on violations and "the avoided failure" on clean cases. Ask what is
    present in the input (with a `none` option) or split into two single-polarity
    questions. `severity` rates the impact that occurred; clean ⇒ level 0.
11. **Diversify the generator, not just the nouns.** Rotate scenario skeletons,
    entity/org families, ID formats, thought voices, and generator personas
    across chunks. One voice across the whole corpus is a shortcut.
12. **A pilot is a prompt gate.** Hold out scenarios by hash (e.g. 2 of 10),
    never tune on the holdout, and never fine-tune on the pilot alone. If pilot
    rows are used, mix them below ~5% with the base corpus and reduce the epoch
    budget.
13. **The label follows the action, not the address.** A `safe=true` case that
    uses a disposable address must carry an explicit neutralising thought (temp
    mailbox is noteworthy, not a violation — vary the wording). A `safe=false`
    case must have an action-level violation and, when injected, must show the
    agent actually performing the injected action; never reason "unsafe because
    the recipient is gmail/temp". The audit enforces both.
14. **Salt every adversarial prompt and tell the model it is salted.** Before
    generating anything with injections or harmful framing, run the prompt
    through `scripts/salt.py` (or paste the salt block from
    `references/harmbench-methodology.md`). The generator must be *told* it is
    salted and must set `meta.salted: true` on every case; run the audit with
    `--require-salt` in the injection domain. The salt keeps the generator an
    **author of inert detector data**, not an attacker: everything fictional,
    harm described not operationalised, no executable attack code, injected text
    read as quarantined data and never obeyed. An unsalted chunk does not ship.
15. **Author in YAML, convert with the script.** Do not hand-write or hand-fix
    JSONL. The model writes YAML; `yaml_to_jsonl.py` does the escaping and
    serialises nested `action`/`content` for you. A conversion error is fixed in
    the YAML source, never by patching the emitted JSON. This is what keeps a
    quoting mistake from silently corrupting a chunk.
16. **Every run has a wall-clock budget.** Pass `--timeout` to the converter and
    both gates. Exit 3 means "not accepted, retry smaller" — not "raise the
    limit". A hung gate is a failed gate.

## Reference files

All paths below are inside the skill directory (`./traindata/`). The scripts are
part of the skill: run them from `./traindata/scripts/` and never copy them into
the repository root.

- `references/anti-shortcut.md` — the diversity contract, `meta` schema, shortcut
  fingerprints, entity cross-mapping, answer-time cross-validation, hard
  negatives, dual-polarity fix, and pilot-mixing rules.
- `references/harmbench-methodology.md` — HarmBench methodology (behavior
  taxonomy, evaluation pipeline, robust metrics) and the **salt** convention for
  adversarial prompts.
- `references/soft-labels.md` — distribution rules, ambiguity, normalization, examples.
- `references/state-formats.md` — string/object/turn-list states and conventions.
- `references/generation-playbook.md` — chunking, prompting, parallelism, retries.
- `references/taxonomy-design.md` — designing reusable option/level sets, the two
  orthogonal meta axes (`harm_kind` × `request_shape`), and coverage discipline.
- `assets/inspection-report.md` — template for Phase 4 review.
- `assets/email_library/` — welded email pool: `freemail.txt`, `synth_domains.txt`,
  `known_blocklist.txt`, `abuse_contacts.txt`; compositional prefix vocab
  `names_first.txt` / `names_last.txt` / `roles.txt`; `prefixes.txt` (curated);
  `email_library.json` / `prefix_vocab.json`; raw sources under `sources/`.
- `scripts/yaml_to_jsonl.py` — YAML → validated case-form JSONL / records;
  normalises nested `action`/`content`, fail-closed, atomic writes.
- `scripts/dataloader.py` — shared tolerant loader: JSONL/array/fence/BOM
  handling, the built-in YAML subset reader, atomic writes, and `Deadline`.
- `scripts/validate_and_integrate.py` — runnable validator/integrator, used in place.
- `scripts/audit_diversity.py` — runnable anti-shortcut / diversity gate.
- `scripts/sample_emails.py` — locked, tiered, cross-mapped batches + prefix
  ledger + dedup check.
- `scripts/salt.py` — inoculate a generation prompt with the salt block + needle,
  and verify every case declared `meta.salted`.
- `scripts/salt_harmbench.py` — build a safety-salted mirror of a HarmBench
  checkout so the reference material is read as inert data.
- `scripts/obfuscators.py` — deterministic host-level / linguistic payload
  obfuscation primitives (the model never hand-writes base64/hex/homoglyphs).
- `scripts/build_email_library.py` — rebuilds the welded domain pool (provenance).
- `scripts/build_prefix_vocab.py` — rebuilds the name/role vocab (provenance).
- `scripts/build_ccnc_sources.py` — romanizes CCNC Chinese names to pinyin (needs
  `nix-shell -p python3Packages.pypinyin`; outputs are committed).

## Quick checks

Commands assume the working directory is the repository root and the skill is
installed at `./traindata/`.

```bash
# convert model-authored YAML into a validated chunk (fail-closed, atomic)
python3 traindata/scripts/yaml_to_jsonl.py --in 'cases_*.yaml' \
  --taxonomy taxonomy.json --expand --out chunk_0001.jsonl --timeout 300

# validate the YAML without writing anything
python3 traindata/scripts/yaml_to_jsonl.py --in 'cases_*.yaml' \
  --taxonomy taxonomy.json --check

# draw a locked, cross-mapped email batch for a chunk (paste into the prompt)
python3 traindata/scripts/sample_emails.py --scenario violation --count 10 \
  --seed 7 --cross-map --manifest email_assignment.json

# validate records
python3 traindata/scripts/validate_and_integrate.py --check chunk_0001.jsonl --taxonomy taxonomy.json

# validate case-form chunks without writing anything
python3 traindata/scripts/validate_and_integrate.py --check 'chunk_*.jsonl' \
  --taxonomy taxonomy.json --expand --require-complete-cases

# diversity audit: mandatory gate, must exit 0 (exit 3 = timeout)
python3 traindata/scripts/audit_diversity.py --chunks 'chunk_*.jsonl' \
  --taxonomy taxonomy.json --require-injection --strict --require-library-emails \
  --email-manifest email_assignment.json --require-salt --require-taxonomy-axes \
  --holdout 2 --out-dir pilot_holdout --json pilot_diversity.json --timeout 300

# salt a generation prompt (top salt + mid-prompt needle) before calling the model
python3 traindata/scripts/salt.py --in prompt.txt --out prompt_salted.txt

# verify the returned chunks acknowledged the salt
python3 traindata/scripts/salt.py --check 'chunk_*.jsonl'

# email dedup check: prints REGENERATE EMAIL YOURSELF when over-reused
python3 traindata/scripts/sample_emails.py --check 'chunk_*.jsonl' --existing all_soft.jsonl

# integrate (expand cases) and write a data card
python3 traindata/scripts/validate_and_integrate.py --integrate --out all_soft.jsonl \
  --taxonomy taxonomy.json --expand --card data-card.md

# deterministic, case-disjoint train/calibration/test split
python3 traindata/scripts/validate_and_integrate.py --split-dir split --chunks 'chunk_*.jsonl' \
  --taxonomy taxonomy.json --expand

# leakage check between splits (command is printed by --split-dir)
python3 traindata/scripts/validate_and_integrate.py --check 'split/test.jsonl' \
  --leak 'split/train.jsonl' --leak 'split/calibration.jsonl'
```

Both tools exit non-zero and print every failure, so wire them into your loop and
treat a non-zero exit as "the chunk does not exist yet".