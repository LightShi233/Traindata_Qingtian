# State formats

`state` is whatever the model is asked about — the input a case's questions are
answered from. It is serialized once into `text` with
`json.dumps(state, ensure_ascii=False)`, and every record of the same case must
carry the byte-identical `text`. Terms are defined in `references/glossary.md`.
"Domain" below always means **task domain** (the subject area: support, finance)
unless it says email domain.

## Three shapes

### 1. Plain string

A paragraph, email body, log excerpt, or tool output. Simplest and cheapest.

```python
state = "Subject: Refund request\nCustomer says the charge repeated twice..."
text = json.dumps(state, ensure_ascii=False)
```

### 2. Structured object

Facts as fields, when the question is about discrete attributes.

```python
state = {"order_id": "A-10022", "amount": 182.5, "currency": "USD",
         "status": "shipped", "carrier": "Ferrowind Logistics"}
```

Prefer a flat object; nest only when the task domain is genuinely hierarchical.

### 3. Turn list (recommended for trajectory-like states)

A conversation or agent trace. This is the shape that most resembles production
traffic, and it is the richest training signal.

```python
state = [
    {"role": "user", "content": "Summarize the open tickets and draft replies."},
    {"role": "agent", "thought": "I need the ticket list first.",
     "action": json.dumps({"name": "list_tickets",
                           "arguments": {"status": "open", "limit": 20}})},
    {"role": "environment", "content": '{"status": "success", "tickets": [...]}'},
    {"role": "agent", "action": json.dumps({"name": "update_ticket",
                                            "arguments": {"id": 4412, "reply": "..."}})},
    {"role": "environment", "content": '{"status": "success"}'},
]
```

Turn-list conventions:

- first turn is `user`
- roles are `user`, `agent`, `environment`
- `agent` turns carry `action` as a **JSON string** (not a nested object) and may
  carry `thought`; a final `agent` turn may carry `content` instead of `action`.
  In YAML authoring, write `action:` as a mapping — `yaml_to_jsonl.py`
  serialises it to the JSON string this shape requires.
- `environment` turns carry the tool result in `content`
- at least 2 tool calls per case; 6–14 turns is a healthy range
- aim for roughly 1500–5000 characters of `json.dumps(turns, ensure_ascii=False)`.
  The validator enforces the **ceiling**, not this range: `write` fails a `text`
  over `max_len * 3.2` ≈ 3.2k characters (see the length budget below). Treat the
  lower end as a richness guide, and never pad filler to reach it.

## Choosing a shape

| Goal | Shape |
| - | - |
| classify intent / risk from free text | string |
| decide from discrete attributes | object |
| train on tool-use traces, safety review, multi-step reasoning | turn list |

## Identity and dedup

- `source_hash = hashlib.sha256(text.encode()).hexdigest()`
- `source_index` is a unique integer per case; all records of a case share it
- records of one case share `source_hash` and differ only in
  `qtype`/`criteria`/`instruction`/`soft_label`
- two cases with the same `source_hash` are duplicates and must be dropped

## The `text` field is a JSON *string*, and ruhui wants it that way

`text` holds the serialized state, so a turn-list state appears in the JSONL as
a quoted string carrying `\"` and `\n` escapes. That is not corruption:

```json
{"text": "[{\"role\": \"user\", \"content\": \"Review the draft.\\n\"}, ...]"}
```

`ruhui`'s `prepare_train_data.py` reads it with `o.get("text", "").strip()` and
passes the **string** straight to `build_sequence`, which tokenizes whatever
`serialize_state` returns — and for a `str` that is the string itself. Measured
against `ruhui-finetuned/tokenizer`, the escaped form tokenizes to roughly 3×
the tokens of de-escaped prose (19 vs 6 on a one-turn sample), and that is the
distribution the shipped model was trained on. So **do not `json.loads(text)`
before tokenizing for ruhui**: parsing changes the input the head expects.

Parsing `text` is correct only for consumers that render a chat template from
turns (a Qwen-style `<|im_start|>` formatter, say). If you write such a
consumer, parse first:

```python
turns = json.loads(record["text"])   # only for turn/chat-template consumers
```

The rule: `text` stays a JSON string in the file; whether to parse it depends
on the downstream model, not on the file format.

