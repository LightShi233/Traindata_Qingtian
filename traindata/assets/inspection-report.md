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
| no `choice`/`score` head with dual-polarity wording | | |

### Label distribution

```
<key>: <count>   # ragged distribution is expected; a spike is a warning
```

## Diversity / anti-shortcut (run the audit, do not self-assess)

Paste the verdict from `traindata/scripts/audit_diversity.py` (`--json` report) and confirm
each dimension by hand:

| dimension | result | notes |
| - | - | - |
| scenarios (by `text` hash) vs rows | | a 30-row, 3-head pilot is 10 |
| injected share ≤ 50% | | |
| at least one resisted injection (`injected` + `safe`) | | |
| injection surfaces (≥2, not all in body) | | |
| payload styles / registers | | |
| payload text reused across cases | | |
| fixed fingerprints / intent-naming local parts | | domains are never blacklisted |
| org / entity families | | |
| ID formats (≥3) | | |
| scenario skeletons (first words of the user turn) | | |
| thought voice (openers, phrase reuse) | | |
| emails all from `email_library` (no invented domains) | | `--require-library-emails` |
| locked batch honoured (no off-batch addresses) | | `--email-manifest` |
| local-part prefixes not reused (>2 cases) | | compositional + `--ledger` |
| email cross-map: disposable share safe vs unsafe ≤ 30 pts | | |
| dangerous attacks not all on disposable domains | | violation profile: ≥50% freemail |
| email presence parity clean vs injected | | |
| safe-with-temp-mail neutralised in thought (varied wording) | | |
| unsafe justified by an action, not the domain | | |
| email dedup (`REGENERATE EMAIL YOURSELF` clean) | | |
| hard negatives (benign instruction-like text) share | | |
| scenario-hash holdout written & disjoint | | |

### Payload samples (for the reviewer)

Copy three payloads from different cases side by side. If a reader can spot the
shared skeleton at a glance, a model can too:

```
case <id>: <payload>
case <id>: <payload>
case <id>: <payload>
```

## Content quality (manual read)

For each case, read the state and all its questions and answer:

- [ ] the primary label is defensible from the state alone
- [ ] every question has one polarity; no "avoided/cause" flip inside a head
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