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
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataloader import Deadline, atomic_write_text, read_text  # noqa: E402
import validate_and_integrate as vi  # noqa: E402
import yaml_to_jsonl as y2j  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo_root(start):
    """Walk up to the project root, or None when this skill is installed alone.

    Installed under ``~/.zcode/skills/`` the script's grandparent is the skills
    directory, not a project — defaulting ``data/`` there would write training
    data into the skill installation. A project root is the nearest ancestor
    holding ``.git`` or the skill's own ``traindata/`` parent marker.
    """
    current = start
    for _ in range(6):
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


REPO_ROOT = _find_repo_root(HERE)
# The output root is a *choice*: --data-dir may name any directory, but it must
# sit inside the project, and the layout under it is fixed. That keeps corpora
# discoverable ("where did this chunk come from") while letting a run pick a
# name other than data/.
DEFAULT_DATA = os.path.join(REPO_ROOT, "data") if REPO_ROOT else None

CHUNK_RE = re.compile(r"^chunk_(\d{4,})\.(jsonl|yaml|yml)$")
ROUND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
TEMP_RE = re.compile(r"(^\.tmp-|\.tmp$|\.bak$|\.orig$|\.rej$|~\$?$|\.swp$|\.pyc$|^\.DS_Store$|^__pycache__$)")
ALLOWED_ROOT_FILES = {"taxonomy.json", "ruhui_soft_all.jsonl", "ruhui_soft_card.md"}
ALLOWED_ROOT_DIRS = {"chunks", "inbox", "split", "rounds"}
ALLOWED_SPLIT = {"train.jsonl", "calibration.jsonl", "test.jsonl", "split_manifest.json"}
ROOT_PY_ALLOWED = set()  # this repo has no top-level Python entry point


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# A generation round has a hard wall-clock budget. Past the warn mark the tool
# starts telling the model to converge; past the stop mark it refuses new work
# and points at `salvage`, so a half-finished round still ships what is good.
DEFAULT_BUDGET_HOURS = 3.0
DEFAULT_WARN_FRACTION = 0.75


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def round_clock(manifest):
    """Time bookkeeping for a round: elapsed, remaining, and which tier we are in.

    Tiers: ``ok`` (within budget), ``warn`` (past the warn mark — converge now),
    ``stop`` (past the budget — no new work, salvage what exists).
    """
    started = _parse_ts(manifest.get("created_at"))
    budget_hours = float(manifest.get("budget_hours", DEFAULT_BUDGET_HOURS))
    if started is None:
        return {"tier": "ok", "elapsed_s": 0.0, "remaining_s": budget_hours * 3600,
                "budget_s": budget_hours * 3600, "warn_at_s": budget_hours * 3600 * DEFAULT_WARN_FRACTION}
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed = (now - started).total_seconds()
    budget = budget_hours * 3600
    warn_at = budget * float(manifest.get("warn_fraction", DEFAULT_WARN_FRACTION))
    remaining = budget - elapsed
    if elapsed >= budget:
        tier = "stop"
    elif elapsed >= warn_at:
        tier = "warn"
    else:
        tier = "ok"
    return {"tier": tier, "elapsed_s": elapsed, "remaining_s": remaining,
            "budget_s": budget, "warn_at_s": warn_at}


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def timer_notice(manifest, round_id):
    """Human/model-facing text for the current tier, or '' when on budget."""
    clock = round_clock(manifest)
    if clock["tier"] == "ok":
        return ""
    if clock["tier"] == "warn":
        return (
            f"⚠ TIME: round {round_id} 已用 {_fmt_duration(clock['elapsed_s'])}，"
            f"预算 {_fmt_duration(clock['budget_s'])}，剩 {_fmt_duration(clock['remaining_s'])}。\n"
            "  现在开始收尾：把正在进行的那份 YAML 写完，不要再开新场景。\n"
            "  建议：python3 traindata/scripts/data_writer.py salvage --round "
            f"{round_id} --taxonomy <taxonomy>")
    return (
        f"⛔ TIME UP: round {round_id} 已超出 {_fmt_duration(clock['budget_s'])} 预算"
        f"（已用 {_fmt_duration(clock['elapsed_s'])}）。强制止损：不再接受新 chunk。\n"
        "  立刻把已写好的部分转换、合并并提交：\n"
        f"  python3 traindata/scripts/data_writer.py salvage --round {round_id} "
        "--taxonomy <taxonomy> --commit")