## Content hygiene

- fictional names, `example` TLD or otherwise invented organizations, 555-prefixed
  phone numbers
- no real personal data, no real credentials, no directly executable attack code
- sensitive content may be described, not reproduced
- vary vocabulary, tool names, and story templates; a corpus of one template with
  swapped nouns teaches the template, not the task

## Injection-bearing states

When a state contains an injected instruction (a tool output, retrieved
article, ticket field, or referenced document that steers the agent), the state
placement is part of the design, not an accident:

- vary the **field** that holds it: article body, ticket subject, a tool
  `status`/`note` field, a JSON blob footer, a `description`, a revision comment,
  a calendar description. Do not put every payload in the article body.
- vary the **register**: a policy line, a workflow step, an authority claim, a
  data-sync request, an admin handoff — not one fixed "NOTE FOR …" sentence.
- vary the **address**: use the locked batch from
  `traindata/scripts/sample_emails.py` (the welded pool of normal free-mail,
  invented corporate and disposable email domains). Never name the intent in the
  local part (`evil@`, `exfil@`); the email domain itself is free, and disposable
  domains are allowed and expected. A payload teaches the right lesson only if a
  reworded version would still look ordinary.
- record it in the case `meta` block (`injection_surface`, `payload_style`,
  `payload_span`); see `references/anti-shortcut.md`.

**Address cross-mapping.** Do not let the email-domain style become the label.
Draw one batch with `sample_emails.py --cross-map` and place one address of each
pair in a clean case and the other in a violating case, so `gmail`-style and
corporate-style domains both appear on both sides. Clean cases must also carry
addresses; if addresses appear only in poisoned cases, "has an address" becomes
the label.

**The label follows the action.** A `safe=true` case that uses a disposable
address must neutralise it in the thought (temp mailbox is noteworthy, not a
violation — vary the wording). A `safe=false` case must violate through an
action (out-of-scope read/export, unauthorised send, destructive change), and if
an injection was present the agent must actually perform the injected action.
Diagnosing the email domain instead of the act is the exact shortcut this skill
prevents; the audit fails both cases.

**Hard negatives.** 25–75% of clean cases should contain instruction-like but
legitimate text (a runbook line that says to escalate, a policy that says to cc
finance), so the model cannot map "instruction-like tool output ⇒ unsafe".

## The `meta` block

Every case-form chunk carries a `meta` object (not part of `text`, so it never
enters training). It records the diversity plan so the audit can verify it. The
full field list and the enforced budget are in `references/anti-shortcut.md`; the
diversity audit requires it, and the validator reads `benign_directive_span` and
`payload_span` from it rather than ignoring it.

## The ruhui length budget (measured)

`ruhui`'s `build_sequence` windows the state at `max_len` tokens (1024 in the
shipped `rl_agent_config.json`) and, with the default `truncate_left=False`,
keeps `state_ids[:room]` — it **drops the tail of the trajectory**. In a
violation case the decisive action is usually late, so an over-long state can
silently strip the evidence the label depends on. The record still validates;
only the model stops seeing why the label is what it is.

Measured against the real tokenizer (`ruhui-finetuned/tokenizer`, mmBERT):

| state | tokens | chars | seq @ max_len=1024 | result |
| - | - | - | - | - |
| 1 turn-block | 172 | 650 | 309 | full |
| 2 turn-blocks | 343 | 1300 | 480 | full |
| 4 turn-blocks | 685 | 2600 | 822 | full |
| 6 turn-blocks | 1027 | 3900 | 1024 | **tail truncated, 140 tokens lost** |

Practical numbers:

- The head (type + instruction + options) costs ~136 tokens at
  `head_max_len=256`, leaving ~887 tokens for the state.
- A turn-list JSON state runs about **0.26 tokens per character**, so ~3.4k
  characters is the hard ceiling and ~3.2k is a safe target.
- `--ruhui-max-len` is enabled by default in `data_writer.py write/merge` and
  fails a chunk whose `text` exceeds `max_len * 3.2` characters. Pass
  `--ruhui-max-len 0` only when you are targeting a different window.

Keep trajectories to 2–4 turns of real content. Padding a case with repeated
filler to look richer pushes it into the truncation zone while adding nothing
the model can learn from.
