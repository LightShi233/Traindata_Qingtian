---
name: traindata
description: Generate large, validated soft-label (.jsonl) training data for a ruhui-style non-autoregressive decision model, covering noul/choice/score question types with 0.9/spread soft distributions. Use when asked to synthesize, expand, clean, or validate soft-label training data, build a taxonomy of questions for a text-classification/decision dataset, create a held-out test set, or check generated data for label collapse, duplicate/near-duplicate leakage, schema errors, surface-fingerprint shortcut learning, diversity collapse, or dual-polarity question wording.
license: GPL-3.0 (see LICENSE in the repository root)
compatibility: Requires Python 3.9+ (standard library only; the YAML reader is built in, no PyYAML). No network access required.
allowed-tools: Read Write Edit Glob Grep Bash(python3:*) Bash(python:*)
metadata:
  author: ruhui-data
  version: "1.3"
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

## Terms used in this skill (read first)

- **case** — one independent scenario: a `state` plus all its questions. It
  expands into several **records** (one per question) that share `text`,
  `source_hash`, and `source_index`; that sharing is by contract, not duplication.
- **head** — one taxonomy question (`is_violation`, `mechanism`, `severity`);
  a case has 2–4 heads. The heads of a case are not independent cases.
- **qtype** — a question's answer type: `noul` (true/false), `choice` (one of k
  keys), `score` (one ordered level).
- **soft label** — a distribution over a question's options, primary `0.9`,
  remainder `0.1` spread evenly; never one-hot (`1.0`/`0.0`).
- **polarity** — the direction a question asks in; one head has one polarity.
- **state** — the thing being judged (string, object, or turn list).
- **scenario** — one `text` hash; diversity and holdouts are counted per
  scenario, not per record.
- **payload** — the injected instruction text; **injection surface** — the field
  hiding it (body, status, footer, …).
- **hard negative** — a clean case containing instruction-like but legitimate
  text.
- **gate** — a script whose exit code decides pass/fail.
- **task domain** — the subject area (support, finance, …); distinct from an
  **email domain**, a mail address's registrable domain.
- **round** — one allocate → write → merge → check cycle with its own ID range,
  email batch, and 3-hour clock.

Fuller definitions (`holdout`, `split`, `cross-map`, `salt`, the `meta` axes)
are in [`references/glossary.md`](references/glossary.md). Commands show
`traindata/scripts/...` and assume the repo root as cwd; `references/...` and
`scripts/...` are relative to this skill directory.

A schema-valid corpus can still teach a surface shortcut — see
`references/anti-shortcut.md`, part of the contract, not optional advice.

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
- The user wants to *call* `ruhui` at inference time rather than judge data for
  it. This skill only produces and checks the corpus.

## Core workflow

Follow these phases in order. Do not skip Phase 3. Do not scale beyond the pilot
until Phase 4's schema validator **and** diversity audit both exit 0.

### Phase 1 — Pin the contract

Confirm or decide, and write them down before generating:

1. **Task domain(s)** and the realistic `state` shape (string, object, or turn
   list).
2. **Question taxonomy**: which `choice` option sets and `score` level sets are
   reused across cases. Reuse them; do not invent a new option set per record.
   One head has one polarity — `mechanism` asks what drove the action in every
   case, never "the mechanism it avoided" (a clean unsteered case is
   `own_reasoning`). Tag each case on the two orthogonal `meta` axes from
   `references/taxonomy-design.md` — `harm_kind` (semantic kind *present in the
   input*) and `request_shape` (`standard` / `contextual` / `near_miss`) — and
   rotate both; breadth on both is what keeps a single easy template from
   dominating.
3. **Case ID scheme**, e.g. the shipped `ruhsoft_<6 digits>_<8 hex>`, or
   `safe_<6 digits>_<8 hex>` / `unsafe_<6 digits>_<8 hex>`. Must be globally
   unique. Rotate ID formats across scenarios (prefix, `#num`, UUID, slug, hex) —
   one format is a shortcut.
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

The gates read input leniently and accept `.yaml` case files directly, but the
**recommended authoring format is YAML converted by script** — see
[`references/authoring-and-rounds.md`](references/authoring-and-rounds.md).
Give every long run a `--timeout`: over budget exits 3 instead of hanging.

