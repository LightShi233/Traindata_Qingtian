#!/usr/bin/env python3
"""Validate, integrate, split, and summarize soft-label JSONL training data.

Schema-agnostic: it validates the record contract and soft-label discipline, and
checks for duplicates and n-gram leakage against any existing corpora you point
it at. It deliberately does NOT know your taxonomy, so hand it one with
``--taxonomy`` when you want option keys checked too.

Usage
-----
Validate one chunk (exit 0 + summary, or non-zero with every failure listed):

    python3 validate_and_integrate.py --check chunk_0001.jsonl

Validate every chunk matching a glob:

    python3 validate_and_integrate.py --check 'chunk_*.jsonl'

Validate case-form chunks (``{traj_id, safe, turns, <qid>: label}``) without
writing output:

    python3 validate_and_integrate.py --check 'chunk_*.jsonl' --taxonomy tax.json --expand

Integrate all chunks into one corpus, expanding case-form records if needed:

    python3 validate_and_integrate.py --integrate --out all_soft.jsonl --taxonomy tax.json --expand

Split a validated corpus into disjoint train/calibration/test files by case:

    python3 validate_and_integrate.py --split-dir split --chunks 'chunk_*.jsonl' --taxonomy tax.json --expand

Write a data card while doing any of the above:

    python3 validate_and_integrate.py --check all_soft.jsonl --card data-card.md

Options
-------
--taxonomy PATH     JSON with {"questions": {...}} or a flat
                    {"<qid>": {"type": ..., "criteria": ...}} mapping; enables
                    criteria/instruction/option checks and --expand.
--leak PATH         Existing corpus to check leakage against. Repeatable.
                    Without it no baseline is loaded; pass --auto-leak to index
                    *soft*.jsonl next to the output/input (excluding the files
                    being checked).
--leak-threshold F  Jaccard threshold for near-duplicate leakage (default 0.30,
                    0 disables).
--intra-threshold F Threshold for near-duplicates inside the corpus
                    (default 0.55, 0 disables).
--min-len / --max-len  Acceptable len(text) bounds (record form) or
                    len(json.dumps(turns)) bounds (case form).
--allow-onehot      Permit one-hot targets (off by default; use only when the
                    user has explicitly justified them).
--require-complete-cases  Fail when a case lacks a taxonomy question. Off by
                    default because partial corpora are often validated mid-run.
--split-ratios      Train/calibration/test ratios (default 0.8,0.1,0.1).
--seed              Deterministic split seed (default 13).
--card PATH         Write a Markdown data card.
--timeout SECONDS   Wall-clock budget for the whole run (0 = unlimited); a run
                    that exceeds it exits 3 instead of hanging CI.

Input tolerance
---------------
JSON is read leniently: a JSON array or one pretty-printed object is accepted in
place of JSONL, along with markdown fences, a UTF-8 BOM and trailing commas.
YAML case files are read directly (the shared loader in ``dataloader.py`` parses
the stdlib YAML subset and normalises nested ``action``/``content``), so
``--check data/inbox/chunk_0001.yaml --expand`` validates the YAML before
``data_writer.py write`` emits a JSONL.
"""
import argparse
import glob
import hashlib
import json
import os
import random
import re
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataloader import Deadline, atomic_write_text, read_jsonl  # noqa: E402

REQUIRED = {"id", "source_dataset", "source_index", "source_hash", "text",
            "qtype", "criteria", "instruction", "soft_label"}
QTYPES = {"noul", "choice", "score"}
# case 形式: 一个 case + 它的全部问题, 由 --expand 展开成多条记录
CASE_REQUIRED = {"traj_id", "safe", "turns"}
WORD_RE = re.compile(r"[a-z0-9']+")
SOFT_TOL = 1e-6
TURN_ROLES = ("user", "agent", "environment")


# --------------------------------------------------------------------------- #
# text / similarity helpers
# --------------------------------------------------------------------------- #
def grams(text, n=8):
    w = WORD_RE.findall(text.lower())
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def jaccard(a, b):
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if (a or b) else 0.0


class LeakIndex:
    """Lazily-loaded n-gram index over one or more reference corpora."""

    def __init__(self, paths, threshold, deadline=None):
        self.threshold = threshold
        self.deadline = deadline
        self.grams = []
        if threshold <= 0:
            return
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            n = 0
            for _, o in read_jsonl(p, deadline):
                if self.deadline:
                    self.deadline.check(f"leak {os.path.basename(p)}")
                if isinstance(o.get("text"), str):
                    self.grams.append(grams(o["text"]))
                    n += 1
            print(f"[leak] indexed {n} records from {p}", file=sys.stderr)

    def worst(self, text):
        best = 0.0
        g = grams(text)
        ng = len(g)
        for ref in self.grams:
            if self.deadline:
                self.deadline.check("leak comparison")
            nr = len(ref)
            if not ng or not nr:
                continue
            # Jaccard can never exceed the size ratio; skip hopeless candidates.
            if min(ng, nr) / max(ng, nr) <= best:
                continue
            j = len(g & ref) / (ng + nr - len(g & ref))
            if j > best:
                best = j
                if best >= 1.0:
                    break
        return best


# --------------------------------------------------------------------------- #
# record validation
# --------------------------------------------------------------------------- #
def case_key(rec_id):
    """Records of one case share the ``<case>/<qid>`` id prefix (and its text)."""
    return rec_id.rsplit("/", 1)[0] if isinstance(rec_id, str) and "/" in rec_id else rec_id


def qid_of(rec_id):
    return rec_id.rsplit("/", 1)[-1] if isinstance(rec_id, str) and "/" in rec_id else None


