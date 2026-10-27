#!/usr/bin/env python3
"""Weld a first-name / last-name / role vocabulary for compositional prefixes.

Reads every ``sources/names_first_*.txt``, ``sources/names_last_*.txt`` and
``sources/roles_*.txt``, sanitises each token to lowercase ASCII letters, drops
duplicates and junk, and writes the merged vocab:

    names_first.txt   given names (e.g. ~160k from names.io + additions)
    names_last.txt    family names
    roles.txt         role/team mailbox words

``sample_emails.py`` composes local parts from these instead of drawing from a
fixed list, so the prefix space is names_first × names_last × patterns (billions)
and cannot be memorised. Only the vocab is welded; the sampler generates fresh
prefixes per batch and de-duplicates them with a ledger.

Usage
-----
    python3 traindata/scripts/build_prefix_vocab.py
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"^[a-z]{2,24}$")
KEEP_SYSTEM = {"abuse", "postmaster", "webmaster", "hostmaster", "mailerdaemon"}


def sanitize(raw):
    t = re.sub(r"[^a-z]", "", raw.strip().lower())
    return t if TOKEN_RE.match(t) else None


def read_tokens(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            t = sanitize(line)
            if t:
                out.append(t)
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None,
                    help="email_library dir (default: ../assets/email_library)")
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    assets = args.dir or os.path.join(os.path.dirname(here), "assets", "email_library")
    src = os.path.join(assets, "sources")
    if not os.path.isdir(src):
        sys.exit(f"no sources dir: {src}")

    groups = {
        "names_first.txt": sorted(glob.glob(os.path.join(src, "names_first_*.txt"))),
        "names_last.txt": sorted(glob.glob(os.path.join(src, "names_last_*.txt"))),
        "roles.txt": sorted(glob.glob(os.path.join(src, "roles_*.txt"))),
    }
    meta = {"vocab": {}, "sha256": {}}
    for out_name, files in groups.items():
        seen, order = set(), []
        for p in files:
            for t in read_tokens(p):
                if t not in seen:
                    seen.add(t)
                    order.append(t)
        with open(os.path.join(assets, out_name), "w", encoding="utf-8") as f:
            for t in order:
                f.write(t + "\n")
        meta["vocab"][out_name] = len(order)
        meta["sha256"][out_name] = sha256_file(os.path.join(assets, out_name))
        print(f"{out_name}: {len(order)} from {len(files)} source files")

    meta["notes"] = ("Compose local parts from these; the space is names_first x "
                     "names_last x patterns. Rebuild with build_prefix_vocab.py.")
    with open(os.path.join(assets, "prefix_vocab.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"-> {assets}/prefix_vocab.json")


if __name__ == "__main__":
    main()