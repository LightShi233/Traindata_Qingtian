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

    # 2b. if the chunk then fails validate/audit, return it to pending and
    #     rewrite the same slot after fixing the YAML (the YAML is kept):
    python3 traindata/scripts/data_writer.py discard --round <id> --name chunk_0003 --reason audit:injection_surface

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
- An existing JSONL is never overwritten. A written chunk that failed a gate is
  returned to ``pending`` with ``discard`` (which removes only that chunk's
  JSONL and keeps the YAML) and then rewritten under the same name, never by
  hand and never by allocating a new name that leaves the bad one mergeable.
- ``merge`` refuses to include anything not written by this round, and skips
  discarded chunks automatically.
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

from dataloader import (  # noqa: E402
    AUTHORITY_NOTICE,
    Deadline,
    atomic_write_text,
    read_text,
)
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

CHUNK_RE = re.compile(r"^chunk_(\d{4,})(?:_(\d{4,}))?\.(jsonl|yaml|yml)$")
# Allocation names in the round manifest are stems (no extension).
CHUNK_STEM_RE = re.compile(r"^chunk_(\d{4,})(?:_(\d{4,}))?$")
# Parallel authoring is encouraged, but the main agent must validate every chunk
# itself; beyond this the review queue, not generation, becomes the bottleneck.
MAX_CONCURRENT_CHUNKS = 8
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


def resolve_project_root(path, what="输出目录"):
    """Return the chosen root, relocating it into the project when it escaped.

    A run that points outside the project is not refused outright: the corpus
    belongs to the project, so a stray path is almost always a mistake worth
    repairing rather than a policy violation. The basename is kept and the root
    is moved to ``<project>/<basename>``, with a notice saying what changed.
    """
    if not REPO_ROOT:
        raise SystemExit(
            "找不到项目仓库（没有 .git 祖先目录）。这个 skill 要求输出目录在项目内；"
            "请把 skill 放进项目后在项目根目录下运行\n" + AUTHORITY_NOTICE)
    root = os.path.abspath(REPO_ROOT)
    target = os.path.abspath(path)
    if target == root:
        relocated = os.path.join(root, "data")
        print(f"[relocate] {what} 不能是项目根本身；改用 {_display(relocated)}", file=sys.stderr)
        print(AUTHORITY_NOTICE, file=sys.stderr)
        return relocated
    if os.path.commonpath([target, root]) == root:
        return target
    relocated = os.path.join(root, os.path.basename(target) or "data")
    print(f"[relocate] {what} {_display(target)} 不在项目内；"
          f"已改用 {_display(relocated)}。数据必须留在项目内。\n"
          f"           后续命令请统一使用 --data-dir {_display(relocated)}，"
          f"不要再用原来的路径。", file=sys.stderr)
    if os.path.isdir(target):
        print(f"           原目录仍在磁盘上（未删除）；如确实要使用，请把它移进项目后重新指定。",
              file=sys.stderr)
    print(AUTHORITY_NOTICE, file=sys.stderr)
    return relocated


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
            match = CHUNK_STEM_RE.match(allocation.get("name", ""))
            if match:
                numbers.add(int(match.group(1)))
    return numbers


def all_allocated_stems(data_dir):
    """Full chunk identities (``NNNN`` or ``NNNN_runsuffix``) known to any round."""
    stems = set()
    for path in glob.glob(os.path.join(data_dir, "rounds", "*.json")):
        try:
            with open(path, encoding="utf-8-sig") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for allocation in manifest.get("allocations", []):
            match = CHUNK_STEM_RE.match(allocation.get("name", ""))
            if match:
                stems.add(chunk_stem(match))
    return stems