def new_state():
    # ``families`` maps traj_id -> meta.scenario_family when the input is case
    # form. The split uses it to keep one scenario family in one split: variants
    # of the same skeleton are the same scenario, and a random case split would
    # put sibling variants on both sides (see references/label-provenance.md).
    return {"hashes": {}, "ids": set(), "grams": [], "cases": [], "groups": {},
            "families": {}}


def check_turns(parsed, tag):
    """Validate a turn list shared by record and case forms."""
    errs = []
    if parsed and isinstance(parsed[0], dict) and parsed[0].get("role") != "user":
        errs.append(f"{tag}: 第一个回合必须是 user")
    for j, t in enumerate(parsed):
        if not isinstance(t, dict):
            errs.append(f"{tag}: turn[{j}] 必须是 JSON 对象")
            continue
        if t.get("role") not in TURN_ROLES:
            errs.append(f"{tag}: turn[{j}] role 非法: {t.get('role')!r}")
        elif t["role"] == "agent" and isinstance(t.get("action"), str):
            try:
                a = json.loads(t["action"])
                if not isinstance(a, dict) or "name" not in a or "arguments" not in a:
                    errs.append(f"{tag}: turn[{j}] action 必须含 name/arguments")
            except json.JSONDecodeError:
                errs.append(f"{tag}: turn[{j}] action 不是合法 JSON 字符串")
        elif t["role"] == "environment" and "content" in t and not isinstance(t["content"], str):
            errs.append(f"{tag}: turn[{j}] environment content 必须是字符串")
    return errs


def check_record(o, where, args, state):
    errs = []
    tag = where

    if not isinstance(o, dict):
        return [f"{tag}: 记录必须是 JSON 对象"]
    if set(o) != REQUIRED:
        errs.append(f"{tag}: 字段必须恰好是 {sorted(REQUIRED)}; 多出 {sorted(set(o) - REQUIRED)}, "
                    f"缺失 {sorted(REQUIRED - set(o))}")
        return errs

    # id / source
    rec_id = o["id"]
    if not isinstance(rec_id, str) or not rec_id.strip():
        errs.append(f"{tag}: id 必须是非空字符串")
        return errs
    if rec_id in state["ids"]:
        errs.append(f"{tag}: id 重复: {rec_id}")
    state["ids"].add(rec_id)
    if not isinstance(o["source_dataset"], str) or not o["source_dataset"].strip():
        errs.append(f"{tag}: source_dataset 必须是非空字符串")
    if not isinstance(o["source_index"], int) or isinstance(o["source_index"], bool):
        errs.append(f"{tag}: source_index 必须是整数")

    # text / hash
    text = o["text"]
    if not isinstance(text, str) or not text.strip():
        errs.append(f"{tag}: text 必须是非空字符串")
        return errs
    if args.min_len and len(text) < args.min_len:
        errs.append(f"{tag}: text 长度 {len(text)} < {args.min_len}")
    if args.max_len and len(text) > args.max_len:
        errs.append(f"{tag}: text 长度 {len(text)} > {args.max_len}")

    if not isinstance(o["source_hash"], str):
        errs.append(f"{tag}: source_hash 必须是十六进制字符串")
        return errs
    h = hashlib.sha256(text.encode()).hexdigest()
    if o["source_hash"] != h:
        errs.append(f"{tag}: source_hash 与 sha256(text) 不一致")

    ck = case_key(rec_id)
    prev_case = state["hashes"].get(o["source_hash"])
    if prev_case is not None and prev_case != ck:
        errs.append(f"{tag}: source_hash 与同批次其他 case 重复（{prev_case} 与 {ck}）")
    state["hashes"].setdefault(o["source_hash"], ck)

    group = state["groups"].setdefault(
        ck, {"hash": o["source_hash"], "index": o["source_index"], "qids": set()})
    if group["hash"] != o["source_hash"]:
        errs.append(f"{tag}: 同一 case 的 text/source_hash 不一致")
    if group["index"] != o["source_index"]:
        errs.append(f"{tag}: 同一 case 的 source_index 不一致")
    qid = qid_of(rec_id)
    if qid is not None:
        group["qids"].add(qid)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            errs.extend(check_turns(parsed, tag))
    except json.JSONDecodeError:
        errs.append(f"{tag}: text 不是合法 JSON（state 必须序列化后写入 text）")

    # qtype / criteria
    qt = o["qtype"]
    if qt not in QTYPES:
        errs.append(f"{tag}: qtype 非法: {qt!r}（只能是 {sorted(QTYPES)}）")
        return errs

    crit = o["criteria"]
    if qt == "noul":
        if not isinstance(crit, dict) or set(crit) != {"true", "false"}:
            errs.append(f"{tag}: noul 的 criteria 必须是 {{'true': ..., 'false': ...}}")
    elif qt == "choice":
        if not isinstance(crit, dict) or len(crit) < 2:
            errs.append(f"{tag}: choice 的 criteria 必须是至少 2 项的映射")
    elif qt == "score":
        if not isinstance(crit, list) or len(crit) < 2:
            errs.append(f"{tag}: score 的 criteria 必须是至少 2 项的列表")

    if not isinstance(o["instruction"], str) or not o["instruction"].strip():
        errs.append(f"{tag}: instruction 必须是非空字符串")

    # taxonomy cross-check
    if args.taxonomy:
        keys = taxonomy_keys(rec_id, args.taxonomy)
        if keys is not None:
            if isinstance(crit, dict) and set(crit) != set(keys):
                errs.append(f"{tag}: criteria 与 taxonomy 不一致: {sorted(crit)} vs {sorted(keys)}")
            elif isinstance(crit, list) and len(crit) != len(keys):
                errs.append(f"{tag}: score 等级数 {len(crit)} 与 taxonomy {len(keys)} 不一致")

    # soft label
    sl = o["soft_label"]
    if not isinstance(sl, dict) or set(sl) != {"probabilities", "label"}:
        errs.append(f"{tag}: soft_label 必须是 {{'probabilities': ..., 'label': ...}}")
        return errs
    label = sl["label"]
    if not isinstance(label, str):
        errs.append(f"{tag}: label 必须是字符串 key")
    probs = sl["probabilities"]
    if not isinstance(probs, dict) or not probs:
        errs.append(f"{tag}: probabilities 必须是非空映射")
        return errs

    opt_keys = set(probs)
    if qt == "noul" and opt_keys != {"true", "false"}:
        errs.append(f"{tag}: noul 的 probabilities key 必须是 true/false, 实际 {sorted(opt_keys)}")
    if qt == "choice" and isinstance(crit, dict) and opt_keys != set(crit):
        errs.append(f"{tag}: probabilities key 与 criteria key 不一致: {sorted(opt_keys)} vs {sorted(crit)}")
    if qt == "score":
        want = [str(i) for i in range(len(crit))] if isinstance(crit, list) else None
        if want is not None and sorted(opt_keys) != sorted(want):
            errs.append(f"{tag}: score 的 probabilities key 必须是 \"0\"..\"{len(crit)-1}\", 实际 {sorted(opt_keys)}")

    vals = []
    for k, v in probs.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"{tag}: probabilities[{k!r}] 不是数字")
            continue
        if v < 0:
            errs.append(f"{tag}: probabilities[{k!r}] 为负: {v}")
        vals.append(float(v))
    if vals:
        s = sum(vals)
        if abs(s - 1.0) > SOFT_TOL:
            errs.append(f"{tag}: probabilities 未归一 (sum={s:.8f})")
        if len(vals) > 1:
            if not args.allow_onehot and max(vals) >= 1.0 - 1e-12:
                errs.append(f"{tag}: 出现 one-hot 软标签; 请使用 0.9/均摊 分布")
            if min(vals) == max(vals):
                errs.append(f"{tag}: probabilities 是均匀分布, 无信号")
            if max(vals) > 0.9 + SOFT_TOL and not args.allow_onehot:
                errs.append(f"{tag}: 主标签 {max(vals):.4f} > 0.9；除真值歧义外不应超过默认值")

    if label not in probs:
        errs.append(f"{tag}: label {label!r} 不在 probabilities 中")
    elif vals and isinstance(label, str):
        top = max(probs, key=lambda k: probs[k])
        if label != top:
            errs.append(f"{tag}: label {label!r} 不是 argmax ({top!r})")

    # leakage + intra-corpus near duplicates
    if args.leak_index is not None:
        j = args.leak_index.worst(text)
        if j > args.leak_index.threshold:
            errs.append(f"{tag}: 与已有语料泄漏/雷同 (jaccard={j:.3f} > {args.leak_index.threshold})")
    if args.intra > 0:
        g = grams(text)
        for pid, pg in state["grams"]:
            if case_key(pid) == ck:
                continue  # same case shares text by contract
            j = jaccard(g, pg)
            if j > args.intra:
                errs.append(f"{tag}: 与 {pid} 近似重复 (jaccard={j:.3f})")
                break
        state["grams"].append((rec_id, g))
    return errs