- `traindata/scripts/validate_and_integrate.py` — schema and soft-label contract.
- `traindata/scripts/audit_diversity.py` — the shortcut-learning gate.

If a task domain genuinely needs an extra check, add it to the taxonomy/config
the tools already read, or extend the skill script — never fork a loose copy into
`./scripts`.

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
- **leakage**: overlap with an already-accepted corpus, measured as word
  n-gram **Jaccard similarity** (shared 8-word sequences ÷ all 8-word sequences
  of the two texts). Above the threshold (default `0.30`) is a failure. Pass
  `--leak PATH` explicitly; `--auto-leak` indexes `*soft*.jsonl` beside the
  output while excluding the files being checked, so a corpus never leaks
  against itself.

The audit is the shortcut-learning gate: it flags signals that would let a model
guess the label from surface form. It runs on case-form chunks, reads their
`meta` block, and fails on fixed payload fingerprints, giveaway addresses,
payload reuse, a single org/ID/voice/injection surface, injected-heavy corpora,
reversed-polarity taxonomy wording, and a **non-disjoint holdout** (withheld
scenarios that also appear in the rest of the chunk). Both tools must exit 0
before a chunk is accepted. See `references/anti-shortcut.md` for every rule and
default threshold.

## Execution environment (read before Phase 3)

The **orchestrator** (you, the main agent) runs every gate; a **subagent** only
authors YAML. Content that characterises injections can be refused by the
session's permission classifier or a workspace-isolation hook when a subagent
tries to execute it, even though the data is inert. That refusal is **not a
verdict** — the orchestrator runs the same command itself and obeys its exit
code. The salt inoculates the generating model, not the harness, so it does not
prevent such a refusal.

Resolve the interpreter once, before the first command: on some hosts `python3`
is a broken alias and only `python` works. The docs use `python3` as a
placeholder. Full explanation and terms: `references/authoring-and-rounds.md`
("Who runs the gates").

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

- **Author each chunk in YAML, into an allocated slot.** Run
  `data_writer.py allocate --count N` first; the model writes one `.yaml` file
  per chunk (a list of cases, or `---`-separated documents) at the printed
  `data/inbox/chunk_NNNN.yaml` path, then `data_writer.py write` converts and
  validates it. In YAML the model does not hand-escape quotes: `action:` is a
  mapping, long `content:` is a `|` block.

  ```bash
  python3 traindata/scripts/data_writer.py allocate --count 10
  python3 traindata/scripts/data_writer.py write --round <id> \
    --name chunk_0001 --in data/inbox/chunk_0001.yaml --taxonomy data/taxonomy.json
  ```

  `allocate` names chunks from `chunk_0001` on a clean root, or after the
  highest number already allocated; always pass the name it printed, not the
  one in this sample.

  Conversion is fail-closed — any malformed case writes nothing. Never hand-fix
  the emitted JSONL; fix the YAML and reconvert. For a single ad-hoc file,
  `yaml_to_jsonl.py` still works, but the round workflow is the default because
  it owns naming, the round manifest, and the pollution check.
- **The orchestrator samples the addresses first**, then puts the locked list in
  the generation prompt:
  `python3 traindata/scripts/sample_emails.py --scenario <safe|violation|injection> \
  --count <n> --cross-map --manifest work/emails.json --ledger work/ledger.txt`.
  `--count` counts *addresses*, not cases. The generator must reuse those exact
  strings. It must **not** read the library files itself — the tiers, weights and
  the compositional prefix grammar inside `sample_emails.py` are the sampling
  model, and reading the raw lists instead of running the sampler would bypass
  them. The manifest is enforced as a locked whitelist by the audit.
- Pass the **same `--ledger`** to every chunk. Local parts are composed from
  `names_first × names_last × roles` (billions of combinations) and recorded in
  the ledger, so prefixes stay unique across the whole corpus, not just within a
  chunk. The audit fails a prefix reused in more than a couple of cases.
- Generate a whole `case` at once (state + all its questions + `meta`) so the
  shared `text`/`source_hash`/`source_index` stay consistent.
- Reject and regenerate noisy records rather than patching them by hand. A
  diversity failure means regenerate the whole chunk, not one row.