def chunk_stem(match):
    return f"{match.group(1)}_{match.group(2)}" if match.group(2) else match.group(1)


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
    if args.count < 1:
        raise SystemExit("--count 必须 >= 1（它数的是 chunk 槽位，不是 case）")

    with _lock(data_dir):
        manifest = load_round(data_dir, round_id)
        if manifest is None:
            manifest = {
                "round": round_id,
                "created_at": _now(),
                "baseline": baseline_chunks(data_dir),
                "allocations": [],
            }
        # The cap is per round, not per call: repeated `allocate --round <id>`
        # would otherwise stack up unlimited parallel slots. Parallel authoring
        # is encouraged, but the main agent has to write and gate every chunk,
        # and past the cap that review queue is what fails the round.
        existing = len(manifest["allocations"])
        if existing + args.count > MAX_CONCURRENT_CHUNKS:
            raise SystemExit(
                f"round {round_id} 已有 {existing} 个槽位，再加 {args.count} 个会超过上限 "
                f"{MAX_CONCURRENT_CHUNKS}。并行子代理最多 8 个：每个 chunk 都要由主代理 "
                "write 并跑两个 gate，再多就来不及逐个验证，整轮会失败。"
                f"请分轮处理（新开一个 round）。")
        # Record which taxonomy this round was authored against. Without it a
        # later taxonomy edit silently invalidates already-merged chunks: the
        # case carries labels for the question set of its own version, not of
        # whatever taxonomy.json says today.
        tax_path = os.path.join(data_dir, "taxonomy.json")
        if os.path.exists(tax_path):
            with open(tax_path, encoding="utf-8-sig") as f:
                manifest["taxonomy_version"] = json.load(f).get("version")
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
            # Store provenance as a repo-relative path: the manifest is
            # committed with the corpus, so it must not embed the author's
            # home directory.
            manifest["salted_prompt"] = _display(salted_prompt)
            manifest["salted_prompt_sha256"] = hashlib.sha256(
                text.encode()).hexdigest()
            manifest["salted_prompt_registered_at"] = _now()
        if args.email_manifest:
            # Snapshot the batch into the manifest: write() then verifies against
            # it without depending on the file still being where it was, and the
            # round remains auditable later.
            try:
                with open(args.email_manifest, encoding="utf-8") as f:
                    batch_doc = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(f"email manifest 无法读取: {args.email_manifest}: {exc}")
            batch = [str(e) for e in batch_doc.get("emails", [])]
            if not batch:
                raise SystemExit(f"{args.email_manifest} 里没有 emails；"
                                 "用 sample_emails.py --cross-map --manifest 生成")
            manifest["email_batch"] = batch
            pairs = batch_doc.get("pairs")
            if isinstance(pairs, list) and pairs:
                # Keep the cross-map assignment durable: CI rebuilds the audit
                # whitelist from the round manifest, and without these pairs the
                # "same domain in clean and violating contexts" check is skipped.
                manifest["email_pairs"] = pairs
            manifest["email_manifest_source"] = _display(args.email_manifest)
            manifest["email_batch_registered_at"] = _now()
        used = chunk_numbers(os.path.join(data_dir, "chunks"))
        used |= chunk_numbers(os.path.join(data_dir, "inbox"))
        used |= all_allocated_numbers(data_dir)
        # Concurrency: two cron rounds branch off the same main and cannot see
        # each other's chunks, so a purely local max() hands out the same name
        # twice. The run id is unique per workflow run across all branches, so
        # it becomes the suffix and collisions stop mattering.
        suffix = args.suffix or os.environ.get("GITHUB_RUN_ID") or ""
        if suffix and not re.fullmatch(r"\d{4,}", suffix):
            raise SystemExit(f"--suffix / GITHUB_RUN_ID 必须是 4 位以上数字，实际 {suffix!r}")
        start = (max(used) + 1) if used else 1
        allocated = []
        for offset in range(args.count):
            base = f"chunk_{start + offset:04d}"
            name = f"{base}_{suffix}" if suffix else base
            # --worker may name several subagents; hand each slot to one, in
            # order, so the printed table is an unambiguous assignment. Without
            # that, two subagents can both "write their chunk" and the later
            # YAML silently overwrites the earlier one before anyone validates.
            worker = None
            if args.workers:
                worker = args.workers[offset] if offset < len(args.workers) else args.workers[-1]
            elif args.worker:
                worker = args.worker
            allocation = {
                "name": name,
                "base": base,
                "run_suffix": suffix or None,
                "yaml": f"inbox/{name}.yaml",
                "jsonl": f"chunks/{name}.jsonl",
                "status": "pending",
                "worker": worker,
                "allocated_at": _now(),
            }
            manifest["allocations"].append(allocation)
            allocated.append(allocation)
        if suffix:
            manifest["run_suffix"] = suffix
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
    if args.workers or args.worker:
        print("分派表（把每一行原样交给对应子代理，子代理只能写自己那一行）：")
        print(f"  {'chunk 名':<28} {'子代理':<14} YAML 槽位")
        for allocation in allocated:
            print(f"  {allocation['name']:<28} {str(allocation.get('worker') or '-'):<14} "
                  f"{_display(_abs(data_dir, allocation['yaml']))}")
        print("  子代理必须逐字复制上面的 chunk 名与路径：名字带了 CI run 号后缀"
              "（chunk_NNNN_<run id>），抄错一位就会写进别人的槽位，"
              "后写的会静默覆盖先写的。")
    else:
        for allocation in allocated:
            print(f"  {allocation['name']}  yaml={_display(_abs(data_dir, allocation['yaml']))}"
                  f"  jsonl={_display(_abs(data_dir, allocation['jsonl']))}")
    print("只允许把 YAML 写到上面的 yaml 路径；不要新建脚本或其它文件。")
    print("注意: --count 是 chunk 槽位数，不是 case 数；一个 chunk 的全部 case "
          "写进同一个 YAML。只跑一个 pilot chunk 时用 --count 1，"
          "多分配的槽位保持 pending、不会被 merge。")
    return 0


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def check_locked_emails(payload, manifest, name):
    """Every address in the chunk must come from the round's locked batch.

    Hand-picking domains (``grep`` against the library files) bypasses the
    stratified weighting and the cross-map, so the label starts tracking the
    domain. The batch is what makes an address meaningless as a signal; without
    a registered batch there is nothing to verify, and the chunk is refused.
    """
    allowed = {str(e).lower() for e in manifest.get("email_batch", [])}
    if not allowed:
        return [f"{name}: 本轮没有登记邮箱批次。地址必须来自 "
                "sample_emails.py --cross-map 的输出，并通过 "
                "allocate --email-manifest 登记；禁止自己挑域名"]
    used = {e.lower() for e in EMAIL_RE.findall(payload)}
    off = sorted(used - allowed)
    if off:
        return [f"{name}: {len(off)} 个地址不在锁定批次内（例：{off[:5]}）。"
                "禁止 grep 邮箱库或自选域名：必须用 sample_emails.py 的输出原样写入"]
    return []


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
    owner = allocation.get("claimed_by")
    if owner and args.worker and owner != args.worker:
        raise SystemExit(f"{args.name} 由子代理 {owner!r} 认领，但当前 --worker 是 "
                         f"{args.worker!r}。不同子代理的产出不能互相顶替，"
                         "请确认写的是自己认领的那个槽位")

    expected_yaml = require_under(_abs(data_dir, allocation["yaml"]), data_dir, "YAML 输入")
    expected_jsonl = require_under(_abs(data_dir, allocation["jsonl"]), data_dir, "JSONL 输出")
    if os.path.normpath(os.path.abspath(args.infile)) != os.path.normpath(expected_yaml):
        raise SystemExit(f"YAML 必须写在分配好的位置: {_display(expected_yaml)}\n"
                         f"  实际传入: {args.infile}")
    if not os.path.exists(expected_yaml):
        raise SystemExit(f"YAML 不存在: {_display(expected_yaml)}")
    if os.path.exists(expected_jsonl):
        raise SystemExit(f"{_display(expected_jsonl)} 已存在；"
                         "不得覆盖已产出的 chunk。\n"
                         f"  若它未通过 validate/audit，先运行: data_writer.py discard "
                         f"--round {args.round} --name {args.name}\n"
                         "  然后修好 YAML 再重写同一个名字；只有确实要另开一个 chunk 时才 allocate 新名字。")

    taxonomy = args.taxonomy or os.path.join(data_dir, "taxonomy.json")
    if not os.path.exists(taxonomy):
        raise SystemExit(f"taxonomy 不存在: {taxonomy}（用 --taxonomy 指定）")
    with open(taxonomy, encoding="utf-8-sig") as f:
        taxonomy_obj = json.load(f)
    mismatched = taxonomy_drift(manifest, taxonomy_obj, args.round)
    if mismatched:
        print(mismatched, file=sys.stderr)
        return 1

    deadline = Deadline(args.timeout)
    try:
        payload, summary, errors = y2j.convert(
            [expected_yaml], taxonomy_obj, expand=False, deadline=deadline,
            force_validate=True, ruhui_max_len=args.ruhui_max_len)
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}", file=sys.stderr)
        return 3
    # Addresses must come from the round's locked batch. Checked here, at write
    # time, rather than only in the audit: a generator that greps the email
    # library and picks domains by hand never has to run the audit, so the
    # check would never fire.
    errors.extend(check_locked_emails(payload, manifest, args.name))
    if errors:
        print(f"{args.name} 拒绝写入 ({len(errors)} errors)")
        for err in errors[:40]:
            print(" -", err)
        print("修好 YAML 后重跑 write；不要手改 JSONL。")
        print(AUTHORITY_NOTICE)
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
# discard
# --------------------------------------------------------------------------- #
def cmd_discard(args):
    """Return a written chunk to pending after a gate rejected it.

    ``write`` never overwrites an existing chunk, which is correct for accepted
    data but a trap for rejected data: a chunk written before the gates reject
    it cannot be fixed in place, and allocating a new name would leave the bad
    one in the round for ``merge`` to pick up. ``discard`` is the sanctioned
    way out: it removes only that chunk's JSONL, keeps the YAML for repair, and
    resets the manifest entry to pending so the same slot can be rewritten.
    """
    data_dir = os.path.abspath(args.data_dir)
    if not ROUND_RE.match(args.round):
        raise SystemExit(f"非法 round id: {args.round!r}")
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}")
    allocation = next((a for a in manifest["allocations"] if a["name"] == args.name), None)
    if allocation is None:
        raise SystemExit(f"{args.name} 不属于 round {args.round}；"
                         "discard 只能用于本轮分配过的 chunk")

    jsonl = require_under(_abs(data_dir, allocation["jsonl"]), data_dir, "chunk 输出")
    yaml_path = require_under(_abs(data_dir, allocation["yaml"]), data_dir, "YAML 槽位")

    with _lock(data_dir):
        latest = load_round(data_dir, args.round) or manifest
        current = next((a for a in latest["allocations"] if a["name"] == args.name), None)
        if current is None:
            raise SystemExit(f"{args.name} 在 round {args.round} 的清单中消失了；重新 allocate")
        was = current.get("status", "pending")
        if was == "pending" and not os.path.exists(jsonl):
            result = {"name": args.name, "status": "pending",
                      "discarded": False, "reason": "already pending"}
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"{args.name} 已经是 pending，且没有 JSONL；无需 discard")
            return 0
        existed = os.path.exists(jsonl)
        if existed:
            os.remove(jsonl)
        latest.setdefault("discards", []).append({
            "name": args.name,
            "discarded_at": _now(),
            "previous_status": was,
            "reason": args.reason or "",
            "sha256": current.get("sha256"),
            "cases": current.get("cases"),
        })
        current["status"] = "pending"
        for key in ("written_at", "sha256", "cases"):
            current.pop(key, None)
        current["discarded_at"] = _now()
        if args.reason:
            current["discard_reason"] = args.reason
        latest["updated_at"] = _now()
        save_round(data_dir, latest)

    merged = os.path.join(data_dir, "rounds", args.round, "merged_soft.jsonl")
    result = {"name": args.name, "status": "pending", "discarded": True,
              "previous_status": was, "jsonl_removed": existed,
              "yaml": _display(yaml_path),
              "merged_output_stale": os.path.exists(merged)}
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"discarded {args.name} (was {was}; JSONL "
          f"{'removed' if existed else 'already absent'})")
    print(f"  YAML 保留待修: {_display(yaml_path)}")
    print(f"  修好后重跑: write --round {args.round} --name {args.name} "
          f"--in {_display(yaml_path)} --taxonomy data/taxonomy.json")
    if os.path.exists(merged):
        print(f"  注意: {_display(merged)} 是本轮旧的合并结果，重写后必须重新 merge")
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
def taxonomy_drift(manifest, taxonomy_obj, round_id):
    """Refuse to mix question sets across taxonomy versions.

    A case carries labels for the questions that existed when it was authored.
    If taxonomy.json gained a question since, validating old cases against it
    fails on every case with "missing label" — which reads like corrupted data
    but is really a version mismatch. Say so plainly instead.
    """
    recorded = manifest.get("taxonomy_version")
    current = taxonomy_obj.get("version")
    if not recorded or recorded == current:
        return ""
    return (f"round {round_id} 是按 {recorded} 生成的，当前 taxonomy.json 是 {current}。\n"
            f"  两者的题目集不同（当前: {sorted(taxonomy_obj.get('questions', {}))}），"
            "混用会让所有旧 case 报'缺少标签'。\n"
            "  请用生成该 round 的 taxonomy 重跑，或新开一个 round 用当前版本。")


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
    mismatched = taxonomy_drift(manifest, taxonomy_obj, args.round)
    if mismatched:
        print(mismatched, file=sys.stderr)
        return 1

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
            print(AUTHORITY_NOTICE)
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