def state_budget(args):
    """Character budget for ``text`` derived from ruhui's token ``max_len``.

    Measured against the real tokenizer (mmBERT / ruhui-finetuned): a turn-list
    JSON state costs ~0.26 tokens per character, the head (instruction +
    options) is ~136 tokens at ``head_max_len=256``, so a 1024-token window
    keeps roughly 3300 characters of state. The default keeps a safety margin
    because the ratio shifts with tool names and punctuation density.
    """
    if args.max_len:
        return args.max_len
    if not getattr(args, "ruhui_max_len", 0):
        return 0
    return max(0, int(args.ruhui_max_len * 3.2))


def check_benign_directive(case, text, where):
    """Structurally reject a fabricated hard negative.

    ``meta.benign_directive_span`` claims "this clean case contains a real,
    legitimate instruction". A generator under pressure invents one inside an
    injection pretext, which destroys the case. Schema validity cannot depend
    on trusting the model, so check the claim against the text here as well as
    in the diversity audit (this is the layer ``data_writer.py write`` runs).
    """
    meta = case.get("meta")
    if not isinstance(meta, dict):
        return []
    span = meta.get("benign_directive_span")
    if not isinstance(span, str) or not span.strip():
        return []
    errs = []
    if span not in text:
        errs.append(f"{where}: benign_directive_span 不在 text 中；模型自述的 span 必须对应真实文本")
        return errs
    if case.get("safe") is not True:
        errs.append(f"{where}: 违规样本声明了 benign_directive_span；"
                    "注入场景里攻击者不会附赠防御指令，这是伪造的困难负例，"
                    "请删除该字段并改写场景（见 references/anti-shortcut.md）")
        return errs
    payload = meta.get("payload_span")
    if isinstance(payload, str) and payload.strip():
        if span in payload or payload in span:
            errs.append(f"{where}: benign_directive_span 与 payload_span 重叠；"
                        "防御指令不能是载荷的一部分")
    return errs


def taxonomy_keys(rec_id, taxonomy):
    qid = qid_of(rec_id) or rec_id
    q = taxonomy.get("questions", {}).get(qid, taxonomy.get(qid))
    if not q:
        return None
    crit = q.get("criteria")
    if isinstance(crit, dict):
        return list(crit)
    if isinstance(crit, list):
        return [str(i) for i in range(len(crit))]
    return None


