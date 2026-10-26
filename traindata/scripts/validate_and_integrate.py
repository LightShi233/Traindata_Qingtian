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


def read_jsonl(path):
    """Yield (lineno, object) with a parse-error sentinel; UTF-8 always."""
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError as e:
                yield i, {"__parse_error__": str(e)}


class LeakIndex:
    """Lazily-loaded n-gram index over one or more reference corpora."""

    def __init__(self, paths, threshold):
        self.threshold = threshold
        self.grams = []
        if threshold <= 0:
            return
        for p in paths:
            if not p or not os.path.exists(p):
                continue
            n = 0
            for _, o in read_jsonl(p):
                if isinstance(o.get("text"), str):
                    self.grams.append(grams(o["text"]))
                    n += 1
            print(f"[leak] indexed {n} records from {p}", file=sys.stderr)

    def worst(self, text):
        best = 0.0
        g = grams(text)
        ng = len(g)
        for ref in self.grams:
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
    return {"hashes": {}, "ids": set(), "grams": [], "cases": [], "groups": {}}


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


def taxonomy_keys(rec_id, taxonomy):
    """Return expected option keys for a record, or None if unknown."""
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
    return errs


def expand_case(case, index, taxonomy):
    """Expand a {traj_id, safe, turns, <qid>: <label>} case into records."""
    text = json.dumps(case["turns"], ensure_ascii=False)
    h = hashlib.sha256(text.encode()).hexdigest()
    rows = []
    for qid, q in taxonomy["questions"].items():
        qt = q["type"]
        crit = q["criteria"]
        if qt == "noul":
            pos = bool(case.get("safe") is False or case.get(qid))
            probs = ({"true": 0.9, "false": 0.1} if pos else {"true": 0.1, "false": 0.9})
            label = "true" if pos else "false"
        elif qt == "choice":
            keys = list(crit)
            k = case[qid]
            probs = {kk: (0.9 if kk == k else 0.1 / (len(keys) - 1)) for kk in keys}
            label = k
        else:
            keys = [str(i) for i in range(len(crit))]
            k = str(case[qid])
            probs = {kk: (0.9 if kk == k else 0.1 / (len(keys) - 1)) for kk in keys}
            label = k
        rows.append({
            "id": f"{case['traj_id']}/{qid}",
            "source_dataset": case.get("source_dataset", "synthetic"),
            "source_index": index,
            "source_hash": h,
            "text": text,
            "qtype": qt,
            "criteria": crit,
            "instruction": q["instructions"],
            "soft_label": {"probabilities": probs, "label": label},
        })
    return rows


# --------------------------------------------------------------------------- #
# file level
# --------------------------------------------------------------------------- #
def check_file(path, args, state, expand):
    errs = []
    for lineno, o in read_jsonl(path):
        where = f"{os.path.basename(path)}:{lineno}"
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
            errs.append(f"{ck}: case 问题集与 taxonomy 不一致; 缺 {missing}, 多 {extra}")
    return errs


def collect(paths, args, expand=False):
    state = new_state()
    errors = []
    for p in paths:
        errors.extend(check_file(p, args, state, expand))
    if not expand:
        errors.extend(completeness_errors(state, args.taxonomy) if args.require_complete_cases else [])
    return errors, state


def load_rows(paths):
    rows = []
    for p in paths:
        for _, o in read_jsonl(p):
            if "__parse_error__" not in o:
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
    lines += ["", "## Known limitations", "",
              "- Generated corpus: verify a pilot by hand before scaling (see assets/inspection-report.md).",
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


def allocate(n, ratios):
    """Integer split sizes, leaving at least one case per non-zero split when possible."""
    sizes = [0, 0, 0]
    active = [i for i, r in enumerate(ratios) if r > 0]
    if n < len(active):
        for i in active[:n]:
            sizes[i] = 1
        return sizes
    remaining = n
    for pos, i in enumerate(active):
        if pos == len(active) - 1:
            sizes[i] = remaining
        else:
            need = len(active) - pos - 1
            k = int(round(n * ratios[i]))
            sizes[i] = max(1, min(k, remaining - need))
            remaining -= sizes[i]
    return sizes


def split_rows(rows, ratios, seed):
    groups = defaultdict(list)
    for r in rows:
        groups[case_key(r["id"])].append(r)
    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    sizes = allocate(len(keys), ratios)
    assignment = {}
    pos = 0
    for name, size in zip(SPLIT_NAMES, sizes):
        for key in keys[pos:pos + size]:
            assignment[key] = name
        pos += size
    out = defaultdict(list)
    for r in rows:
        out[assignment[case_key(r["id"])]].append(r)
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
    if args.leak_threshold <= 0:
        return LeakIndex([], 0)
    if args.leak:
        return LeakIndex(args.leak, args.leak_threshold)
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
    return LeakIndex(cands, args.leak_threshold)


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
    ap.add_argument("--allow-onehot", action="store_true")
    ap.add_argument("--expand", action="store_true",
                    help="把 case 形式展开为 ruhui 记录（需要 --taxonomy）")
    ap.add_argument("--require-complete-cases", action="store_true")
    ap.add_argument("--split-ratios", default="0.8,0.1,0.1")
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--card", default=None, help="输出 Markdown data card 的路径")
    args = ap.parse_args()

    if not (args.check or args.integrate or args.split_dir):
        ap.error("需要 --check、--integrate 或 --split-dir")
    if args.expand and not args.taxonomy:
        ap.error("--expand 需要 --taxonomy")
    if args.taxonomy:
        with open(args.taxonomy, encoding="utf-8") as f:
            args.taxonomy = json.load(f)
    args.intra = args.intra_threshold
    args.leak_index = None

    inputs = resolve_inputs(args)
    args.leak_index = resolve_leak(args, inputs)

    errors, state = collect(inputs, args, expand=args.expand)
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
        rows = load_rows(inputs)

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
        splits, assignment = split_rows(rows, ratios, args.seed)
        os.makedirs(args.split_dir, exist_ok=True)
        manifest = {
            "seed": args.seed,
            "ratios": ratios,
            "taxonomy_version": taxonomy_version,
            "splits": {},
        }
        for name in SPLIT_NAMES:
            path = os.path.join(args.split_dir, f"{name}.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for r in splits[name]:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
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
        with open(os.path.join(args.split_dir, "split_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"split -> {args.split_dir} (seed={args.seed}, ratios={ratios})")
        for name in SPLIT_NAMES:
            m = manifest["splits"][name]
            print(f"  {name}: {m['cases']} cases / {m['records']} records -> {m['file']}")
        print("  请在拆分后运行泄漏检查，例如：")
        print(f"  python3 {os.path.basename(__file__)} --check '{args.split_dir}/test.jsonl' "
              f"--leak '{args.split_dir}/train.jsonl' --leak '{args.split_dir}/calibration.jsonl'")
    elif args.integrate:
        out = args.out or "all_soft.jsonl"
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"整合完成: {len(inputs)} chunks, {len(rows)} records -> {out}")
    else:
        print(f"OK: {len(rows)} records, {len({case_key(r['id']) for r in rows})} cases")

    print_summary(s, rows)
    if args.card:
        os.makedirs(os.path.dirname(os.path.abspath(args.card)), exist_ok=True)
        source_desc = ", ".join(inputs)
        with open(args.card, "w", encoding="utf-8") as f:
            f.write(render_card(s, rows, source_desc, taxonomy_version, checks))
        print(f"data card -> {args.card}")


if __name__ == "__main__":
    main()