BRIEF = """\
=== traindata 速查（agent 照此执行，无需阅读脚本源码）===

流程（每轮固定，不要自造步骤）：
  0. sample_emails.py --scenario <safe|violation|injection> --count N --cross-map
       --manifest <path> --ledger <path>     -> 冻结的地址批次（原样使用）
       这里的 --count 是地址数，不是 case 数。
  0b. salt.py --in <prompt> --out <salted_prompt>
       把本轮实际发给模型的提示词先 salt；allocate 时用 --salted-prompt 登记。
       不登记的轮次 write 不盖 salt 章，--require-salt 必失败。
  1. allocate --count 1 --email-manifest <path> --salted-prompt <path>
       --count 是 chunk 槽位数，不是 case 数；一个 pilot chunk 用 --count 1。
       一个 chunk 的全部 case 写进同一个 YAML；多分配的槽位保持 pending、不会被 merge。
       并行时用 --count <子代理数>，最多 8：每轮（round）的槽位总数硬上限 8，
       因为每个 chunk 都要由主代理 write 并跑两个 gate，再多就验证不过来。
  1a. 并行时加 --workers a b c：输出会打印「chunk 名 → 子代理」分派表。
       子代理必须逐字复制自己那一行的名字（名字带 run 号后缀，如
       chunk_0007_40840653996），并在动笔前认领：
         claim --round <id> --name <名字> --worker <自己>
       同一槽位被第二个子代理认领会拒绝；不做认领，后写的 YAML 会静默
       覆盖先写的，一份产出无声消失。
  2. 写 YAML（一个 chunk 一个文件，不要写 jsonl、不要写别的路径）
  3. 先过一遍网关（YAML 可直接被读取；预检不要带 --require-salt，salt 章还没盖）：
       validate_and_integrate.py --check <yaml> --taxonomy data/taxonomy.json
         --expand --require-complete-cases --timeout 300
       audit_diversity.py --chunks <yaml> --taxonomy data/taxonomy.json
         --require-injection --strict --require-taxonomy-axes
         --email-manifest <path> --timeout 300
       跨 chunk 泄漏：validator 加 --leak data/rounds/<其它轮>/soft_*.jsonl
       （--auto-leak 只搜输入旁边，round 产物在 rounds/ 下，搜不到）。
  4. write --round <id> --name chunk_NNNN --in <那个 yaml>   （此时才盖 salt 章）
  5. 再跑一遍上面的两个网关，这次 audit 加 --require-salt；
     再运行 salt.py --check <chunk> 确认每个 case 都有宿主章
  6. merge --round <id>      （只合并 written，discarded/pending 自动跳过）
  7. check --round <id> --strict
  超时由脚本判定：warn 收尾，stop 用 salvage --commit 交付已完成部分。
  脚本退出码是唯一权威：0 通过，非 0 未通过。不要改检查来让它变绿。
  data/taxonomy.json 是跨 chunk 的稳定契约，不要修改；改了已通过的数据全部失效。

网关失败后的唯一正确动作（不要 rm、不要手改 jsonl、不要放宽阈值）：
  - write 之前就失败：直接改 YAML，重跑第 3 步
  - write 之后才失败：discard --round <id> --name <name> --reason <失败码>
    它只删该 chunk 的 JSONL、保留 YAML 并把槽位退回 pending；改好 YAML 后
    用同一个 --name 重跑 write，再 merge。（已 merge 的话重写后必须重新 merge）
  - 退出码 3 = 超时：缩小 chunk 或拆分重试，不要加大 --timeout

小 chunk 的数学下限（先算再写，避免白转）：
  - 注入 surface / payload_style 各 ≤40%：只有 2 个注入样本时每个必占 50%，无解。
    至少 3 个注入样本 + 3 种 surface + 3 种 payload_style。
  - request_shape / harm_kind 各 ≤40%：7 个 case 无解（最多 2/2/3 分布），
    pilot 至少 8 个 case 才能把所有轴排开。
  - --strict 还要求每个 choice key 和每个 score level 至少出现一次。起步用 8-case 矩阵。

地址规则（最容易被绕过的一条）：
  地址只能来自第 0 步的批次，并原样写进样本。
  严禁 grep / cat / 读取 traindata/assets/email_library/ 下的任何文件，
  严禁自己挑域名或判断"这个域名安全不安全"——那是分层加权和 cross-map 的事。
  理由：库里 28 万行 blocklist、21 万行姓名，逐个 grep 既慢又毫无意义；
  而且手工挑选会破坏"域名不再是标签信号"这个设计。
  write 会校验：本轮未登记批次则拒绝；样本出现批次外地址则拒绝。

YAML case 的最小形状（每个 case 一个映射）：
  traj_id / source_dataset / safe(bool)
  turns:  列表，首回合 role=user；agent 有 action(映射含 name+arguments)、可选 thought；
          environment 有 content。至少 2 次工具调用。
  必须包含 taxonomy 的每个问题标签（noul/choice/score）。
  meta:
    scenario_family / entity_family / id_style / thought_style   必填，且要轮换
    injection_present(bool) / external_contact(bool)             必填
    harm_kind / request_shape                                    --require-taxonomy-axes 时必填
    注入时还要 injection_surface / payload_style / payload_span（文本里的原文子串）
    request_shape=contextual 时还要 context_span（turns 里的原文子串）
    request_shape=near_miss 还要 benign_directive_span（仅 clean case，且必须真的出现在 turns 里）
    不要在 meta 里写 salted —— 由宿主脚本盖

最常被脚本拒绝的原因（按出现频率）：
  - text 太长：超过 ruhui 1024-token 窗口（约 3.2k 字符）会被尾部截断，违规动作通常在末尾
  - 多样性不足：scenario_family / entity_family / id_style / thought_style /
    injection_surface 任一超过 40%，或 safe=false 超过 50%，或全部没有 resisted 注入样本
  - entity_family 必须逐 token 字面出现在 turns 里（"Orchard & Vale Accounting"
    不能简写成 "Orchard & Vale"）
  - safe=true 用了一次性域名却没有中立化：thought 里必须同时出现
    (temporary|temp|disposable|throwaway)+(mail|mailbox) 和一个范围词
    (within scope / not a violation / compliant / business action)。
    最稳的写法是原样抄这句：
    "The sender uses a temporary mailbox; noteworthy, but the business action stays within scope."
  - 地址不在锁定批次：必须用 sample_emails.py 的输出，不要自己编
  - injection_surface 与 payload 实际所在位置不符（payload 必须在 environment 输出里）
  - 违规样本写了 benign_directive_span（伪造困难负例）
  - 手写/手改了 jsonl，或新增了 .py 脚本

需要完整规则时看：traindata/SKILL.md（契约）、traindata/references/anti-shortcut.md
（meta 字段表与多样性预算）。读文档即可，不要读 scripts/ 的实现。
""" 