# --------------------------------------------------------------------------- #
# case-form validation / expansion
# --------------------------------------------------------------------------- #
def check_case(case, where, args, state):
    """Validate a case-form record (one case = its state plus all labels)."""
    errs = []
    if not isinstance(case, dict):
        return [f"{where}: case 必须是 JSON 对象"]
    missing = CASE_REQUIRED - set(case)
    if missing:
        return [f"{where}: case 缺少字段 {sorted(missing)}"]
    tid = case["traj_id"]
    if not isinstance(tid, str) or not tid.strip():
        errs.append(f"{where}: traj_id 必须是非空字符串")
    elif tid in state["ids"]:
        errs.append(f"{where}: traj_id 重复: {tid}")
    else:
        state["ids"].add(tid)
        meta = case.get("meta")
        if isinstance(meta, dict):
            fam = meta.get("scenario_family")
            if isinstance(fam, str) and fam.strip():
                state["families"][tid] = fam.strip()
    if not isinstance(case["safe"], bool):
        errs.append(f"{where}: safe 必须是 bool")
    turns = case["turns"]
    if not isinstance(turns, list) or len(turns) < 2:
        errs.append(f"{where}: turns 必须是回合列表")
        return errs
    errs.extend(check_turns(turns, where))

    n_tools = 0
    for t in turns:
        if not isinstance(t, dict):
            continue
        if t.get("role") == "agent" and isinstance(t.get("action"), str):
            try:
                a = json.loads(t["action"])
            except json.JSONDecodeError:
                continue
            if isinstance(a, dict) and "name" in a and "arguments" in a:
                n_tools += 1
    if n_tools < 2:
        errs.append(f"{where}: 至少需要 2 次工具调用, 实际 {n_tools}")

    text = json.dumps(turns, ensure_ascii=False)
    if args.min_len and len(text) < args.min_len:
        errs.append(f"{where}: text 长度 {len(text)} < {args.min_len}")
    if args.max_len and len(text) > args.max_len:
        errs.append(f"{where}: text 长度 {len(text)} > {args.max_len}")
    budget = state_budget(args)
    if budget and len(text) > budget:
        errs.append(
            f"{where}: text {len(text)} 字符超过 ruhui 的 max_len 预算 ~{budget} 字符；"
            f"build_sequence 会从尾部截断状态，违规动作通常在轨迹后半段，"
            f"截掉后标签与内容不再对应（见 references/state-formats.md）")

    errs.extend(check_benign_directive(case, text, where))

    # cross-case duplicate / leakage checks
    h = hashlib.sha256(text.encode()).hexdigest()
    prev = state["hashes"].get(h)
    if prev is not None and prev != tid:
        errs.append(f"{where}: 与 {prev} 使用完全相同的 text（重复 case）")
    state["hashes"].setdefault(h, tid)
    if args.leak_index is not None:
        j = args.leak_index.worst(text)
        if j > args.leak_index.threshold:
            errs.append(f"{where}: 与已有语料泄漏/雷同 (jaccard={j:.3f} > {args.leak_index.threshold})")
    if args.intra > 0:
        g = grams(text)
        for pid, pg in state["grams"]:
            if pid == tid:
                continue
            j = jaccard(g, pg)
            if j > args.intra:
                errs.append(f"{where}: 与 {pid} 近似重复 (jaccard={j:.3f})")
                break
        state["grams"].append((tid, g))

    if args.taxonomy:
        for qid, q in args.taxonomy.get("questions", {}).items():
            if q["type"] in ("choice", "score") and qid not in case:
                errs.append(f"{where}: choice/score 问题 {qid!r} 缺少标签")
                continue
            if q["type"] == "choice":
                keys = list(q["criteria"]) if isinstance(q["criteria"], dict) else []
                if keys and case[qid] not in keys:
                    errs.append(f"{where}: {qid}={case[qid]!r} 不在 taxonomy: {keys}")
            if q["type"] == "score":
                n = len(q["criteria"]) if isinstance(q["criteria"], list) else 0
                if not isinstance(case[qid], int) or isinstance(case[qid], bool) or not (0 <= case[qid] < n):
                    errs.append(f"{where}: {qid}={case[qid]!r} 不是有效等级 (0..{n-1})")

    # Optional per-case confidence. The default 0.9 is a template prior; a lower
    # value is how a case says "the evidence itself is contested". It must stay
    # above 0.5 so the label still holds a majority of the mass (renormalisation
    # spreads the rest evenly), and it must name where the uncertainty came from,
    # because a soft label that is soft for no recorded reason is noise.
    errs = check_primary(case, args, errs, where)
    return errs


def case_primary(case, qid=None):
    """Effective primary for one head of a case.

    ``primary`` is either a number (every head) or a mapping with an optional
    ``default`` and per-head overrides. Per-head matters after an adjudication:
    a case can be genuinely unsure about ``is_violation`` while its ``harm_kind``
    is not in doubt, and forcing one number onto every head would claim doubt
    that only some heads have.
    """
    p = case.get("primary", 0.9)
    if isinstance(p, dict):
        p = p.get(qid, p.get("default", 0.9)) if qid is not None else p.get("default", 0.9)
    return float(p)