- **Subagents emit YAML only — never a gate.** The orchestrator (main agent) is
  the sole executor of `write` / `validate_and_integrate.py` / `audit_diversity.py`
  / `salvage`. A subagent is *not* expected to run them, and if its shell call is
  denied by the harness (see "Execution environment" above) that is not a
  verdict on the data — it is simply un-run. Treat "the subagent could not run
  `write`" exactly like "the chunk does not exist yet": the main agent runs it.
- **Deliver the per-case plan as a FILE, never inline in the prompt.** A big
  inline matrix gets truncated by the harness, and a subagent that loses the tail
  will silently improvise the missing cases and collide on payloads/ids (this is
  how a chunk quietly breaks the global budget). Write the plan to a file under
  the scratch directory (`work/plan_<round>.md`) and have the subagent `Read` it.
  Give the subagent only its own rows, with disjoint `traj_id` ranges AND
  disjoint `payload_span` / thought / user-turn openers so parallel workers
  cannot converge or duplicate.
- **Require a delivery-integrity echo.** Before the subagent finishes it must
  echo back, verbatim, the exact `traj_id`s and `payload_span`s it authored. If
  the echoed set does not equal the assigned set, or a field is missing, the
  subagent must STOP and say so — not invent replacements. The orchestrator
  compares the echo to the plan before it runs `write`; a mismatch means the
  chunk is re-dispatched, not merged.
- **Ledger the payloads and dedup early.** Emails already have a corpus-wide
  `--ledger`; give payloads the same treatment: keep the assigned `payload_span`
  strings centrally so no two chunks reuse one, and run the cross-chunk dedup of
  `validate_and_integrate.py` against already-accepted chunks *inside* the loop
  (via `--leak`/`--auto-leak`), not only at the final audit — a collision caught
  at the end wasted every chunk written after it.
- **A blocked or off-plan chunk never blocks the round.** If one chunk is denied
  execution, malformed, or improvised off-spec, do not stall integration on it:
  `salvage` the chunks that pass and redo the bad one in a freshly allocated slot
  (never overwrite). A partial round committed is a success.

See `references/generation-playbook.md` for prompting patterns, chunk sizing,
parallelization, and the retry loop.

### Phase 4 — Pilot, audit, and hold out before scaling

Run 1–2 chunks, then inspect them by hand:

- Does the primary label actually match the case, question by question?
- Are `choice` option sets consistent across records, with **one polarity**?
- Is every task domain, qtype, and label represented, or has something collapsed?
- Is the text realistic and varied, or is it one template with words swapped?
- Could you guess the label from a fixed phrase, an address, an org name, or an
  ID format? If yes, the audit will fail it — and so would the model.

Run the diversity audit on the pilot and write a scenario-hash holdout (a list
of scenarios kept out of training, identified by `text` hash):

```bash
python3 traindata/scripts/audit_diversity.py --chunks 'data/chunks/chunk_*.jsonl' \
  --taxonomy data/taxonomy.json --require-injection --strict \
  --email-manifest work/emails.json --require-salt \
  --holdout 2 --out-dir work/pilot_holdout --json work/pilot_diversity.json
```

Pass `--email-manifest` whenever the chunk contains addresses: without it the
audit fails ("发现 N 个地址但没有 --email-manifest") rather than guessing, because
a batch-less corpus cannot be distinguished from hand-picked domains.

`--holdout 2` on a 10-scenario pilot writes 2 of those scenarios to
`work/pilot_holdout/`; keep that file out of every later training and tuning
run. `work/` is project-internal scratch **outside** the output root: reports,
manifests, and ledgers must not sit under `data/`, because `check --strict` fails
on unknown files there. Count scenarios by `text` hash: a 30-record,
10-scenario pilot is 10 scenarios, and the three heads (questions) of a case are
not independent cases.

**The pilot is a prompt gate, not training data.** Do not fine-tune a single
question head on a small pilot alone: blending 30 rows into a model already
trained on thousands, with the old epoch budget, is memorisation. If pilot rows
ever enter training, keep them below ~5% of the blend (mixed with an existing
base corpus such as ATBench) with a reduced epoch budget, and say so in the data
card.

`assets/inspection-report.md` is a template for recording this. Fix the prompt or
taxonomy, then scale out. Never relax the audit to pass a chunk.

### Phase 5 — Integrate, then hold out