def _rel(data_dir, path):
    return os.path.relpath(path, data_dir).replace(os.sep, "/")


def _display(path):
    """Repo-relative when inside a project checkout, absolute otherwise."""
    absolute = os.path.abspath(path)
    if REPO_ROOT:
        try:
            if os.path.commonpath([absolute, REPO_ROOT]) == REPO_ROOT:
                return os.path.relpath(absolute, REPO_ROOT)
        except ValueError:
            pass
    return absolute


def _abs(data_dir, rel):
    return os.path.abspath(os.path.join(data_dir, rel))


def require_under(path, data_dir, what):
    """Reject any path that escapes the chosen output root."""
    target = os.path.abspath(path)
    root = os.path.abspath(data_dir)
    if os.path.commonpath([target, root]) != root:
        raise SystemExit(f"{what} 必须在 {_display(root)} 下，拒绝: {path}")
    return target


def require_in_project(path, what="输出目录"):
    """The root may be renamed, but it must live inside the project checkout."""
    if not REPO_ROOT:
        raise SystemExit(
            "当前不是项目仓库（找不到 .git）。这个 skill 需要把数据放在项目内；"
            "请在项目根目录下运行，或把 skill 复制进项目后使用")
    target = os.path.abspath(path)
    root = os.path.abspath(REPO_ROOT)
    if os.path.commonpath([target, root]) != root:
        raise SystemExit(f"{what} 必须在项目 {_display(root)} 内，拒绝: {path}")
    if target == root:
        raise SystemExit(f"{what} 不能是项目根本身，请选一个子目录，例如 {_display(root)}/data")
    return target


def list_project_data_roots():
    """Candidate output roots: existing ones plus the default."""
    roots = []
    if not REPO_ROOT:
        return roots
    for name in sorted(os.listdir(REPO_ROOT)):
        candidate = os.path.join(REPO_ROOT, name)
        if not os.path.isdir(candidate) or name.startswith("."):
            continue
        if os.path.isdir(os.path.join(candidate, "rounds")) or \
                os.path.isdir(os.path.join(candidate, "chunks")):
            roots.append(candidate)
    if DEFAULT_DATA and os.path.isdir(DEFAULT_DATA) and DEFAULT_DATA not in roots:
        roots.insert(0, DEFAULT_DATA)
    return roots


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
        if args.budget_hours is not None:
            if args.budget_hours <= 0:
                raise SystemExit("--budget-hours 必须为正数")
            manifest["budget_hours"] = float(args.budget_hours)
        if args.warn_fraction is not None:
            if not 0.0 < args.warn_fraction < 1.0:
                raise SystemExit("--warn-fraction 必须在 (0, 1) 之间，例如 0.75")
            manifest["warn_fraction"] = float(args.warn_fraction)
        if args.salted_prompt:
            # The prompt is an input, not an output: it legitimately lives
            # beside the run (a harness temp dir) rather than inside data/.
            salted_prompt = os.path.abspath(args.salted_prompt)
            if not os.path.exists(salted_prompt):
                raise SystemExit(f"salted prompt 不存在: {salted_prompt}")
            text = read_text(salted_prompt)
            if "SALT" not in text.upper():
                raise SystemExit(f"{_display(salted_prompt)} 里没有 salt 块；"
                                 "请先用 salt.py --in/--out 生成")
            manifest["salted_prompt"] = _rel(data_dir, salted_prompt)
            manifest["salted_prompt_sha256"] = hashlib.sha256(
                text.encode()).hexdigest()
            manifest["salted_prompt_registered_at"] = _now()
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
                          "clock": round_clock(manifest),
                          "allocations": allocated}, indent=2, ensure_ascii=False))
        return 0
    print(f"round: {round_id}")
    print(f"manifest: {_display(round_file(data_dir, round_id))}")
    clock = round_clock(manifest)
    print(f"budget: {_fmt_duration(clock['budget_s'])} "
          f"(收尾提示在 {_fmt_duration(clock['warn_at_s'])})")
    for allocation in allocated:
        print(f"  {allocation['name']}  yaml={_display(_abs(data_dir, allocation['yaml']))}"
              f"  jsonl={_display(_abs(data_dir, allocation['jsonl']))}")
    print("只允许把 YAML 写到上面的 yaml 路径；不要新建脚本或其它文件。")
    return 0


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def stamp_salt(payload, manifest):
    """Replace any model-authored salt claim with a host attestation.

    Returns ``(payload, stripped)`` where ``stripped`` counts model-written
    claims. The stamp is ``meta.salted`` plus ``meta.salted_at`` written from
    the round's registered salted prompt; a case carrying ``salted`` without a
    host timestamp is a model hallucination, so the field is dropped first and
    only re-added when the round can actually vouch for it.
    """
    salted = bool(manifest.get("salted_prompt"))
    stamped_at = manifest.get("salted_prompt_registered_at")
    if salted and not stamped_at:
        stamped_at = manifest.get("created_at")
    stripped = 0
    out = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        meta = case.get("meta")
        if isinstance(meta, dict):
            for field in ("salted", "salted_at"):
                if field in meta:
                    stripped += 1
                    meta.pop(field, None)
            if salted:
                meta["salted"] = True
                meta["salted_at"] = stamped_at
        out.append(json.dumps(case, ensure_ascii=False))
    return "".join(line + "\n" for line in out), stripped


