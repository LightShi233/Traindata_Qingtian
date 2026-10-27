#!/usr/bin/env python3
"""Extract CCNC Chinese names into ASCII pinyin vocab (GPL-3.0 source).

CCNC (https://github.com/jaaack-wang/ccnc) is a GPL-3.0 Chinese name corpus.
The repo stores only the Chinese text; the pinyin version lives on Drive. This
script streams ``ccnc.txt`` (tab-separated: 姓 / 名 / 全名 / 性别), takes distinct
surnames and given names up to caps, romanizes them with ``pypinyin`` (no tones),
sanitises to ``[a-z]{2,24}`` and writes:

    sources/names_first_ccnc.txt
    sources/names_last_ccnc.txt

``build_prefix_vocab.py`` then merges them with the other name sources.

Requires ``pypinyin``; run it as a one-off, e.g.:

    nix-shell -p python3Packages.pypinyin --run \\
      "python3 traindata/scripts/build_ccnc_sources.py \\
         --zip /path/to/ccnc.txt.zip \\
         --out traindata/assets/email_library/sources"

The generated files are committed, so the skill does not need pypinyin at run
time.
"""
import argparse
import os
import re
import sys
import zipfile

CJK = re.compile(r"[\u4e00-\u9fff]")
TOKEN_RE = re.compile(r"^[a-z]{2,24}$")


def clean(syllables):
    t = re.sub(r"[^a-z]", "", "".join(syllables).lower())
    return t if TOKEN_RE.match(t) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", required=True, help="ccnc.txt.zip")
    ap.add_argument("--member", default="ccnc.txt")
    ap.add_argument("--out", required=True, help="sources dir")
    ap.add_argument("--max-last", type=int, default=4000)
    ap.add_argument("--max-first", type=int, default=400000)
    args = ap.parse_args()

    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        sys.exit("需要 pypinyin；用 nix-shell -p python3Packages.pypinyin 运行")

    last, first = {}, {}
    with zipfile.ZipFile(args.zip) as z, z.open(args.member) as f:
        for raw in f:
            try:
                line = raw.decode("utf-8", "replace").rstrip("\n")
            except Exception:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ln, fn = parts[0].strip(), parts[1].strip()
            if ln and ln not in last and len(last) < args.max_last:
                last[ln] = None
            if fn and fn not in first and len(first) < args.max_first:
                first[fn] = None

    def romanize(mapping):
        out, seen = [], set()
        for han in mapping:
            if not CJK.search(han):
                continue
            t = clean(lazy_pinyin(han))
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    out_last = romanize(last)
    out_first = romanize(first)
    os.makedirs(args.out, exist_ok=True)
    for name, toks in (("names_last_ccnc.txt", out_last),
                       ("names_first_ccnc.txt", out_first)):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as g:
            g.write("\n".join(toks) + "\n")
        print(f"{name}: {len(toks)}")
    print(f"distinct han: last {len(last)} first {len(first)}")


if __name__ == "__main__":
    main()