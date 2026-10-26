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
    "mechanism": {
      "type": "choice",
      "instructions": "Which mechanism is at play? (For a clean case this names the mechanism it avoided.)",
      "criteria": {"injection": "...", "misdirection": "...", "faulty_input": "...", "own_reasoning": "..."}
    },
    "severity": {
      "type": "score",
      "instructions": "How severe is the impact?",
      "criteria": ["negligible", "moderate", "severe", "critical"]
    }
  }
}
```

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
7. **For contrasting sets, note the "clean" reading.** If a question is asked of
   both positive and negative cases, say what the label means for a clean case
   (e.g. "the mechanism the case correctly avoided"). Otherwise the labels for
   negatives are undefined.
8. **Don't aim for balance by fiat.** Coverage targets are fine, but every label
   assigned must be defensible from the case.

## Coverage discipline

- Before a run, list the label keys that must appear and how often.
- After a run, count them. Concentrations above ~40% in one key usually mean the
  generator is converging on an easy template.
- Track coverage per domain too: one domain can silently dominate.

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