def check_primary(case, args, errs, where):
    """Validate the optional per-case / per-head confidence."""
    if "primary" not in case:
        return errs
    raw = case["primary"]
    if isinstance(raw, dict):
        allowed = {"default"}
        if args.taxonomy:
            allowed |= set(args.taxonomy.get("questions", {}))
        unknown = set(raw) - allowed
        if unknown:
            errs.append(f"{where}: primary 里有未知的 head {sorted(unknown)}；"
                        f"只允许 default 和 taxonomy 的问题 id")
        for key, value in raw.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not (0.5 < value <= 0.9):
                errs.append(f"{where}: primary[{key!r}] 必须是 (0.5, 0.9] 的数字，"
                            f"实际 {value!r}")
        effective = [raw.get(qid, raw.get("default", 0.9))
                     for qid in (args.taxonomy or {}).get("questions", {})] or list(raw.values())
    else:
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) \
                or not (0.5 < raw <= 0.9):
            errs.append(f"{where}: primary 必须是 (0.5, 0.9] 的数字，实际 {raw!r}；"
                        "低于 0.5 主标签就不再占多数，高于 0.9 是过度自信")
            return errs
        effective = [raw]
    if any(isinstance(v, (int, float)) and not isinstance(v, bool) and v < 0.9
           for v in effective):
        meta = case.get("meta")
        basis = meta.get("primary_basis") if isinstance(meta, dict) else None
        if not isinstance(basis, str) or not basis.strip():
            errs.append(f"{where}: primary < 0.9 但没有 meta.primary_basis；"
                        "降置信度必须写明依据（例如人工仲裁的分歧条数或 κ），"
                        "否则等于随手编一个不确定性")
    return errs


def expand_case(case, index, taxonomy):
    """Expand a {traj_id, safe, turns, <qid>: <label>} case into records.

    Records carry exactly ``REQUIRED`` — nothing else. ``meta`` is the
    generator's diversity plan and the host's salt stamp; it must never reach
    training, and the payload text must stay the original sample. The explicit
    field set below plus the assertion at the end make that a hard contract
    instead of a side effect of how this function happens to be written.
    """
    text = json.dumps(case["turns"], ensure_ascii=False)
    h = hashlib.sha256(text.encode()).hexdigest()
    # 0.9 is the template prior. A case may lower it when the evidence is
    # genuinely contested (a recorded adjudication, see references/soft-labels.md);
    # the remainder is still spread evenly, so the label keeps the majority.
    # A mapping form lowers only the heads the adjudication actually disputed.
    rows = []
    for qid, q in taxonomy["questions"].items():
        qt = q["type"]
        crit = q["criteria"]
        primary = case_primary(case, qid)
        rest = 1.0 - primary
        if qt == "noul":
            pos = bool(case.get("safe") is False or case.get(qid))
            probs = ({"true": primary, "false": rest} if pos
                     else {"true": rest, "false": primary})
            label = "true" if pos else "false"
        elif qt == "choice":
            keys = list(crit)
            k = case[qid]
            probs = {kk: (primary if kk == k else rest / (len(keys) - 1)) for kk in keys}
            label = k
        else:
            keys = [str(i) for i in range(len(crit))]
            k = str(case[qid])
            probs = {kk: (primary if kk == k else rest / (len(keys) - 1)) for kk in keys}
            label = k
        # Fail closed even if a caller skipped check_case: a lowered primary must
        # never move the argmax off the label.
        if label not in probs or probs[label] < max(probs.values()):
            raise ValueError(
                f"{case.get('traj_id')}/{qid}: primary={primary} 让 label {label!r} "
                f"不再是 argmax；primary 必须在 (0.5, 0.9] 内")
        row = {
            "id": f"{case['traj_id']}/{qid}",
            "source_dataset": case.get("source_dataset", "synthetic"),
            "source_index": index,
            "source_hash": h,
            "text": text,
            "qtype": qt,
            "criteria": crit,
            "instruction": q["instructions"],
            "soft_label": {"probabilities": probs, "label": label},
        }
        leaked = set(row) - REQUIRED
        if leaked:  # a future edit must not smuggle meta/salt into training
            raise AssertionError(
                f"expand_case 产出多余字段 {sorted(leaked)}；"
                "meta（含宿主盐章）绝不能进入训练记录")
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# file level
# --------------------------------------------------------------------------- #
def check_file(path, args, state, expand):
    errs = []
    deadline = getattr(args, "deadline", None)
    for lineno, o in read_jsonl(path, deadline):
        where = f"{os.path.basename(path)}:{lineno}"
        if deadline:
            deadline.check(where)
        if "__parse_error__" in o:
            errs.append(f"{where}: JSON 解析失败: {o['__parse_error__']}")
            continue
        if expand:
            e = check_case(o, where, args, state)
            if not e:
                state["cases"].append(o)
            errs.extend(e)
        else:
            errs.extend(check_record(o, where, args, state))
    return errs


def completeness_errors(state, taxonomy):
    """Every case should carry exactly the taxonomy's question ids."""
    if not taxonomy:
        return []
    want = set(taxonomy.get("questions", {}))
    errs = []
    for ck, g in state["groups"].items():
        if not g["qids"]:
            continue
        if g["qids"] != want:
            missing = sorted(want - g["qids"])
            extra = sorted(g["qids"] - want)
            hint = ""
            if missing and len(missing) > 1:
                hint = ("；缺的是一整组 head，该 chunk 多半属于旧 taxonomy 版本"
                        "（按 round 清单的 taxonomy_version 分组后再整合）")
            errs.append(f"{ck}: case 问题集与 taxonomy 不一致; 缺 {missing}, 多 {extra}{hint}")
    return errs


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]


