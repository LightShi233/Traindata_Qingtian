#!/usr/bin/env python3
"""The only writer the generator is allowed to use.

A model asked to "generate training data" will happily invent a generator
script (``author.py``), write files wherever it likes, and commit its scratch
files. This module removes that freedom. It is the fixed tooling; the model's
only job is to emit **YAML** into the slot this tool hands it.

Round model
-----------
One generation round owns a manifest at ``data/rounds/<round>.json``. The
manifest records every chunk name allocated to that round, its YAML slot, its
JSONL target, and whether it has been written. Merge reads *only* that
manifest, so a round can never sweep in another round's or a hand-written file.

    # 1. reserve the next names (also prints where to put the YAML)
    python3 traindata/scripts/data_writer.py allocate --count 10

    # 2. after the model writes data/inbox/chunk_0003.yaml
    python3 traindata/scripts/data_writer.py write --round <id> \
      --name chunk_0003 --in data/inbox/chunk_0003.yaml --taxonomy data/taxonomy.json

    # 3. merge ONLY this round's written chunks, expanding to records
    python3 traindata/scripts/data_writer.py merge --round <id> \
      --taxonomy data/taxonomy.json

    # 4. verify the round left no stray chunk / temp file / invented script
    python3 traindata/scripts/data_writer.py check --round <id> --strict

Rules this tool enforces
------------------------
- Every output lives under ``./data``. A path outside it is rejected.
- Chunk names are ``chunk_<4+ digits>`` and are allocated, never guessed;
  ``write`` requires the YAML to sit at the exact allocated path.
- An existing JSONL is never overwritten; allocate a new name instead.
- ``merge`` refuses to include anything not written by this round.
- ``check`` fails on unallocated chunk files, temp/backup files, and any
  model-authored ``.py`` under ``data/``.

Exit codes: 0 ok, 1 rule violation, 2 usage/missing round, 3 timeout.
"""
import argparse
import contextlib
import datetime
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataloader import Deadline, atomic_write_text  # noqa: E402
import validate_and_integrate as vi  # noqa: E402
import yaml_to_jsonl as y2j  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_DATA = os.path.join(REPO_ROOT, "data")

CHUNK_RE = re.compile(r"^chunk_(\d{4,})\.(jsonl|yaml|yml)$")
ROUND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
TEMP_RE = re.compile(r"(^\.tmp-|\.tmp$|\.bak$|\.orig$|\.rej$|~\$?$|\.swp$|\.pyc$|^\.DS_Store$|^__pycache__$)")
ALLOWED_ROOT_FILES = {"taxonomy.json", "ruhui_soft_all.jsonl", "ruhui_soft_card.md"}
ALLOWED_ROOT_DIRS = {"chunks", "inbox", "split", "rounds"}
ALLOWED_SPLIT = {"train.jsonl", "calibration.jsonl", "test.jsonl", "split_manifest.json"}
ROOT_PY_ALLOWED = set()  # this repo has no top-level Python entry point


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _rel(data_dir, path):
    return os.path.relpath(path, data_dir).replace(os.sep, "/")


def _display(path):
    """Repo-relative when inside the repo, absolute otherwise."""
    absolute = os.path.abspath(path)
    try:
        if os.path.commonpath([absolute, REPO_ROOT]) == REPO_ROOT:
            return os.path.relpath(absolute, REPO_ROOT)
    except ValueError:
        pass
    return absolute


def _abs(data_dir, rel):
    return os.path.abspath(os.path.join(data_dir, rel))


def require_under(path, data_dir, what):
    """Reject any path that escapes ``./data``."""
    target = os.path.abspath(path)
    root = os.path.abspath(data_dir)
    if os.path.commonpath([target, root]) != root:
        raise SystemExit(f"{what} 必须在 {os.path.relpath(root, REPO_ROOT)} 下，拒绝: {path}")
    return target


@contextlib.contextmanager
def _lock(data_dir):
    """Serialise manifest updates between parallel subagents."""
    rounds_dir = os.path.join(data_dir, "rounds")
    os.makedirs(rounds_dir, exist_ok=True)
    handle = open(os.path.join(rounds_dir, ".lock"), "a+")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def round_file(data_dir, round_id):
    return os.path.join(data_dir, "rounds", f"{round_id}.json")


