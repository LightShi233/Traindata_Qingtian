# TrainData-CommonSkill

A standalone copy of the `traindata` skill. Same contract, same gates, same
scripts — with every environment dependency removed:

- **no GitHub Actions** — nothing reads a workflow, a runner variable, or a
  branch name;
- **no private data repository** — there is no `publish`, no remote, no SSH
  key handling, and no assumption that the corpus is version-controlled;
- **no run/commit ids** — chunk names are plain `chunk_0001`, or carry a
  caller-supplied `--suffix` of your choosing; nothing is read from the
  environment;
- **no project confinement** — `--data-dir` may be any writable directory; the
  skill does not require a `.git` ancestor and never relocates a path.

It also ships the revised **v4 taxonomy**: `mechanism` is gone (it restated
`meta.injection_present` about an unobservable internal state), and `severity` /
`grounding` are conditional on `is_violation=true`, so clean cases carry two
heads and violating cases carry four. See `references/label-provenance.md` §7
for the reasoning and the migration rule.

## Requirements

Python 3.9+ and its standard library. No network, no PyYAML, no third-party
packages. The YAML reader is part of `scripts/dataloader.py`, and the email
pool under `assets/email_library/` is vendored with its upstream licences.

## Quick start

Run everything from this directory.

```bash
# 0. pick a taxonomy (the shipped one is an example for the ruhui triage task)
mkdir -p data
cp assets/taxonomy-template.json data/taxonomy.json

# 1. freeze an address batch and register a salted generation prompt
python3 scripts/sample_emails.py --scenario violation --count 8 --cross-map \
  --manifest work/emails.json --ledger work/ledger.txt
python3 scripts/salt.py --in work/prompt.txt --out work/salted_prompt.txt

# 2. allocate a chunk slot (--count is chunk slots, not cases)
python3 scripts/data_writer.py allocate --count 1 \
  --email-manifest work/emails.json --salted-prompt work/salted_prompt.txt

# 3. write the case YAML into the printed inbox/<name>.yaml, then:
python3 scripts/data_writer.py write --round <id> --name chunk_0001 \
  --in data/inbox/chunk_0001.yaml --taxonomy data/taxonomy.json
python3 scripts/validate_and_integrate.py --check data/chunks/chunk_0001.jsonl \
  --taxonomy data/taxonomy.json --expand --require-complete-cases --timeout 300
python3 scripts/audit_diversity.py --chunks data/chunks/chunk_0001.jsonl \
  --taxonomy data/taxonomy.json --require-injection --strict \
  --require-taxonomy-axes --require-salt --email-manifest work/emails.json --timeout 300
python3 scripts/salt.py --check data/chunks/chunk_0001.jsonl

# 4. merge this round only, then verify the round left no stray files
python3 scripts/data_writer.py merge --round <id> --taxonomy data/taxonomy.json
python3 scripts/data_writer.py check --round <id> --strict

# 5. measure the residual surface shortcut (report, not a gate)
python3 scripts/audit_shortcut.py --chunks 'data/chunks/chunk_*.jsonl'
```

The full contract — phases, hard rules, `meta` schema, soft-label rules,
taxonomy design, and the anti-shortcut gate — is in `SKILL.md`, with details in
`references/`. Read `SKILL.md` first.

## Parallel authoring

To split a round across subagents, pass unique labels to `allocate`:

```bash
python3 scripts/data_writer.py allocate --count 8 --workers a1 a2 a3 a4 b1 b2 b3 b4 \
  --email-manifest work/emails.json --salted-prompt work/salted_prompt.txt
```

It prints a chunk→worker table and then **the subagent contract itself**
(`SUBAGENT_CONTRACT` in `scripts/data_writer.py`). Paste that block unedited into
each subagent's task; `data_writer.py claim` prints the same block when the
worker claims its slot. The contract is fixed: three obligations (claim, write
one YAML, echo the authored ids) and five prohibitions (no `write`/merge/gate/git,
no second file, no invented names, no self-certified pass, no improvising cases).
The orchestrator runs every `write` and every gate itself.

## Layout

```
data/            output root (created by allocate; override with --data-dir)
  inbox/         model-authored case YAML
  chunks/        validated case-form JSONL
  rounds/        round manifests and merged output
  taxonomy.json  question/option contract
work/            scratch: salted prompt, email manifest/ledger, reports
scripts/         the fixed tooling; never copy these into the output root
references/      the written contract
assets/          vendored email pool and provenance
```

## What this copy intentionally drops

`../traindata/` keeps the CI wiring used by its original project (a scheduled
workflow, a private corpus repository, run-suffixed chunk names, `publish`,
`salvage --commit`, and project-root confinement). None of that is part of the
method; this copy is the method without the deployment.