def cmd_write(args):
    data_dir = os.path.abspath(args.data_dir)
    if not ROUND_RE.match(args.round):
        raise SystemExit(f"非法 round id: {args.round!r}")
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}；先运行 allocate")
    notice = timer_notice(manifest, args.round)
    if notice:
        print(notice, file=sys.stderr)
    # Past the budget no new case is accepted: the round must ship what already
    # exists via `salvage` rather than grow further and ship nothing.
    if round_clock(manifest)["tier"] == "stop":
        print(f"⛔ round {args.round} 已超时，拒绝新 chunk {args.name}。"
              f"用 salvage 把已完成的部分转换并提交：\n"
              f"  python3 traindata/scripts/data_writer.py salvage --round {args.round} "
              "--taxonomy data/taxonomy.json", file=sys.stderr)
        return 1
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

    # The salt is a property of the run, not of the model's self-description.
    # The generator writes no ``meta.salted`` at all; this host stamps the
    # attestation it can actually vouch for: the prompt really went through
    # salt.py in this round. Anything the model wrote is discarded.
    payload, stripped_salt = stamp_salt(payload, manifest)

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
# salvage
# --------------------------------------------------------------------------- #
def cmd_salvage(args):
    """Convert whatever this round already produced and ship it.

    This is the stop-loss path: when the clock runs out (or the run is about to
    be killed), a half-finished round must still hand over every case that is
    already valid instead of losing all of it. Pending slots whose YAML exists
    and passes validation are written; ones that fail are reported and left
    alone. Nothing new is accepted.
    """
    data_dir = os.path.abspath(args.data_dir)
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}")

    clock = round_clock(manifest)
    tier = clock["tier"]
    print(f"salvage round {args.round}: 已用 {_fmt_duration(clock['elapsed_s'])}"
          f" / 预算 {_fmt_duration(clock['budget_s'])}（{tier}）")

    taxonomy = args.taxonomy or os.path.join(data_dir, "taxonomy.json")
    if not os.path.exists(taxonomy):
        raise SystemExit(f"taxonomy 不存在: {taxonomy}")
    with open(taxonomy, encoding="utf-8-sig") as f:
        taxonomy_obj = json.load(f)

    converted, already, skipped = [], [], []
    for allocation in manifest["allocations"]:
        name = allocation["name"]
        yaml_path = _abs(data_dir, allocation["yaml"])
        jsonl_path = _abs(data_dir, allocation["jsonl"])
        if allocation.get("status") == "written" and os.path.exists(jsonl_path):
            already.append(name)
            continue
        if not os.path.exists(yaml_path):
            continue  # never started; nothing to salvage
        deadline = Deadline(args.timeout)
        try:
            payload, _summary, errors = y2j.convert(
                [yaml_path], taxonomy_obj, expand=False, deadline=deadline,
                force_validate=True, ruhui_max_len=args.ruhui_max_len)
        except TimeoutError as exc:
            skipped.append((name, f"timeout: {exc}"))
            continue
        if errors:
            skipped.append((name, errors[0]))
            continue
        payload, _stripped = stamp_salt(payload, manifest)
        atomic_write_text(jsonl_path, payload)
        allocation.update({
            "status": "written",
            "written_at": _now(),
            "salvaged": True,
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "cases": payload.count("\n"),
        })
        converted.append(name)

    manifest["salvaged_at"] = _now()
    manifest["salvage_tier"] = tier
    manifest["updated_at"] = _now()
    save_round(data_dir, manifest)

    print(f"  已转换 {len(converted)}: {converted or '—'}")
    print(f"  先前已完成 {len(already)}: {already or '—'}")
    if skipped:
        print(f"  跳过 {len(skipped)} 个（YAML 无效，未阻塞其余）：")
        for name, reason in skipped:
            print(f"    - {name}: {str(reason)[:110]}")

    if not converted and not already:
        print("没有可挽救的 chunk。")
        return 1

    merge_args = argparse.Namespace(
        data_dir=data_dir, round=args.round, taxonomy=taxonomy, out=args.out,
        timeout=args.timeout, ruhui_max_len=args.ruhui_max_len)
    rc = cmd_merge(merge_args)
    if rc != 0:
        return rc

    if args.commit:
        return _commit_salvage(data_dir, args.round, converted + already)
    print("\n未提交。确认无误后加 --commit，或手动提交上述文件。")
    return 0


