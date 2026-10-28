# Authoring, record schema, and the round workflow

Stable procedure extracted from `SKILL.md` so the main body stays short. Read this
when you are about to write a chunk, convert YAML, or run the round commands.
Every term used here (case, head, qtype, soft label, state, round, `meta`) is
defined in `references/glossary.md`. Two record fields are bookkeeping, not
authoring choices: `source_dataset` defaults to `"synthetic"` if the case does
not set it, and `source_index` is assigned per case during expansion.

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
`references/anti-shortcut.md`). It is **not** a training field: it is dropped
when cases are expanded into records. The audit reads all of it; the validator
reads `benign_directive_span` and `payload_span` while checking, but nothing it
emits carries `meta`.

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
    harm_kind: none
    request_shape: standard
  is_violation: false
  mechanism: own_reasoning
  severity: 0
```

`meta` must carry `harm_kind` and `request_shape` when the audit runs with
`--require-taxonomy-axes`; the labels (`is_violation`, `mechanism`, `severity`)
are the question keys, not part of `meta`.

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

**Keep an email address mid-line inside a `|` block.** When a block scalar is
JSON-escaped, a real newline becomes the two characters `\n`; the address regex
sees `\nmeeme@…` and binds the `n` onto the local part (`nmeeme@…`), which then
fails the locked-batch check as an off-batch address. Put a word before the
address on the same line, or use a quoted scalar for single-line content.

### Round workflow: allocate → write → merge → check

`scripts/data_writer.py` is the **only** writer. Do not invent a generator, do
not write JSONL by hand, do not choose your own filenames.

Start every run with `data_writer.py brief`: it prints the flow, the YAML field
set, and the most common rejection reasons, so an agent needs neither the
script sources nor a guess. Read `SKILL.md` and `references/*.md` for detail;
the implementation under `scripts/` is not the contract — do not read it to
work out what to generate, and never edit it.

```bash
# 0. freeze the address batch first — addresses come from here, nowhere else
#    --count is the number of ADDRESSES, not cases; work/ is outside the output root
python3 traindata/scripts/sample_emails.py --scenario violation --count 12 \
  --cross-map --manifest work/emails.json --ledger work/ledger.txt

# 1. reserve names for this round; register the batch so write() can verify
#    names start at chunk_0001 and are never guessed — read the id allocate prints.
#    If this round contains injections, salt the prompt FIRST and register it, or
#    write() stamps no salt and the audit's --require-salt fails the chunk.
python3 traindata/scripts/salt.py --in work/prompt.txt --out work/salted_prompt.txt
python3 traindata/scripts/data_writer.py allocate --count 10 \
  --email-manifest work/emails.json --salted-prompt work/salted_prompt.txt \
  [--worker sub-3]
#    -> round: r-2026...   chunk_0001  yaml=data/inbox/chunk_0001.yaml

# 2. write the model's YAML to that exact path, then convert in place
python3 traindata/scripts/data_writer.py write --round r-2026... \
  --name chunk_0001 --in data/inbox/chunk_0001.yaml --taxonomy data/taxonomy.json

# 3. merge ONLY this round's written chunks (never a glob over all chunks)
python3 traindata/scripts/data_writer.py merge --round r-2026... \
  --taxonomy data/taxonomy.json

# 4. fail the round if it left anything behind
python3 traindata/scripts/data_writer.py check --round r-2026... --strict
```

What the tool enforces:

- **One output root, chosen inside the project.** `--data-dir` defaults to
  `./data` but may name any directory *inside the project*; it cannot be the
  project root itself or anything outside it. Everything the round writes
  (inbox, chunks, rounds) stays under that root, so a corpus is always
  discoverable. Run `data_writer.py list-dirs` to see existing roots.
- **Names are allocated, not guessed.** `chunk_<4+ digits>`; `write` requires
  the YAML to be at the exact slot from `allocate` and refuses unallocated
  names.
- **No overwrites.** An existing `chunks/chunk_NNNN.jsonl` is never replaced;
  allocate a fresh name to redo a chunk.
- **Round-scoped merge.** `merge` reads the round manifest only, so another
  round's chunks or a hand-placed file can never slip in.
- **No pollution.** `check` fails on unallocated chunk files, temp/backup files
  (`.tmp-*`, `*.tmp`, `*.bak`, `*.swp`, `__pycache__`, …), and any `.py` under
  the output root — i.e. a model that tried to write its own `author.py`.
- **No unknown files in the output root.** Only `taxonomy.json`,
  `ruhui_soft_all.jsonl`, `ruhui_soft_card.md`, `chunks/`, `inbox/`, `rounds/`,
  and `split/` (with `train.jsonl` / `calibration.jsonl` / `test.jsonl` /
  `split_manifest.json`) are recognised. Anything else is a WARN, and
  `--strict` turns WARNs into failures.
- Round manifests live in `data/rounds/<round>.json`; sources in
  `data/inbox/`; accepted chunks in `data/chunks/`.

**Where scratch files go.** The salted prompt, the email manifest and prefix
ledger, audit JSON reports, and `--out-dir` holdouts are *not* corpus files, so
they must live **outside the output root** or `check --strict` will fail the
round. Keep them in one project-internal scratch directory, e.g. `./work/`, and
never commit it:

```bash
python3 traindata/scripts/sample_emails.py --scenario violation --count 12 \
  --cross-map --manifest work/emails.json --ledger work/ledger.txt
```

If `check` reports a stray file, **move it out of the output root** (or delete
it if it is a temp file); never commit it. The tool writes each output through a
`.tmp-*` file and an atomic rename, so a leftover `.tmp-*` means a crash —
remove it, don't commit it. There is no sanctioned place for scratch files
*inside* the output root.

## Who runs the gates

Two environment facts decide who may run the gates, and the whole workflow is
built around them.

Terms: the **orchestrator** is the main agent running the skill; a **subagent**
is a worker it dispatches; the **harness** is the host program (CLI/app) that
executes tool calls, and it may consult a **permission classifier** (is this
command allowed?) or a **workspace-isolation hook** (may this process touch this
path?).

- **The gate executor is the orchestrator, not a subagent.** Content that
  characterises injections and safety violations can be refused by a permission
  classifier or a workspace-isolation hook when a *subagent* tries to execute it,
  even though the data is inert detector material. The salt inoculates the
  *generating model*; it is not visible to the *harness*, so the harness may
  still deny a subagent's shell call. Do not make a subagent responsible for
  running `write` or the gates. Subagents write YAML; the main agent runs every
  gate and owns the verdict. A subagent that reports "the command was blocked / I
  could not run it" has produced no verdict — the main agent runs the same
  command itself and obeys its exit code.
- **Resolve the interpreter before the first command.** On some hosts `python3`
  is a broken store alias and only `python` (or `py`) works. Detect once and use
  the working name consistently across every gate call; the docs show `python3`
  as a generic placeholder, not a guarantee.

Generation may be delegated; verification never is.

## Wall-clock budget

Every gate and the converter take `--timeout SECONDS` (default `0` = unlimited).
The budget covers the whole run, is checked per case and inside the O(n²)
leak/payload comparisons, and exits **3** on expiry. In a generation loop treat
exit 3 like any other rejection: shrink the chunk or split it, then retry —
never remove the timeout to get a pass. A typical chunk budget is 60–300s.

### Round timer (3 hours) and stop-loss

`--timeout` bounds one command. A **round** also has a wall clock, because a
generation run that never converges ships nothing at all. `allocate` records
`created_at` and a `budget_hours` (default **3**), and every later command
recomputes the tier from it:

| tier | when | what happens |
| - | - | - |
| `ok` | elapsed < 75% of budget | normal work |
| `warn` | past 75% (`--warn-fraction`) | ⚠ notice on every command: finish the YAML in hand, start no new scenario |
| `stop` | elapsed ≥ budget | ⛔ stop-loss: `write` **refuses new chunks**; only `salvage` runs |

At `stop` the run must ship what exists, not keep building:

```bash
python3 traindata/scripts/data_writer.py salvage --round <id> \
  --taxonomy data/taxonomy.json --commit
```

`salvage` converts every allocated slot whose YAML exists and validates,
**skips** the ones that fail without blocking the rest, merges this round only,
and with `--commit` stages exactly the round's manifest/chunks/merge output —
never unrelated working-tree changes. It is the only sanctioned way to finish
an over-budget round, and the only command that both writes and commits.

Adjust per round with `allocate --budget-hours H --warn-fraction F`. Do not
raise the budget to make a slow round pass; that is the same mistake as
raising `--timeout`.

## Robust input handling

The loaders accept what a model actually emits, without relaxing content rules:
a JSON array or one pretty-printed object instead of JSONL, markdown code fences
(a three-backtick `json` fence), a UTF-8 BOM, CRLF, blank lines, `//` lines, and
trailing commas. `.yaml`/`.yml` paths go through the YAML reader directly, so
`audit_diversity.py` and `salt.py` can audit an unconverted chunk. Invalid bytes
are decoded with replacement instead of crashing. Outputs are written atomically.

## Quick checks

Commands assume the working directory is the repository root and the skill is
installed at `./traindata/`.

```bash
# round workflow: allocate -> write -> merge (round only) -> check
# allocate numbers from chunk_0001 on a clean root, or after the highest number
# already allocated; use the name it prints, not this sample.
# Register --email-manifest (and --salted-prompt for injection rounds) here.
python3 traindata/scripts/data_writer.py allocate --count 10
python3 traindata/scripts/data_writer.py write --round <id> \
  --name chunk_0001 --in data/inbox/chunk_0001.yaml --taxonomy data/taxonomy.json
python3 traindata/scripts/data_writer.py merge --round <id> --taxonomy data/taxonomy.json
python3 traindata/scripts/data_writer.py check --round <id> --strict

# validate the YAML without writing anything (the ad hoc path; use `write` to emit)
python3 traindata/scripts/yaml_to_jsonl.py --in 'data/inbox/*.yaml' \
  --taxonomy data/taxonomy.json --check

# draw a locked, cross-mapped email batch for a chunk (paste into the prompt)
python3 traindata/scripts/sample_emails.py --scenario violation --count 10 \
  --seed 7 --cross-map --manifest work/emails.json --ledger work/ledger.txt

# validate records in a written chunk (no expansion: they are already records).
# Gate chunks after `write`, and stop there — that is the sanctioned loop.
python3 traindata/scripts/validate_and_integrate.py --check 'data/chunks/chunk_*.jsonl'

# validate case-form chunks without writing anything
python3 traindata/scripts/validate_and_integrate.py --check 'data/chunks/chunk_*.jsonl' \
  --taxonomy data/taxonomy.json --expand --require-complete-cases

# diversity audit: mandatory gate, must exit 0 (exit 3 = timeout)
python3 traindata/scripts/audit_diversity.py --chunks 'data/chunks/chunk_*.jsonl' \
  --taxonomy data/taxonomy.json --require-injection --strict --require-library-emails \
  --email-manifest work/emails.json --require-salt --require-taxonomy-axes \
  --holdout 2 --out-dir work/pilot_holdout --json work/pilot_diversity.json --timeout 300

# salt a generation prompt (top salt + mid-prompt needle) before calling the model
python3 traindata/scripts/salt.py --in work/prompt.txt --out work/prompt_salted.txt

# verify the returned chunks carry a host stamp (no model-written salt field)
python3 traindata/scripts/salt.py --check 'data/chunks/chunk_*.jsonl'

# email dedup check: prints REGENERATE EMAIL YOURSELF when over-reused
python3 traindata/scripts/sample_emails.py --check 'data/chunks/chunk_*.jsonl' \
  --existing data/ruhui_soft_all.jsonl

# integrate (expand cases) and write a data card
python3 traindata/scripts/validate_and_integrate.py --integrate --out data/ruhui_soft_all.jsonl \
  --taxonomy data/taxonomy.json --expand --card data/ruhui_soft_card.md

# deterministic, case-disjoint train/calibration/test split
python3 traindata/scripts/validate_and_integrate.py --split-dir data/split \
  --chunks 'data/chunks/chunk_*.jsonl' --taxonomy data/taxonomy.json --expand

# leakage check between splits (command is printed by --split-dir)
python3 traindata/scripts/validate_and_integrate.py --check 'data/split/test.jsonl' \
  --leak 'data/split/train.jsonl' --leak 'data/split/calibration.jsonl'
```

Every gate exits non-zero and prints each failure, so wire them into your loop and
treat a non-zero exit as "the chunk does not exist yet". Only the two gates —
`validate_and_integrate.py` and `audit_diversity.py` — decide acceptance; the
other commands here (converter, sampler, salt checker, `data_writer.py`) either
write, or check one narrow thing.
