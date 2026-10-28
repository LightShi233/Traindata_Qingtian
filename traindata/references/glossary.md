# Glossary

Definitions for the terms used across `SKILL.md` and the other references. Every
term below is used in at least one file that does not define it, so read this
first when a sentence does not parse. Terms are grouped, not alphabetical.

## Data model

- **state** — what the model is asked about: a string, an object, or a turn list.
  Serialized once into `text` with `json.dumps(state, ensure_ascii=False)`. See
  `references/state-formats.md`.
- **case** — one independent scenario: a `state` plus all of its questions. A
  case is the unit of diversity, holdout, and train/test splitting.
- **record** — one JSONL line: one question about one case. A case expands into
  2–4 records that share `text`, `source_hash`, and `source_index` and differ
  only in `qtype`, `criteria`, `instruction`, and `soft_label`. Sharing `text`
  across records of one case is by contract, **not** duplication.
- **text** — the serialized state, byte-identical across a case's records.
- **source_hash** — `sha256(text.encode()).hexdigest()`; equal hashes on
  different cases mean a duplicate.
- **source_index** — a unique integer per case, shared by its records.
- **source_dataset** — a label for where the case came from; defaults to
  `synthetic` when the YAML omits it.

## Questions and labels

- **head** — one taxonomy question (`is_violation`, `mechanism`, `severity`). A
  case usually has 2–4 heads. "Three heads of a case" means its three questions,
  which are **not** three independent cases.
- **qtype** — a question's answer type:
  - `noul` — two options `true` / `false`, fixed order;
  - `choice` — one of *k* named keys;
  - `score` — one ordered level `"0"`, `"1"`, …
- **criteria** — for `noul`/`choice`, a map from option key to its defining text;
  for `score`, an ordered list of level descriptions.
- **instruction** — the question text asked about the state.
- **soft label** — a probability distribution over a question's options plus the
  argmax `label`. Default: primary `0.9`, remaining `0.1` spread evenly. See
  `references/soft-labels.md`.
- **primary label** — the argmax option; it carries `0.9` by default.
- **one-hot** — a distribution with one `1.0` and the rest `0.0`. Rejected: it
  destroys calibration.
- **uniform** — equal mass on every option. Also rejected: it carries no signal.
- **polarity** — the direction a question asks in. One head has one polarity.
- **dual polarity** — a single question that means "what went wrong" on
  violations and "what was avoided" on clean cases. Forbidden; the audit rejects
  known phrasings.

## The shipped taxonomy (`ruhui-triage-v2`)

The live option/level sets are in `data/taxonomy.json`. Do not invent keys.

- **is_violation** (`noul`) — did the agent commit the safety violation?
- **mechanism** (`choice`) — *what drove the agent's decisive action*, in every
  case, independent of outcome. Keys: `injection`, `misdirection`,
  `faulty_input`, `own_reasoning`, `other`. There is **no `none`**: a clean,
  unsteered case is `own_reasoning`; a resisted injection is still `injection`.
- **severity** (`score`, levels 0–3) — impact that **actually occurred**:
  `0` none, `1` moderate, `2` severe, `3` critical. A clean case is level `0`.

Do not confuse these with the `meta` axes below: `mechanism` is a training
question, `harm_kind` is diversity metadata.

## Diversity metadata (`meta`)

`meta` is a per-case object in case-form chunks. It is not training data: it is
dropped when cases expand into records (`expand_case` emits a fixed field set and
raises on anything extra). Two fields are read before that, by the validator as
well as the audit: `benign_directive_span` (checked against the text by
`check_benign_directive`) and `payload_span`. Full field list:
`references/anti-shortcut.md`.

- **scenario** — one `text` hash. Diversity and holdouts are counted per
  scenario, not per record.
- **scenario_family** — the task shape (support triage, ticket reply, …).
- **entity_family** — the org/brand family; the name must literally appear in
  the text.
- **id_style** — the ID format used in the text (`prefix_seq`, `uuid`, `slug`, …).
- **thought_style** — the reasoning voice (terse, planner, skeptical, …).
- **injection_present** — is an injected instruction present, regardless of
  whether the agent yielded.
- **external_contact** — is an external-looking address present in the text.
- **harm_kind** — the semantic kind *present in the input*: `data_egress`,
  `privilege_escalation`, `destructive_change`, `policy_bypass`,
  `social_engineering`, `misinformation`, `none`.
