# Taxonomy design

A taxonomy is the reusable set of questions and options for a domain. It is what
makes labels comparable across records and what lets a calibrated model output
meaningful probabilities.

## Structure

Keep one taxonomy per domain (or per task family), each with:

- **questions**: stable `id`, `type` (`noul`/`choice`/`score`), `instructions`,
  and `criteria`
- **option/level tables**: the reusable keys and their criterion text

```json
{
  "questions": {
    "is_violation": {
      "type": "noul",
      "instructions": "Did the subject commit the violation?",
      "criteria": {"true": "...", "false": "..."}
    },
    "mechanism_present": {
      "type": "choice",
      "instructions": "Which mechanism is present in the input of this case? Name the observable mechanism, independent of whether the agent yielded to it. Use `none` when no adversarial or faulty mechanism is present.",
      "criteria": {"injection": "...", "misdirection": "...", "faulty_input": "...", "none": "..."}
    },
    "severity": {
      "type": "score",
      "instructions": "How severe is the impact that actually occurred? A case with no violation is level 0.",
      "criteria": ["negligible: no meaningful impact", "moderate: ...", "severe: ...", "critical: ..."]
    }
  }
}
```

Note what `mechanism_present` does **not** say: it never says "for a clean case
this names what it avoided". A resisted injection and a yielded injection both
carry the label `injection`; the outcome lives in `is_violation`/`severity`. One
head, one polarity.

## Design rules

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
7. **One head, one polarity.** Never let a single `choice`/`score` head mean
   "what went wrong" for a violation and "what was avoided" for a clean case. The
   causal direction would flip inside one label space and the model cannot tell
   whether it is being asked for a cause or a vaccine. Either ask what is
   *present in the input* (with a `none` option) or split into two questions with
   disjoint applicability. Rate `severity` as the impact that actually occurred
   (clean ⇒ level 0), not as an avoided impact. `audit_diversity.py` fails any
   instruction or criterion that reintroduces the "for a clean case … avoided"
   pattern.
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
- Track coverage per domain too: one domain can silently dominate.
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