# Soft labels

Soft labels are the training signal: a probability distribution over a
question's options instead of a single right answer. Hard one-hot targets push
the model to saturate its softmax and destroy the calibration the model exists to
provide. **qtype** is a question's answer type — `noul` (true/false), `choice`
(one of k keys), `score` (one ordered level); other terms are in
`references/glossary.md`.

## Default distribution

**Primary label `0.9`, remaining mass `0.1` spread evenly over the other options.**

| qtype | options | distribution |
| - | - | - |
| `noul` | `false`, `true` | primary `0.9`, other `0.1` |
| `choice` | `k` options | primary `0.9`, each other `0.1 / (k - 1)` |
| `score` | `k` levels | primary `0.9`, each other `0.1 / (k - 1)` |

Always renormalize after computing, and assert `abs(sum(p) - 1.0) <= 1e-6`.
The live option sets are the ones in `data/taxonomy.json`; the keys in the
examples below are illustrative only — never copy `risk_source`/`sink`/`delay`
into a real record.

### Worked examples

`noul`, primary `true`:

```json
{"probabilities": {"true": 0.9, "false": 0.1}, "label": "true"}
```

`choice` with 4 options, primary `risk_source`:

```json
{"probabilities": {"risk_source": 0.9, "sink": 0.0333333333, "delay": 0.0333333333, "other": 0.0333333333}, "label": "risk_source"}
```

Prefer emitting spread values rounded to 6-8 decimals and then renormalizing the
last key so the sum is exactly 1.0 in float64.

`score` with 3 levels (the shipped `severity` shape after v4 made it
conditional), primary level 1:

```json
{"probabilities": {"0": 0.05, "1": 0.9, "2": 0.05}, "label": "1"}
```

## Deliberately ambiguous cases

When the case genuinely supports two readings, do not pretend certainty: set a
lower `primary` on the case and record why. `primary` is an optional case-form
field (default `0.9`); expansion puts `primary` on the label and spreads
`1 - primary` evenly over the rest, so the shape stays honest without hand
arithmetic.

```yaml
- traj_id: ruhsoft_000123_deadbeef
  safe: false
  primary: 0.7            # 0.5 < primary <= 0.9
  meta:
    primary_basis: "arbitration 2/3: two reviewers read the export as authorised"
  # ... turns, labels
```

A head can disagree with the case. The mapping form sets a case default and
overrides only the heads the adjudication actually disputed — forcing one number
onto every head claims doubt that some heads do not have:

```yaml
  primary:
    default: 0.55          # is_violation and the other heads
    severity: 0.90         # impact is not in doubt, only whether it was authorised
    grounding: 0.90
```

Keys are `default` plus taxonomy question ids; anything else fails validation.

Rules for ambiguity:

- `primary` must be in `(0.5, 0.9]`: above `0.5` the label still holds the
  majority of the mass, and above `0.9` is over-confidence the validator rejects.
  Every value in the mapping is checked, not just `default`.
- `meta.primary_basis` is **required** whenever any effective value is below
  `0.9` and must name the evidence (adjudication split, κ, how many reviewers
  disagreed). A lowered primary without a basis is an invented uncertainty and
  fails validation.
- The basis is also the only thing that makes the number meaningful: on a
  proper scoring rule (which is what the consumer uses) a wrong `0.7` teaches a
  wrong distribution shape, not just a wrong argmax.
- Do not lower it by default. The audit warns when more than half of the
  `(case, head)` pairs are below `0.9`: if most samples are "unsure", the
  question or the rubric is the problem, not each sample. The opposite end is
  reported too — all-`0.9` is an INFO at generation time and a WARN once the
  chunk contains corrected cases, because only then does adjudication evidence
  exist.

Sanctioned mapping from an adjudication record (a reviewed corpus produced
these three tiers):

| evidence | primary |
| - | - |
| both reviewers agree, flip confirmed | `0.90` |
| reviewers directly conflict, one re-judged the flip | `0.70` |
| arbitration records a genuine downgrade | `0.55` |

## Anti-patterns

- **One-hot** (`1.0 / 0.0`). Rejected by the validator.
- **Uniform** (`0.25 / 0.25 / 0.25 / 0.25`). Carries no signal; the validator
  rejects an exactly uniform distribution.
- **Unnormalized** mass (e.g. `0.9 / 0.9`). Rejected by the validator.
- **Label/argmax mismatch.** Rejected by the validator.
- **Sharpening too far.** Repeated `0.99` primaries turn calibration into a
  fiction; `0.9` is the working default and `0.7` is the floor for genuine
  ambiguity unless the user specifies otherwise.
- **Uncosted certainty.** A corpus where every case is `0.9` is a corpus that
  claims no sample is contested. On a proper scoring rule that trains uniform
  over-confidence; temperature scaling is a monotone transform and cannot
  invent sample-level differences the labels never carried.

## Why `0.9` and not `1.0`

The model is trained with a proper scoring rule plus a soft cross-entropy term.
A `0.9` target leaves room for the model to represent "likely, not certain",
which is what keeps the published confidence meaningful. Once calibrated, a
well-fitted temperature can only help if the targets were not already saturated.

## Validation checklist

- [ ] every `probabilities` value `>= 0`
- [ ] `sum(probabilities) == 1.0` within 1e-6
- [ ] `label == argmax(probabilities)`
- [ ] no one-hot unless explicitly justified and documented
- [ ] no uniform distributions
- [ ] primary `0.9` by default; lower only with `meta.primary_basis`, and never
      `<= 0.5` or `> 0.9`