def _commit_salvage(data_dir, round_id, names):
    """Commit exactly this round's artifacts, nothing else."""
    if not REPO_ROOT:
        print("当前目录不是 git 仓库，无法 --commit；请手动提交上述文件。")
        return 1
    rel_data = os.path.relpath(os.path.abspath(data_dir), REPO_ROOT).replace(os.sep, "/")
    targets = [f"{rel_data}/rounds/{round_id}.json"]
    for name in names:
        targets.append(f"{rel_data}/inbox/{name}.yaml")
        targets.append(f"{rel_data}/chunks/{name}.jsonl")
    targets.append(f"{rel_data}/rounds/{round_id}/")
    existing = [t for t in targets if os.path.exists(_abs(REPO_ROOT, t))]
    if not existing:
        print("没有可提交的文件。")
        return 1
    add = subprocess.run(["git", "add", "--"] + existing, cwd=REPO_ROOT,
                         capture_output=True, text=True)
    if add.returncode != 0:
        print(f"git add 失败: {add.stderr.strip()}")
        return 1
    total = sum(a.get("cases", 0) for a in
                (load_round(data_dir, round_id) or {}).get("allocations", []))
    message = (f"data: salvage {len(names)} chunk(s) from round {round_id} "
               f"after the time budget ({total} cases)")
    commit = subprocess.run(["git", "commit", "-q", "-m", message], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if commit.returncode != 0:
        print(f"git commit 失败: {commit.stderr.strip() or commit.stdout.strip()}")
        return 1
    print(f"已提交: {message}")
    return 0


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def cmd_merge(args):
    data_dir = os.path.abspath(args.data_dir)
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}")
    notice = timer_notice(manifest, args.round)
    if notice:
        print(notice, file=sys.stderr)

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


