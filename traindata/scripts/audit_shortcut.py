#!/usr/bin/env python3
"""Measure how far a case-form corpus is separable by surface features alone.

The schema validator and the diversity audit both ask "is this chunk legal?".
This probe asks a different question, and answers it with a number:

    could a classifier that understands nothing reach the label from the
    surface form — which tool verbs appear, how long the trajectory is, how
    many hedged sentences it contains?

That is the measured version of the shortcut-learning worry. A corpus can pass
both gates and still be worthless for training if a bag of nouns predicts its
labels, because a trained model will find the same feature and skip the rule the
data was supposed to teach.

Method (deliberately small, stdlib only, deterministic):

1. Each case becomes a fixed feature vector, derived from ``turns`` only. No
   ``meta``, because ``meta`` is model-authored: a generator that wanted to pass
   a gate could fill it in.
2. Per-feature AUC against ``safe`` (1 = unsafe), plus one L2 logistic
   regression over the whole vector.
3. Combined AUC and k-fold cross-validated accuracy, so the number is not the
   in-sample fit of six features on sixty cases.

Interpretation, not gates:

- AUC near 0.5: the label is not visible in these features; whatever the model
  learns has to come from the text.
- AUC near 0.8+: a lookup table over verbs and lengths gets most labels right.
  The corpus is teaching "long and destructive-looking = violation". Fix the
  data (cross-map the verbs, pad the clean trajectories), not this script.
- The per-feature column says *which* feature to cross-map first. In the audit
  that produced this probe, tool verbs dominated and email domains were noise.

This script never edits data and prints no case text, so its report is safe to
emit into a public CI log even when the corpus is private.

Exit code: 0 always, unless ``--fail-above`` is set and the combined AUC meets
it on at least ``--min-cases`` cases. A per-chunk chunk of 8–12 cases has an AUC
standard error far too large to gate on; use the threshold on a merged corpus.

Usage
-----
    python3 audit_shortcut.py --chunks 'data/chunks/chunk_*.jsonl'
    python3 audit_shortcut.py --chunks 'data/rounds/<id>/soft_*.jsonl' \
        --json work/shortcut.json --fail-above 0.85 --min-cases 40
"""
import argparse
import glob
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataloader import Deadline, atomic_write_text, load_cases  # noqa: E402

# Coarse verb classes. Three is enough to answer "does this feature track the
# label?"; a full tool ontology is a separate problem (and a much larger one).
DESTRUCTIVE = {
    "delete", "deleted", "drop", "purge", "destroy", "wipe", "erase", "remove",
    "truncate", "overwrite", "reset", "cancel", "revoke", "deactivate",
}
EGRESS = {
    "send", "sent", "email", "mail", "post", "publish", "upload", "export",
    "download", "share", "transfer", "forward", "webhook", "transmit", "publish",
}
ADMIN = {
    "grant", "permission", "permissions", "role", "roles", "privilege",
    "privileges", "escalate", "admin", "administrator", "disable", "enable",
    "override", "bypass", "sudo", "impersonate",
}
TOOL_CLASSES = (("destructive", DESTRUCTIVE), ("egress", EGRESS), ("admin", ADMIN))

# A concession/qualifier voice. The audit that motivated this probe measured a
# *negative* coefficient for it: agents that write "however, this stays in
# scope" mostly did the right thing, so the phrase is not a violation marker.
HEDGE = (
    r"\bhowever\b", r"\bbut\b", r"\bonly if\b", r"\bonly after\b", r"\bunless\b",
    r"\bwithin scope\b", r"\bin scope\b", r"\bconfirm\b", r"\bverify\b",
    r"\bdouble[- ]check\b", r"\bnot yet\b", r"\bpending\b", r"\bbefore\b",
    r"但是", r"不过", r"仅", r"只能", r"需要先", r"确认", r"核实", r"范围内",
)

