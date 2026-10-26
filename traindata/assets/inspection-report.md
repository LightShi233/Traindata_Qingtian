# Pilot inspection report

Fill this in after Phase 4 (1–2 chunks) and before scaling. Keep it with the
corpus as part of the data card.

## Scope

- chunks inspected: `<paths>`
- cases / records: `<n> / <n>`
- taxonomy version: `<id>`
- generator model + prompt revision: `<model> / <rev>`

## Schema

| check | result | notes |
| - | - | - |
| every record validated (exit 0) | | |
| `source_hash` matches `text` | | |
| case records share `text`/`hash`/`source_index` | | |
| no extra or missing fields | | |

## Labels

| check | result | notes |
| - | - | - |
| `label == argmax` | | |
| all distributions normalized | | |
| no one-hot / uniform | | |
| primary is `0.9` (or documented `0.7`) | | |
| label coverage across keys | | |
| coverage per domain | | |

### Label distribution

```
<key>: <count>   # ragged distribution is expected; a spike is a warning
```

## Content quality (manual read)

For each case, read the state and all its questions and answer:

- [ ] the primary label is defensible from the state alone
- [ ] the advantageous/clean reading of contrasting labels is clear
- [ ] `choice` option sets match the taxonomy exactly
- [ ] the `text` reads like real traffic, not a template with swapped nouns
- [ ] at least two tool calls / expected structure present
- [ ] length within the target band
- [ ] fictional data only; no real names, credentials, or executable payloads

Notes per case:

```
case <id>: <verdict and any correction needed>
```

## Leakage

| check | result |
| - | - |
| max 8-gram Jaccard vs train corpus | |
| max 8-gram Jaccard vs calibration corpus | |
| max 8-gram Jaccard vs test corpus | |
| duplicates found | |

## Verdict

- [ ] approved to scale
- [ ] needs prompt change (describe)
- [ ] needs taxonomy change (describe)
- [ ] rejected (describe)

## Corrections applied

```
<prompt/taxonomy/generator changes made, with the revision id>
```