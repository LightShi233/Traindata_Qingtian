# Soft labels

Soft labels are the training signal. Hard one-hot targets push the model to
saturate its softmax and destroy the calibration the model exists to provide.

## Default distribution

**Primary label `0.9`, remaining mass `0.1` spread evenly over the other options.**

| qtype | options | distribution |
| - | - | - |
| `noul` | `false`, `true` | primary `0.9`, other `0.1` |
| `choice` | `k` options | primary `0.9`, each other `0.1 / (k - 1)` |
| `score` | `k` levels | primary `0.9`, each other `0.1 / (k - 1)` |

Always renormalize after computing, and assert `abs(sum(p) - 1.0) <= 1e-6`.

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

`score` with 3 levels, primary level 1:

```json
{"probabilities": {"0": 0.05, "1": 0.9, "2": 0.05}, "label": "1"}
```

## Deliberately ambiguous cases

When the case genuinely supports two readings, do not pretend certainty: lower
the primary and record why. A common pattern is `0.7 / 0.2 / spread 0.1`:

```json
{"probabilities": {"a": 0.7, "b": 0.2, "c": 0.05, "d": 0.05}, "label": "a"}
```

Rules for ambiguity:

- The primary must still be the argmax.
- The runner-up carries the second mass, not an arbitrary option.
- The reason must live in your generation log or data card — it is not a field in
  the record, and it must be reproducible for inspection.

## Anti-patterns

- **One-hot** (`1.0 / 0.0`). Rejected by the validator.
- **Uniform** (`0.25 / 0.25 / 0.25 / 0.25`). Carries no signal.
- **Unnormalized** mass (e.g. `0.9 / 0.9`). Rejected by the validator.
- **Label/argmax mismatch.** Rejected by the validator.
- **Sharpening too far.** Repeated `0.99` primaries turn calibration into a
  fiction; `0.9` is the working default and `0.7` is the floor for genuine
  ambiguity unless the user specifies otherwise.

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
- [ ] primary `0.9` by default; `0.7` only for documented ambiguity