def load_round(data_dir, round_id):
    path = round_file(data_dir, round_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_round(data_dir, manifest):
    atomic_write_text(round_file(data_dir, manifest["round"]),
                      json.dumps(manifest, indent=2, ensure_ascii=False))


def chunk_numbers(directory):
    numbers = set()
    if not os.path.isdir(directory):
        return numbers
    for name in os.listdir(directory):
        match = CHUNK_RE.match(name)
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def all_allocated_numbers(data_dir):
    numbers = set()
    for path in glob.glob(os.path.join(data_dir, "rounds", "*.json")):
        try:
            with open(path, encoding="utf-8-sig") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for allocation in manifest.get("allocations", []):
            match = CHUNK_RE.match(allocation.get("name", ""))
            if match:
                numbers.add(int(match.group(1)))
    return numbers


def baseline_chunks(data_dir):
    found = []
    for sub in ("chunks", "inbox"):
        directory = os.path.join(data_dir, sub)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if CHUNK_RE.match(name):
                found.append(f"{sub}/{name}")
    return found


# --------------------------------------------------------------------------- #
# allocate
# --------------------------------------------------------------------------- #
def cmd_allocate(args):
    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(os.path.join(data_dir, "chunks"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "inbox"), exist_ok=True)
    os.makedirs(os.path.join(data_dir, "rounds"), exist_ok=True)

    round_id = args.round or f"r-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}-{os.urandom(3).hex()}"
    if not ROUND_RE.match(round_id):
        raise SystemExit(f"非法 round id: {round_id!r}（只用字母数字 . _ -）")

    with _lock(data_dir):
        manifest = load_round(data_dir, round_id)
        if manifest is None:
            manifest = {
                "round": round_id,
                "created_at": _now(),
                "baseline": baseline_chunks(data_dir),
                "allocations": [],
            }
        used = chunk_numbers(os.path.join(data_dir, "chunks"))
        used |= chunk_numbers(os.path.join(data_dir, "inbox"))
        used |= all_allocated_numbers(data_dir)
        start = (max(used) + 1) if used else 1
        allocated = []
        for offset in range(args.count):
            name = f"chunk_{start + offset:04d}"
            allocation = {
                "name": name,
                "yaml": f"inbox/{name}.yaml",
                "jsonl": f"chunks/{name}.jsonl",
                "status": "pending",
                "worker": args.worker,
                "allocated_at": _now(),
            }
            manifest["allocations"].append(allocation)
            allocated.append(allocation)
        manifest["updated_at"] = _now()
        save_round(data_dir, manifest)

    if args.json:
        print(json.dumps({"round": round_id, "manifest": _display(round_file(data_dir, round_id)),
                          "allocations": allocated}, indent=2, ensure_ascii=False))
        return 0
    print(f"round: {round_id}")
    print(f"manifest: {_display(round_file(data_dir, round_id))}")
    for allocation in allocated:
        print(f"  {allocation['name']}  yaml={_display(_abs(data_dir, allocation['yaml']))}"
              f"  jsonl={_display(_abs(data_dir, allocation['jsonl']))}")
    print("只允许把 YAML 写到上面的 yaml 路径；不要新建脚本或其它文件。")
    return 0


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def cmd_write(args):
    data_dir = os.path.abspath(args.data_dir)
    if not ROUND_RE.match(args.round):
        raise SystemExit(f"非法 round id: {args.round!r}")
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}；先运行 allocate")
    allocation = next((a for a in manifest["allocations"] if a["name"] == args.name), None)
    if allocation is None:
        raise SystemExit(f"{args.name} 不属于 round {args.round}；"
                         "禁止使用未分配的名字，先运行 allocate")

    expected_yaml = require_under(_abs(data_dir, allocation["yaml"]), data_dir, "YAML 输入")
    expected_jsonl = require_under(_abs(data_dir, allocation["jsonl"]), data_dir, "JSONL 输出")
    if os.path.normpath(os.path.abspath(args.infile)) != os.path.normpath(expected_yaml):
        raise SystemExit(f"YAML 必须写在分配好的位置: {_display(expected_yaml)}\n"
                         f"  实际传入: {args.infile}")
    if not os.path.exists(expected_yaml):
        raise SystemExit(f"YAML 不存在: {_display(expected_yaml)}")
    if os.path.exists(expected_jsonl):
        raise SystemExit(f"{_display(expected_jsonl)} 已存在；"
                         "不得覆盖已产出的 chunk。需要重做请 allocate 一个新名字。")

    taxonomy = args.taxonomy or os.path.join(data_dir, "taxonomy.json")
    if not os.path.exists(taxonomy):
        raise SystemExit(f"taxonomy 不存在: {taxonomy}（用 --taxonomy 指定）")
    with open(taxonomy, encoding="utf-8-sig") as f:
        taxonomy_obj = json.load(f)

    deadline = Deadline(args.timeout)
    try:
        payload, summary, errors = y2j.convert(
            [expected_yaml], taxonomy_obj, expand=False, deadline=deadline,
            force_validate=True, ruhui_max_len=args.ruhui_max_len)
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}", file=sys.stderr)
        return 3
    if errors:
        print(f"{args.name} 拒绝写入 ({len(errors)} errors)")
        for err in errors[:40]:
            print(" -", err)
        print("修好 YAML 后重跑 write；不要手改 JSONL。")
        return 1

    # Re-read under the lock: another subagent may have written its own chunk
    # between our validation and this save. Conversion stays outside the lock
    # so parallel workers are not serialised by each other's YAML parsing.
    with _lock(data_dir):
        latest = load_round(data_dir, args.round) or manifest
        current = next((a for a in latest["allocations"] if a["name"] == args.name), None)
        if current is None:
            raise SystemExit(f"{args.name} 在 round {args.round} 的清单中消失了；重新 allocate")
        if os.path.exists(expected_jsonl):
            raise SystemExit(f"{_display(expected_jsonl)} 已存在；不得覆盖已产出的 chunk")
        atomic_write_text(expected_jsonl, payload)
        current.update({
            "status": "written",
            "written_at": _now(),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "cases": payload.count("\n"),
        })
        latest["updated_at"] = _now()
        save_round(data_dir, latest)
    print(f"{summary} -> {_display(expected_jsonl)}")
    return 0


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def cmd_merge(args):
    data_dir = os.path.abspath(args.data_dir)
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}")

    written = [a for a in manifest["allocations"] if a.get("status") == "written"]
    if not written:
        raise SystemExit(f"round {args.round} 还没有已写入的 chunk；先运行 write")

    paths = [require_under(_abs(data_dir, a["jsonl"]), data_dir, "合并输入") for a in written]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise SystemExit("清单里标记为 written 但文件不存在: "
                         + ", ".join(_display(p) for p in missing))

    taxonomy = args.taxonomy or os.path.join(data_dir, "taxonomy.json")
    if not os.path.exists(taxonomy):
        raise SystemExit(f"taxonomy 不存在: {taxonomy}")
    with open(taxonomy, encoding="utf-8-sig") as f:
        taxonomy_obj = json.load(f)

    deadline = Deadline(args.timeout)
    check_args = argparse.Namespace(
        min_len=0, max_len=0, leak_index=None, intra=0, taxonomy=taxonomy_obj,
        ruhui_max_len=args.ruhui_max_len,
        require_complete_cases=False, deadline=deadline)
    try:
        errors, state = vi.collect(paths, check_args, expand=True)
        if errors:
            print(f"合并前校验失败 ({len(errors)} errors)")
            for err in errors[:40]:
                print(" -", err)
            return 1
        rows = []
        for index, case in enumerate(state["cases"]):
            rows.extend(vi.expand_case(case, index, taxonomy_obj))
        deadline.check("merge")
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}", file=sys.stderr)
        return 3

    out = args.out or os.path.join(data_dir, "rounds", args.round, "merged_soft.jsonl")
    out = require_under(out, data_dir, "合并输出")
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    atomic_write_text(out, payload)

    # ruhui's prepare_train_data.py only globs ``soft_*.jsonl``; emit that name
    # too so the trainer can read it without a manual rename.
    soft_out = os.path.join(os.path.dirname(out),
                            f"soft_{args.round.replace('-', '_')}.jsonl")
    atomic_write_text(soft_out, payload)

    print(f"merged {len(state['cases'])} cases / {len(rows)} records from "
          f"{len(paths)} chunk(s) -> {_display(out)}")
    print(f"ruhui 可直接读取: {_display(soft_out)}")
    print("只合并了本轮 chunk：")
    for path in paths:
        print("  -", _display(path))
    return 0


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def _scan_files(data_dir):
    found = []
    for root, dirs, files in os.walk(data_dir):
        if "__pycache__" in dirs:
            found.append(os.path.join(root, "__pycache__"))
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            found.append(os.path.join(root, name))
    return found


