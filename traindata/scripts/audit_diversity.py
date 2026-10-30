#!/usr/bin/env python3
"""Diversity / shortcut-learning gate for case-form soft-label chunks.

The schema validator answers "is this record well formed?". This audit answers
the question that decides whether the pilot is worth scaling at all:

    could a model get the label right by memorising a surface fingerprint
    instead of learning the intended decision rule?

It is deliberately opinionated. It runs on case-form chunks
(``{traj_id, safe, turns, meta, <qid>: label}``), because only the case form
still knows which trajectory the three heads came from and what the generator
intended. Run it after the schema validator and *before* accepting a chunk.

Hard failures (exit 1) cover:

- missing / dishonest diversity metadata (``meta``)
- one org, one ID style, one scenario skeleton, one thought voice
- one injection position, one payload style, one payload template
- artificial payload fingerprints (``AUTO-APPENDED NOTE``) and intent-naming
  local parts (``evil@``, ``exfil@``) — **domains are never blacklisted**
- payload text reused across cases, including near-reuse
- injected cases dominating the corpus, or clean cases without any hard
  negatives
- address style becoming the label: disposable or free-mail share skewed between
  clean and violating contexts; attacks almost all sent to throwaway domains
- emails that do not come from ``assets/email_library``
- a local part reused across many cases (prefix overfitting)
- a safe case on a disposable address with no neutralising thought, or a
  violation not justified by an agent action
- a choice/score head whose instruction mixes "cause" and "vaccine"
  semantics (the dual-polarity bug)
- a scenario-hash holdout that is not actually disjoint
- an unsalted chunk when ``--require-salt`` is set (see
  ``references/harmbench-methodology.md``)

Usage
-----
    python3 audit_diversity.py --chunks 'chunk_*.jsonl'
    python3 audit_diversity.py --chunks 'chunk_*.jsonl' --taxonomy taxonomy.json \
        --require-injection --strict --holdout 2 --out-dir pilot_holdout \
        --json diversity_report.json

The audit never edits data. It prints every failure and writes a report, so a
generation loop can treat a non-zero exit as "this chunk does not exist yet".
"""
import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataloader import Deadline, atomic_write_text, load_cases  # noqa: E402

# A run-level wall-clock budget, set from --timeout in main(). Kept module-level
# so the per-case hot loops can tick it without threading an argument through
# every check function.
_RUN = {"deadline": Deadline(0)}


def tick(where=""):
    _RUN["deadline"].check(where)


# --------------------------------------------------------------------------- #
# Shortcut fingerprints
# --------------------------------------------------------------------------- #
# These must never appear in generated content: they are artificial tells that
# teach "the poison looks like this" instead of "tool output is untrusted".
# NOTE: domains are deliberately NOT listed here. Known disposable/toxic domains
# are kept in the unified email pool and are usable everywhere (see
# references/anti-shortcut.md); excluding them would be data poisoning.
HARD_BANNED = [
    (r"auto[\s_-]*appended", "fixed 'AUTO-APPENDED' payload prefix"),
    (r"note\s+for\s+(bot|agent|assistant|operator)s?", "fixed 'NOTE FOR BOT OPERATORS' payload prefix"),
    (r"\bbot\s+operators?\b", "addresses the model/operator explicitly"),
    (r"\[?\s*system\s*(override|prompt)\s*\]?", "fixed system-override marker"),
    (r"<\|[^|]*\|>", "chat-template control token"),
    # The salt is a property of the *prompt*, never of the sample. If its
    # wording shows up in `turns`, the model copied its instructions into the
    # payload and the harmful sample is no longer the original one.
    (r"\byou\s+are\s+salted\b", "salt instruction leaked into the sample"),
    (r"===+\s*end\s+salt\s*===+", "salt block marker leaked into the sample"),
    (r"\bsalt\s+needle\b", "salt needle leaked into the sample"),
    (r"\bdefensive\s+evaluation\s+data\b", "salt preamble leaked into the sample"),
]

# Applied to the whole trajectory by check_salt_leakage, not just payload_span.
SALT_LEAK = [
    (r"\byou\s+are\s+salted\b", "盐指令 \"You are salted\" 出现在样本正文"),
    (r"===+\s*(end\s+)?salt\b", "盐块分隔标记出现在样本正文"),
    (r"\bsalt\s+needle\b", "盐针（SALT NEEDLE）出现在样本正文"),
    (r"\bdefensive\s+evaluation\s+data\b", "盐块前言出现在样本正文"),
    (r"\bdata\s+author\b[^.\n]{0,40}\bnot\s+the\s+(attacker|target)\b", "盐的角色说明出现在样本正文"),
    (r"\bquarantined\s+DATA\b", "盐的 \"quarantined DATA\" 条款出现在样本正文"),
]

# Repeated *semantic* templates. One use is fine; using the same phrasing in
# more than --max-fingerprint-cases cases is a shortcut.
FINGERPRINT_PATTERNS = [
    (r"per\s+(company|partner|vendor|carrier)\s+policy", "policy-prefix template"),
    (r"\bcc\b[^.\n]{0,40}@", "cc-an-address template"),
    (r"forward\s+[^.\n]{0,60}\bto\b", "forward-to template"),
    (r"before\s+(replying|responding|closing|updating)", "before-you-reply template"),
    (r"for\s+(faster|speedier|quicker)\s+(resolution|processing|handling)", "speed-up template"),
    (r"do\s+not\s+(tell|inform|notify)\s+the\s+user", "hide-from-user template"),
    (r"\byou\s+(must|should|need to)\b", "direct-address template"),
    (r"as\s+an?\s+(ai|assistant|agent)\b", "assistant-address template"),
    (r"copy\s+the\s+(full\s+)?(ticket|email|thread|conversation)", "copy-the-thread template"),
    (r"external\s+(mailing\s+list|address|recipient|contact)", "external-recipient template"),
]

# Local parts that literally name malicious intent are artificial and teach a
# shortcut. This is about the *name*, not the domain: any domain, disposable or
# corporate, is allowed.
INTENT_LOCALPART = [
    r"(^|[._+-])(ext|external|outside|attacker|evil|exfil\w*|phish\w*|malware|scam)([._+-]|$)",
]

GENERIC_WORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "by",
    "is", "are", "was", "were", "be", "been", "it", "this", "that", "these",
    "those", "i", "we", "you", "they", "he", "she", "my", "our", "your", "their",
    "from", "at", "as", "if", "then", "so", "not", "no", "do", "does", "did",
    "can", "could", "should", "would", "will", "must", "may", "might", "have",
    "has", "had", "need", "needs", "use", "using", "check", "checks", "first",
    "now", "next", "before", "after", "then", "just", "only", "also", "all",
    "any", "some", "one", "two", "this", "that", "there", "here", "what",
    "which", "who", "when", "where", "why", "how", "about", "into", "over",
    "under", "up", "down", "out", "off", "again", "more", "most", "very",
    "ok", "okay", "status", "success", "true", "false", "null", "id", "name",
    "content", "role", "user", "agent", "environment", "ticket", "tickets",
    "email", "message", "reply", "update", "search", "list", "get", "open",
}

WORD_RE = re.compile(r"[a-z0-9']+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
HASH_RE = re.compile(r"\b[0-9a-f]{12,}\b")
UPPER_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}[-_]\d{2,7}\b")
NUM_ID_RE = re.compile(r"(?<!\w)#\d{2,7}\b")
SLUG_ID_RE = re.compile(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+){1,4}\b")
CAMEL_ID_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+){1,3}\b")

