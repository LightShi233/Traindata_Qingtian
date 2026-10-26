# Soft-label data card

- source: `data/chunks/chunk_0003.jsonl, data/chunks/chunk_0004.jsonl`
- taxonomy version: `ruhui-triage-v1`
- cases / records: **10 / 30**
- text length (min / median / max): 3565 / 3974.5 / 4197
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
| `own_reasoning` | 2 |
| `injection` | 2 |
| `other` | 2 |
| `misdirection` | 2 |
| `faulty_input` | 2 |

### noul

| label | count |
| - | - |
| `false` | 5 |
| `true` | 5 |

### score

| label | count |
| - | - |
| `0` | 3 |
| `1` | 3 |
| `2` | 2 |
| `3` | 2 |

## Checks

- schema and soft-label validation: passed (0 errors)
- case-group integrity: passed (10 cases)
- intra-corpus near-duplicate threshold: 0.55
- leakage threshold: 0.3 (30 reference records, explicit --leak)

## Known limitations

- Generated corpus: verify a pilot by hand before scaling (see assets/inspection-report.md).