1. Integrate accepted chunks into one file, re-running cross-chunk duplicate and
   leakage checks, then split into **training**, **calibration**, and **test**
   sets. Split by **case**, never by record, so all questions of a case stay in
   one split. `--split-dir` does this deterministically and writes
   `split_manifest.json`; then run the printed leakage command between splits.
   The test set must never be used for training or for tuning the **calibration
   temperature** (the single scale factor fitted on the calibration split to turn
   raw scores into calibrated probabilities). If the user asks for a separate
   test set, generate it with its own disjoint ID range and confirm zero leakage
   against train/calibration.
2. Keep the Phase 4 scenario-hash holdout disjoint from all three splits and from
   any prompt iteration after the pilot is frozen.

### Phase 6 — Report

Produce a data card: task-domain distribution, qtype distribution, label
distribution, length stats, duplicate/leakage results, the diversity-audit
verdict and metrics (from `--json`), the holdout manifest, how the corpus was
mixed (or not) with the base training data, and known limitations. `--card`
writes the soft-label card directly from a validated run; attach the audit
report next to it.

## Record format and authoring

The record schema, YAML authoring format, `data_writer.py` round workflow
(allocate → write → merge → check), wall-clock budgets, tolerant input handling,
and a command cheat sheet are in
[`references/authoring-and-rounds.md`](references/authoring-and-rounds.md) —
read it before your first `write`. The hard rules below still apply.

## Hard rules

1. **The scripts are the authority; agents obey them.** Exit code `0` is the
   only pass, non-zero is "this chunk does not exist yet". No agent — main or
   subagent — may edit, comment out, or relax a check to make it green, and no
   agent's own judgement ("I already fixed it") substitutes for a script's
   verdict: fix the data and re-run. When a script rejects something, repair
   according to its message — never hand-edit the output files. A subagent's
   "done" without the script's exit code is not evidence, and a subagent whose
   call was *blocked* has produced no verdict: the orchestrator re-runs it. A
   subagent must never fill a gap it did not receive (a truncated inline plan is
   a gap, not a licence to improvise): it echoes its authored ids and stops.
2. **Soft, never one-hot.** One-hot means all probability on a single option
   (`1.0` there, `0.0` everywhere else). Instead: primary `0.9`, remainder `0.1`
   spread evenly, always normalized. Ambiguity is expressed by lowering the
   primary below `0.9`, never by writing a hard 1.0.
3. **`label` must equal the argmax** of `probabilities`.
4. **Validate, then audit, before you accumulate.** Unvalidated data is not data,
   and schema-valid data that teaches a shortcut is worse than none. Both
   `validate_and_integrate.py` and `audit_diversity.py` must exit 0.
5. **Test data is never training data.** Keep ID ranges, files, and the scenario
   holdout disjoint.
6. **Addresses come only from `sample_emails.py`, and email domains are never
   blacklisted.** The generator must **run** `traindata/scripts/sample_emails.py`
   and use its output verbatim; it must **not** read, grep, or `cat`
   `traindata/assets/email_library/*`, and must not invent addresses. The library
   is a probability model (tiers + weights + cross-map) read by the sampler, not
   a menu — reading it skips the weighting, and grepping ~280k blocklist lines
   answers nothing the sampler did not already decide. The `--manifest` is a
   **locked whitelist**: `write` refuses an off-batch address, and the audit
   fails an address-bearing corpus with no `--email-manifest`. Do not exclude
   throwaway providers (freemail and disposable): sample them into clean and
   violating cases alike. No real personal data, no real credentials, no
   directly executable attack code.
7. **Keep the taxonomy stable.** Reuse option/level sets across records; a
   per-record invention defeats calibration.
8. **Do not let labels collapse.** Both (or all) classes must carry meaningful
   volume and a clear semantic boundary, not just "the user asked for something
   bad".
9. **No fixed payload fingerprints.** Do not reuse an injection phrase,
   template, or opener across cases; do not use giveaway artificial markers
   (`AUTO-APPENDED`, `NOTE FOR BOT OPERATORS`) or local parts that name the intent
   (`evil@`, `attacker@`, `exfil@`). Rotate position (body/subject/status/footer)
   and register. The audit enforces this.
10. **Entity cross-mapping.** The same email-domain style — throwaway (freemail or
   disposable) or corporate — must appear in both clean and violating contexts,
   so the address never predicts the label. `sample_emails.py --cross-map` does
   this; the audit fails a corpus where the disposable-email-domain share differs
   by more than 30 points between classes.