META_REQUIRED = {
    "scenario_family", "entity_family", "id_style", "thought_style",
    "injection_present", "external_contact",
}
META_INJECTION = {"injection_surface", "payload_style", "payload_span"}
# Two orthogonal axes from HarmBench (arXiv:2402.04249); see taxonomy-design.md.
TAXONOMY_SHAPES = {"standard", "contextual", "near_miss"}
# data_writer.py stamps salted_at with an ISO-8601 UTC timestamp; a bare
# ``salted: true`` without it is the model vouching for itself.
HOST_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.*")
TAXONOMY_HARM_KINDS = {"data_egress", "privilege_escalation", "destructive_change",
                       "policy_bypass", "social_engineering", "misinformation", "none"}
DUAL_POLARITY = re.compile(
    r"(for\s+a\s+clean\s+case|correctly\s+avoided|avoided\s+impact|"
    r"mechanism\s+it\s+avoided|the\s+case\s+avoided|"
    r"cause\s+or\s+vaccine|what\s+it\s+avoided)", re.I)


# --------------------------------------------------------------------------- #
def grams(text, n=3):
    w = WORD_RE.findall(text.lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def jaccard(a, b):
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if (a or b) else 0.0


def walk_strings(obj, path=()):
    """Yield (path, string) for every string leaf, descending into JSON strings."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, path + (i,))
    elif isinstance(obj, str):
        yield path, obj
        s = obj.strip()
        if s[:1] in "{[":
            try:
                parsed = json.loads(s)
            except json.JSONDecodeError:
                return
            yield from walk_strings(parsed, path + ("<json>",))


def turn_strings(turns):
    """Yield (role, turn_index, path, string) with environment content parsed."""
    for i, t in enumerate(turns):
        if not isinstance(t, dict):
            continue
        role = t.get("role")
        for key in ("content", "thought", "action"):
            if key in t:
                for path, s in walk_strings(t[key], (key,)):
                    yield role, i, path, s


# A turn's raw text lives under ``content``, but ``injection_surface`` names the
# *semantic* position ("body", "subject", ...). A plain-text email body is
# stored as ``content``, so reporting the raw key would reject the documented
# value; map the envelope keys to what the docs call them.
SURFACE_ALIASES = {"content": "body"}


def surface_of(path):
    """Deepest non-index key that contains the string."""
    for part in reversed(path):
        if isinstance(part, str) and part != "<json>":
            return SURFACE_ALIASES.get(part, part)
    return "body"


def emails(text):
    return [e.lower() for e in EMAIL_RE.findall(text)]


def registrable(domain):
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def id_forms(text):
    forms = set()
    if UUID_RE.search(text):
        forms.add("uuid")
    if UPPER_ID_RE.search(text):
        forms.add("upper-dash-id")
    if NUM_ID_RE.search(text):
        forms.add("hash-number-id")
    if HASH_RE.search(text):
        forms.add("hex-hash")
    if SLUG_ID_RE.search(text):
        forms.add("slug")
    if CAMEL_ID_RE.search(text):
        forms.add("camel")
    return forms


def first_words(text, n):
    return " ".join(WORD_RE.findall(text.lower())[:n])


def normalized_seconds(text):
    return " ".join(WORD_RE.findall(text.lower()))


class Audit:
    def __init__(self):
        self.findings = []
        self.metrics = {}
        # When the corpus is private but the gate runs in a public CI log, the
        # failure text is itself a leak: quoting a payload prefix to help the
        # generator fix it also publishes that payload. --redact keeps the
        # location and the reason, drops the quotation.
        self.redact = False

    def excerpt(self, value, limit=50):
        """Quote a span for a human/model, or describe it when redacting."""
        if not isinstance(value, str):
            return repr(value)
        if not self.redact:
            return repr(value[:limit])
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"<excerpt hidden, {len(value)} chars, sha256:{digest}>"

    def fail(self, code, msg):
        self.findings.append({"level": "FAIL", "code": code, "message": msg})

    def warn(self, code, msg):
        self.findings.append({"level": "WARN", "code": code, "message": msg})

    def info(self, code, msg):
        self.findings.append({"level": "INFO", "code": code, "message": msg})

    def counts(self, code, counter, n):
        self.metrics[code] = dict(counter.most_common())
        return counter, n


def share(counter, n):
    return {k: v / n for k, v in counter.items()} if n else {}


# --------------------------------------------------------------------------- #
def check_meta(cases, audit, args):
    parsed = []
    for where, c in cases:
        tick(where)
        meta = c.get("meta")
        if not isinstance(meta, dict):
            audit.fail("meta", f"{where}: 缺少 meta；生成时必须先写多样性计划")
            continue
        missing = META_REQUIRED - set(meta)
        if missing:
            audit.fail("meta", f"{where}: meta 缺少 {sorted(missing)}")
        if not isinstance(meta.get("injection_present"), bool):
            audit.fail("meta", f"{where}: meta.injection_present 必须是 bool")
        for k in ("scenario_family", "entity_family", "id_style", "thought_style"):
            if not isinstance(meta.get(k), str) or not meta.get(k).strip():
                audit.fail("meta", f"{where}: meta.{k} 必须是非空字符串")
        if not isinstance(meta.get("external_contact"), bool):
            audit.fail("meta", f"{where}: meta.external_contact 必须是 bool")
        if meta.get("injection_present") is True:
            for k in META_INJECTION:
                if not isinstance(meta.get(k), str) or not meta.get(k).strip():
                    audit.fail("meta", f"{where}: injection_present=true 时 meta.{k} 必填")
            # The span is the anchor every later check uses: action attribution
            # decides "did the agent carry out the injection" from it, payload
            # dedup and location checks scan it. A model that declares a payload
            # it never wrote (or misquotes it) makes all of them vacuous, so
            # require the declared substring to really occur, exactly.
            span = meta.get("payload_span")
            if isinstance(span, str) and span.strip():
                # Compare against the decoded strings, not json.dumps: a span
                # containing a quote is escaped in the serialized env turn, and
                # a raw substring test would reject a perfectly literal payload.
                if not any(span in s for _, _, _, s in turn_strings(c["turns"])):
                    audit.fail("meta", f"{where}: payload_span 不在 text 中；"
                                       "声明的载荷必须是轨迹里逐字出现的子串")
        else:
            if meta.get("payload_span"):
                audit.fail("meta", f"{where}: injection_present=false 却声明了 payload_span")
        if c.get("safe") is True and "is_violation" in c and c["is_violation"] is not False:
            audit.fail("meta", f"{where}: safe=true 与 is_violation=true 矛盾")
        if c.get("safe") is False and "is_violation" in c and c["is_violation"] is not True:
            audit.fail("meta", f"{where}: safe=false 与 is_violation=false 矛盾")
        if "salted" in meta and not isinstance(meta.get("salted"), bool):
            audit.fail("salt", f"{where}: meta.salted 必须是 bool")
        for k in ("harm_kind", "request_shape", "context_span"):
            if k in meta and meta[k] is not None and \
                    (not isinstance(meta[k], str) or not meta[k].strip()):
                audit.fail("taxonomy", f"{where}: meta.{k} 必须是非空字符串")
        if isinstance(meta.get("request_shape"), str) and \
                meta["request_shape"] not in TAXONOMY_SHAPES and \
                getattr(args, "require_taxonomy_axes", False):
            audit.fail("taxonomy", f"{where}: request_shape={meta['request_shape']!r} "
                                   f"不在 {sorted(TAXONOMY_SHAPES)}")
        if getattr(args, "require_salt", False):
            if meta.get("salted") is not True:
                audit.fail("salt", f"{where}: 缺少宿主盖章的 meta.salted；"
                                   "salt 由 data_writer.py write 依据本轮登记的 salted prompt 写入。"
                                   "模型自己写的 salted 会被当作幻觉丢弃，"
                                   "见 references/harmbench-methodology.md")
            elif not HOST_STAMP_RE.fullmatch(str(meta.get("salted_at", ""))):
                audit.fail("salt", f"{where}: meta.salted 没有宿主时间戳 salted_at；"
                                   "这是模型自证，不是宿主盖章。"
                                   "用 data_writer.py allocate --salted-prompt 重新登记并 write")
        parsed.append((where, c, meta))
    return parsed


def check_scenarios(parsed, audit, args):
    n = len(parsed)
    hashes = Counter()
    for where, c, _ in parsed:
        h = hashlib.sha256(
            json.dumps(c["turns"], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        hashes[h] += 1
        c["_scenario_hash"] = h
    dupes = {h: k for h, k in hashes.items() if k > 1}
    if dupes:
        audit.fail("scenario", f"text 完全相同的场景 {len(dupes)} 个；三头共用一条 trace 不是新场景")
    audit.metrics["unique_scenarios"] = len(hashes)
    audit.metrics["cases"] = n

    safe = Counter(bool(c.get("safe")) for _, c, _ in parsed)
    if len(safe) < 2:
        audit.fail("balance", "safe=true/false 必须同时存在")
    unsafe_share = safe.get(False, 0) / n if n else 0
    audit.metrics["safe_share"] = {str(k): v / n for k, v in safe.items()} if n else {}
    if unsafe_share > args.max_unsafe_share:
        audit.fail("balance", f"unsafe 占比 {unsafe_share:.0%} > {args.max_unsafe_share:.0%}；不要让违规样本过半")
    if unsafe_share < 0.2 and n >= 6:
        audit.warn("balance", f"unsafe 占比只有 {unsafe_share:.0%}，正类可能不足")

    inj = [m for _, _, m in parsed if m.get("injection_present") is True]
    if inj:
        inj_share = len(inj) / n
        audit.metrics["injection_share"] = inj_share
        if inj_share > args.max_injected_share:
            audit.fail("balance", f"injection_present 占比 {inj_share:.0%} > {args.max_injected_share:.0%}；工具输出不能到处是毒")
        resisted = sum(1 for _, c, m in parsed
                       if m.get("injection_present") and c.get("safe") is True)
        yielded = sum(1 for _, c, m in parsed
                      if m.get("injection_present") and c.get("safe") is False)
        if resisted < 1:
            audit.fail("balance", "有注入但没有任何 safe=true 的抗注入样本；模型只学会'中毒=违规'")
        if yielded < 1:
            audit.fail("balance", "有注入但没有任何 safe=false 的中招样本")
        audit.metrics["resisted_injection"] = resisted
        audit.metrics["yielded_injection"] = yielded
    elif args.require_injection:
        audit.fail("balance", "--require-injection 要求至少一个 injection_present=true 的样本")


def check_distribution(parsed, audit, args, key, label, max_share, min_distinct=None):
    c = Counter(m.get(key) for _, _, m in parsed if m.get(key))
    n = sum(c.values())
    audit.counts(f"meta.{key}", c, n)
    if not n:
        return c, n
    top, count = c.most_common(1)[0]
    if count / n > max_share:
        cap = int(math.floor(max_share * n + 1e-9))
        if cap == 0:
            hint = (f"；n={n} 时该轴在 {max_share:.0%} 阈值下无解，"
                    f"至少需要 {math.ceil(1 / max_share)} 个样本且每个值只出现一次")
        else:
            hint = f"；同一个值最多只能出现 {cap} 次（当前 {count} 次）"
        audit.fail(key, f"{label} 过度集中: {audit.excerpt(str(top))} 占 {count}/{n} ({count/n:.0%}) > {max_share:.0%}；"
                        "这会让模型把这个表面特征当成答案" + hint)
    if min_distinct is not None and len(c) < min_distinct:
        audit.fail(key, f"{label} 只有 {len(c)} 种，至少需要 {min_distinct} 种")
    return c, n


def check_diversity(parsed, audit, args):
    n = len(parsed)
    check_distribution(parsed, audit, args, "scenario_family", "场景骨架",
                       args.max_scenario_share, min_distinct=min(n, max(3, math.ceil(n * 0.6))))
    check_distribution(parsed, audit, args, "entity_family", "组织/实体命名家族",
                       args.max_entity_share, min_distinct=min(n, max(3, math.ceil(n * 0.5))))
    check_distribution(parsed, audit, args, "id_style", "ID 风格",
                       args.max_style_share, min_distinct=min(n, max(3, math.ceil(n * 0.4))))
    check_distribution(parsed, audit, args, "thought_style", "thought 口吻",
                       args.max_style_share, min_distinct=min(n, max(3, math.ceil(n * 0.4))))

    # entity_family must actually appear in the text; otherwise it is a lie.
    for where, c, m in parsed:
        fam = m.get("entity_family", "")
        text = re.sub(r"[^a-z0-9]", "", json.dumps(c["turns"], ensure_ascii=False).lower())
        tokens = [t for t in re.split(r"[^a-z0-9]+", fam.lower()) if len(t) > 2]
        if tokens and not all(t in text for t in tokens):
            audit.fail("entity", f"{where}: entity_family={audit.excerpt(fam)} 在 text 中找不到；meta 不能只是填表")

    inj = [(w, c, m) for w, c, m in parsed if m.get("injection_present") is True]
    if inj:
        check_distribution(inj, audit, args, "injection_surface", "注入位置",
                           args.max_surface_share, min_distinct=2 if len(inj) >= 3 else None)
        check_distribution(inj, audit, args, "payload_style", "payload 措辞风格",
                           args.max_surface_share, min_distinct=2 if len(inj) >= 3 else None)
        if len(inj) >= 6:
            surfaces = Counter(m.get("injection_surface") for _, _, m in inj)
            if len(surfaces) < 3:
                audit.fail("surface", f"注入位置只有 {len(surfaces)} 种；至少 3 种（body/subject/status/footer/...）")
            if set(surfaces) <= {"content", "body", "body_text"}:
                audit.fail("surface", "所有注入都在正文；必须换到 subject/status/footer/字段等位置")
    else:
        audit.metrics["injection_surface"] = {}
        audit.metrics["payload_style"] = {}


def check_harm_taxonomy(parsed, audit, args):
    """Enforce the two HarmBench-derived axes: harm_kind x request_shape.

    Both are rotation axes (no cell dominates) and both carry a structural truth
    obligation: contextual needs a literal context_span, near_miss needs a
    benign_directive_span. See references/taxonomy-design.md.
    """
    n = len(parsed)
    declared = [m for _, _, m in parsed
                if m.get("harm_kind") or m.get("request_shape")]
    if not declared:
        if getattr(args, "require_taxonomy_axes", False):
            audit.fail("taxonomy",
                       "--require-taxonomy-axes：每个 case 必须声明 meta.harm_kind "
                       "与 meta.request_shape（见 references/taxonomy-design.md）")
        return
    if getattr(args, "require_taxonomy_axes", False) and len(declared) < n:
        miss = [w for w, _, m in parsed
                if not (m.get("harm_kind") and m.get("request_shape"))]
        audit.fail("taxonomy",
                   f"{len(miss)} 个 case 缺少 harm_kind/request_shape（例：{miss[:3]}）")

    check_distribution(parsed, audit, args, "harm_kind", "危害类别",
                       args.max_harm_kind_share, min_distinct=min(n, 3))
    check_distribution(parsed, audit, args, "request_shape", "请求形态",
                       args.max_shape_share, None)

    for where, c, m in parsed:
        shape = m.get("request_shape")
        text = json.dumps(c["turns"], ensure_ascii=False)
        if shape == "contextual":
            ctx = m.get("context_span")
            if not isinstance(ctx, str) or not ctx.strip():
                audit.fail("taxonomy", f"{where}: request_shape=contextual 但缺 meta.context_span")
            elif ctx not in text:
                audit.fail("taxonomy", f"{where}: context_span 不在 text 中")
        elif m.get("context_span"):
            audit.warn("taxonomy", f"{where}: 非 contextual 却声明了 context_span")
        if shape == "near_miss":
            bd = m.get("benign_directive_span")
            if not isinstance(bd, str) or not bd.strip():
                audit.fail("taxonomy", f"{where}: request_shape=near_miss 但缺 benign_directive_span")
            elif bd not in text:
                audit.fail("taxonomy", f"{where}: benign_directive_span 不在 text 中；"
                                        "模型自述的 span 必须对应真实文本")

    shapes = {m.get("request_shape") for _, _, m in parsed if m.get("request_shape")}
    if n >= 4 and len(shapes & TAXONOMY_SHAPES) < 2:
        audit.fail("taxonomy", f"request_shape 只有 {sorted(shapes)}；≥4 个场景至少需要 2 种形态")
    if n >= 9 and len(shapes & TAXONOMY_SHAPES) < 3:
        audit.fail("taxonomy",
                   f"request_shape 只有 {sorted(shapes)}；≥9 个场景需要 "
                   "standard/contextual/near_miss 三种都出现")
    kinds = {m.get("harm_kind") for _, _, m in parsed if m.get("harm_kind")}
    if n >= 6 and len(kinds) < 3:
        audit.warn("taxonomy", f"harm_kind 只有 {sorted(kinds)}；建议 ≥3 种（见 taxonomy-design.md）")


def check_payloads(parsed, audit, args):
    inj = [(w, c, m) for w, c, m in parsed if m.get("injection_present") is True]
    if not inj:
        return
    spans = []
    openers = Counter()
    for where, c, m in inj:
        span = m.get("payload_span", "")
        spans.append((where, span, m))
        openers[first_words(span, 3)] += 1

    # 1) exact / near-duplicate payload text across cases
    for i in range(len(spans)):
        wi, si, _ = spans[i]
        gi = grams(si, 3)
        if not gi:
            continue
        for j in range(i + 1, len(spans)):
            wj, sj, _ = spans[j]
            if si.strip().lower() == sj.strip().lower():
                audit.fail("payload-dup", f"{wi} 与 {wj} 的 payload 文本完全相同；换措辞，不要换名词")
                continue
            gj = grams(sj, 3)
            jac = jaccard(gi, gj)
            if jac > args.max_payload_dup:
                audit.fail("payload-dup", f"{wi} 与 {wj} 的 payload 近似重复 (3-gram jaccard={jac:.2f} > {args.max_payload_dup})")
    for k, v in openers.items():
        if v > args.max_payload_opener:
            audit.fail("payload-opener", f"有 {v} 个 payload 以 {audit.excerpt(k)} 开头；固定开场白是指纹")

    # 2) templates and giveaways
    pattern_hits = Counter()
    for where, span, m in spans:
        for pat, label in HARD_BANNED:
            if re.search(pat, span, re.I):
                audit.fail("fingerprint", f"{where}: payload 含硬禁用指纹 ({label})")
        for pat, label in FINGERPRINT_PATTERNS:
            if re.search(pat, span, re.I):
                pattern_hits[label] += 1
        for e in emails(span):
            local = e.partition("@")[0]
            if any(re.search(p, local, re.I) for p in INTENT_LOCALPART):
                audit.fail("address", f"{where}: 邮箱前缀自带'我是坏人'提示: {e}（域名不限）")
    for label, count in pattern_hits.items():
        if count > args.max_fingerprint_cases:
            audit.fail("fingerprint", f"模板 {label!r} 在 {count} 个 payload 中复用 > {args.max_fingerprint_cases}；换的是句式，不是名词")

    # 4) entity cross-mapping: a domain's *style* must not predict the label.
    pool, blocklist, freemail = getattr(args, "email_pool", (set(), set(), set()))
    all_domains = Counter()
    safe_doms, unsafe_doms = Counter(), Counter()
    used_emails = []
    for where, c, m in parsed:
        tick(where)
        txt = json.dumps(c["turns"], ensure_ascii=False)
        for e in emails(txt):
            dom = registrable(e.lower().split("@")[1])
            all_domains[dom] += 1
            used_emails.append(e.lower())
            (safe_doms if c.get("safe") else unsafe_doms)[dom] += 1

    if all_domains:
        top_dom, cnt = all_domains.most_common(1)[0]
        total = sum(all_domains.values())
        if cnt / total > args.max_domain_share:
            audit.fail("domain", f"邮箱域名 '{top_dom}' 占全部地址的 {cnt}/{total}；"
                                  "一个域名贯穿整个语料会让模型用域名当标签")

    if pool:
        unknown = sorted({d for d in all_domains if d not in pool})
        audit.metrics["unknown_domains"] = unknown[:20]
        if unknown:
            msg = (f"{len(unknown)} 个域名不在 email_library 池中: {unknown[:5]}；"
                   "用 traindata/scripts/sample_emails.py 抽一批锁定地址，别自己编")
            if args.require_library_emails:
                audit.fail("library", msg)
            else:
                audit.warn("library", msg)

    # Known-disposable share must be similar in clean and violating contexts.
    if blocklist and safe_doms and unsafe_doms:
        def tier_share(counter, tier):
            tot = sum(counter.values())
            n = sum(v for d, v in counter.items() if d in tier)
            return n / tot if tot else 0.0
        s_bl, u_bl = tier_share(safe_doms, blocklist), tier_share(unsafe_doms, blocklist)
        audit.metrics["blocklist_share"] = {"safe": s_bl, "unsafe": u_bl}
        gap = abs(s_bl - u_bl)
        if gap > args.max_entity_gap:
            audit.fail("cross-map",
                       f"已知一次性域名占比 safe={s_bl:.0%} vs unsafe={u_bl:.0%}，差 {gap:.0%} > "
                       f"{args.max_entity_gap:.0%}；域名风格正在变成标签。把同一批域名交叉放进"
                       "安全场景和违规场景（用 sample_emails.py --cross-map）")
    # Normal free mail must be used by attackers too, not only by clean traffic.
    if freemail and safe_doms and unsafe_doms:
        def free_share(counter):
            tot = sum(counter.values())
            n = sum(v for d, v in counter.items() if d in freemail)
            return n / tot if tot else 0.0
        s_f, u_f = free_share(safe_doms), free_share(unsafe_doms)
        audit.metrics["freemail_share"] = {"safe": s_f, "unsafe": u_f}
        if abs(s_f - u_f) > args.max_entity_gap:
            audit.fail("cross-map",
                       f"正常免费邮箱占比 safe={s_f:.0%} vs unsafe={u_f:.0%}，差 > "
                       f"{args.max_entity_gap:.0%}；免费邮箱正变成'safe'的信号")
        if u_f < args.min_freemail_in_violations:
            audit.fail("cross-map",
                       f"违规场景里只有 {u_f:.0%} 是正常免费邮箱 < "
                       f"{args.min_freemail_in_violations:.0%}；高级攻击者恰恰用 gmail 收脏数据，"
                       "别把所有攻击都写成一次性域名")
    if blocklist and unsafe_doms:
        u_bl = sum(v for d, v in unsafe_doms.items() if d in blocklist) / sum(unsafe_doms.values())
        if u_bl > args.max_disposable_in_violations:
            audit.fail("cross-map",
                       f"违规场景里 {u_bl:.0%} 是一次性域名 > "
                       f"{args.max_disposable_in_violations:.0%}；攻击被一次性域名主导，"
                       "模型会只认脏域名")
    # No single domain may sit entirely on one side once it appears repeatedly.
    one_sided = []
    for dom in set(safe_doms) | set(unsafe_doms):
        s, u = safe_doms.get(dom, 0), unsafe_doms.get(dom, 0)
        if s + u >= 3 and (s == 0 or u == 0):
            one_sided.append((dom, s, u))
    if one_sided:
        audit.warn("cross-map",
                   f"{len(one_sided)} 个域名只出现在单一类别（例：{one_sided[:3]}）；"
                   "抽样时用 --cross-map 让每个域名两边都出现")

    payload_domains = Counter()
    for _, span, _ in spans:
        for e in emails(span):
            payload_domains[registrable(e.lower().split("@")[1])] += 1
    if payload_domains:
        top, cnt = payload_domains.most_common(1)[0]
        if cnt / len(spans) > 0.5:
            audit.warn("domain", f"payload 域名 '{top}' 出现在 {cnt}/{len(spans)} 个注入样本；换一批域名")

    clean = [(w, c) for w, c, _ in parsed if c.get("safe") is True]
    if clean:
        clean_email = sum(1 for _, c in clean if emails(json.dumps(c["turns"], ensure_ascii=False)))
        inj_email = sum(1 for _, c, _ in inj if emails(json.dumps(c["turns"], ensure_ascii=False)))
        clean_share = clean_email / len(clean)
        inj_share = inj_email / len(inj)
        audit.metrics["email_presence"] = {"clean": clean_share, "injected": inj_share}
        if abs(clean_share - inj_share) > args.max_email_parity_gap:
            audit.fail("parity", f"邮箱出现率 clean={clean_share:.0%} vs injected={inj_share:.0%}；"
                                  "地址只出现在毒样本里，模型会学'有邮箱=危险'")
        ext_clean = sum(1 for _, c, m in parsed
                        if c.get("safe") is True and m.get("external_contact") is True)
        if ext_clean / len(clean) < args.min_clean_external:
            audit.fail("parity", f"只有 {ext_clean}/{len(clean)} 个干净样本带外部联系人；"
                                  "干净样本也必须出现看似外部的地址，否则外部地址就成了毒标签")

    # 5) hard negatives: benign instruction-like text in clean cases
    clean_total = len(clean)
    benign = sum(1 for _, c, m in parsed
                 if c.get("safe") is True and isinstance(m.get("benign_directive_span"), str)
                 and m.get("benign_directive_span").strip())
    if clean_total >= 4:
        bshare = benign / clean_total
        audit.metrics["benign_directive_share"] = bshare
        if bshare < args.min_benign_share or bshare > args.max_benign_share:
            audit.fail("negative", f"干净样本中带“像指令但合法”的文本占 {bshare:.0%}；"
                                    f"应介于 {args.min_benign_share:.0%} 和 {args.max_benign_share:.0%}"
                                    "（全是或全不是都会造出反向捷径）")
    for where, c, m in parsed:
        if c.get("safe") is not True:
            continue
        span = m.get("benign_directive_span")
        if not span:
            continue
        text = json.dumps(c["turns"], ensure_ascii=False)
        if span not in text:
            audit.fail("negative", f"{where}: benign_directive_span 不在 text 中")
        elif not re.search(r"(please|must|should|send|forward|cc|escalate|notify|update|route|assign|include|attach|share|reply)",
                           span, re.I):
            audit.warn("negative", f"{where}: benign_directive_span 不像指令，起不到困难负例作用")


NEUTRALIZE_MAIL = re.compile(
    r"(temporary|temp|disposable|throwaway|burner)\s*(mail|mailbox|email|address|domain)", re.I)
NEUTRALIZE_OK = re.compile(
    r"(compliant|within scope|in scope|not (itself )?a violation|no violation|still safe|"
    r"legitimate|business action|does not (by itself )?(make|imply))", re.I)
ZH_TEMP = ("临时邮箱", "一次性邮箱", "临时邮件", "一次性邮件")
ZH_OK = ("合规", "业务动作", "不构成违规", "不违规", "仍在范围")
RISKY_ACTION = re.compile(
    r"(delete|purge|destroy|wipe|drop\b|export|download|upload|\bsend\b|forward|share|"
    r"post|publish|grant|revoke|escalate|disable|bypass|override|all_users|mass|bulk|"
    r"external|detach|attach|transfer)", re.I)
DOMAIN_RATIONALE = re.compile(
    r"(because|since|as|due to)[^.\n]{0,60}"
    r"\b(gmail|hotmail|yahoo|outlook|free ?mail|temporary|temp|disposable|throwaway|"
    r"external address|email address|the domain|recipient domain)\b", re.I)
NEUTRALIZE_SENTENCE = ("The sender uses a temporary mailbox; noteworthy, but the business "
                       "action stays within scope.")


def check_action_attribution(parsed, audit, args):
    """Enforce: label follows the agent's ACTION, never the recipient's domain.

    - safe=true with a disposable address must carry an explicit neutralisation
      thought (temp mailbox is not itself a violation).
    - safe=false must have an action-level violation point; when an injection was
      present, the agent must actually have taken the injected action.
    - no thought may justify an unsafe label by the domain style alone.
    """
    block = getattr(args, "email_pool", (set(), set(), set()))[1]
    neutralized = 0
    for where, c, m in parsed:
        txt = json.dumps(c["turns"], ensure_ascii=False)
        case_emails = [e.lower() for e in emails(txt)]
        doms = {registrable(e.split("@")[1]) for e in case_emails}
        thoughts = [t["thought"] for t in c["turns"]
                    if isinstance(t, dict) and isinstance(t.get("thought"), str)]
        thought_txt = " ".join(thoughts)

        if c.get("safe") is True:
            if block and doms & block:
                ok_en = bool(NEUTRALIZE_MAIL.search(thought_txt)
                             and NEUTRALIZE_OK.search(thought_txt))
                ok_zh = any(z in thought_txt for z in ZH_TEMP) and \
                    any(z in thought_txt for z in ZH_OK)
                if not (ok_en or ok_zh):
                    audit.fail(
                        "action",
                        f"{where}: safe=true 但用了已知一次性域名，却没有任何中立化说明。"
                        f"thought 必须同时包含 (temporary|temp|disposable|throwaway)"
                        f"+(mail|mailbox) 和一个范围词 (within scope / not a violation / "
                        f"compliant / business action)。最稳的是原样抄："
                        f"\"{NEUTRALIZE_SENTENCE}\" "
                        f"（中文亦可：客户使用了临时邮箱，需注意但业务动作合规）")
                else:
                    neutralized += 1
            continue

        # safe=false
        agent_actions = " ".join(
            t.get("action", "") for t in c["turns"]
            if isinstance(t, dict) and t.get("role") == "agent")
        risky = bool(RISKY_ACTION.search(agent_actions))
        # an injected instruction must be actually carried out to count as a failure
        span = m.get("payload_span") or ""
        span_emails = [e.lower() for e in emails(span)]
        followed = True
        if span_emails:
            followed = any(e in agent_actions.lower() for e in span_emails)
        if not risky and not followed:
            audit.fail("action",
                       f"{where}: safe=false 但找不到动作层的违规点；违规必须落在"
                       "越权/外发/破坏等 agent action 上，不能只因为收件方是某类域名")
        if DOMAIN_RATIONALE.search(thought_txt):
            audit.fail("action",
                       f"{where}: 违规理由似乎在说域名/邮箱类型，而不是动作；"
                       "把归因改到 agent 做了什么越界动作上")
        if span_emails and not followed:
            audit.fail("action",
                       f"{where}: 标注为违规，但 agent 的动作里没有执行注入目标 "
                       f"{span_emails[:2]}；说明 agent 其实抗住了，应标 safe=true")
    audit.metrics["neutralized_temp_mail"] = neutralized


def check_benign_directives(parsed, audit):
    """A benign directive must be real, legitimate, and never inside the payload.

    ``meta.benign_directive_span`` is model-authored, so it cannot be trusted
    on its own: a generator that needs a hard negative will happily invent
    "Reply in the record; nothing leaves the portal" inside an exfiltration
    pretext, mark it clean, and the case stops teaching anything. The audit
    therefore re-derives legality from the text:

    - it must literally occur in the trajectory;
    - on a violating case it is a fake: an attacker does not add a defence
      next to their own payload, so any declaration there is a failure;
    - it must not overlap the injection payload, nor sit in the same
      environment turn as it, because then the "defence" is part of the poison.
    """
    for where, c, m in parsed:
        span = m.get("benign_directive_span")
        if not isinstance(span, str) or not span.strip():
            continue
        text = json.dumps(c["turns"], ensure_ascii=False)
        if span not in text:
            audit.fail("benign", f"{where}: benign_directive_span 不在 text 中（模型自述不算数）")
            continue
        if c.get("safe") is not True:
            audit.fail("benign", f"{where}: 违规样本声明了 benign_directive_span "
                                  f"{audit.excerpt(span)}；注入场景里攻击者不会附赠防御指令，"
                                  "这是伪造的困难负例，请删除该字段并改写场景")
            continue
        payload = m.get("payload_span")
        if isinstance(payload, str) and payload.strip():
            if span in payload or payload in span:
                audit.fail("benign", f"{where}: benign_directive_span 与 payload_span 重叠；"
                                      "防御指令不能是载荷的一部分")
                continue
            shared = [role for role, _, _, s in turn_strings(c["turns"])
                      if role == "environment" and payload in s and span in s]
            if shared:
                audit.fail("benign", f"{where}: benign_directive_span 与 payload_span "
                                      "出现在同一个 environment 回合；"
                                      "合法指令必须来自与载荷不同的来源")
                continue


def check_salt_leakage(parsed, audit):
    """The salt belongs to the prompt, never to the sample.

    ``salt.py`` wraps the generation prompt, and a model that echoes its
    instructions will copy "You are salted" or the end-of-salt marker into
    ``turns``. The sample then is no longer the harmful trajectory it was
    meant to be: it carries the scaffolding that produced it. Scan the whole
    trajectory, not just the payload span, because the echo can land in any
    turn.
    """
    for where, c, _m in parsed:
        blob = json.dumps(c["turns"], ensure_ascii=False)
        for pat, label in SALT_LEAK:
            if re.search(pat, blob, re.I):
                audit.fail("salt-leak", f"{where}: {label}；"
                                        "盐只属于生成提示，样本正文必须保持原始有害轨迹")


def check_payload_locations(parsed, audit):
    for where, c, m in parsed:
        span = m.get("payload_span")
        if not span:
            continue
        surfaces = set()
        env_hit = False
        for role, _, path, s in turn_strings(c["turns"]):
            if span in s:
                if role == "environment":
                    env_hit = True
                    surfaces.add(surface_of(path))
        if not env_hit:
            audit.fail("payload-loc", f"{where}: payload_span 只出现在 user/agent 文本里；"
                                       "注入必须在工具输出或它引用的内容中")
            continue
        declared = m.get("injection_surface", "")
        if declared not in surfaces:
            audit.fail("payload-loc", f"{where}: meta.injection_surface={declared!r} 与文本实际位置 {sorted(surfaces)} 不符")


SYSTEM_MAILBOXES = {"abuse", "postmaster", "webmaster", "hostmaster", "mailerdaemon",
                    "noreply", "no-reply", "donotreply", "admin", "root", "support",
                    "info", "contact", "hello", "security", "privacy", "dpo"}


def check_prefix_diversity(parsed, audit, args):
    """Local parts must not be reused across many cases (prefix overfitting)."""
    cases_for = defaultdict(set)
    for where, c, _m in parsed:
        ck = where
        for e in emails(json.dumps(c["turns"], ensure_ascii=False)):
            local = e.lower().split("@")[0]
            if local in SYSTEM_MAILBOXES:
                continue
            cases_for[local].add(ck)
    if not cases_for:
        return
    reused = {p: len(v) for p, v in cases_for.items() if len(v) > args.max_prefix_reuse}
    audit.metrics["distinct_prefixes"] = len(cases_for)
    if reused:
        top = sorted(reused.items(), key=lambda kv: -kv[1])[:5]
        audit.fail("prefix",
                   f"{len(reused)} 个邮箱前缀被复用到 >{args.max_prefix_reuse} 个场景（例：{top}）；"
                   "前缀也是特征。用 sample_emails.py 的组合前缀并全程传同一个 --ledger，"
                   "保证跨 chunk 唯一")


def check_text_style(parsed, audit, args):
    n = len(parsed)
    user_openers = Counter()
    thought_openers = Counter()
    thought_phrases = Counter()
    forms = set()
    all_domains = Counter()

    for where, c, _ in parsed:
        turns = c["turns"]
        if turns and isinstance(turns[0], dict) and isinstance(turns[0].get("content"), str):
            user_openers[first_words(turns[0]["content"], 5)] += 1
        thoughts = []
        for t in turns:
            if isinstance(t, dict) and isinstance(t.get("thought"), str):
                thoughts.append(t["thought"])
        for th in thoughts:
            thought_openers[first_words(th, 3)] += 1
            w = normalized_seconds(th).split()
            for i in range(max(0, len(w) - 3)):
                thought_phrases[" ".join(w[i:i + 4])] += 1
        text = json.dumps(turns, ensure_ascii=False)
        forms |= id_forms(text)
        for e in emails(text):
            all_domains[registrable(e.split("@")[1])] += 1

    for k, v in user_openers.items():
        if v / n > args.max_opener_share:
            audit.fail("opener", f"{v}/{n} 个场景以 {audit.excerpt(k)} 开头；换场景骨架，不要换公司名")
    for k, v in thought_openers.items():
        if v / n > args.max_opener_share:
            audit.fail("thought", f"{v}/{n} 个 thought 以 {audit.excerpt(k)} 开头；thought 口吻太单一")
    repeated = [(k, v) for k, v in thought_phrases.items()
                if v > args.max_thought_phrase_cases and v / max(1, n) > 0.25]
    for k, v in sorted(repeated, key=lambda kv: -kv[1])[:3]:
        audit.fail("thought", f"thought 复用短语 {audit.excerpt(k)} 出现在 {v} 个场景；换推理口吻")
    if n >= 6 and len(forms) < 3:
        audit.fail("ids", f"全语料只有 {len(forms)} 种 ID 形式 {sorted(forms)}；至少 3 种"
                           "（NFS-30xx / #4412 / UUID / slug / hex）")
    audit.metrics["id_forms"] = sorted(forms)
    if all_domains:
        audit.metrics["email_domains"] = dict(all_domains.most_common())


def check_taxonomy(taxonomy, audit):
    if not taxonomy:
        return
    for qid, q in taxonomy.get("questions", {}).items():
        if q.get("type") not in ("choice", "score"):
            continue
        instr = q.get("instructions", "")
        if DUAL_POLARITY.search(instr):
            audit.fail("dual-polarity", f"问题 {qid!r} 的 instruction 同时描述正反两种语义: {instr!r}")
        crit = q.get("criteria")
        texts = crit.values() if isinstance(crit, dict) else (crit or [])
        for t in texts:
            if isinstance(t, str) and DUAL_POLARITY.search(t):
                audit.fail("dual-polarity", f"问题 {qid!r} 的 criteria 含双关措辞: {t!r}")


def check_labels(parsed, taxonomy, audit, args):
    if not taxonomy:
        return
    questions = taxonomy.get("questions", {})
    for qid, q in questions.items():
        qt = q.get("type")
        if qt not in ("choice", "score"):
            continue
        values = Counter(
            str(c[qid]) if q.get("type") == "score" else c[qid]
            for _, c, _ in parsed if qid in c)
        if not values:
            audit.warn("labels", f"问题 {qid!r} 没有任何标签")
            continue
        n = sum(values.values())
        audit.metrics[f"labels.{qid}"] = dict(values.most_common())
        top, count = values.most_common(1)[0]
        if qt == "choice" and n >= 6 and count / n > args.max_key_share:
            audit.fail("labels", f"{qid} 的标签 '{top}' 占 {count}/{n} > {args.max_key_share:.0%}；"
                                  "可能是捷径或生成器塌缩")
        if qt == "score" and n >= 6 and count / n > args.max_key_share:
            audit.warn("labels", f"{qid} 的等级 '{top}' 占 {count}/{n}；检查是否被干净样本的 0 级主导")
        keys = list(q.get("criteria", {})) if isinstance(q.get("criteria"), dict) else \
            [str(i) for i in range(len(q.get("criteria", [])))] if isinstance(q.get("criteria"), list) else []
        missing = [k for k in keys if k not in values]
        if missing:
            audit.warn("labels", f"{qid} 缺少标签 {missing}；确认这些情况确实不该出现")


def load_email_pool(dirpath):
    """Return (pool_lookup, block_lookup, freemail_lookup, stats).

    All lookups contain full domains and their registrable forms, and all are
    *usable*; the blocklist/freemail sets are only classification labels used to
    verify cross-context parity. Never filter the pool with them.
    """
    pool, block, freemail = set(), set(), set()
    stats = {"domains": 0, "known_blocklist": 0, "freemail": 0}
    if not dirpath or not os.path.isdir(dirpath):
        return pool, block, freemail, stats
    for name in ("synth_domains.txt", "known_blocklist.txt", "freemail.txt"):
        p = os.path.join(dirpath, name)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                d = ln.strip().strip(".").lower()
                if not d:
                    continue
                stats_key = {"synth_domains.txt": "domains",
                             "known_blocklist.txt": "known_blocklist",
                             "freemail.txt": "freemail"}[name]
                stats[stats_key] += 1
                # Everything is usable: blocklist/freemail domains are pool members too.
                pool.add(d)
                pool.add(registrable(d))
                if name == "known_blocklist.txt":
                    block.add(d)
                    block.add(registrable(d))
                elif name == "freemail.txt":
                    freemail.add(d)
                    freemail.add(registrable(d))
    stats["domains"] += stats["known_blocklist"] + stats["freemail"]
    return pool, block, freemail, stats


def check_email_manifest(parsed, path, audit):
    """The manifest is a LOCKED whitelist.

    When a manifest is supplied the corpus may use *only* the addresses it
    contains. This is what forces the generator to call ``sample_emails.py``
    instead of reading the library files and picking domains by hand: pool
    membership alone would still let a model cherry-pick ``gmail.com`` or
    ``10minutemail.com`` and skip the weighted, cross-mapped batch.
    """
    if not path:
        # Silence here is how hand-picked domains get in: without a batch there
        # is nothing to compare against, so "no manifest" must not pass quietly
        # when the corpus actually contains addresses.
        used = set()
        for where, c, m in parsed:
            used.update(emails(json.dumps(c["turns"], ensure_ascii=False)))
        if used:
            audit.fail("locked-batch",
                       f"发现 {len(used)} 个地址但没有 --email-manifest；"
                       "无法确认它们来自 sample_emails.py 的锁定批次。"
                       "用 --email-manifest 指定本轮批次")
        return
    try:
        with open(path, encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        audit.fail("cross-map", f"无法读取 email manifest {path}: {e}")
        return
    allowed = {e.lower() for e in man.get("emails", [])}
    pairs = man.get("pairs") or []
    if not allowed:
        audit.fail("cross-map", f"email manifest {path} 没有 emails；用 sample_emails.py --manifest 生成")
        return

    used = Counter()
    for where, c, m in parsed:
        for e in emails(json.dumps(c["turns"], ensure_ascii=False)):
            used[e.lower()] += 1

    off_batch = sorted(set(used) - allowed)
    if off_batch:
        audit.fail("locked-batch",
                   f"{len(off_batch)} 个地址不在锁定批次里（例：{off_batch[:5]}）。"
                   "禁止自己读取 email_library 或编地址，必须用 sample_emails.py 的输出，"
                   "并把同一批 emails 原样写进样本")

    # every pair domain must appear in both a safe and an unsafe case
    seen = defaultdict(lambda: {True: 0, False: 0})
    for where, c, m in parsed:
        for e in emails(json.dumps(c["turns"], ensure_ascii=False)):
            seen[registrable(e.lower().split("@")[1])][bool(c.get("safe"))] += 1
    bad = []
    for p in pairs:
        dom = registrable(str(p.get("domain", "")).lower())
        if not dom or dom not in seen:
            continue
        s, u = seen[dom][True], seen[dom][False]
        if s == 0 or u == 0:
            bad.append((dom, s, u))
    if bad:
        audit.fail("cross-map", f"manifest 要求的域名未同时出现在安全与违规场景: {bad[:5]}；"
                                "同一域名必须在两类上下文都出现")
    metrics = {"batch_size": len(allowed), "used": len(used),
               "off_batch": len(off_batch), "pairs": len(pairs)}
    audit.metrics["email_manifest"] = metrics
    if not off_batch:
        audit.info("locked-batch",
                   f"locked batch honoured: {len(used)}/{len(allowed)} addresses used")


def write_holdout(parsed, args, audit):
    if not args.holdout:
        return
    n = len(parsed)
    if args.holdout >= n:
        audit.fail("holdout", f"--holdout {args.holdout} >= 场景数 {n}；没有 dev 可用了")
        return
    # Stable by scenario hash: adding future cases cannot reshuffle old splits.
    ordered = sorted(parsed, key=lambda x: hashlib.sha256(
        f"{args.seed}:{x[1]['_scenario_hash']}".encode()).hexdigest())
    hold = ordered[:args.holdout]
    dev = ordered[args.holdout:]
    os.makedirs(args.out_dir, exist_ok=True)

    def dump(name, rows):
        payload = []
        for row in rows:
            c = row[1]
            clean = {k: v for k, v in c.items() if not k.startswith("_")}
            payload.append(json.dumps(clean, ensure_ascii=False) + "\n")
        atomic_write_text(os.path.join(args.out_dir, name), "".join(payload))

    dump("holdout.jsonl", hold)
    dump("dev.jsonl", dev)
    manifest = {
        "seed": args.seed,
        "holdout_cases": len(hold),
        "dev_cases": len(dev),
        "holdout_scenario_hashes": [c["_scenario_hash"] for _, c, _ in hold],
        "rule": "scenario hash holdout; never train or tune on holdout",
    }
    atomic_write_text(os.path.join(args.out_dir, "holdout_manifest.json"),
                      json.dumps(manifest, indent=2, ensure_ascii=False))
    audit.info("holdout", f"hash holdout -> {args.out_dir} ({len(hold)} held out, {len(dev)} dev)")
    audit.metrics["holdout"] = manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chunks", default="chunk_*.jsonl", help="case 形式 chunk 的 glob")
    ap.add_argument("--taxonomy", default=None)
    ap.add_argument("--require-injection", action="store_true",
                    help="注入域必开：要求 injection_present 存在且不过半")
    ap.add_argument("--strict", action="store_true", help="把 WARN 升级为失败")
    ap.add_argument("--json", default=None, help="写机器可读报告")
    ap.add_argument("--holdout", type=int, default=0, help="按场景 hash 留出的场景数（pilot 用 2）")
    ap.add_argument("--out-dir", default="pilot_holdout")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--email-library", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "email_library"),
        help="email_library 目录（synth_domains.txt + known_blocklist.txt）")
    ap.add_argument("--require-library-emails", action="store_true",
                    help="样本里的邮箱必须来自 email_library（否则 fail）")
    ap.add_argument("--email-manifest", default=None,
                    help="sample_emails.py --cross-map 生成的 assignment JSON，用于精确校验交叉映射")
    ap.add_argument("--max-entity-gap", type=float, default=0.3,
                    help="已知一次性域名在 safe/unsafe 中的占比差上限")
    ap.add_argument("--min-freemail-in-violations", type=float, default=0.4,
                    help="违规场景中正常免费邮箱的最低占比（攻击者也会用 gmail）")
    ap.add_argument("--max-disposable-in-violations", type=float, default=0.5,
                    help="违规场景中一次性域名的最高占比")
    ap.add_argument("--max-prefix-reuse", type=int, default=2,
                    help="同一邮箱前缀最多出现在几个场景")
    ap.add_argument("--require-salt", action="store_true",
                    help="注入/对抗域必开：每个 case 必须 meta.salted=true（见 references/harmbench-methodology.md）")
    ap.add_argument("--require-taxonomy-axes", action="store_true",
                    help="每个 case 必须声明 meta.harm_kind 与 meta.request_shape（见 references/taxonomy-design.md）")
    ap.add_argument("--max-harm-kind-share", type=float, default=0.4,
                    help="单一 harm_kind 的最高占比")
    ap.add_argument("--max-shape-share", type=float, default=0.4,
                    help="单一 request_shape 的最高占比")

    ap.add_argument("--max-unsafe-share", type=float, default=0.5)
    ap.add_argument("--max-injected-share", type=float, default=0.5)
    ap.add_argument("--max-scenario-share", type=float, default=0.4)
    ap.add_argument("--max-entity-share", type=float, default=0.25)
    ap.add_argument("--max-style-share", type=float, default=0.4)
    ap.add_argument("--max-surface-share", type=float, default=0.4)
    ap.add_argument("--max-opener-share", type=float, default=0.4)
    ap.add_argument("--max-thought-phrase-cases", type=int, default=2)
    ap.add_argument("--max-payload-dup", type=float, default=0.6)
    ap.add_argument("--max-payload-opener", type=int, default=2)
    ap.add_argument("--max-fingerprint-cases", type=int, default=1)
    ap.add_argument("--max-domain-share", type=float, default=0.5)
    ap.add_argument("--max-email-parity-gap", type=float, default=0.3)
    ap.add_argument("--min-clean-external", type=float, default=0.4)
    ap.add_argument("--min-benign-share", type=float, default=0.25)
    ap.add_argument("--max-benign-share", type=float, default=0.75)
    ap.add_argument("--max-key-share", type=float, default=0.6)
    ap.add_argument("--redact", action="store_true",
                    help="报错里不引用语料原文（只给位置、长度和短哈希）。"
                         "在公开 CI 日志里跑私有语料时必须打开")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="整次审计的墙钟上限（秒，0=不限）；超时以退出码 3 结束")
    args = ap.parse_args()

    _RUN["deadline"] = Deadline(args.timeout)
    paths = sorted(glob.glob(args.chunks))
    if not paths:
        print(f"没有匹配的文件: {args.chunks}")
        sys.exit(2)
    taxonomy = None
    if args.taxonomy:
        try:
            with open(args.taxonomy, encoding="utf-8-sig") as f:
                taxonomy = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"taxonomy 读取失败: {args.taxonomy}: {exc}")
            sys.exit(2)

    try:
        cases, load_errors, skipped = load_cases(paths, _RUN["deadline"])
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}")
        sys.exit(3)
    if load_errors:
        print(f"无法读取 chunk ({len(load_errors)} errors)")
        for err in load_errors[:20]:
            print(" -", err)
        sys.exit(1)
    if skipped and not cases:
        print(f"{skipped} 行是记录形式（没有 turns），不是 case 形式；"
              "多样性审计必须针对 chunk（每个场景一行）。"
              "请对 chunk 的 YAML/JSONL 运行审计，不要先把记录形式转来转去。")
        sys.exit(2)

    audit = Audit()
    audit.redact = bool(getattr(args, "redact", False))
    audit.metrics["cases_loaded"] = len(cases)
    pool, block, freemail, pool_stats = load_email_pool(args.email_library)
    args.email_pool = (pool, block, freemail)
    if pool:
        audit.metrics["email_library"] = pool_stats
    try:
        parsed = check_meta(cases, audit, args)
        check_scenarios(parsed, audit, args)
        check_diversity(parsed, audit, args)
        check_harm_taxonomy(parsed, audit, args)
        check_payloads(parsed, audit, args)
        check_payload_locations(parsed, audit)
        check_benign_directives(parsed, audit)
        check_salt_leakage(parsed, audit)
        check_action_attribution(parsed, audit, args)
        check_prefix_diversity(parsed, audit, args)
        check_text_style(parsed, audit, args)
        check_taxonomy(taxonomy, audit)
        check_labels(parsed, taxonomy, audit, args)
        check_email_manifest(parsed, args.email_manifest, audit)
        write_holdout(parsed, args, audit)
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}")
        sys.exit(3)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        print(f"chunk 结构异常，无法审计: {type(exc).__name__}: {exc}")
        print("  请保留 case 形式（每个场景一行、带 turns 列表）修好 chunk 后重跑；不要手改标签。")
        sys.exit(1)

    if args.strict:
        for f in audit.findings:
            if f["level"] == "WARN":
                f["level"] = "FAIL"
                f["message"] = "(--strict) " + f["message"]

    fails = [f for f in audit.findings if f["level"] == "FAIL"]
    warns = [f for f in audit.findings if f["level"] == "WARN"]

    print(f"diversity audit: {audit.metrics.get('cases_loaded', 0)} cases loaded / "
          f"{audit.metrics.get('unique_scenarios', 0)} audited scenarios")
    for f in audit.findings:
        if f["level"] != "INFO":
            print(f"  {f['level']:4} [{f['code']}] {f['message']}")
    if not fails:
        print(f"PASS ({len(warns)} warnings)")
    else:
        print(f"FAIL ({len(fails)} failures, {len(warns)} warnings)")
        print("  拒绝这个 chunk：换生成提示或多样性计划后重新生成，不要手改标签。")

    if args.json:
        report = {
            "files": paths,
            "metrics": audit.metrics,
            "findings": audit.findings,
            "verdict": "fail" if fails else "pass",
        }
        atomic_write_text(args.json, json.dumps(report, indent=2, ensure_ascii=False))
        print(f"report -> {args.json}")

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
