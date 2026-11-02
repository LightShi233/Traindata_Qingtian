# Taxonomy design

A taxonomy is the reusable set of questions and options for a **task domain** —
the subject area of the cases (support triage, finance approvals, …), not to be
confused with an **email domain**, which is a mail address's registrable domain.
The taxonomy is what makes labels comparable across records and what lets a
calibrated model output meaningful probabilities. Every term below is defined in
`references/glossary.md`.

## Structure

Keep one taxonomy per task domain (or per task family), each with:

- **questions**: stable `id`, `type` (`noul`/`choice`/`score`), `instructions`,
  and `criteria`
- **option/level tables**: the reusable keys and their criterion text

The shipped taxonomy (`assets/taxonomy-template.json`, version
`ruhui-triage-v4`) is the reference example. Reproduced with shortened
criterion text:

```json
{
  "version": "ruhui-triage-v4",
  "questions": {
    "is_violation": {"type": "noul", "criteria": {"true": "…", "false": "…"}},
    "harm_kind": {"type": "choice", "criteria": {"data_egress": "…", "…": "…", "none": "…"}},
    "severity": {
      "type": "score",
      "applies_when": {"is_violation": true},
      "criteria": ["moderate: …", "severe: …", "critical: …"]
    },
    "grounding": {
      "type": "score",
      "applies_when": {"is_violation": true},
      "criteria": ["none: …", "weak: …", "partial: …", "ungrounded: …"]
    }
  }
}
```

Two properties do the work:

- **`harm_kind` is asked of every case** because it describes what is present in
  the *input*, not what the agent did. A clean case that resisted a
  social-engineering email still carries `social_engineering`; only its
  `is_violation` is false.
- **`severity` and `grounding` are conditional.** `applies_when` makes the head
  hierarchical: it is asked only when the named answer holds, and a case that
  does not satisfy the condition must not carry the field. This replaces two
  earlier designs that both failed:
  - asking every case and letting clean cases answer a constant (v3: severity
    ≡ 0 for all clean cases, variance zero, temperature inestimable);
  - splitting one head into `severity` / `avoided_severity`, which is the
    dual-polarity bug (the same head means two different questions).

Conditional heads also mean the record count is not constant: a clean case
expands to 2 records, a violating case to 4. `--require-complete-cases`
compares each case to the questions that actually apply to it, never to the
whole question list.

## Design rules## Design rules

1. **Mutually exclusive options.** If two options can both be true, every case
   becomes arbitrary labelling noise.
2. **Collectively reasonable coverage.** The option set should be able to describe
   most cases; add an explicit `other` rather than forcing a bad fit.
3. **Criterion text carries the semantics.** The model reads it, so write what
   distinguishes the option, not a restatement of the key.
4. **Same instruction across records of a question.** Rewording per record makes
   the question a moving target.
5. **`choice` key naming.** Short, stable, English identifiers; snapshot them once
   and never rename in place.
6. **`score` is ordered.** Level `0` is the low end; keep the ordering stable
   because the training objective uses rank-aware scoring.
7. **One head, one polarity; use `applies_when` instead of a constant branch.**
   Never let a single `choice`/`score` head mean "what went wrong" for a
   violation and "what was avoided" for a clean case. If a question only makes
   sense under a condition, declare the condition (`applies_when`) so it is not
   asked of the other cases at all. Do not ask it of everyone and encode the
   non-applicable half as a constant level or a `none` key: a constant label
   carries no information and, on a proper scoring rule, teaches the model to be
   confident about nothing. `audit_diversity.py` still fails any instruction or
   criterion that reintroduces the "for a clean case … avoided" pattern.
8. **Keep option sets reusable and present-tense.** Criterion text describes an
   observable state of the case, not a story about it. If a label depends on the
   agent's outcome, that outcome belongs in another head.

## Two orthogonal axes (HarmBench-derived)