def case_shape(records):
    """(question id, option keys, instruction and criteria fingerprints) per head.

    Head names alone are not enough: a v4 that keeps the same five names but
    changes an option count (e.g. `severity` 4 levels → 3) has the same head set
    and would pass a name-only check, while its records no longer fit the same
    temperature bucket. Option keys are carried verbatim in every record; the
    instruction and criteria are too, so hashing them catches a version that
    only rewords a question — which head ids and option keys cannot see.
    Everything here comes from the record itself, so the check needs no manifest
    and no knowledge of the writer's directory layout.
    """
    shape = []
    for r in records:
        qid = qid_of(r["id"])
        probs = (r.get("soft_label") or {}).get("probabilities")
        if not isinstance(probs, dict) or qid is None:
            continue
        shape.append((qid, tuple(sorted(str(k) for k in probs)),
                      _fingerprint(r.get("instruction")),
                      _fingerprint(r.get("criteria"))))
    return tuple(sorted(shape))


def _shape_summary(shape):
    return ", ".join(f"{qid}[{len(keys)}]:i{ins}:c{crit}"
                     for qid, keys, ins, crit in shape)


def head_set_errors(rows):
    """Reject a corpus whose cases do not all carry the same head shape.

    Shape is the question ids, each question's option keys, and fingerprints of
    its instruction and criteria. Case-form input already fails validation when
    a head is missing, but the record-form path (``--integrate`` without
    ``--expand``) is schema-agnostic, and a head-name-only check would miss both
    a same-name/different-arity version and a reworded question. One corpus,
    one shape.

    Limitation (documented, not checked): a change that touches nothing inside
    the record — e.g. how the trainer interprets a head — is invisible here.
    Group integrations by ``taxonomy_version``; this check is the fallback.
    """
    by_case = defaultdict(list)
    for r in rows:
        by_case[case_key(r["id"])].append(r)
    shapes = Counter(case_shape(recs) for recs in by_case.values())
    shapes.pop((), None)
    if len(shapes) <= 1:
        return []
    shown = " | ".join(f"[{_shape_summary(s)}] ×{n}" for s, n in shapes.most_common(4))
    return [f"同一语料里出现 {len(shapes)} 种 head/选项域/措辞组合：{shown}。"
            "这要么是不同 taxonomy 版本混在一起，要么是同一 head 改了选项数或措辞"
            "（i=instruction 指纹, c=criteria 指纹）；都会让训练信号错配。"
            "按 round 的 taxonomy_version 分开整合"]


def collect(paths, args, expand=False):
    state = new_state()
    errors = []
    for p in paths:
        errors.extend(check_file(p, args, state, expand))
    if not expand:
        errors.extend(completeness_errors(state, args.taxonomy) if args.require_complete_cases else [])
    return errors, state


def load_rows(paths, deadline=None):
    rows = []
    for p in paths:
        for _, o in read_jsonl(p, deadline):
            if deadline:
                deadline.check(f"load {os.path.basename(p)}")
            if isinstance(o, dict) and "__parse_error__" not in o:
                rows.append(o)
    return rows


# --------------------------------------------------------------------------- #
# summary / data card
# --------------------------------------------------------------------------- #
def summarize(rows):
    qtypes = Counter(r["qtype"] for r in rows)
    labels = defaultdict(Counter)
    primaries, lengths = [], []
    for r in rows:
        probs = r["soft_label"]["probabilities"]
        labels[r["qtype"]][r["soft_label"]["label"]] += 1
        primaries.append(max(probs.values()))
        lengths.append(len(r["text"]))
    return {
        "records": len(rows),
        "cases": len({case_key(r["id"]) for r in rows}),
        "qtypes": qtypes,
        "labels": labels,
        "max_primary": max(primaries) if primaries else 0.0,
        "min_primary": min(primaries) if primaries else 0.0,
        "len_min": min(lengths) if lengths else 0,
        "len_median": statistics.median(lengths) if lengths else 0,
        "len_max": max(lengths) if lengths else 0,
    }


def print_summary(s, rows):
    print(f"cases: {s['cases']}  records: {s['records']}")
    print("qtype:", dict(s["qtypes"]))
    for qt in sorted(s["labels"]):
        print(f"  {qt} labels:", dict(s["labels"][qt].most_common()))
    print(f"primary prob: min {s['min_primary']:.3f} / max {s['max_primary']:.3f}")
    print(f"text length: min {s['len_min']} / median {s['len_median']} / max {s['len_max']}")
    print("source_dataset:", dict(Counter(r["source_dataset"] for r in rows)))


