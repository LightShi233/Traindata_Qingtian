#!/usr/bin/env python3
"""Convert model-authored YAML case files into validated case-form JSONL.

Why YAML: a model writing raw JSONL has to hand-escape every quote inside
``turns``, and one bad escape silently produces a broken chunk. In YAML the
same case is written as structure — ``action:`` is a mapping, ``content:`` is a
``|`` block — and this script does the escaping. The model writes what it means;
the script produces the byte-exact JSON the gates require.

    # case-form YAML -> case-form JSONL (the unit the gates accept)
    python3 traindata/scripts/yaml_to_jsonl.py --in 'cases_*.yaml' --out chunk_0003.jsonl

    # validate only, write nothing
    python3 traindata/scripts/yaml_to_jsonl.py --in 'cases_*.yaml' --taxonomy data/taxonomy.json --check

    # expand straight to ruhui records (skips the separate --integrate step)
    python3 traindata/scripts/yaml_to_jsonl.py --in 'cases_*.yaml' --expand \
      --taxonomy data/taxonomy.json --out chunk_0003.jsonl

Conversions are fail-closed: if any case is malformed, nothing is written and
the exit code is non-zero. Normalisation makes the two common authoring
mistakes safe:

- ``action:`` written as a mapping (or list) is serialised to a JSON string.
- ``content:`` / ``thought:`` written as a mapping is serialised to a string.

No PyYAML: the reader is the stdlib subset in ``dataloader.py``.

Exit codes: 0 ok, 1 invalid input, 2 no input matched, 3 timeout.
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataloader import (  # noqa: E402
    Deadline,
    atomic_write_text,
    load_cases,
)
import validate_and_integrate as vi  # noqa: E402


def _stringify(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def normalise_case(case, where):
    """Make a parsed YAML case match the JSON case contract. Returns errors."""
    errors = []
    if not isinstance(case, dict):
        return [f"{where}: case 必须是映射/对象，实际 {type(case).__name__}"]

    if "safe" in case and not isinstance(case["safe"], bool):
        errors.append(f"{where}: safe 必须是 true/false，实际 {case['safe']!r}")
    if "meta" in case and not isinstance(case["meta"], dict):
        errors.append(f"{where}: meta 必须是映射，实际 {type(case['meta']).__name__}")

    turns = case.get("turns")
    if turns is None:
        errors.append(f"{where}: 缺少 turns；case-form 必须包含回合列表")
        return errors
    if not isinstance(turns, list):
        errors.append(f"{where}: turns 必须是列表，实际 {type(turns).__name__}")
        return errors

    for index, turn in enumerate(turns):
        tag = f"{where}: turns[{index}]"
        if not isinstance(turn, dict):
            errors.append(f"{tag} 必须是映射，实际 {type(turn).__name__}")
            continue
        if "role" not in turn:
            errors.append(f"{tag} 缺少 role")
        for field in ("content", "thought", "action"):
            if field not in turn:
                continue
            value = turn[field]
            if isinstance(value, (dict, list)):
                turn[field] = _stringify(value)
            elif value is None:
                turn.pop(field)
            elif not isinstance(value, str):
                errors.append(f"{tag}.{field} 必须是字符串/映射，实际 {value!r}")
        action = turn.get("action")
        if isinstance(action, str) and action.strip():
            try:
                parsed = json.loads(action)
            except json.JSONDecodeError as exc:
                errors.append(f"{tag}.action 不是合法 JSON: {exc}；请写成映射（action: {{name: ..., arguments: {{...}}}}）")
                continue
            if not isinstance(parsed, dict) or "name" not in parsed or "arguments" not in parsed:
                errors.append(f"{tag}.action 必须含 name/arguments")
    return errors


def validate_cases(cases, taxonomy, ruhui_max_len=0):
    args = argparse.Namespace(
        min_len=0, max_len=0, leak_index=None, intra=0, taxonomy=taxonomy,
        ruhui_max_len=ruhui_max_len)
    state = vi.new_state()
    errors = []
    for where, case in cases:
        errors.extend(vi.check_case(case, where, args, state))
    return errors


def convert(paths, taxonomy=None, expand=False, deadline=None, force_validate=False,
            ruhui_max_len=0):
    """Convert YAML case files to text payload.

    Returns ``(payload, summary, errors)``. ``payload`` is ``None`` when there
    are errors; callers must treat that as fail-closed and write nothing. This
    is the single conversion path shared by the CLI and ``data_writer.py``.
    """
    deadline = deadline or Deadline(0)
    cases, load_errors, skipped = load_cases(paths, deadline)
    errors = list(load_errors)
    for where, case in cases:
        errors.extend(normalise_case(case, where))
    if not errors and (taxonomy or force_validate):
        errors.extend(validate_cases(cases, taxonomy, ruhui_max_len))
    if skipped:
        errors.append(f"{skipped} 行是记录形式（无 turns），不是 case 形式；"
                      "请提供每场景一行的 case")
    if expand and not taxonomy:
        errors.append("--expand 需要 --taxonomy")
    deadline.check("conversion")
    if errors:
        return None, None, errors
    try:
        if expand:
            rows = []
            for index, (_, case) in enumerate(cases):
                rows.extend(vi.expand_case(case, index, taxonomy))
            payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            summary = f"{len(cases)} cases -> {len(rows)} records"
        else:
            payload = "".join(json.dumps(c, ensure_ascii=False) + "\n" for _, c in cases)
            summary = f"{len(cases)} cases"
    except (KeyError, ValueError) as exc:
        return None, None, [f"展开失败: {exc}；请确认 taxonomy 与 case 标签一致"]
    return payload, summary, []


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="inputs", nargs="+", default=["cases_*.yaml"],
                        help="YAML 文件或 glob（默认 cases_*.yaml）")
    parser.add_argument("--out", default=None,
                        help="输出 JSONL；省略则写到 stdout（--check 时不写）")
    parser.add_argument("--taxonomy", default=None,
                        help="taxonomy.json；--expand 必需，其他情况用于校验标签")
    parser.add_argument("--expand", action="store_true",
                        help="直接展开为 ruhui 记录（需要 --taxonomy）")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    parser.add_argument("--timeout", type=float, default=0.0,
                        help="整次运行的墙钟上限（秒，0=不限）")
    args = parser.parse_args()

    deadline = Deadline(args.timeout)
    if args.expand and not args.taxonomy:
        parser.error("--expand 需要 --taxonomy")

    paths = []
    for pattern in args.inputs:
        paths.extend(sorted(glob.glob(pattern)))
    paths = sorted(dict.fromkeys(paths))
    if not paths:
        print(f"没有匹配的文件: {' '.join(args.inputs)}", file=sys.stderr)
        return 2

    taxonomy = None
    if args.taxonomy:
        try:
            with open(args.taxonomy, encoding="utf-8") as f:
                taxonomy = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"taxonomy 读取失败: {args.taxonomy}: {exc}", file=sys.stderr)
            return 1

    try:
        payload, summary, errors = convert(
            paths, taxonomy, args.expand, deadline,
            force_validate=args.check or bool(taxonomy))
    except TimeoutError as exc:
        print(f"TIMEOUT: {exc}", file=sys.stderr)
        return 3

    if errors:
        print(f"YAML 转换失败 ({len(errors)} errors)")
        for err in errors[:40]:
            print(" -", err)
        if len(errors) > 40:
            print(f" ... 还有 {len(errors) - 40} 条")
        print("未写入任何文件（fail-closed）。修好 YAML 后重跑；不要手改 JSONL。")
        return 1

    if args.check:
        print(f"OK: {summary}（未写入）")
        return 0
    if args.out:
        atomic_write_text(args.out, payload)
        print(f"{summary} -> {args.out}")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())