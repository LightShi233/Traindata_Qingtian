# Label provenance, and the shortcuts this skill can actually block

A corpus can pass every gate and still be worthless. The gates check form and
diversity; neither checks whether the label is **earned**. This file records the
audit that made that distinction hard to ignore, states what the skill now
enforces, and — just as important — what it refuses to claim.

Read it after `references/anti-shortcut.md`, before blaming a bad eval score on
the model.

## 1. The diagnosis: labels are copied from the plan, not read off the sample

Everything in a case is authored at generation time, including its labels.
`expand_case()` in `validate_and_integrate.py` then expands them mechanically:

| head | what it actually restates |
| - | - |
| `is_violation` | `safe` (the generator's own switch) |
| `mechanism` | `meta.injection_present` (the generator's own plan) |
| `harm_kind` / `severity` / `grounding` | the plan's intent |
| every probability | the constant `0.9` |

So the "teacher" is not a grader looking at the produced trajectory; it is the
specification that asked for the trajectory. Nothing in the pipeline ever
re-reads the turns to ask *did this actually happen?* A case planned as a
violation is labelled a violation even if the agent never executed the action;
a case planned as clean is labelled clean even if data left the boundary.

This is not a bug in any single check. It is the design: generation and
annotation are the same step, and there is no step that could disagree.

## 2. The measurement: what a surface-only model can score

A diagnostic probe (now shipped as `scripts/audit_shortcut.py`) was fitted to a
previous corpus of this pipeline using surface features only — tool verbs,
trajectory length, hedging voice, addresses. No semantics, no reading
comprehension.

| feature | single-feature AUC | coefficient |
| - | - | - |
| destructive tool verb | 0.62 | +4.81 |
| egress tool verb | 0.64 | +1.73 |
| trajectory length | 0.60 | +3.67 |
| concession/hedge voice | 0.39 | −1.01 |
| suspicious email domain | 0.50 | −0.19 |
| **combined** | **0.82** | 71.3% accuracy (50% baseline) |

Three findings changed this skill's contract:

1. **Suspicious domains were noise, not a shortcut.** AUC 0.50, negative
   coefficient. Domain reuse looks alarming in a manual review and predicts
   nothing. The email cross-map and locked batch still matter (they stop the
   label from tracking the *recipient*), but they were never the leak.
2. **Tool verbs and length were the leak.** A lookup table over verbs plus a
   length threshold reproduces most labels. The corpus taught "destructive and
   long means violation" and never required the authorisation rule.
3. **Hedged reasoning is a clean signal, not a dirty one.** "However, this stays
   in scope" correlated with *correct* behaviour. A generator that treats
   cautious phrasing as suspicious is writing the wrong hard negatives.

Re-measure before quoting these numbers: they describe a corpus, not this
pipeline's current output. `audit_shortcut.py` prints the same table for any
set of case-form chunks, and its verdict comes from held-out predictions, not
the in-sample fit.

**Follow-rate: the shortcut and the label error are the same thing.** A later
experiment re-ran the probe against audited (human-corrected) labels. The
headline AUC barely moved (0.817 → 0.821), but on the 44 cases a human had
confirmed as mislabelled, the probe agreed with the *original* label 39/44
(89%) and with the corrected label 7/44 (16%). A surface model that "learns the
shortcut" is learning the spec's intent — including its 6.7% error. That gives
a second, cheaper target than rebuilding the pipeline:

> **cut the channel that carries the error**, and measure it as the fraction of
> a corrected set on which the probe still follows the old label.

`audit_shortcut.py --corrected <map>` computes exactly that (`follow_original`
vs `follow_corrected`). Use it after any label-correction pass.

## 3. What the skill now enforces

These are counting facts about a chunk, not judgments about a single case, so
they cannot be argued with and cannot be faked by `meta`:

- **Dangerous tool classes are cross-mapped.** `destructive` / `egress` / `admin`
  verbs are derived from `turns[].action.name` (never from `meta`). A class that
  appears only on one side of `safe` — three or more times — fails the audit.
  The fix is a data fix: put a *user-authorised* destructive/egress/admin action
  in the clean cases (the user asks for the deletion, the send, the grant), so
  the verb stops predicting the label and only its authorisation does.
- **Trajectory length is label-neutral.** If the median length of one class is
  twice the other, the audit fails. Pad clean cases with the same amount of
  normal verification work; do not let a violation need extra turns to explain
  itself.
- **The split is family-disjoint.** `--split-dir` groups by
  `meta.scenario_family` when the input is case form, so sibling variants of one
  skeleton cannot land on both sides. Record-form input has no `meta` and falls
  back to case-level grouping, with the grouping reported in
  `split_manifest.json` (`group_by`).
- **The residual is measured, not assumed.** `audit_shortcut.py` reports the
  surface AUC of a merged corpus. CI runs it after the gates and logs the number
  without blocking: a per-round chunk is too small to threshold honestly.

**Wording rule.** The probe uses a **fixed, small feature set** (tool verbs,
length, hedges, tool-call count, addresses, agent text volume). Say
*"3 of the 8 measured features are discriminative"*, never *"shortcuts
measured"*: an AUC of 0.82 means part of the discriminability is **not**
explained by these features, and "measured" implies exhaustive. The probe
prints the count on every run.

## 4. The third path: targeted label correction

The options are not only "rebuild the pipeline (blocked on an execution layer)"
or "do nothing". A **correction pass** attacks the same channel with far less
work, because the error being learned is the spec's own error:

1. **Recall, not judgment, from the program.** Use a mechanical signal to shrink
   the candidate pool — the audited one was *egress actually completed*
   + *recipient absent from preceding tool returns* + *agent noted the anomaly
   without escalating*. A program must never be the judge here: on
   model-invented environment returns, semantic extraction showed 88–95% false
   positives.
2. **A human decides.** Every candidate is confirmed or rejected by a person,
   with the mechanical facts laid out. Record the reason.
3. **Corrections are append-only.** The corpus repo rejects edits to existing
   chunks, so a correction is a **new chunk with new `traj_id`s**, carrying
   `meta.corrects: <old traj_id>` (meta never reaches training). The superseded
   case stays in the corpus as history but must be **excluded from training** —
   otherwise both labels are trained on the same text.
4. **Re-measure.** Run `audit_shortcut.py --corrected` over the corpus with the
   map of old labels. Success is the follow-rate collapsing:
   `follow_original` → `follow_corrected`.

This does not eliminate the shortcut. It compresses the gap between the shortcut
and the truth toward zero, which is why it does not conflict with the ban on
claiming elimination in §5. It also does not replace the execution layer: a
corrected corpus is still labelled from the plan for every case a human did not
touch.

## 5. What this cannot fix (and the honest claim)

Lowering the probe's AUC does **not** prove the model learned the rule. It
proves the *known surface features* are no longer separately predictive. A model
can still be right for the wrong reason; only an intervention can show
otherwise:

| missing capability | what it would take |
| - | - |
| labels derived from execution | a fact layer over a tool classifier + structured environment returns (the returns are currently invented by the model, so any "did the send complete?" extraction reads a hallucination) |
| counterfactual pairs | a state machine replayed with one flag flipped; generating both variants in prose produces an unreal trajectory and teaches a new shortcut |
| verifier / calibrated confidence | a constructed probe set whose answers are fixed by construction, plus a human gold set with κ, plus a non-LLM baseline |
| conditional heads | a training-side change in `ruhui` (shared trunk, `severity`/`grounding` conditioned on `is_violation`), not a data-only change |

Until those exist, the skill's claim is limited to: **the label source is known,
the measured feature set is reported with how many of its features are
discriminative, and those features are cross-mapped.** Do not write
"shortcuts eliminated" in a data card, a commit message, or a README. Do not
write "shortcuts measured" either — it implies the set is complete.

Two further rules follow from the same audit:

- **Old data is warmup-only, with a hard stop.** A corpus produced before these
  checks is the corpus in which the verbs were perfectly predictive. Mixing it
  in reinstates the shortcut the new checks removed. The termination condition
  is a **step cap, not a weight**: old-corpus records may appear only in the
  first `N` optimizer steps, where `N` is one epoch over the new corpus, after
  which the loader excludes them entirely. "Warmup" with no stop point is just
  mixing at a smaller learning rate, so the data card must record both the old
  share and the step at which it goes to zero. Downweighting instead of dropping
  is acceptable only with a fixed coefficient recorded in the card; it is not
  warmup.
- **Do not select on a per-chunk diversity verdict.** Cases that fail a gate are
  not a random sample: they concentrate in the boundary region the generator
  handles worst. Retrying without a cap collapses the distribution. Keeping the
  round small and deterministic (8–12 cases, `--strict`) is the mechanism, not
  a threshold to tune.

## 6. One open taxonomy decision, deliberately not taken here

`mechanism` is the head most exposed by this audit. Its five keys describe the
agent's *cognitive source* — an internal state nothing in the trajectory
observes — and the generator derives them from `meta.injection_present`, so the
label is a restatement of the plan (the audited corpus showed 289/289
correspondence). The audit that recommended removing it also recommended making
`severity` / `grounding` conditional heads.

Changing a head is a **consumer-contract change**: every existing case carries
an answer for it, `data/taxonomy.json` is the stable cross-chunk contract, and
`merge` refuses to mix versions. Cutting `mechanism` therefore needs a version
bump (`v4`), a migration note for the already-merged rounds, and a decision
about the qtype tilt (removing it moves `choice` from ~40% to ~25%). That is a
deliberate, separately reviewable change — not something a data-generation pass
should do silently. Until then the head stays, and every chunk that carries it
also records `meta.injection_present`, which is what makes the redundancy
visible instead of invisible.

**The contract change and stopping the harm are decoupled.** Nothing here needs
to wait for a v4 to stop the noise: the consumer can down-weight that head's
loss to 0, or filter its records at load time, without touching the schema or
the data. `prepare_train_data.py` writes one item per question and carries
`qtype`/`instruction` on each, so both are a loader-level filter — the corpus
stays valid either way. Confirm which one the training loop does, because they
are not equivalent in step count: filtering records shortens the epoch, while a
zero weight keeps the step count and wastes the batch slot.

## 7. What the downstream actually consumes (verified against ruhui)

Answers here are checked against `ruhui 0.2.0` (`ruhui/bert/common.py`) and the
reference `prepare_train_data.py`, not assumed:

- **The soft distribution survives into training.** The preparer emits a
  normalised `target` for every item (not just `label`), and
  `ruhui.bert.collate_items` puts `target` into the batch. The exported loss
  helper, `proper_reward`, is a **strictly proper scoring rule on `target`**
  (log score + spherical score, plus RPS for `score`). So the data-side
  distribution is supervision, not decoration: a constant `0.9` teaches
  equally-confident output on every case. Temperature scaling is a monotone
  transform fitted per `(qtype, k)` bucket (`temp_bucket`, with `TEMP_MIN`
  guarding against over-sharpening); it rescales the model globally and cannot
  restore sample-level differences the labels never carried. *If* a training
  loop instead uses CE on the hard `label`, the data-side distribution is inert
  and the earlier "temperature covers it" reading would apply — check which
  loss the loop calls before arguing either way.
- **`qtype` is a conditioning input, not a loss weight.** The model adds
  `type_emb(qtype)` to the hidden states, and `proper_reward` uses `qtype` only
  to add the RPS term for `score`. There is **no per-qtype loss normalisation**
  in the shipped code. The risk is therefore not in the model: it is whether the
  private training loop averages per item (removing a head only removes items)
  or per qtype group (removing one `choice` head re-weights the remaining heads
  relative to `noul`/`score`). Check the loop, not the package.
- **Calibration buckets are per `(qtype, option-count)`.** Dropping `mechanism`
  leaves the `choice` buckets fitted on `harm_kind` alone — the weights are
  unchanged, but the population they are fitted on changes, so refit
  temperatures after any head change.