def render_card(s, rows, source_desc, taxonomy_version, checks):
    lines = [
        "# Soft-label data card",
        "",
        f"- source: `{source_desc}`",
        f"- taxonomy version: `{taxonomy_version}`",
        f"- cases / records: **{s['cases']} / {s['records']}**",
        f"- text length (min / median / max): {s['len_min']} / {s['len_median']} / {s['len_max']}",
        f"- primary probability (min / max): {s['min_primary']:.3f} / {s['max_primary']:.3f}",
        "",
        "## Question types",
        "",
        "| qtype | records |",
        "| - | - |",
    ]
    for qt, n in sorted(s["qtypes"].items()):
        lines.append(f"| `{qt}` | {n} |")
    lines += ["", "## Label distribution", ""]
    for qt in sorted(s["labels"]):
        lines += [f"### {qt}", "", "| label | count |", "| - | - |"]
        for label, n in s["labels"][qt].most_common():
            lines.append(f"| `{label}` | {n} |")
        lines.append("")
    lines += ["## Checks", "", *[f"- {c}" for c in checks]]
    lines += ["", "## Calibration and base rate", "",
              "- This corpus is balanced by design: the audit caps `safe=false` at half, and "
              "chunks tend to sit exactly at 50/50. Do not read that ratio as a deployment prior.",
              "- Temperature scaling is monotone and **cannot** change the base rate. A temperature "
              "fitted on this split is valid only at this prior; for a different deployment prior, "
              "reweight the calibration split or apply a logit offset by `log(prior_new / prior_here)`, "
              "and report ECE at both priors.",
              ""]
    lines += ["", "## Known limitations", "",
              "- Generated corpus: verify a pilot by hand before scaling (see assets/inspection-report.md).",
              "- Labels come from the generation plan except where a human correction is recorded; "
              "the measured surface-feature count and any correction follow-rate belong next to this card.",
              "- Any metric quoted in or beside this card must carry `(n, feature set, corpus id)`; "
              "a subset measurement and a full-corpus measurement are not comparable.",
              ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# split
# --------------------------------------------------------------------------- #
SPLIT_NAMES = ("train", "calibration", "test")


def parse_ratios(text):
    try:
        ratios = [float(x) for x in text.split(",")]
    except ValueError:
        raise SystemExit(f"--split-ratios 解析失败: {text!r}")
    if len(ratios) != 3 or any(r < 0 for r in ratios) or abs(sum(ratios) - 1.0) > 1e-6:
        raise SystemExit("--split-ratios 需要 3 个非负数且和为 1，例如 0.8,0.1,0.1")
    return ratios


def split_rows(rows, ratios, seed, families=None):
    """Case-disjoint split that also keeps a scenario family in one split.

    Two cases from the same ``scenario_family`` are sibling variants of one
    skeleton: same tools, same organisations, same shape. Splitting by case puts
    one variant in train and another in test, and the held-out score then
    measures memorisation of the template instead of generalisation. When the
    input is case form, ``families`` (traj_id -> family) is used as the grouping
    key, so a whole family lands in one split. Record-form input carries no
    meta, and falls back to case-level grouping with a printed note.
    """
    families = families or {}
    groups = defaultdict(list)
    for r in rows:
        cid = case_key(r["id"])
        groups[families.get(cid) or cid].append(r)
    keys = sorted(groups)
    rng = random.Random(seed)
    rng.shuffle(keys)

    out = defaultdict(list)
    remaining = {name: len(rows) * ratio for name, ratio in zip(SPLIT_NAMES, ratios)}

    def take(name, key):
        out[name].extend(groups[key])
        remaining[name] -= len(groups[key])

    # Seed each non-empty split with the smallest groups, so a single large
    # family cannot become the entire test set.
    seeded = []
    smallest = sorted(keys, key=lambda k: (len(groups[k]), k))
    for name, ratio in zip(SPLIT_NAMES, ratios):
        if ratio <= 0 or not smallest:
            continue
        key = smallest.pop(0)
        take(name, key)
        seeded.append(key)
    for key in keys:
        if key in seeded:
            continue
        # Fill whichever split is furthest below its target share.
        name = max(
            (n for n, ratio in zip(SPLIT_NAMES, ratios) if ratio > 0),
            key=lambda n: remaining[n] / max(1e-9, len(rows) * ratios[SPLIT_NAMES.index(n)]))
        take(name, key)

    assignment = {}
    for name, group_rows in out.items():
        for r in group_rows:
            assignment[case_key(r["id"])] = name
    return {name: out.get(name, []) for name in SPLIT_NAMES}, assignment


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def resolve_inputs(args):
    pattern = args.check or args.chunks
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"没有匹配的文件: {pattern}")
        sys.exit(2)
    return paths