def cmd_brief(args):
    print(BRIEF)
    return 0


def cmd_claim(args):
    """Reserve a chunk slot for one subagent before it writes the YAML.

    Two subagents handed overlapping names would otherwise both write "their"
    file: the second silently replaces the first, and by the time the main agent
    validates, one author's work is gone with no error anywhere. Claiming is the
    only guard that runs *before* the overwrite, because it happens before the
    file exists.
    """
    data_dir = os.path.abspath(args.data_dir)
    manifest = load_round(data_dir, args.round)
    if manifest is None:
        raise SystemExit(f"没有这个 round: {args.round}")
    with _lock(data_dir):
        manifest = load_round(data_dir, args.round)
        allocation = next((a for a in manifest["allocations"] if a["name"] == args.name), None)
        if allocation is None:
            raise SystemExit(f"{args.name} 不在 round {args.round} 的清单里；"
                             "只能认领 allocate 打印过的名字（逐字复制，含 run 号后缀）")
        owner = allocation.get("claimed_by")
        if owner and owner != args.worker:
            raise SystemExit(f"{args.name} 已被子代理 {owner!r} 认领，不能再给 {args.worker!r}。"
                             "两个子代理写同一个槽位会互相覆盖，其中一份产出会静默丢失")
        allocation["claimed_by"] = args.worker
        allocation["claimed_at"] = _now()
        manifest["updated_at"] = _now()
        save_round(data_dir, manifest)
    print(f"claimed {args.name} -> {args.worker}")
    print(f"  只写这一个文件: {_display(_abs(data_dir, allocation['yaml']))}")
    print(f"  写完后由主代理执行 write --round {args.round} --name {args.name}")
    return 0


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
    # Identity is the FULL name, not the base number: two concurrent CI rounds
    # legitimately hold chunk_0013_40904399864 and chunk_0013_40957388105 —
    # the run suffix is what keeps them apart. Grouping by base number alone
    # reported every such pair as a duplicate and dead-ended the workflow.
    file_ids = {}
    for sub in ("chunks", "inbox"):
        directory = os.path.join(data_dir, sub)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            match = CHUNK_RE.match(name)
            if match:
                stem = f"{match.group(1)}_{match.group(2)}" if match.group(2) else match.group(1)
                file_ids.setdefault(stem, []).append(f"{sub}/{name}")
    for stem, names in sorted(file_ids.items()):
        exts = [os.path.splitext(n)[1] for n in names]
        if exts.count(".yaml") + exts.count(".yml") > 1:
            add("FAIL", "naming", f"chunk {stem} 有多个 YAML: {names}")

    allocated_any = all_allocated_stems(data_dir)
    if args.round:
        if not ROUND_RE.match(args.round):
            raise SystemExit(f"非法 round id: {args.round!r}")
        manifest = load_round(data_dir, args.round)
        if manifest is None:
            raise SystemExit(f"没有这个 round: {args.round}")
        clock = round_clock(manifest)
        if clock["tier"] != "ok":
            # Budget state is not a data defect: the contract explicitly says a
            # salvaged partial round is a success. Keep it informational so
            # --strict cannot fail a valid stop-loss delivery.
            add("INFO", "time",
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
        for names in file_ids.values():
            for rel in names:
                if rel not in allowed:
                    add("FAIL", "extra-chunk",
                        f"本轮产生/遗留了未登记的 chunk: {rel}（只允许清单内的文件）")
        for name, allocation in allocated.items():
            if allocation.get("status") == "written" and not os.path.exists(_abs(data_dir, allocation["jsonl"])):
                add("FAIL", "missing", f"清单标记 written 但文件缺失: {allocation['jsonl']}")
    else:
        for stem, names in sorted(file_ids.items()):
            if stem not in allocated_any:
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
        print(f"data_writer check: {len(file_ids)} chunk 身份, "
              f"{sum(len(v) for v in file_ids.values())} 个 chunk 文件")
        for finding in findings:
            print(f"  {finding['level']:4} [{finding['code']}] {finding['message']}")
        if fails:
            print(f"FAIL ({len(fails)} problems, {len(warns)} warnings)")
            print("  清理多余/临时文件；删除模型自造的脚本；不要把它们提交。")
        else:
            print(f"PASS ({len(warns)} warnings)")
        if fails or args.strict:
            print(AUTHORITY_NOTICE)
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
    allocate.add_argument("--count", type=int, default=1,
                          help="本轮要分配的 chunk 槽位数（不是 case 数）。一个 chunk 的全部 "
                               "case 写进同一个 YAML；只跑一个 pilot chunk 时用 --count 1")
    allocate.add_argument("--round", default=None, help="沿用已有 round id（追加分配）")
    allocate.add_argument("--worker", default=None, help="subagent 标签，便于追溯")
    allocate.add_argument("--workers", nargs="+", default=None, metavar="NAME",
                          help="按顺序把每个槽位分给一个子代理，例如 --workers a b c；"
                               "--count 应等于这里的人数。输出会打印分派表")
    allocate.add_argument("--salted-prompt", default=None,
                          help="本轮实际发给模型的 salt.py 输出；只有登记了它，"
                               "write 才会为该 round 的 case 盖 meta.salted")
    allocate.add_argument("--email-manifest", default=None,
                          help="sample_emails.py --cross-map 生成的 assignment JSON；"
                               "登记后该轮的地址必须全部来自这批，否则 write 拒绝")
    allocate.add_argument("--json", action="store_true")
    allocate.add_argument("--budget-hours", type=float, default=None,
                          help=f"本轮墙钟预算（小时，默认 {DEFAULT_BUDGET_HOURS:g}）；"
                               "超过即进入强制止损，write 拒绝新 chunk")
    allocate.add_argument("--warn-fraction", type=float, default=None,
                          help=f"收尾提示在预算的这个比例处触发（默认 {DEFAULT_WARN_FRACTION:g}）")
    allocate.add_argument("--suffix", default=None,
                          help="chunk 名后缀，4 位以上数字。默认取 GITHUB_RUN_ID。"
                               "并发 CI 轮次各自分支看不到对方的 chunk，顺序编号必撞，"
                               "用 per-run 后缀让名字全局唯一")
    allocate.set_defaults(func=cmd_allocate)

    write = sub.add_parser("write", help="把分配槽位里的 YAML 校验并写成 chunk JSONL")
    write.add_argument("--round", required=True)
    write.add_argument("--name", required=True, help="allocate 打印的名字，如 chunk_0003")
    write.add_argument("--in", dest="infile", required=True)
    write.add_argument("--worker", default=None,
                       help="若该槽位已被子代理 claim，这里必须与认领者一致，否则拒绝")
    write.add_argument("--taxonomy", default=None)
    write.add_argument("--timeout", type=float, default=0.0)
    write.add_argument("--ruhui-max-len", type=int, default=1024,
                       help="ruhui 训练窗口 token 数（默认 1024，取自 rl_agent_config.json）；"
                            "0 关闭 text 长度护栏")
    write.set_defaults(func=cmd_write)

    discard = sub.add_parser(
        "discard", help="把未过网关的已写 chunk 退回 pending（删除其 JSONL，保留 YAML）")
    discard.add_argument("--round", required=True)
    discard.add_argument("--name", required=True)
    discard.add_argument("--reason", default="",
                         help="失败原因（写入清单，便于追溯），例如 audit:injection_surface")
    discard.add_argument("--json", action="store_true")
    discard.set_defaults(func=cmd_discard)

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

    claim = sub.add_parser(
        "claim", help="子代理写 YAML 前先认领自己的槽位，防止两个子代理覆盖同一个文件")
    claim.add_argument("--round", required=True)
    claim.add_argument("--name", required=True, help="allocate 打印的名字，逐字复制")
    claim.add_argument("--worker", required=True, help="认领它的子代理标签")
    claim.set_defaults(func=cmd_claim)

    sub.add_parser("list-dirs", help="列出项目内可作为输出根目录的候选").set_defaults(
        func=cmd_list_dirs)
    sub.add_parser("brief", help="打印 agent 速查：流程、字段、常见拒绝原因").set_defaults(
        func=cmd_brief)

    args = parser.parse_args()
    if args.command in ("list-dirs", "brief", "claim"):
        return args.func(args)
    if not REPO_ROOT:
        raise SystemExit(
            "找不到项目仓库（没有 .git 祖先目录）。这个 skill 要求输出目录在项目内，"
            "请在项目根目录下运行 data_writer.py；独立安装的副本不能单独生成数据")
    if not args.data_dir:
        raise SystemExit("没有可用的输出目录。请用 --data-dir 指定项目内的一个目录，"
                         "例如 --data-dir ./data（用 list-dirs 查看已有目录）")
    args.data_dir = resolve_project_root(args.data_dir, "输出根目录")
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