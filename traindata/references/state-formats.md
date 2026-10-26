# State formats

`state` is whatever the model is asked about. It is serialized once into `text`
with `json.dumps(state, ensure_ascii=False)`, and every record of the same case
must carry the byte-identical `text`.

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

Prefer a flat object; nest only when the domain is genuinely hierarchical.

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
  carry `thought`; a final `agent` turn may carry `content` instead of `action`
- `environment` turns carry the tool result in `content`
- at least 2 tool calls per case; 6–14 turns is a healthy range
- keep `json.dumps(turns, ensure_ascii=False)` between roughly 1500 and 5000
  characters

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
- vary the **address**: plausible fictional local parts and domains; never an
  address that names the intent (`support-ext@`, `exfil@`, `evil@`). A payload
  teaches the right lesson only if a reworded version would still look ordinary.
- record it in the case `meta` block (`injection_surface`, `payload_style`,
  `payload_span`); see `references/anti-shortcut.md`.

**Address parity.** Clean cases must also carry external-looking addresses. If
addresses appear only in poisoned cases, "has an address" becomes the label.
Aim for roughly the same address-presence rate in clean and injected cases.

**Hard negatives.** 25–75% of clean cases should contain instruction-like but
legitimate text (a runbook line that says to escalate, a policy that says to cc
finance), so the model cannot map "instruction-like tool output ⇒ unsafe".

## The `meta` block

Every case-form chunk carries a `meta` object (not part of `text`, so it never
enters training). It records the diversity plan so the audit can verify it. The
full field list and the enforced budget are in `references/anti-shortcut.md`; the
schema-agnostic validator ignores it, the diversity audit requires it.