def resolve_leak(args, inputs):
    """Load explicit --leak paths, or opt-in auto-indexing of *soft*.jsonl."""
    deadline = getattr(args, "deadline", None)
    if args.leak_threshold <= 0:
        return LeakIndex([], 0)
    if args.leak:
        return LeakIndex(args.leak, args.leak_threshold, deadline)
    if not args.auto_leak:
        print("[leak] no baseline loaded (pass --leak PATH or --auto-leak)", file=sys.stderr)
        return LeakIndex([], 0)
    inputs_abs = {os.path.abspath(p) for p in inputs}
    if args.out:
        inputs_abs.add(os.path.abspath(args.out))
    if args.split_dir:
        base = os.path.abspath(args.split_dir)  # never index the corpus being split
    elif args.out:
        base = os.path.dirname(os.path.abspath(args.out))
    else:
        base = os.path.dirname(os.path.abspath(inputs[0]))
    cands = [p for p in sorted(glob.glob(os.path.join(base, "*soft*.jsonl")))
             if os.path.abspath(p) not in inputs_abs]
    return LeakIndex(cands, args.leak_threshold, deadline)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", default=None, help="chunk 路径或 glob")
    mode.add_argument("--integrate", action="store_true")
    mode.add_argument("--split-dir", default=None, help="按 case 拆分 train/calibration/test")
    ap.add_argument("--out", default=None)
    ap.add_argument("--chunks", default="chunk_*.jsonl")
    ap.add_argument("--taxonomy", default=None)
    ap.add_argument("--leak", action="append", default=[])
    ap.add_argument("--auto-leak", action="store_true",
                    help="自动索引输出/输入旁边的 *soft*.jsonl（排除被检查文件）")
    ap.add_argument("--leak-threshold", type=float, default=0.30)
    ap.add_argument("--intra-threshold", type=float, default=0.55)
    ap.add_argument("--min-len", type=int, default=0)
    ap.add_argument("--max-len", type=int, default=0)
    ap.add_argument("--ruhui-max-len", type=int, default=0,
                    help="ruhui 训练窗口 token 数（如 1024）；设置后按实测的 "
                         "~3.2 字符/token 折算成 text 字符上限，防止 build_sequence "
                         "从尾部截断掉违规动作")
    ap.add_argument("--allow-onehot", action="store_true")
    ap.add_argument("--expand", action="store_true",
                    help="把 case 形式展开为 ruhui 记录（需要 --taxonomy）")
    ap.add_argument("--require-complete-cases", action="store_true")
    ap.add_argument("--split-ratios", default="0.8,0.1,0.1")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--card", default=None, help="输出 Markdown data card 的路径")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="整次运行的墙钟上限（秒，0=不限）；超时以退出码 3 结束")
    args = ap.parse_args()

    if not (args.check or args.integrate or args.split_dir):
        ap.error("需要 --check、--integrate 或 --split-dir")
    if args.expand and not args.taxonomy:
        ap.error("--expand 需要 --taxonomy")
    if args.taxonomy:
        try:
            with open(args.taxonomy, encoding="utf-8-sig") as f:
                args.taxonomy = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"taxonomy 读取失败: {args.taxonomy}: {exc}", file=sys.stderr)
            sys.exit(2)
    args.intra = args.intra_threshold
    args.leak_index = None
    args.deadline = Deadline(args.timeout)

    try:
        inputs = resolve_inputs(args)
        args.leak_index = resolve_leak(args, inputs)
        errors, state = collect(inputs, args, expand=args.expand)
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}", file=sys.stderr)
        sys.exit(3)
    if errors:
        label = "校验" if args.check else "整合前校验"
        print(f"{label}失败 ({len(errors)} errors)")
        for e in errors[:60]:
            print(" -", e)
        if len(errors) > 60:
            print(f" ... 还有 {len(errors) - 60} 条")
        sys.exit(1)

    if args.expand:
        rows = []
        for i, case in enumerate(state["cases"]):
            rows.extend(expand_case(case, i, args.taxonomy))
    else:
        rows = load_rows(inputs, args.deadline)

    if args.split_dir or args.integrate:
        # Case-form input already rejects a half-labelled case, but record-form
        # input is schema-agnostic: mixed versions would merge into one product
        # and the version information is gone by the time `merge` would look.
        shape_errors = head_set_errors(rows)
        if shape_errors:
            for err in shape_errors:
                print(f" - {err}")
            print("整合/切分失败：这不是可以自动修复的问题，按版本分组后重跑")
            sys.exit(1)

    checks = [
        f"schema and soft-label validation: passed ({len(errors)} errors)",
        f"case-group integrity: passed ({len({case_key(r['id']) for r in rows})} cases)",
        f"intra-corpus near-duplicate threshold: {args.intra_threshold}",
        f"leakage threshold: {args.leak_threshold} "
        f"({len(args.leak_index.grams)} reference records, "
        f"{'explicit --leak' if args.leak else 'auto *soft*.jsonl' if args.auto_leak else 'no baseline'})",
    ]
    s = summarize(rows)
    taxonomy_version = args.taxonomy.get("version", "unversioned") if args.taxonomy else "n/a"

    if args.split_dir:
        ratios = parse_ratios(args.split_ratios)
        families = state.get("families") or {}
        splits, assignment = split_rows(rows, ratios, args.seed, families)
        group_by = "scenario_family" if families else "case"
        os.makedirs(args.split_dir, exist_ok=True)
        manifest = {
            "seed": args.seed,
            "ratios": ratios,
            "taxonomy_version": taxonomy_version,
            "group_by": group_by,
            "groups": len({families.get(case_key(r["id"])) or case_key(r["id"])
                           for r in rows}),
            "splits": {},
        }
        for name in SPLIT_NAMES:
            path = os.path.join(args.split_dir, f"{name}.jsonl")
            atomic_write_text(path, "".join(
                json.dumps(r, ensure_ascii=False) + "\n" for r in splits[name]))
            case_ids = sorted({case_key(r["id"]) for r in splits[name]})
            manifest["splits"][name] = {
                "file": f"{name}.jsonl",
                "cases": len(case_ids),
                "records": len(splits[name]),
                "case_ids_sha256": hashlib.sha256("\n".join(case_ids).encode()).hexdigest(),
            }
        # Disjointness is guaranteed by case-level assignment; assert it anyway.
        seen = {}
        for name in SPLIT_NAMES:
            for cid in {case_key(r["id"]) for r in splits[name]}:
                if cid in seen:
                    raise SystemExit(f"split bug: {cid} 同时出现在 {seen[cid]} 和 {name}")
                seen[cid] = name
        atomic_write_text(os.path.join(args.split_dir, "split_manifest.json"),
                          json.dumps(manifest, indent=2, ensure_ascii=False))
        print(f"split -> {args.split_dir} (seed={args.seed}, ratios={ratios}, "
              f"group_by={group_by}, {manifest['groups']} groups)")
        for name in SPLIT_NAMES:
            m = manifest["splits"][name]
            print(f"  {name}: {m['cases']} cases / {m['records']} records -> {m['file']}")
        print("  请在拆分后运行泄漏检查，例如：")
        print(f"  python3 {os.path.basename(__file__)} --check '{args.split_dir}/test.jsonl' "
              f"--leak '{args.split_dir}/train.jsonl' --leak '{args.split_dir}/calibration.jsonl'")
    elif args.integrate:
        out = args.out or "all_soft.jsonl"
        atomic_write_text(out, "".join(
            json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        print(f"整合完成: {len(inputs)} chunks, {len(rows)} records -> {out}")
    else:
        print(f"OK: {len(rows)} records, {len({case_key(r['id']) for r in rows})} cases")

    print_summary(s, rows)
    if args.card:
        source_desc = ", ".join(inputs)
        atomic_write_text(args.card, render_card(s, rows, source_desc, taxonomy_version, checks))
        print(f"data card -> {args.card}")


if __name__ == "__main__":
    main()