def cmd_check(args):
    data_dir = os.path.abspath(args.data_dir)
    findings = []

    def add(level, code, message):
        findings.append({"level": level, "code": code, "message": message})

    for path in _scan_files(data_dir):
        rel = _rel(data_dir, path)
        base = os.path.basename(path)
        if base == ".lock":
            continue
        if TEMP_RE.search(base) or "__pycache__" in rel:
            add("FAIL", "temp", f"临时/备份文件: {rel}")
            continue
        if base.endswith(".py") or base.endswith(".pyc"):
            add("FAIL", "script", f"data/ 下出现脚本（禁止模型自造生成器）: {rel}")
            continue
        parts = rel.split("/")
        if parts[0] in ("chunks", "inbox"):
            if not CHUNK_RE.match(base):
                add("FAIL", "naming", f"chunk 命名不合规（应为 chunk_<4+位>.jsonl/.yaml）: {rel}")
                continue
            if len(parts) != 2:
                add("FAIL", "naming", f"chunk 不允许放在子目录: {rel}")
                continue
            if parts[0] == "chunks" and not base.endswith(".jsonl"):
                add("FAIL", "naming", f"chunks/ 下只能是 .jsonl: {rel}")
        elif parts[0] == "split":
            if base not in ALLOWED_SPLIT:
                add("WARN", "layout", f"split/ 下出现未知文件: {rel}")
        elif parts[0] == "rounds":
            if len(parts) == 2 and not base.endswith(".json"):
                add("FAIL", "layout", f"rounds/ 顶层只能是 <round>.json: {rel}")
            elif len(parts) > 2 and base != "merged_soft.jsonl":
                add("WARN", "layout", f"rounds/ 下出现未知文件: {rel}")
        elif parts[0] in ALLOWED_ROOT_FILES:
            continue
        elif os.path.dirname(rel) == "" and parts[0] in ALLOWED_ROOT_DIRS:
            continue
        else:
            add("WARN", "layout", f"data/ 下出现未知文件: {rel}")

    # A model that ignores the rule tends to drop its generator at the repo
    # root. Only applied to the repo's own data dir so a temp-dir test run
    # does not inspect the developer's checkout.
    if (args.round and os.path.abspath(data_dir) == os.path.abspath(DEFAULT_DATA)
            and os.path.isdir(REPO_ROOT)):
        for name in sorted(os.listdir(REPO_ROOT)):
            path = os.path.join(REPO_ROOT, name)
            if os.path.isfile(path) and name.endswith(".py") and name not in ROOT_PY_ALLOWED:
                add("FAIL", "script", f"仓库根目录出现脚本（禁止模型自造生成器）: {name}")

    # chunk allocation accounting
    file_numbers = {}
    for sub in ("chunks", "inbox"):
        directory = os.path.join(data_dir, sub)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            match = CHUNK_RE.match(name)
            if match:
                file_numbers.setdefault(int(match.group(1)), []).append(f"{sub}/{name}")
    for number, names in sorted(file_numbers.items()):
        suffixes = [os.path.splitext(n)[1] for n in names]
        if suffixes.count(".yaml") + suffixes.count(".yml") > 1:
            add("FAIL", "naming", f"chunk_{number:04d} 有多个 YAML: {names}")

    allocated_any = all_allocated_numbers(data_dir)
    if args.round:
        if not ROUND_RE.match(args.round):
            raise SystemExit(f"非法 round id: {args.round!r}")
        manifest = load_round(data_dir, args.round)
        if manifest is None:
            raise SystemExit(f"没有这个 round: {args.round}")
        baseline = set(manifest.get("baseline", []))
        allocated = {a["name"]: a for a in manifest["allocations"]}
        allocated_rel = set()
        for allocation in manifest["allocations"]:
            allocated_rel.add(allocation["jsonl"])
            allocated_rel.add(allocation["yaml"])
        allowed = baseline | allocated_rel
        for names in file_numbers.values():
            for rel in names:
                if rel not in allowed:
                    add("FAIL", "extra-chunk",
                        f"本轮产生/遗留了未登记的 chunk: {rel}（只允许清单内的文件）")
        for name, allocation in allocated.items():
            if allocation.get("status") == "written" and not os.path.exists(_abs(data_dir, allocation["jsonl"])):
                add("FAIL", "missing", f"清单标记 written 但文件缺失: {allocation['jsonl']}")
    else:
        for number, names in sorted(file_numbers.items()):
            if number not in allocated_any:
                add("WARN", "unregistered", f"chunk 未被任何 round 登记（历史数据？）: {names}")

    if args.strict:
        for finding in findings:
            if finding["level"] == "WARN":
                finding["level"] = "FAIL"
                finding["message"] = "(--strict) " + finding["message"]

    fails = [f for f in findings if f["level"] == "FAIL"]
    warns = [f for f in findings if f["level"] == "WARN"]

    if args.json:
        print(json.dumps({"data_dir": _display(data_dir), "round": args.round,
                          "findings": findings,
                          "verdict": "fail" if fails else "pass"}, indent=2, ensure_ascii=False))
    else:
        print(f"data_writer check: {len(file_numbers)} chunk 编号, "
              f"{sum(len(v) for v in file_numbers.values())} 个 chunk 文件")
        for finding in findings:
            print(f"  {finding['level']:4} [{finding['code']}] {finding['message']}")
        if fails:
            print(f"FAIL ({len(fails)} problems, {len(warns)} warnings)")
            print("  清理多余/临时文件；删除模型自造的脚本；不要把它们提交。")
        else:
            print(f"PASS ({len(warns)} warnings)")
    return 1 if fails else 0


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=DEFAULT_DATA,
                        help="数据根目录（默认仓库下 data/）；所有读写都被限制在其中")
    sub = parser.add_subparsers(dest="command", required=True)

    allocate = sub.add_parser("allocate", help="分配本轮的 chunk 名字与 YAML 槽位")
    allocate.add_argument("--count", type=int, default=1)
    allocate.add_argument("--round", default=None, help="沿用已有 round id（追加分配）")
    allocate.add_argument("--worker", default=None, help="subagent 标签，便于追溯")
    allocate.add_argument("--json", action="store_true")
    allocate.set_defaults(func=cmd_allocate)

    write = sub.add_parser("write", help="把分配槽位里的 YAML 校验并写成 chunk JSONL")
    write.add_argument("--round", required=True)
    write.add_argument("--name", required=True, help="allocate 打印的名字，如 chunk_0003")
    write.add_argument("--in", dest="infile", required=True)
    write.add_argument("--taxonomy", default=None)
    write.add_argument("--timeout", type=float, default=0.0)
    write.add_argument("--ruhui-max-len", type=int, default=1024,
                       help="ruhui 训练窗口 token 数（默认 1024，取自 rl_agent_config.json）；"
                            "0 关闭 text 长度护栏")
    write.set_defaults(func=cmd_write)

    merge = sub.add_parser("merge", help="只合并本轮已写入的 chunk")
    merge.add_argument("--round", required=True)
    merge.add_argument("--taxonomy", default=None)
    merge.add_argument("--out", default=None)
    merge.add_argument("--timeout", type=float, default=0.0)
    merge.add_argument("--ruhui-max-len", type=int, default=1024,
                       help="ruhui 训练窗口 token 数（默认 1024）；0 关闭 text 长度护栏")
    merge.set_defaults(func=cmd_merge)

    check = sub.add_parser("check", help="检查多余 chunk、临时文件、自造脚本")
    check.add_argument("--round", default=None)
    check.add_argument("--strict", action="store_true")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    if not os.path.isdir(args.data_dir):
        # allocate bootstraps a fresh data dir; every other command needs one.
        if args.command == "allocate":
            os.makedirs(args.data_dir, exist_ok=True)
        else:
            raise SystemExit(f"data 目录不存在: {args.data_dir}；先运行 allocate")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())