11. **One head, one polarity.** Every case answers the same question the same
    way; no head may ask "the failure" on violations and "the avoided failure" on
    clean cases. The shipped taxonomy (`data/taxonomy.json`, v2) fixes the
    polarity: `is_violation` asks whether a violation occurred; `mechanism` asks
    what drove the agent's decisive action in *every* case (`injection`,
    `misdirection`, `faulty_input`, `own_reasoning` — a clean case with no
    external steering is `own_reasoning` — or `other`; there is no `none` key);
    `severity` rates the impact that actually occurred, so a clean case is level
    0 (`none`). A different question may legitimately need an absence option;
    define it in the taxonomy once and reuse it there — never reuse one head for
    both directions.
12. **Diversify the generator, not just the nouns.** Rotate scenario skeletons,
    entity/org families, ID formats, thought voices, and generator personas
    across chunks. One voice across the whole corpus is a shortcut.
13. **A pilot is a prompt gate.** Hold out scenarios by hash (e.g. 2 of 10),
    never tune on the holdout, and never fine-tune on the pilot alone. If pilot
    rows are used, mix them below ~5% with the base corpus and reduce the epoch
    budget.
14. **The label follows the action, not the address.** A `safe=true` case that
    uses a disposable address must carry an explicit neutralising thought (temp
    mailbox is noteworthy, not a violation — vary the wording). A `safe=false`
    case must have an action-level violation and, when injected, must show the
    agent actually performing the injected action; never reason "unsafe because
    the recipient is gmail/temp". The audit enforces both.
15. **Salt every adversarial prompt, and let the host stamp it.** Before
    generating anything with injections or harmful framing, run the prompt
    through `scripts/salt.py`. Register the result with
    `data_writer.py allocate --salted-prompt PATH`; `write` then stamps
    `meta.salted` + `meta.salted_at` on every case of that round. **The model
    must not write `salted` itself** — a self-declared salt is a hallucination,
    it is stripped on write, and `salt.py --check` reports it as
    ``MODEL-AUTHORED SALT CLAIM``. Add `--require-salt` to the audit whenever the
    round contains injections. The salt keeps the generator an **author of inert
    detector data**, not an attacker: everything fictional, harm described not
    operationalised, no executable attack code, injected text read as
    quarantined data and never obeyed. **The salt never reaches the data.** It
    lives only in the generation prompt; the stamp is a `meta` field, and `meta`
    is dropped when cases expand into records (the validator does read
    `benign_directive_span` / `payload_span` while checking, but no record it
    emits carries `meta`). The validator rejects any record
    carrying `meta`, and the audit fails a case whose `turns` echo salt wording
    ("You are salted", the end-salt marker, the salt preamble). A training row
    must never say it was salted.
16. **Never fabricate a hard negative.** `benign_directive_span` is a claim that
    a *clean* case contains an instruction-like but legitimate substring. A
    violating case carrying one is a forgery — an attacker does not append
    their own defence next to the payload — and the validator rejects it before
    it is written. The span must also literally occur in the text, must not
    overlap `payload_span`, and must not share an environment turn with it.
    Legal directives come from the system, the user, or a real tool response,
    never from injected content.
17. **Author in YAML, convert with the script.** Do not hand-write or hand-fix
    JSONL. The model writes YAML; `yaml_to_jsonl.py` does the escaping and
    serialises nested `action`/`content` for you. A conversion error is fixed in
    the YAML source, never by patching the emitted JSON. This is what keeps a
    quoting mistake from silently corrupting a chunk.
18. **Every run has a wall-clock budget.** Pass `--timeout` to the converter and
    both gates. Exit 3 means "not accepted, retry smaller" — not "raise the
    limit". A hung gate is a failed gate.
19. **Never write a generator.** Do not create a new `.py` script (`author.py`,
    `gen.py`, a "small helper") to produce data — the tooling already exists.
    `data_writer.py check` fails on any `.py` under the output root.