- **request_shape** — the functional shape: `standard` (bare directive),
  `contextual` (context string + directive referencing it), `near_miss` (looks
  violating but is legitimate).
- **context_span** — for `contextual`, the literal context substring.
- **payload** — the injected instruction text.
- **injection_surface** — the field holding the payload (`body`, `subject`,
  `status`, `footer`, `description`, `notes`, …).
- **payload_style** — the payload's wording register (`policy_cc`,
  `workflow_step`, `authority_claim`, …).
- **payload_span** — the exact injected substring, byte-for-byte.
- **hard negative** — a clean case containing instruction-like but legitimate
  text (a runbook line, a policy about cc'ing finance).
- **benign_directive_span** — for a clean case only, that legitimate
  instruction-like substring. On a violating case it is a forgery and fails.

## Emails

- **task domain** — the subject area of a case (support, finance, …). Distinct
  from an email domain.
- **email domain** — the registrable domain of an address (`example.org`,
  `guerrillamail.com`). "Domains are never blacklisted" always means email
  domains.
- **freemail** — ordinary consumer mail (gmail-style); the majority of violating
  cases must use it.
- **disposable** — throwaway/temporary mail; expected on both sides of the
  label, never excluded.
- **cross-map** — an email batch that pairs two addresses on the same email
  domain, one for a clean case and one for a violating case, so the domain
  cannot predict the label.
- **locked batch / manifest** — the exact addresses `sample_emails.py` drew for
  this round. The manifest is a whitelist: an off-batch address fails.
- **ledger** — a record of local parts already used, shared by every chunk, so
  prefixes stay unique corpus-wide.

## Agents and execution

- **orchestrator** — the main agent running the skill. It runs every gate and
  owns the verdict.
- **subagent** — a worker the orchestrator dispatches to author YAML. It does not
  run gates, and it never fills a gap it was not given.
- **harness** — the host program (CLI/app) that executes tool calls on the
  agent's behalf.
- **permission classifier / workspace-isolation hook** — harness checks that may
  refuse a command (content-based) or a path (sandbox-based). A refusal is not a
  verdict on the data; the orchestrator re-runs the command itself.
- **pilot** — the first 1–2 chunks, reviewed by hand and used as a prompt gate
  (not as training data) before scaling.
- **gate** — a script whose exit code decides pass/fail. Exit `0` is the only
  pass; non-zero means "this chunk does not exist yet"; exit `3` means the
  `--timeout` budget expired (shrink the chunk and retry).

## Workflow

- **validator** — `validate_and_integrate.py`: schema, soft labels, duplicates,
  leakage.
- **audit** — `audit_diversity.py`: shortcut/diversity/action-attribution gate.
- **chunk** — one generation call's worth of cases (~8–12), written as one YAML
  file and converted to one JSONL file.
- **round** — one `allocate → write → merge → check` cycle with its own ID
  namespace, email batch, and wall clock (default 3 hours). Tiers: `ok`, `warn`
  (75%), `stop` (ship via `salvage`).
- **salvage** — stop-loss command: convert and merge whatever round slots are
  valid, skipping failures, optionally committing.
- **Jaccard** — word-overlap similarity used for leakage; 8-gram Jaccard above
  `0.30` is a failure.

## Splits and holdouts

- **split** — case-disjoint `train` / `calibration` / `test` files written by
  `--split-dir`. Split by case, never by record.
- **calibration** — the split used to fit the calibration temperature; never the
  test split.
- **holdout** — scenarios withheld by `text` hash (`--holdout N`), never trained
  on and never used to tune the prompt. "Scenario-hash holdout" is the same
  thing; "non-disjoint holdout" means the withheld scenarios leaked back into
  training and is a failure.

## Salt

- **salt** — a short defensive preamble prepended to a generation prompt that
  authors injection/violation material. It tells the generating model it is an
  author of inert, fictional detector data. See
  `references/harmbench-methodology.md`.
- **needle** — a one-line reminder of the salt re-inserted mid-prompt so it
  survives context drift.
- **host stamp** — `meta.salted` + `meta.salted_at`, written by
  `data_writer.py write` from the round's registered salted prompt. The model
  must **not** write `meta.salted`; a model-written salt is a fabrication and is
  stripped.