HEDGE_RE = re.compile("|".join(HEDGE), re.I)
ACTION_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
WORD_RE = re.compile(r"[a-z]+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _turn_action_name(turn):
    """The tool name of an agent turn, whether stored as dict or JSON string."""
    action = turn.get("action")
    if isinstance(action, dict):
        name = action.get("name")
        return name if isinstance(name, str) else ""
    if isinstance(action, str):
        m = ACTION_NAME_RE.search(action)
        if m:
            return m.group(1)
    return ""


def _tool_classes(name):
    tokens = set(WORD_RE.findall(name.lower()))
    return {cls for cls, lexicon in TOOL_CLASSES if tokens & lexicon}


def case_features(case):
    """Surface features for one case. Nothing here reads ``meta``."""
    turns = case.get("turns") or []
    classes = set()
    tool_calls = 0
    hedges = 0
    agent_chars = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        if turn.get("role") == "agent":
            name = _turn_action_name(turn)
            if name:
                tool_calls += 1
                classes |= _tool_classes(name)
            voice = " ".join(
                t for t in (turn.get("thought"), turn.get("content"))
                if isinstance(t, str))
            hedges += len(HEDGE_RE.findall(voice))
            agent_chars += len(voice)
    text = json.dumps(turns, ensure_ascii=False)
    return {
        "destructive_verb": 1.0 if "destructive" in classes else 0.0,
        "egress_verb": 1.0 if "egress" in classes else 0.0,
        "admin_verb": 1.0 if "admin" in classes else 0.0,
        "tool_calls": float(tool_calls),
        "log_length": math.log1p(len(text)),
        "hedge_count": float(hedges),
        "has_address": 1.0 if EMAIL_RE.search(text) else 0.0,
        "agent_chars": math.log1p(agent_chars),
    }


FEATURE_ORDER = [
    "destructive_verb", "egress_verb", "admin_verb",
    "tool_calls", "log_length", "hedge_count", "has_address", "agent_chars",
]


def auc(scores, labels):
    """Rank-based AUC with tie handling; labels are 1 = positive (unsafe)."""
    pairs = list(zip(scores, labels))
    n_pos = sum(1 for _, y in pairs if y == 1)
    n_neg = len(pairs) - n_pos
    if not n_pos or not n_neg:
        return float("nan")
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
    ranks = [0.0] * len(pairs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and pairs[order[j + 1]][0] == pairs[order[i]][0]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    rank_sum = sum(r for r, (_, y) in zip(ranks, pairs) if y == 1)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def standardize(rows):
    m = len(rows[0])
    n = len(rows)
    means = [sum(r[j] for r in rows) / n for j in range(m)]
    sds = []
    for j in range(m):
        var = sum((r[j] - means[j]) ** 2 for r in rows) / n
        sds.append(math.sqrt(var) or 1.0)
    return [[(r[j] - means[j]) / sds[j] for j in range(m)] for r in rows]


def fit_logreg(X, y, iters=600, lr=0.5, l2=1e-3):
    """Full-batch L2 logistic regression from a zero start (deterministic)."""
    m, n = len(X[0]), len(X)
    w = [0.0] * m
    b = 0.0
    for _ in range(iters):
        grad = [0.0] * m
        gb = 0.0
        for xi, yi in zip(X, y):
            z = b + sum(wj * xj for wj, xj in zip(w, xi))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            d = p - yi
            for j in range(m):
                grad[j] += d * xi[j]
            gb += d
        for j in range(m):
            w[j] -= lr * (grad[j] / n + l2 * w[j])
        b -= lr * gb / n
    return w, b


def predict(w, b, x):
    z = b + sum(wj * xj for wj, xj in zip(w, x))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def cv_scores(X, y, folds):
    """Out-of-fold predictions. Returns (pooled scores, accuracy, folds used).

    The in-sample fit of eight features on forty cases is meaningless — it can
    separate pure noise. Every verdict below is computed from these held-out
    predictions; the in-sample coefficients are printed only to say which
    feature moved.
    """
    n = len(X)
    folds = max(2, min(folds, n // 4 or 2))
    scores = [float("nan")] * n
    correct = total = 0
    for k in range(folds):
        train = [i for i in range(n) if i % folds != k]
        test = [i for i in range(n) if i % folds == k]
        if not train or not test or len({y[i] for i in train}) < 2:
            continue
        w, b = fit_logreg([X[i] for i in train], [y[i] for i in train], iters=400)
        for i in test:
            s = predict(w, b, X[i])
            scores[i] = s
            correct += (1 if s >= 0.5 else 0) == y[i]
            total += 1
    accuracy = (correct / total) if total else float("nan")
    return scores, accuracy, folds


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chunks", default="data/chunks/chunk_*.jsonl",
                    help="case 形式 chunk 的 glob（可重复逗号分隔）")
    ap.add_argument("--json", default=None, help="写机器可读报告")
    ap.add_argument("--timeout", type=float, default=0.0, help="墙钟上限（秒，0=不限）")
    ap.add_argument("--fail-above", type=float, default=0.0,
                    help="合并 AUC 达到该值时以非 0 退出（默认 0 = 只报告）")
    ap.add_argument("--min-cases", type=int, default=40,
                    help="少于该样本数时不据阈值判失败（AUC 标准误太大）")
    args = ap.parse_args()

    deadline = Deadline(args.timeout)
    paths = []
    for part in args.chunks.split(","):
        paths.extend(sorted(glob.glob(part.strip())))
    if not paths:
        print(f"没有匹配的文件: {args.chunks}")
        return 2

    cases, errors, skipped = load_cases(paths, deadline)
    if errors:
        for err in errors[:20]:
            print(f"FAIL [parse] {err}")
        return 1
    rows = [(where, c) for where, c in cases if isinstance(c.get("turns"), list)]
    if skipped:
        print(f"[note] 跳过 {skipped} 条 record 形式记录；探针需要 case 形式")
    labelled = [(w, c) for w, c in rows if isinstance(c.get("safe"), bool)]
    if len(labelled) < 8:
        print(f"样本不足: {len(labelled)} cases；探针需要至少 8 个带 safe 的 case")
        return 2

    deadline.check("feature extraction")
    matrix_raw = [[case_features(c)[f] for f in FEATURE_ORDER] for _, c in labelled]
    X = standardize(matrix_raw)
    y = [0 if c.get("safe") is True else 1 for _, c in labelled]
    n_pos = sum(y)
    n_neg = len(y) - n_pos
    if not n_pos or not n_neg:
        print(f"标签单一: safe=false {n_pos} / safe=true {n_neg}；无法测量可分性")
        return 2

    per_feature = {}
    for j, name in enumerate(FEATURE_ORDER):
        per_feature[name] = auc([row[j] for row in matrix_raw], y)

    deadline.check("logistic regression")
    w, b = fit_logreg(X, y)
    # Coefficients come from the full fit (they only say which feature moved);
    # every headline number comes from held-out predictions.
    folds = 5 if len(y) >= 40 else max(2, len(y) // 8)
    oof, cv, folds = cv_scores(X, y, folds)
    scored = [(s, yy) for s, yy in zip(oof, y) if not math.isnan(s)]
    combined_auc = auc([s for s, _ in scored], [yy for _, yy in scored]) \
        if scored else float("nan")
    baseline = max(n_pos, n_neg) / len(y)
    verdict_auc = combined_auc if not math.isnan(combined_auc) else 0.5

    print(f"surface probe: {len(y)} cases ({n_pos} unsafe / {n_neg} safe), "
          f"{len(paths)} file(s)")
    print(f"  {'feature':<18} {'AUC':>6}  coef(std)")
    for j, name in enumerate(FEATURE_ORDER):
        auc_v = per_feature[name]
        flag = "  <-- near-noise" if abs(auc_v - 0.5) < 0.06 else ""
        print(f"  {name:<18} {auc_v:6.3f}  {w[j]:+7.3f}{flag}")
    print(f"  {'combined':<18} {combined_auc:6.3f}  "
          f"{folds}-fold CV acc {cv:.3f} (majority baseline {baseline:.3f})")

    verdict = "OK"
    if verdict_auc >= 0.80:
        verdict = "SUSCEPTIBLE"
    elif verdict_auc >= 0.70:
        verdict = "WEAK"
    print(f"verdict: {verdict}")
    if verdict != "OK":
        top = sorted(per_feature.items(), key=lambda kv: -abs(kv[1] - 0.5))[:2]
        print("  最该先交叉映射的特征: " + ", ".join(f"{k} (AUC {v:.3f})" for k, v in top))
        print("  做法：同一个工具动词类 / 同等长度的轨迹必须同时出现在 safe=true 与 "
              "safe=false 里；见 references/label-provenance.md")
    if len(y) < 30:
        print(f"  [note] 只有 {len(y)} 个 case，AUC 标准误很大；这个数只当方向看")

    report = {
        "cases": len(y),
        "unsafe": n_pos,
        "safe": n_neg,
        "per_feature_auc": per_feature,
        "coefficients_std": dict(zip(FEATURE_ORDER, w)),
        "combined_auc": combined_auc,
        "cv_accuracy": cv,
        "majority_baseline": baseline,
        "folds": folds,
        "verdict": verdict,
        "files": [os.path.basename(p) for p in paths],
    }
    if args.json:
        atomic_write_text(args.json, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(f"report -> {args.json}")

    if args.fail_above and len(y) >= args.min_cases and verdict_auc >= args.fail_above:
        print(f"FAIL: 留出预测 AUC {verdict_auc:.3f} >= {args.fail_above:.3f}；"
              f"语料仍可被表面特征区分（{len(y)} cases >= {args.min_cases}）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())