HarmBench (arXiv:2402.04249) tags every behavior on **two independent axes** and
demands breadth on both, so a method that only cracks one corner cannot look
good. We adopt the same idea for case-form chunks. Both live in `meta` (so they
never enter `text`) and both are rotation axes the audit checks.

**Axis A — `harm_kind` (semantic kind, "what is present in the input").** Adapted
from HarmBench's 7 semantic categories to agent/data violations; keep it one
polarity (present-in-input, independent of whether the agent yielded):

| `harm_kind` | meaning | HarmBench analogue |
| - | - | - |
| `data_egress` | unauthorised read/send/export of data | cybercrime & intrusion |
| `privilege_escalation` | grant·revoke·impersonate beyond mandate | cybercrime & intrusion |
| `destructive_change` | delete·purge·overwrite·disable | illegal activities |
| `policy_bypass` | ignore approval/instructions/policy | illegal activities |
| `social_engineering` | deceive a human or another agent | harassment & bullying |
| `misinformation` | fabricate or misrepresent facts | misinformation & disinformation |
| `none` | no adversarial/faulty mechanism present | — |

**Axis B — `request_shape` (functional shape).** From HarmBench's 4 functional
categories; `multimodal` does not apply to text/turn states:

| `request_shape` | shape | requires | HarmBench analogue |
| - | - | - | - |
| `standard` | a bare directive in the input | — | standard |
| `contextual` | a context string **plus** a directive referencing it | `meta.context_span` (literal) | contextual |
| `near_miss` | looks violating but is legitimate (hard negative) | `meta.benign_directive_span` | dual-intent handling |

`injection_present` stays a separate cross-cutting flag: a `standard` or
`contextual` case can *also* be injected. Do not fold injection into the shape.

**Rules.**

1. Rotate both axes: no single `harm_kind` or `request_shape` above 40%; once a
   chunk has ≥ 9 cases, all three shapes should appear and ≥ 3 harm kinds.
2. `contextual` without a literal `context_span` is a lie; `near_miss` without a
   `benign_directive_span` is a lie. The audit fails both.
3. `near_miss` is the functional home of hard negatives: it should cover 25–75%
   of clean cases (same window as `benign_directive_span`).
4. **Attempt counts, discussion does not** (HarmBench's success criterion, which
   matches our action attribution): a `safe=false` case must be violated by an
   agent *action*; a completion/thought that merely analyzes or reacts does not
   count. See `references/anti-shortcut.md`.
5. **Differential harm / not searchable.** Prefer cases whose answer a model must
   *reason* about over cases copied from a search engine — and, unlike HarmBench,
   never reproduce an operational harmful behavior: characterise the category.

```json
{
  "meta_axes": {
    "harm_kind": ["data_egress", "privilege_escalation", "destructive_change",
                  "policy_bypass", "social_engineering", "misinformation", "none"],
    "request_shape": ["standard", "contextual", "near_miss"]
  }
}
```

## Coverage discipline

- Before a run, list the label keys that must appear and how often.
- After a run, count them. Concentrations above ~40% in one key usually mean the
  generator is converging on an easy template.
- Track coverage per task domain too: one task domain can silently dominate.
- Check a `choice` key that exceeds ~40%: over half usually means the generator
  found a shortcut template, not that the world is that skewed.

## Do not aim for balance by fiat

Coverage targets are fine, but every label assigned must be defensible from the
case. A balanced corpus of wrong labels is worse than an imbalanced corpus of
right ones; fix the generator, not the counts.

## Versioning

- Treat a taxonomy as versioned: renaming a key invalidates earlier labels.
- If the taxonomy must change, add new keys and mark old ones; do not rewrite
  history in the corpus.
- Record the taxonomy version in the data card.

## Anti-patterns

- per-record option sets (calibration cannot transfer between records)
- overlapping options
- instructions that leak the answer ("this is clearly an injection because...")
- questions whose negative label means "no data" instead of a defined state
- taxonomy keys that only exist to make a distribution look balanced
- dual-polarity heads: one label set that means "the failure" on violations and
  "the avoided failure" on clean cases