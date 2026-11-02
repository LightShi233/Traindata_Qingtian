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

**A number without its provenance is not a number.** Two measurements of "the
same" probe are incomparable if any of these differ, so cite all three:

    (n = 356 cases, 8 measured features, corpus = local copy @ 2026-11-02)

- *n* — cases, not records;
- *feature set* — this probe measures **8**: destructive verb, egress verb,
  admin verb, tool-call count, log length, hedge count, address presence, agent
  text volume. A 5-feature probe (the external audit's set) produces a different
  AUC on the same corpus;
- *corpus identity* — version and copy; a subset is not the corpus.

Concrete example of the failure: 0.886 came from 356 cases × 8 features on a
partial copy, 0.817 from 756 cases × 5 features on the full corpus. Neither
number refutes the other, and "the AUC is about 0.8" is a meaningless sentence.

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
  without blocking: a per-round chunk is too small to threshold honestly, and
  the number is a consistency measure rather than a verdict (see §5).

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
4. **Re-measure — but not with `follow_corrected`.** That number is
   **self-fulfilling**: the probe is fitted on the corpus, so once the corpus
   carries the corrected labels it learns them and agrees out of fold. When the
   corpus is *not* corrected, `follow_corrected` is computed against the same
   label as `follow_original` and the two are identical. The tool detects both
   cases and prints `[NOT A SUCCESS CRITERION]`; do not report the number as
   progress.

   Two measurements are valid, and they answer different questions:
   - **Label-flip sensitivity** (`--predictions-out` twice + `--compare`): the
     same texts, two runs that differ only in their label source. The fraction
     of predictions that flip is evidence that the decision is driven by the
     label, not the text — a clean existence proof of the shortcut, because the
     text is held fixed by construction.
   - **AUC after correction**: expect it to *rise* (a reviewed corpus measured
     0.821 → 0.859). Corrections are usually justified by exactly the features
     the probe measures (a completed egress), so the corrected labels are more
     surface-consistent, not less. AUC is a **label-surface consistency** number,
     never a data-quality score; a rise is not a regression and a drop is not a
     win.

   What neither number shows is the thing that matters in deployment: whether
   the model still fails when the surface feature and the truth disagree. That
   needs a targeted probe set (surface-present, truth-absent), which is §5.

   **Two measurements that keep a correction pass honest.** Both come from an
   external study of this pipeline's output (2026-11-02); re-measure them, do
   not quote them as constants:

   - **Net gain is `N(2p − 1)`**, where `N` is the number of flips and `p` the
     precision of the filter that proposed them (all-or-nothing flips: the `p`
     fraction that were truly wrong gains, the rest loses). Measured `p` for the
     audited recaller was 51/136 ≈ 37%, so bulk correction with that filter is
     **negative**: `−0.26N`. The "program recalls, human decides" rule is
     arithmetic, not taste. But `p` is unknown and must be estimated, so the
     threshold is the **95% Wilson lower bound of `p`, not the point estimate**:
     `p̂ > 0.5` at n=50 still passes a filter whose true precision may be 50%.
     Require `LB > 0.5` on a reviewed sample, and never estimate `p` from the
     cases that motivated the filter:

     | reviewed n | min true flips k | p̂ | Wilson LB |
     | - | - | - | - |
     | 20 | 15 | 0.75 | 0.531 |
     | 30 | 21 | 0.70 | 0.521 |
     | 50 | 32 | 0.64 | 0.501 |
     | 200 | 114 | 0.57 | 0.501 |

     The lower bound is what makes a small sample expensive: at n=20 the filter
     must be right 15 times, at n=50 it must be right 32 times.
   - **The effect is a phase transition, not a learning curve.** Holding out
     the corrected cases gives `follow_corrected` 48%, versus 94% when the same
     cases are scored in-fold. As the corrected share grows, it stays flat at
     ~34–43% up to ~6% coverage, then jumps to ~84–93% by ~9–12%. The corrected
     cases share the probe's features (clean + egress/destructive verbs), so
     flipping them in bulk drags a linear boundary across the whole region.
     This is the **probe** being re-aimed, not evidence about the trained
     model — but it does mean the corrected corpus remains surface-recoverable,
     so a shortcut learner can still score well on it without learning why the
     new labels are right. Do not read the far end as convergence.

This does not eliminate the shortcut. It compresses the gap between the shortcut
and the truth toward zero, which is why it does not conflict with the ban on
claiming elimination in §5. It also does not replace the execution layer: a
corrected corpus is still labelled from the plan for every case a human did not
touch.

**Keep a floor of contested cases, or the three tiers degenerate.** If every
case finishes at `0.90`, the `0.70` / `0.55` tiers exist in the schema and feed
nothing. After a review pass, aim to retain **at least ~2% of the corpus at a
lowered primary** — those are the only rows that teach the model that evidence
can be ambiguous. This is a review-pass quota, not a generation gate: at
generation time there is no adjudication to base a lower value on (see §3), so
the audit reports the spread and warns only when corrections exist.

**The uncertainty is already recoverable for these cases.** An adjudication
record supplies a real difficulty tier, which is what a lowered primary is for:
two reviewers agree on the flip → `0.90`; they directly conflict → `0.70`;
arbitration records a genuine downgrade → `0.55`. Set `primary` on those cases
and `meta.primary_basis` to the arbitration fact (`references/soft-labels.md`).
This is the one situation where a non-constant primary is evidence-backed rather
than invented, and it matters more than usual here: the consumer scores a
strictly proper rule on the distribution, so a corpus of constant `0.9` trains
uniform over-confidence and a fitted temperature cannot undo it.

## 5. What this cannot fix (and the honest claim)

The probe's AUC is **label-surface consistency**, not a quality score. Lowering
it does **not** prove the model learned the rule — it proves the measured
features are no longer separately predictive. Raising it does **not** mean the
data got worse either; a correction pass usually raises it, because corrections
are justified by the same features the probe sees. A model can still be right
for the wrong reason; only an intervention can show otherwise:

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

**A missing head is a version boundary, not missing data.** Early chunks in the
private corpus (`chunk_0001`, `chunk_0002`, `chunk_0004`) carry only 3 of the 5
heads. Their round manifests say `taxonomy_version: ruhui-triage-v2` — the v2
taxonomy had three questions, so those rows are complete for the contract they
were authored against. They are not a generator defect, not random missing
values, and not something to repair: validating them against v3 must fail, and
`--require-complete-cases` failing there is correct. The consequences follow:

- **Do not mix versions in one training corpus.** Under per-item averaging a
  v2 case contributes 3 items instead of 5, so it is silently down-weighted to
  60%; its rows also fall outside the v3 `(choice,7)` / `(score,4)` buckets.
  `merge` already refuses a mixed round. `integrate`/`split` now **hard-fail**
  when the input carries more than one **head shape**, where shape is the head
  ids, each head's option keys (`severity{0,1,2,3}`), **and fingerprints of the
  instruction and criteria text**. Head names alone miss a v4 that keeps the
  same five names but changes an option count; keys alone miss a v4 that only
  rewords the question. All of it is carried verbatim in every record, so the
  check needs no manifest and no knowledge of the data root. Case-form input
  fails earlier (a missing head is a validation error even without
  `--require-complete-cases`); the record-form path — which is schema-agnostic —
  gets the explicit `head_set_errors` check before anything is written. The
  `INFO` below is a diagnostic, not the safeguard.
- **What is still invisible: a change outside the record.** If a version change
  touches neither the head ids, the option keys, the instruction, nor the
  criteria — e.g. how the trainer interprets a head — the shape is identical and
  the check passes. That is not a data mix, but it is also not something a
  corpus gate can see. Keep grouping integrations by `taxonomy_version`
  (`data_writer.py check` names the mismatched chunks); the shape check is the
  fallback, not the policy. The one assumption to state: it treats "one head =
  one instruction template". A taxonomy that intentionally paraphrases a
  question per case would trip it, and the current taxonomy does not (verified:
  one instruction hash per head across the corpus).
- **`data_writer.py check` prints the mismatches** (`INFO [taxonomy-version]`,
  non-failing) so a build run can see which chunks belong to another version
  without opening the manifests.
- **Already-merged training files need the same check by hand.** The gate above
  protects new builds, not history: a `soft_*.jsonl` produced before this check
  may already contain 3-head cases, and if it was trained on, those rows are
  part of the model's history and cannot be "un-mixed". Audit them with
  `validate_and_integrate.py --check 'data/rounds/*/soft_*.jsonl'
  --taxonomy data/taxonomy.json --require-complete-cases`; record the result in
  the data card instead of assuming a clean corpus.
- **CI needs no exemption for history.** The workflow gates only the diff
  (`git diff --base..HEAD -- 'chunks/chunk_*.jsonl'`); already-published v2
  chunks are never re-validated against v3. The failure only appears if someone
  points the v3 validator at a whole-root glob by hand.
- **Rejection is whole-case, not per-head.** The validator errors the case and
  `expand_case` raises on a missing question — it never emits the 3 good heads
  and drops 2. That is deliberate: a half-labelled case is ambiguous (was the
  head absent, or was it skipped?), and silently keeping the remaining items is
  how a version mismatch becomes invisible.

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
  in the shipped code. Whether removing a head re-weights the rest depends on
  the training loop, which is not in the wheel; the loop need not be readable to
  answer it — **run two batches and compare the loss**:
  `A = 10 noul + 10 choice`, `B = 10 noul + 20 choice` with identical items.
  A different loss means per-item averaging (dropping a head only removes its
  items). An unchanged loss means per-qtype grouping (dropping one `choice`
  head re-weights the remaining heads against `noul`/`score`).
- **The corpus is balanced on purpose; the calibration prior is not the
  deployment prior.** The audit caps `safe=false` at half, and in practice every
  chunk sits at exactly 50/50. That is a training choice (it removes the trivial
  "majority class" shortcut), not an estimate of how often violations occur.
  Temperature scaling is monotone and cannot change the base rate: a temperature
  fitted on a 50/50 split is calibrated only at 50/50. For a long-tailed
  deployment prior, reweight the calibration split before fitting or apply a
  logit offset of `log(prior_new / prior_here)`, and report ECE at both priors.
  The data card now states this next to the label distribution.
- **Calibration buckets are per `(qtype, option-count)`.** The heads split as
  `mechanism (choice, 5)`, `harm_kind (choice, 7)`, `severity (score, 4)`,
  `grounding (score, 4)`. So dropping `mechanism` does **not** change
  `harm_kind`'s fitted population — different `k`, different bucket, they never
  shared one. The truly shared bucket is `severity` + `grounding`; refit it
  after any change to either.
- **A conditional head needs a conditional bucket, and one branch must not be
  fitted at all.** If `severity` is conditioned on `is_violation` (§6), the
  clean branch is `severity ≡ 0` by this taxonomy's own definition ("none: no
  impact") — variance zero, so a temperature fitted there has a zero
  denominator. Split the buckets as `(score, true, 3)`: the violation branch
  never takes level 0, so it is a 3-way fit — and `(score, false, 1)`: a
  constant, published as `1.0` without fitting. Do not merge the two into one
  `(score, 4)` bucket; that fits a degenerate half and inflates the estimate's
  variance. The same applies to `grounding`, whose clean branch is almost
  constant.
- **Missing labels are not a special case.** A score head stored as `null`
  (which an external audit reported 13 times) is not a valid level: the current
  validator rejects it (`不是有效等级`), and `expand_case` would refuse to emit
  an argmax over it. Those rows are legacy data from before that check and must
  be excluded or corrected, not counted in a bucket.