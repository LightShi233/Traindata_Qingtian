# Soft-label data card

- source: `data/chunks/chunk_0001.jsonl, data/chunks/chunk_0002.jsonl`
- taxonomy version: `ruhui-triage-v1`
- cases / records: **10 / 30**
- text length (min / median / max): 2251 / 2852.5 / 4092
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
| `injection` | 3 |
| `misdirection` | 2 |
| `faulty_input` | 2 |
| `own_reasoning` | 2 |
| `other` | 1 |

### noul

| label | count |
| - | - |
| `false` | 6 |
| `true` | 4 |

### score

| label | count |
| - | - |
| `1` | 4 |
| `2` | 2 |
| `0` | 2 |
| `3` | 2 |

## Checks

- schema and soft-label validation: passed (0 errors)
- case-group integrity: passed (10 cases)
- intra-corpus near-duplicate threshold: 0.55
- leakage threshold: 0.3 (0 reference records, no baseline)
- split leakage (train vs calibration vs test, 8-gram Jaccard @ 0.30): 0 duplicates, exit 0
- splits are case-disjoint: `data/split/split_manifest.json` (seed 13, ratios 0.8/0.1/0.1)

## Pipeline notes

- Generated via 2 subagent chunks (chunk_0001: cases 000101–000105, chunk_0002: cases 000201–000205); both chunks passed `scripts/validate_and_integrate.py --expand --require-complete-cases` with min-len 1500 / max-len 5000 on the first attempt, zero retries.
- Each case produced by one subagent (5 cases ≤ the 10-sample-per-run cap); per-run generation used a subagent plus the validator (documented exception to the cap).
- Domain: fictional customer-support / ops-triage agent trajectories (turn-list state). Story skeletons deliberately disjoint between chunks (different orgs, tools, incidents).
- Ambiguity: chunk-1 case 000105 and chunk-2 case 000205 are deliberately dual-reading cases; labeled with the standard 0.9 primary (no sub-0.9 distribution used).

## Known limitations

- 10 cases is a pilot-scale corpus: label coverage is thin (choice: 5 keys, 1–3 each; score: 4 levels, 2–4 each) and train/calibration/test splits are 8/1/1 cases.
- Generated corpus: verify a pilot by hand before scaling (see assets/inspection-report.md).