def cmd_list_dirs(args):
    """Show which project directories look like output roots."""
    if not REPO_ROOT:
        print("当前不是项目仓库（找不到 .git）。这个 skill 要求数据放在项目内。")
        return 1
    roots = list_project_data_roots()
    print(f"项目: {_display(REPO_ROOT)}")
    if not roots:
        print("还没有输出目录。用 allocate --data-dir <项目内路径> 创建，"
              f"例如 {_display(os.path.join(REPO_ROOT, 'data'))}")
        return 0
    print("可作为输出根目录的候选（--data-dir）：")
    for root in roots:
        marker = "  <- 默认" if DEFAULT_DATA and root == DEFAULT_DATA else ""
        rounds = len(glob.glob(os.path.join(root, "rounds", "*.json")))
        chunks = len(glob.glob(os.path.join(root, "chunks", "chunk_*.jsonl")))
        print(f"  {_display(root)}   rounds={rounds} chunks={chunks}{marker}")
    return 0


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
            add("FAIL", "script", f"输出根目录内出现脚本（禁止模型自造生成器）: {rel}")
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
            elif len(parts) > 2 and not (base == "merged_soft.jsonl"
                                         or (base.startswith("soft_") and base.endswith(".jsonl"))):
                add("WARN", "layout", f"rounds/ 下出现未知文件: {rel}")
        elif parts[0] in ALLOWED_ROOT_FILES:
            continue
        elif os.path.dirname(rel) == "" and parts[0] in ALLOWED_ROOT_DIRS:
            continue
        else:
            add("WARN", "layout", f"输出根目录下出现未知文件: {rel}")

    # A model that ignores the rule tends to drop its generator at the repo
    # root, outside the output root this check scans. The data root is always
    # inside the project now, so scanning the project root is always in scope.
    if args.round and REPO_ROOT and os.path.isdir(REPO_ROOT):
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
        clock = round_clock(manifest)
        if clock["tier"] != "ok":
            add("WARN", "time",
                f"round 已用 {_fmt_duration(clock['elapsed_s'])} / 预算 "
                f"{_fmt_duration(clock['budget_s'])}（{clock['tier']}）；"
                "先 salvage 已完成部分，不要再开新场景")
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
                        help="输出根目录（默认项目内 data/）。可以改名字，但必须在项目内；"
                             "其下 inbox/chunks/rounds 布局固定，所有读写都限制在这个根目录内。"
                             "用 list-dirs 查看已有目录")
    sub = parser.add_subparsers(dest="command", required=True)

    allocate = sub.add_parser("allocate", help="分配本轮的 chunk 名字与 YAML 槽位")
    allocate.add_argument("--count", type=int, default=1)
    allocate.add_argument("--round", default=None, help="沿用已有 round id（追加分配）")
    allocate.add_argument("--worker", default=None, help="subagent 标签，便于追溯")
    allocate.add_argument("--salted-prompt", default=None,
                          help="本轮实际发给模型的 salt.py 输出；只有登记了它，"
                               "write 才会为该 round 的 case 盖 meta.salted")
    allocate.add_argument("--json", action="store_true")
    allocate.add_argument("--budget-hours", type=float, default=None,
                          help=f"本轮墙钟预算（小时，默认 {DEFAULT_BUDGET_HOURS:g}）；"
                               "超过即进入强制止损，write 拒绝新 chunk")
    allocate.add_argument("--warn-fraction", type=float, default=None,
                          help=f"收尾提示在预算的这个比例处触发（默认 {DEFAULT_WARN_FRACTION:g}）")
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

    salvage = sub.add_parser(
        "salvage", help="止损：转换本轮已完成且合法的 YAML，合并并（可选）提交")
    salvage.add_argument("--round", required=True)
    salvage.add_argument("--taxonomy", default=None)
    salvage.add_argument("--out", default=None)
    salvage.add_argument("--timeout", type=float, default=0.0)
    salvage.add_argument("--ruhui-max-len", type=int, default=1024)
    salvage.add_argument("--commit", action="store_true",
                         help="只暂存并提交本轮的 manifest/chunk/合并产物")
    salvage.set_defaults(func=cmd_salvage)

    check = sub.add_parser("check", help="检查多余 chunk、临时文件、自造脚本")
    check.add_argument("--round", default=None)
    check.add_argument("--strict", action="store_true")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

    sub.add_parser("list-dirs", help="列出项目内可作为输出根目录的候选").set_defaults(
        func=cmd_list_dirs)

    args = parser.parse_args()
    if args.command == "list-dirs":
        return args.func(args)
    if not REPO_ROOT:
        raise SystemExit(
            "找不到项目仓库（没有 .git 祖先目录）。这个 skill 要求输出目录在项目内，"
            "请在项目根目录下运行 data_writer.py；独立安装的副本不能单独生成数据")
    if not args.data_dir:
        raise SystemExit("没有可用的输出目录。请用 --data-dir 指定项目内的一个目录，"
                         "例如 --data-dir ./data（用 list-dirs 查看已有目录）")
    args.data_dir = require_in_project(args.data_dir, "输出根目录")
    if not os.path.isdir(args.data_dir):
        # allocate bootstraps a fresh root; every other command needs one.
        if args.command == "allocate":
            try:
                os.makedirs(args.data_dir, exist_ok=True)
            except OSError as exc:
                raise SystemExit(f"无法创建输出目录 {args.data_dir}: {exc}")
        else:
            raise SystemExit(f"输出目录不存在: {args.data_dir}；先运行 allocate")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())