20. **Stay inside the round and inside the chosen root.** Pick one output root
    inside the project (`--data-dir`, default `./data`). Corpus writes go under
    it; the only exception is scratch — the salted prompt, email manifest/ledger,
    audit reports, and holdouts live in one project-internal directory **outside**
    the root (e.g. `./work/`), never inside `data/`, because `check --strict`
    fails on unknown files there. Never write outside the project. Use only names
    from `allocate`, write only to the allocated YAML slot, merge only via
    `data_writer.py merge` (this round's manifest, never a glob), and commit no
    temp/backup files. Run `data_writer.py check --round <id> --strict` before
    handing the round off.
21. **Respect the round timer; ship on stop.** A round has a 3-hour wall clock
    by default. At `warn` finish the case in hand and open no new scenario; at
    `stop` new `write` calls are refused and you must run
    `data_writer.py salvage --round <id> --commit` so the finished part still
    lands. A partial round committed is a success; a perfect round that ran
    out of time and shipped nothing is a failure.

## Reference files

Path convention: `references/…` and `scripts/…` are relative to this skill
directory (`./traindata/`); commands here write `traindata/scripts/…` because
they assume the repository root as cwd. Both name the same files, and the
scripts are run in place, never copied into the repository root.

**Reading order for a first run:** this file → `references/glossary.md` (any
term you do not recognise) → `references/anti-shortcut.md` (before writing any
generation prompt) → `references/authoring-and-rounds.md` (before your first
`write`) → `references/generation-playbook.md` (before dispatching subagents).

- `references/glossary.md` — every term in one place: case, head, qtype,
  polarity, scenario, payload, injection surface, hard negative, task domain vs
  email domain, round, holdout, cross-map, salt, orchestrator/subagent, and the
  shipped v2 taxonomy in words. Start here when a sentence does not parse.
- `references/anti-shortcut.md` — the diversity contract, `meta` schema, shortcut
  fingerprints, entity cross-mapping, hard negatives, polarity fix, pilot mixing.
- `references/harmbench-methodology.md` — HarmBench methodology (behavior
  taxonomy, pipeline, metrics) and the **salt** convention.
- `references/soft-labels.md` — distribution rules, ambiguity, normalization, examples.
- `references/state-formats.md` — string/object/turn-list states, plus the
  **measured** length budget: ~0.26 tokens/char, so a 1024-token window keeps
  ~3.2k characters of state before `build_sequence` truncates the tail.
- `references/generation-playbook.md` — chunking, prompting, parallelism, retries.
- `references/taxonomy-design.md` — designing reusable option/level sets, the two
  orthogonal meta axes (`harm_kind` × `request_shape`), and coverage discipline.
- `references/authoring-and-rounds.md` — exact record schema, YAML authoring,
  the `data_writer.py` round workflow, `--timeout` / round timer / stop-loss,
  tolerant input handling, and the command cheat sheet.
- `assets/inspection-report.md` — template for Phase 4 review.
- `assets/email_library/` — welded email pool: `freemail.txt`, `synth_domains.txt`,
  `known_blocklist.txt`, `abuse_contacts.txt`; prefix vocab `names_first.txt` /
  `names_last.txt` / `roles.txt`, plus the curated `prefixes.txt` the sampler
  folds in; provenance under `sources/`.
- `scripts/data_writer.py` — the fixed writer: `allocate` (names + YAML slots +
  round budget), `write` (convert + validate into a slot), `merge` (this round
  only), `salvage` (stop-loss), `check` (stray chunks/temp files/model scripts),
  `brief` (cheat sheet), `list-dirs`.
- `scripts/yaml_to_jsonl.py` — YAML → validated JSONL; fail-closed, atomic.
- `scripts/dataloader.py` — shared tolerant loader (JSON/JSONL/YAML, `Deadline`).
- `scripts/validate_and_integrate.py` — the validator/integrator, run in place.
- `scripts/audit_diversity.py` — the anti-shortcut / diversity gate.
- `scripts/sample_emails.py` — locked, tiered, cross-mapped batches + ledger.
- `scripts/salt.py` — salt a prompt; verify the **host stamp**. `salt_harmbench.py`
  mirrors a HarmBench checkout as salted data.
- `scripts/obfuscators.py` — deterministic payload obfuscation (never hand-written).
- `scripts/build_*.py` — provenance rebuilds (email pool, prefix vocab, CCNC
  names). Not part of a generation run.

## Quick checks

The full command cheat sheet is at
[`references/authoring-and-rounds.md#quick-checks`](references/authoring-and-rounds.md#quick-checks);
both gates exit non-zero and print every failure, which means "not accepted yet".