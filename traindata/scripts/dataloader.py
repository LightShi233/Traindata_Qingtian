#!/usr/bin/env python3
"""Tolerant input layer shared by the traindata gate scripts.

Models do not reliably emit strict JSONL. This module makes the gates accept
what a model actually produces, without relaxing any content rule:

- UTF-8 BOM, CRLF, blank lines, trailing commas
- a JSON array / single pretty-printed object instead of JSONL
- markdown fences (```` ```json ````) around the payload
- YAML case files (stdlib subset parser; no PyYAML dependency)

It also provides atomic writes (a crash must not leave a half-written corpus)
and a wall-clock ``Deadline`` so a gate can never hang a CI job forever.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time

FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*$")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
INT_RE = re.compile(r"[-+]?\d+")
FLOAT_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?")
KEY_RE = re.compile(r"^(?:[A-Za-z_][\w.\-]*|\"[^\"]*\"|'[^']*')\s*$")
BLOCK_INDICATOR_RE = re.compile(r"^[|>][+-]?\d*$")


class YamlError(ValueError):
    """A YAML document could not be parsed by the built-in subset reader."""


# Every gate is the authority on whether a chunk may exist. Prompts drift, a
# model can rationalise, a subagent can report success it never achieved — the
# script's exit code cannot. Print this wherever a decision is made so the
# calling agent knows the verdict is not negotiable.
AUTHORITY_NOTICE = (
    "AUTHORITY: 本脚本的输出是唯一裁判。退出码 0 才算通过，非 0 即未通过。\n"
    "           agent 与 subagent 都必须服从脚本结论：不得改写、绕过、放宽检查，\n"
    "           不得用自述或自证代替脚本判定；被拒绝时按提示修复后重跑脚本。")


class Deadline:
    """Wall-clock budget for a run. ``seconds <= 0`` disables the check."""

    def __init__(self, seconds: float = 0.0):
        self.seconds = float(seconds or 0.0)
        self.expires = (time.monotonic() + self.seconds) if self.seconds > 0 else None

    def check(self, where: str = "") -> None:
        if self.expires is not None and time.monotonic() > self.expires:
            suffix = f" ({where})" if where else ""
            raise TimeoutError(f"--timeout {self.seconds:g}s 已用尽{suffix}")

    def remaining(self) -> float | None:
        return None if self.expires is None else max(0.0, self.expires - time.monotonic())


# --------------------------------------------------------------------------- #
# file IO
# --------------------------------------------------------------------------- #
def read_text(path: str) -> str:
    """Read UTF-8, dropping a BOM and never crashing on invalid bytes."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def atomic_write_text(path: str, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + rename in the same dir."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_jsonl(path: str, rows) -> None:
    atomic_write_text(path, "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


# --------------------------------------------------------------------------- #
# tolerant JSON / JSONL reader
# --------------------------------------------------------------------------- #
def _repair(line: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", line)


def _yaml_items(path: str):
    """Parse a YAML file into a flat list of items (``cases:`` is unwrapped)."""
    items = []
    for doc in parse_yaml(read_text(path)):
        if doc is None:
            continue
        if isinstance(doc, list):
            items.extend(doc)
        elif isinstance(doc, dict) and isinstance(doc.get("cases"), list):
            items.extend(doc["cases"])
        else:
            items.append(doc)
    return items


def read_jsonl(path: str, deadline: Deadline | None = None):
    """Yield ``(lineno, obj)``; a parse failure yields ``{"__parse_error__": ...}``.

    Accepts JSONL, a JSON array, one pretty-printed object, a fenced block, or a
    ``.yaml``/``.yml`` case file. A file that is one well-formed document is
    returned whole; otherwise the reader falls back to line-by-line JSONL. For
    YAML the first tuple element is the 1-based case index.
    """
    if os.path.splitext(path)[1].lower() in (".yaml", ".yml"):
        try:
            items = _yaml_items(path)
        except YamlError as exc:
            yield 1, {"__parse_error__": f"YAML 解析失败: {exc}"}
            return
        for index, item in enumerate(items, 1):
            if deadline:
                deadline.check(f"{os.path.basename(path)} case {index}")
            yield index, coerce_case(item) if isinstance(item, dict) else item
        return

    text = read_text(path)
    lines = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            continue
        lines.append((lineno, line))

    body = "\n".join(line for _, line in lines).strip()
    if body[:1] in ("{", "["):
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            try:
                document = json.loads(_repair(body))
            except json.JSONDecodeError:
                document = None
        if document is not None:
            if isinstance(document, list):
                for i, item in enumerate(document, 1):
                    if deadline:
                        deadline.check(f"{os.path.basename(path)} item {i}")
                    yield i, item
                return
            if isinstance(document, dict):
                if isinstance(document.get("cases"), list):
                    for i, item in enumerate(document["cases"], 1):
                        if deadline:
                            deadline.check(f"{os.path.basename(path)} item {i}")
                        yield i, item
                    return
                yield 1, document
                return

    for lineno, line in lines:
        if deadline:
            deadline.check(f"{os.path.basename(path)}:{lineno}")
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            yield lineno, json.loads(stripped)
            continue
        except json.JSONDecodeError as exc:
            first = exc
        try:
            yield lineno, json.loads(_repair(stripped.rstrip(",")))
        except json.JSONDecodeError:
            yield lineno, {"__parse_error__": str(first)}


def coerce_case(case):
    """Serialise YAML-native nested turn fields to the JSON strings the contract wants.

    A YAML author writes ``action:`` as a mapping and ``content:`` as a block.
    The record contract stores both as strings, so the loader normalises them
    once, at the boundary, instead of every consumer re-implementing it.
    """
    turns = case.get("turns")
    if not isinstance(turns, list):
        return case
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        for field in ("action", "content", "thought"):
            value = turn.get(field)
            if isinstance(value, (dict, list)):
                turn[field] = json.dumps(value, ensure_ascii=False)
            elif value is not None and not isinstance(value, str):
                turn[field] = str(value)
    return case


def load_cases(paths, deadline: Deadline | None = None):
    """Load case-form rows from JSON/JSONL/YAML files.

    Returns ``(cases, errors, skipped_record_form)`` where ``cases`` is a list of
    ``(where, case_dict)``. Record-form rows (no ``turns``) are counted, not
    failed: gate scripts that need case form report the count themselves.
    """
    cases, errors = [], []
    skipped = 0
    for path in paths:
        base = os.path.basename(path)
        if os.path.splitext(path)[1].lower() in (".yaml", ".yml"):
            try:
                items = _yaml_items(path)
            except YamlError as exc:
                errors.append(f"{base}: YAML 解析失败: {exc}")
                continue
            for i, item in enumerate(items, 1):
                if deadline:
                    deadline.check(f"{base} case {i}")
                where = f"{base}:case{i}"
                if not isinstance(item, dict):
                    errors.append(f"{where}: case 必须是映射/对象，实际 {type(item).__name__}")
                    continue
                cases.append((where, coerce_case(item)))
            continue

        for lineno, obj in read_jsonl(path, deadline):
            where = f"{base}:{lineno}"
            if isinstance(obj, dict) and "__parse_error__" in obj:
                errors.append(f"{where}: JSON 解析失败: {obj['__parse_error__']}")
                continue
            if not isinstance(obj, dict):
                errors.append(f"{where}: 必须是 JSON 对象，实际 {type(obj).__name__}")
                continue
            if "turns" not in obj:
                skipped += 1
                continue
            cases.append((where, coerce_case(obj)))
    return cases, errors, skipped


# --------------------------------------------------------------------------- #
# YAML subset parser (stdlib only)
# --------------------------------------------------------------------------- #
def _strip_comment(text: str) -> str:
    """Drop a ``#`` comment that starts a token, honouring quotes."""
    in_single = in_double = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
                return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _is_seq_line(raw: str, indent: int) -> bool:
    body = raw[indent:].lstrip()
    return body == "-" or body.startswith("- ")


def _unquote_key(key: str) -> str:
    if len(key) >= 2 and key[0] == key[-1] == "'":
        return key[1:-1].replace("''", "'")
    if len(key) >= 2 and key[0] == key[-1] == '"':
        try:
            return json.loads(key)
        except json.JSONDecodeError:
            return key[1:-1]
    return key


def _split_mapping(text: str):
    """Return ``(key, rest)`` for a ``key: value`` line, else ``None``."""
    if SCHEME_RE.match(text):
        return None
    in_single = in_double = False
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == ":" and depth == 0:
                key_raw = text[:i].strip()
                if text[i + 1:i + 2] in ("", " "):
                    return _unquote_key(key_raw), text[i + 1:].strip()
                # lenient: ``role:user`` — accept an identifier key with no space
                if KEY_RE.match(key_raw):
                    return _unquote_key(key_raw), text[i + 1:].strip()
        i += 1
    return None


def _plain_scalar(text: str):
    value = text.strip()
    low = value.lower()
    if low in ("null", "~", ""):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if INT_RE.fullmatch(value):
        # Keep leading-zero tokens as strings: ``id: 000101`` must not become 101.
        if re.fullmatch(r"[-+]?0\d+", value):
            return value
        try:
            return int(value)
        except ValueError:
            return value
    if FLOAT_RE.fullmatch(value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _scan_quoted(text: str, lineno: int):
    """Parse one quoted scalar at the start of ``text``; return (value, end)."""
    quote = text[0]
    i = 1
    if quote == '"':
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                try:
                    return json.loads(text[:i + 1]), i + 1
                except json.JSONDecodeError as exc:
                    raise YamlError(f"第 {lineno} 行：双引号字符串转义非法: {exc}") from exc
            i += 1
        raise YamlError(f"第 {lineno} 行：双引号字符串未闭合；多行文本请用 | 块标量")
    buf = []
    while i < len(text):
        if text[i] == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            return "".join(buf), i + 1
        buf.append(text[i])
        i += 1
    raise YamlError(f"第 {lineno} 行：单引号字符串未闭合；多行文本请用 | 块标量")


def _parse_flow(text: str, lineno: int):
    """Parse an inline ``[...]`` / ``{...}`` flow collection."""
    pos = 0

    def skip_ws():
        nonlocal pos
        while pos < len(text) and text[pos] in " \t":
            pos += 1

    def value():
        nonlocal pos
        skip_ws()
        if pos >= len(text):
            return None
        ch = text[pos]
        if ch == "[":
            return sequence()
        if ch == "{":
            return mapping()
        if ch in "\"'":
            val, end = _scan_quoted(text[pos:], lineno)
            pos += end
            return val
        start = pos
        depth = 0
        while pos < len(text):
            c = text[pos]
            if c in "[{":
                depth += 1
            elif c in "]}":
                if depth == 0:
                    break
                depth -= 1
            elif c == "," and depth == 0:
                break
            pos += 1
        return _plain_scalar(text[start:pos])

    def sequence():
        nonlocal pos
        pos += 1
        out = []
        while True:
            skip_ws()
            if pos >= len(text):
                raise YamlError(f"第 {lineno} 行：flow 序列未闭合")
            if text[pos] == "]":
                pos += 1
                return out
            out.append(value())
            skip_ws()
            if pos < len(text) and text[pos] == ",":
                pos += 1
                continue
            if pos < len(text) and text[pos] == "]":
                pos += 1
                return out
            raise YamlError(f"第 {lineno} 行：flow 序列语法错误")

    def mapping():
        nonlocal pos
        pos += 1
        out = {}
        while True:
            skip_ws()
            if pos >= len(text):
                raise YamlError(f"第 {lineno} 行：flow 映射未闭合")
            if text[pos] == "}":
                pos += 1
                return out
            if text[pos] in "\"'":
                key, end = _scan_quoted(text[pos:], lineno)
                pos += end
            else:
                start = pos
                while pos < len(text) and text[pos] not in ":},":
                    pos += 1
                key = text[start:pos].strip()
            skip_ws()
            if pos >= len(text) or text[pos] != ":":
                raise YamlError(f"第 {lineno} 行：flow 映射缺少 ':'")
            pos += 1
            out[key] = value()
            skip_ws()
            if pos < len(text) and text[pos] == ",":
                pos += 1
                continue
            if pos < len(text) and text[pos] == "}":
                pos += 1
                return out
            raise YamlError(f"第 {lineno} 行：flow 映射语法错误")

    return value(), pos


def _flow_depth(text: str) -> int:
    """Net unclosed ``[``/``{`` count, ignoring quotes."""
    depth = 0
    in_single = in_double = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
        i += 1
    return depth


def _fold(lines) -> str:
    out, buf = [], []
    for line in lines:
        if line.strip() == "":
            if buf:
                out.append(" ".join(buf))
                buf = []
            out.append("")
        else:
            buf.append(line)
    if buf:
        out.append(" ".join(buf))
    return "\n".join(out)


class _YamlParser:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def _peek(self):
        while self.i < len(self.lines):
            lineno, indent, raw = self.lines[self.i]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                self.i += 1
                continue
            return self.i, lineno, indent, raw
        return None

    def parse(self):
        first = self._peek()
        if first is None:
            return None
        value = self.parse_block(first[2])
        leftover = self._peek()
        if leftover is not None:
            raise YamlError(f"第 {leftover[1]} 行：无法解析的剩余内容 {leftover[3].strip()!r}")
        return value

    def parse_block(self, indent: int):
        peek = self._peek()
        if peek is None:
            return None
        _, lineno, actual, raw = peek
        if actual < indent:
            return None
        if actual > indent:  # sloppy over-indent: adopt it rather than fail
            indent = actual
        text = _strip_comment(raw[indent:]).strip()
        if text == "-" or text.startswith("- "):
            return self.parse_sequence(indent)
        if _split_mapping(text) is not None:
            return self.parse_mapping(indent)
        self.i += 1
        return self.parse_inline(text, lineno, indent)

    def parse_mapping(self, indent: int):
        out = {}
        while True:
            peek = self._peek()
            if peek is None:
                break
            _, lineno, actual, raw = peek
            if actual < indent:
                break
            if actual > indent:
                raise YamlError(f"第 {lineno} 行：缩进 {actual} 与映射的 {indent} 不一致")
            text = _strip_comment(raw[indent:]).strip()
            if text == "-" or text.startswith("- "):
                break
            pair = _split_mapping(text)
            if pair is None:
                raise YamlError(f"第 {lineno} 行：不是 key: value 形式: {text!r}")
            key, rest = pair
            self.i += 1
            if rest == "":
                nxt = self._peek()
                if nxt is None:
                    out[key] = None
                elif nxt[2] > actual:
                    out[key] = self.parse_block(nxt[2])
                elif nxt[2] == actual and _is_seq_line(nxt[3], actual):
                    out[key] = self.parse_sequence(actual)
                else:
                    out[key] = None
            elif BLOCK_INDICATOR_RE.match(rest):
                out[key] = self._block_scalar(rest, actual, lineno)
            else:
                out[key] = self.parse_inline(rest, lineno, actual)
        return out

    def parse_sequence(self, indent: int):
        out = []
        while True:
            peek = self._peek()
            if peek is None:
                break
            idx, lineno, actual, raw = peek
            if actual < indent:
                break
            if actual > indent:
                raise YamlError(f"第 {lineno} 行：序列缩进 {actual} 与 {indent} 不一致")
            text = raw[indent:]
            if not (text == "-" or text.startswith("- ")):
                break
            tail = text[1:]
            lead = len(tail) - len(tail.lstrip(" "))
            content = _strip_comment(tail).strip()
            self.i += 1
            if content == "":
                nxt = self._peek()
                out.append(self.parse_block(nxt[2]) if nxt and nxt[2] > actual else None)
            elif BLOCK_INDICATOR_RE.match(content):
                out.append(self._block_scalar(content, actual, lineno))
            else:
                column = actual + 1 + lead
                self.lines[idx] = [lineno, column, " " * column + content]
                self.i = idx
                out.append(self.parse_block(column))
        return out

    def _block_scalar(self, indicator: str, parent_indent: int, lineno: int) -> str:
        fold = indicator[0] == ">"
        chomp = indicator[1] if len(indicator) > 1 and indicator[1] in "+-" else ""
        body = []
        block_indent = None
        while self.i < len(self.lines):
            _, indent, raw = self.lines[self.i]
            if raw.strip() == "":
                body.append("")
                self.i += 1
                continue
            if indent <= parent_indent:
                break
            if block_indent is None:
                block_indent = indent
            if indent < block_indent:
                break
            body.append(raw[block_indent:])
            self.i += 1
        while body and body[-1].strip() == "" and chomp != "+":
            body.pop()
        text = _fold(body) if fold else "\n".join(body)
        if chomp == "-":
            return text.rstrip("\n")
        if chomp == "+":
            return text + ("\n" if text else "")
        return text + ("\n" if text else "")

    def parse_inline(self, text: str, lineno: int, indent: int):
        if not text:
            return None
        if text[0] in "\"'":
            value, end = _scan_quoted(text, lineno)
            if text[end:].strip():
                raise YamlError(f"第 {lineno} 行：引号后还有多余内容 {text[end:].strip()!r}")
            return value
        if text[0] in "[{":
            # A flow collection may continue over several lines; gather until
            # the brackets balance (quotes respected) before parsing.
            buffer = text
            while _flow_depth(buffer) > 0 and self.i < len(self.lines):
                buffer += " " + self.lines[self.i][2].strip()
                self.i += 1
            value, end = _parse_flow(buffer, lineno)
            if buffer[end:].strip():
                raise YamlError(f"第 {lineno} 行：flow 后还有多余内容 {buffer[end:].strip()!r}")
            return value
        return _plain_scalar(text)


def _lex(text: str):
    lines = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        raw = raw.rstrip("\r")
        stripped = raw.lstrip(" \t")
        lead = raw[:len(raw) - len(stripped)]
        if "\t" in lead:
            raise YamlError(f"第 {lineno} 行：缩进不允许使用 tab")
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append([lineno, indent, raw])
    return lines


def _split_documents(lines):
    documents, current = [], []
    for record in lines:
        _, indent, raw = record
        stripped = raw.strip()
        if indent == 0 and (stripped == "---" or stripped.startswith("--- ")):
            if current:
                documents.append(current)
            current = []
            continue
        if indent == 0 and stripped == "...":
            if current:
                documents.append(current)
            current = []
            continue
        current.append(record)
    if current:
        documents.append(current)
    return [d for d in documents
            if any(r[2].strip() and not r[2].strip().startswith("#") for r in d)]


def parse_yaml(text: str):
    """Parse YAML into a list of documents using the supported subset.

    Supported: block mappings/sequences, quoted and plain scalars, literal and
    folded block scalars, inline flow collections, comments, multiple documents,
    and a top-level ``cases:`` list. Anchors, aliases, tags and complex keys are
    rejected with a location, never silently misread.
    """
    if re.search(r"(^|\s)[&*][A-Za-z_]", text):
        raise YamlError("不支持 YAML 锚点/别名（& / *），请展开后再写")
    documents = []
    for lines in _split_documents(_lex(text)):
        documents.append(_YamlParser(lines).parse())
    return documents