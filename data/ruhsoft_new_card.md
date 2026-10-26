# Soft-label data card

- source: `data/chunks/chunk_0003.jsonl, data/chunks/chunk_0004.jsonl`
- taxonomy version: `ruhui-triage-v2`
- cases / records: **10 / 30**
- text length (min / median / max): 2022 / 3040.0 / 3752
- primary probability (min / max): 0.900 / 0.900

## Question types

| qtype | records |
| - | - |
| `choice` | 10 |
| `noul` | 10 |
| `score` | 10 |

## Label distribution

### choice

| label | count |
| - | - |
| `injection` | 5 |
| `misdirection` | 2 |
| `none` | 1 |
| `faulty_input` | 1 |
| `own_reasoning` | 1 |

### noul

| label | count |
| - | - |
| `false` | 8 |
| `true` | 2 |

### score

| label | count |
| - | - |
| `0` | 6 |
| `1` | 2 |
| `3` | 1 |
| `2` | 1 |

## Checks

- schema and soft-label validation: passed (0 errors)
- case-group integrity: passed (10 cases)
- intra-corpus near-duplicate threshold: 0.55
- leakage threshold: 0.3 (30 reference records, explicit --leak)

## Pipeline notes

- Generated via 2 subagent chunks (chunk_0003: cases 000301-000305, chunk_0004: cases 000401-000405); both chunks passed `traindata/scripts/validate_and_integrate.py --expand` (min-len 1500 / max-len 5000) and `traindata/scripts/audit_diversity.py --require-injection --strict` (exit 0, 0 warnings) before acceptance.
- Diversity plan: `data/diversity_plan_v2_0003_0004.json` (written before generation, per the skill contract).
- Label repair rounds after the first audit: (1) payload template "before replying" reused in 2 payloads -> both payloads reworded; (2) severity re-leveled to {0:6, 1:2, 2:1, 3:1} with the level-2/3 slots justified from the turns text; (3) mechanism "own_reasoning" missing at strict -> case 000405 re-labeled from faulty_input to own_reasoning (its faulty data was verified, not the deciding factor; the agent's own deadline-scope judgment produced the fabricated completion), leaving faulty_input covered by case 000401.
- Not merged with the existing corpus: `data/ruhsoft_new_soft.jsonl` is a standalone 30-record file; leakage was checked against `data/ruhui_soft_all.jsonl` at 8-gram Jaccard threshold 0.30 during integration (exit 0).

## Split and holdout

- `data/split_new/`: deterministic case-disjoint train/calibration/test (8/1/1 cases, seed 13); split-to-split leakage check exited 0 both directions.
- `data/pilot_holdout/`: scenario-hash holdout of 2 of the 10 scenarios (from `audit_diversity.py --holdout 2`, seed 13). The holdout scenarios are never trained on or used for calibration tuning; dev scenarios are the other 8.
- Pilot usage rule: these 10 cases are a pilot-scale prompt gate. If any of them enter training, blend them below ~5% of the total and reduce the epoch budget; state the ratio in the data card at blend time.

## Known limitations

- 10 cases is pilot-scale: choice key coverage is thin (injection 5, others 1-2 each) and score level 0 is 60% (allowed, not >60%); coverage should be re-checked at scale.
- Generated corpus: verify a pilot by hand before scaling (see `data/inspection_report_v2.md`).
