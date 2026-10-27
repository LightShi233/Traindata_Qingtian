#!/usr/bin/env python3
"""Weld an email domain/prefix library into the skill.

Builds ``traindata/assets/email_library/`` from:

- the generated part files in ``parts/`` (one token per line), and
- a disposable/toxic domain list (default ``/home/light/下载/disposabledomains.txt``).

Design rules (do not "fix" these):

- **Everything goes into one pool.** Disposable, toxic and self-invented domains
  are all usable addresses. There is no quality tier and no exclusion.
- The source list is kept *only* as a classification label
  (``known_blocklist.txt``) so the audit can later check that blocklist-style and
  corporate-style domains appear proportionally in clean and violating contexts.
  It must never be used to refuse an address.

Outputs (all inside ``traindata/assets/email_library/``):

    known_blocklist.txt   cleaned domains from the disposable/toxic source
    synth_domains.txt     cleaned domains generated for the skill
    freemail.txt          normal free-mail providers (gmail, hotmail, …)
    abuse_contacts.txt    abuse@<domain> reporting contacts
    prefixes.txt          cleaned local-part prefixes
    email_library.json    metadata + sha256 so drift is detectable

The four domain/contact groups are tiers used for *weighted scenario sampling*
(see ``sample_emails.py``); they are all usable and never used to exclude.

Usage
-----
    python3 traindata/scripts/build_email_library.py \
        --parts traindata/assets/email_library/parts \
        --disposable /home/light/下载/disposabledomains.txt \
        --freemail traindata/assets/email_library/sources/freemail.txt \
        --abuse traindata/assets/email_library/sources/freemail_abuse.txt \
        --out traindata/assets/email_library
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys

DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# Local part: letters, digits and . _ - ; must start and end alphanumeric.
PREFIX_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$")
MAX_DOMAIN_LEN = 253


def clean_domain(raw):
    d = raw.strip().strip(".").lower()
    if not d or d.startswith("#") or " " in d:
        return None
    if len(d) > MAX_DOMAIN_LEN or not DOMAIN_RE.match(d):
        return None
    return d


def clean_prefix(raw):
    p = raw.strip().lower()
    if not p or p.startswith("#") or " " in p:
        return None
    if not PREFIX_RE.match(p) or len(p) > 64:
        return None
    return p


def read_lines(path, keep):
    seen = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            item = keep(line)
            if item:
                seen.append(item)
    return seen


def dedupe_preserve(items):
    out, seen = [], set()
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_lines(path, items):
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(it + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parts", default=None, help="dir with *_prefixes_*.txt / *_domains_*.txt")
    ap.add_argument("--disposable", default="/home/light/下载/disposabledomains.txt")
    ap.add_argument("--freemail", default=None, help="normal free-mail domain list (one per line)")
    ap.add_argument("--abuse", default=None, help="two-column 'domain abuse@domain' reporting list")
    ap.add_argument("--out", default=None, help="output dir (default: beside this script's ../assets/email_library)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    skill = os.path.dirname(here)
    assets = os.path.join(skill, "assets", "email_library")
    parts_dir = args.parts or os.path.join(assets, "parts")
    out_dir = args.out or assets
    freemail_src = args.freemail or os.path.join(assets, "sources", "freemail.txt")
    abuse_src = args.abuse or os.path.join(assets, "sources", "freemail_abuse.txt")
    os.makedirs(out_dir, exist_ok=True)

    prefix_files = sorted(glob.glob(os.path.join(parts_dir, "*prefixes*.txt")))
    domain_files = sorted(glob.glob(os.path.join(parts_dir, "*domains*.txt")))

    prefixes = []
    for p in prefix_files:
        prefixes += read_lines(p, clean_prefix)
    if not prefixes and os.path.exists(os.path.join(out_dir, "prefixes.txt")):
        prefixes = read_lines(os.path.join(out_dir, "prefixes.txt"), clean_prefix)
    prefixes = dedupe_preserve(prefixes)
    if not prefixes:
        sys.exit(f"没有前缀：{parts_dir}/*prefixes*.txt 或 {out_dir}/prefixes.txt")

    synth = []
    for p in domain_files:
        synth += read_lines(p, clean_domain)
    if not synth and os.path.exists(os.path.join(out_dir, "synth_domains.txt")):
        synth = read_lines(os.path.join(out_dir, "synth_domains.txt"), clean_domain)
    synth = dedupe_preserve(synth)

    blocklist = []
    dropped = 0
    if os.path.exists(args.disposable):
        raw = read_lines(args.disposable, lambda l: l.rstrip("\n"))
        for r in raw:
            d = clean_domain(r)
            if d:
                blocklist.append(d)
            else:
                dropped += 1
        blocklist = dedupe_preserve(blocklist)
    else:
        print(f"[warn] disposable list not found: {args.disposable}; pool will be synth-only",
              file=sys.stderr)

    # The pool is the union. Keep the sources contiguous so each tier can still be
    # used as a *classification label* by the audit.
    synth_only = [d for d in synth if d not in set(blocklist)]

    freemail = []
    if os.path.exists(freemail_src):
        freemail = dedupe_preserve(read_lines(freemail_src, clean_domain))
    else:
        print(f"[warn] freemail list not found: {freemail_src}", file=sys.stderr)

    abuse_contacts = []
    if os.path.exists(abuse_src):
        for line in read_lines(abuse_src, lambda l: l.rstrip("\n")):
            parts = line.split()
            if not parts:
                continue
            addr = parts[-1].strip().lower()
            local, _, dom = addr.partition("@")
            if local and clean_domain(dom) and PREFIX_RE.match(local):
                abuse_contacts.append(f"{local}@{dom}")
        abuse_contacts = dedupe_preserve(abuse_contacts)
    else:
        print(f"[warn] abuse list not found: {abuse_src}", file=sys.stderr)

    write_lines(os.path.join(out_dir, "prefixes.txt"), prefixes)
    write_lines(os.path.join(out_dir, "known_blocklist.txt"), blocklist)
    write_lines(os.path.join(out_dir, "synth_domains.txt"), synth_only)
    write_lines(os.path.join(out_dir, "freemail.txt"), freemail)
    write_lines(os.path.join(out_dir, "abuse_contacts.txt"), abuse_contacts)

    meta = {
        "version": 2,
        "notes": "Tiered but fully usable pool: freemail (normal providers), "
                 "synth_domains (invented corporate), known_blocklist (disposable/toxic) "
                 "and abuse_contacts (reporting). Tiers drive weighted scenario "
                 "sampling and audit parity only; none of them is an exclusion list.",
        "prefixes": len(prefixes),
        "domains_total": len(blocklist) + len(synth_only) + len(freemail),
        "domains_known_blocklist": len(blocklist),
        "domains_synth": len(synth_only),
        "domains_freemail": len(freemail),
        "abuse_contacts": len(abuse_contacts),
        "disposable_source": args.disposable,
        "disposable_lines_dropped": dropped,
        "sha256": {},
    }
    for name in ("prefixes.txt", "known_blocklist.txt", "synth_domains.txt",
                 "freemail.txt", "abuse_contacts.txt"):
        meta["sha256"][name] = sha256_file(os.path.join(out_dir, name))
    with open(os.path.join(out_dir, "email_library.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"prefixes: {len(prefixes)}")
    print(f"domains pool: {meta['domains_total']} "
          f"({len(blocklist)} disposable + {len(synth_only)} synth + {len(freemail)} freemail)")
    print(f"abuse contacts: {len(abuse_contacts)}")
    if dropped:
        print(f"malformed lines dropped: {dropped}")
    print(f"-> {out_dir}")


if __name__ == "__main__":
    main()