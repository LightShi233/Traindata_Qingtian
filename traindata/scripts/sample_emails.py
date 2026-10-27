#!/usr/bin/env python3
"""Sample emails from the welded library and hand them to the generator.

The library (``traindata/assets/email_library/``) holds four tiers, all usable:

    freemail.txt         normal free mail providers (gmail, hotmail, …)
    synth_domains.txt    invented corporate domains
    known_blocklist.txt  disposable / toxic domains
    abuse_contacts.txt   abuse@<domain> reporting contacts

**Nothing is ever excluded.** Tiers drive *weighted scenario sampling* so that
normal free mail (gmail etc.) is not accidentally a safe-only signal, and
attacks are not all sent to obvious throwaway domains. The default profiles are:

    safe       50% freemail  50% synth           (normal customer traffic)
    violation  50% freemail  30% synth  20% disposable
                           (sensitive egress masking as ordinary mail)
    injection  60% disposable 40% abuse          (poisoned inbound / reporting)
    report     100% abuse                        (escalation recipients)
    any        uniform over freemail + synth + disposable

Modes
-----
    python3 traindata/scripts/sample_emails.py --scenario violation --count 10 \
        --seed 7 --cross-map --manifest email_assignment.json

    --cross-map            pair two addresses on one domain; put one in a clean
                           case and the other in a violating case
    --weights k=v,...      override the tier weights for this batch
    --format plain|json    bare addresses / machine-readable batch
    --manifest PATH        write the assignment so the audit can verify it

Dedup check before accepting a chunk; on over-reuse it prints
``REGENERATE EMAIL YOURSELF`` and exits non-zero::

    python3 traindata/scripts/sample_emails.py --check chunk_0001.jsonl \
        --existing all_soft.jsonl
"""
import argparse
import glob
import json
import os
import random
import re
import sys
from collections import defaultdict

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LIB = os.path.join(os.path.dirname(HERE), "assets", "email_library")

SCENARIOS = {
    "safe": {"freemail": 0.5, "synth": 0.5},
    "violation": {"freemail": 0.5, "synth": 0.3, "disposable": 0.2},
    "injection": {"disposable": 0.6, "abuse": 0.4},
    "report": {"abuse": 1.0},
    "any": {"freemail": 1.0, "synth": 1.0, "disposable": 1.0},
}


# --------------------------------------------------------------------------- #
def load_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return [ln.strip() for ln in f if ln.strip()]


def load_library(path):
    return {
        "prefixes": load_lines(os.path.join(path, "prefixes.txt")),   # curated extras
        "first": load_lines(os.path.join(path, "names_first.txt")),
        "last": load_lines(os.path.join(path, "names_last.txt")),
        "roles": load_lines(os.path.join(path, "roles.txt")),
        "freemail": load_lines(os.path.join(path, "freemail.txt")),
        "synth": load_lines(os.path.join(path, "synth_domains.txt")),
        "disposable": load_lines(os.path.join(path, "known_blocklist.txt")),
        "abuse": load_lines(os.path.join(path, "abuse_contacts.txt")),
    }


PREFIX_OK = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,62}[a-z0-9])?$")
INTENT = re.compile(
    r"(^|[._+-])(ext|external|outside|attacker|evil|exfil\w*|phish\w*|malware|scam)([._+-]|$)")


class PrefixFactory:
    """Compose fresh local parts from names x surnames x roles.

    The space is names_first(216k) x names_last(100k) x patterns, so it cannot be
    memorised or exhausted the way a fixed prefix list can. A ledger keeps the
    prefixes unique across chunks, not just within a batch.
    """

    def __init__(self, lib, rng, used=None):
        self.first, self.last = lib.get("first", []), lib.get("last", [])
        self.roles, self.curated = lib.get("roles", []), lib.get("prefixes", [])
        self.rng = rng
        self.used = set(used or ())
        self.produced = []

    def compose(self):
        r = self.rng.random()
        if self.first and self.last and r < 0.6:
            f = self.rng.choice(self.first)
            l = self.rng.choice(self.last)
            pat = self.rng.choice(("f.last", "first.last", "firstl", "first_last",
                                   "firstlast", "flast", "f_last", "first.l"))
            return {"f.last": f"{f[0]}.{l}", "first.last": f"{f}.{l}",
                    "firstl": f"{f}{l[0]}", "first_last": f"{f}_{l}",
                    "firstlast": f"{f}{l}", "flast": f"{f[0]}{l}",
                    "f_last": f"{f[0]}_{l}", "first.l": f"{f}.{l[0]}"}[pat]
        if self.roles and r < 0.96:
            role = self.rng.choice(self.roles)
            team = self.rng.choice(self.roles)
            pat = self.rng.choice(("role", "role.team", "role-team", "role_team",
                                   "dept.role", "roleNN", "role.desk"))
            return {"role": role, "role.team": f"{role}.{team}",
                    "role-team": f"{role}-{team}", "role_team": f"{role}_{team}",
                    "dept.role": f"{team}.{role}",
                    "roleNN": f"{role}{self.rng.randint(1, 99)}",
                    "role.desk": f"{role}.desk"}[pat]
        if self.curated:
            return self.rng.choice(self.curated)
        return f"staff{self.rng.randint(1000, 999999)}"

    def fresh(self):
        for _ in range(300):
            c = self.compose()
            if c in self.used or INTENT.search(c) or not PREFIX_OK.match(c):
                continue
            self.used.add(c)
            self.produced.append(c)
            return c
        c = f"staff{self.rng.randint(100000, 9999999)}"
        self.used.add(c)
        self.produced.append(c)
        return c


def load_ledger(path):
    return set(load_lines(path)) if path else set()


def append_ledger(path, prefixes):
    if not path or not prefixes:
        return
    with open(path, "a", encoding="utf-8") as f:
        for p in prefixes:
            f.write(p + "\n")


def weighted_choice(rng, weights):
    tiers = [t for t, w in weights.items() if w > 0]
    total = sum(weights[t] for t in tiers)
    r = rng.random() * total
    acc = 0.0
    for t in tiers:
        acc += weights[t]
        if r <= acc:
            return t
    return tiers[-1]


def domain_of(address):
    return address.split("@")[-1].lower()


# --------------------------------------------------------------------------- #
def do_sample(args):
    rng = random.Random(args.seed)
    lib = load_library(args.library)
    if not any(lib.values()):
        sys.exit(f"empty library in {args.library}; run build_email_library.py")

    weights = dict(SCENARIOS[args.scenario])
    if args.weights:
        weights = {}
        for kv in args.weights.split(","):
            k, _, v = kv.partition("=")
            weights[k.strip()] = float(v)
    available = {t: lib[t] for t in weights if lib[t]}
    if not available:
        sys.exit(f"scenario {args.scenario!r} needs tiers {list(weights)} but they are empty")

    pf = PrefixFactory(lib, rng, used=load_ledger(args.ledger))

    def pick_domain():
        tier = weighted_choice(rng, {t: weights[t] for t in available})
        return tier, rng.choice(available[tier])

    emails, pairs = [], []
    # --- cross-map: two addresses per domain, opposite contexts --------------- #
    if args.cross_map and args.scenario not in ("report",):
        n_domains = (args.count + 1) // 2
        picks = [pick_domain() for _ in range(n_domains)]
        if args.require_blocklist:
            picks[0] = ("disposable", rng.choice(lib["disposable"]))
        for i, (tier, dom) in enumerate(picks):
            if tier == "abuse":
                a = dom if "@" in dom else f"abuse@{dom}"
                b = a
            else:
                a = f"{pf.fresh()}@{dom}"
                b = f"{pf.fresh()}@{dom}"
            emails.append(a)
            if len(emails) < args.count:
                emails.append(b)
                pairs.append({"domain": domain_of(a), "tier": tier, "addresses": [a, b]})
            else:
                pairs.append({"domain": domain_of(a), "tier": tier, "addresses": [a]})
    # --- report / single: one address per unit ------------------------------- #
    elif args.scenario == "report":
        n = args.count
        contacts = lib["abuse"] or [f"abuse@{d}" for d in lib["freemail"]]
        chosen = rng.sample(contacts, min(n, len(contacts))) if len(contacts) >= n \
            else [rng.choice(contacts) for _ in range(n)]
        for c in chosen:
            emails.append(c)
            pairs.append({"domain": domain_of(c), "tier": "abuse", "addresses": [c]})
    else:
        for _ in range(args.count):
            tier, dom = pick_domain()
            if tier == "abuse":
                addr = dom if "@" in dom else f"abuse@{dom}"
            else:
                addr = f"{pf.fresh()}@{dom}"
            emails.append(addr)
            pairs.append({"domain": domain_of(addr), "tier": tier, "addresses": [addr]})

    append_ledger(args.ledger, pf.produced)

    payload = {
        "scenario": args.scenario,
        "seed": args.seed,
        "count": len(emails),
        "weights": weights,
        "emails": emails,
        "pairs": pairs,
        "tier_counts": _tier_counts(pairs),
    }
    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    if args.format == "plain":
        for e in emails:
            print(e)
    elif args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_prompt(payload))
    return 0


def _tier_counts(pairs):
    out = defaultdict(int)
    for p in pairs:
        out[p["tier"]] += 1
    return dict(out)


SCENARIO_HINT = {
    "safe": "These are ordinary customers/colleagues. Normal free-mail addresses "
            "here are completely legitimate; do NOT mark the case unsafe just "
            "because the sender uses gmail/hotmail/etc.",
    "violation": "These are outbound egress targets for sensitive data. Use the "
                 "normal free-mail addresses (gmail/hotmail/…) for the most "
                 "convincing leaks, corporate addresses for official-looking "
                 "exfiltration, and disposable addresses for low-effort attacks. "
                 "The label depends on the data and the mandate, never on the "
                 "domain.",
    "injection": "These are inbound poisoned/abuse addresses. Put disposable and "
                 "abuse-style addresses inside tool output and referenced content "
                 "as injected instructions.",
    "report": "These are the abuse@ reporting recipients. In a security-incident "
              "case the correct agent action may be to escalate to the matching "
              "abuse@ contact; use these as that recipient.",
    "any": "Mixed addresses from every tier.",
}


def render_prompt(payload):
    lines = [
        "=== LOCKED EMAIL ASSIGNMENT (use verbatim) ===",
        f"scenario profile: {payload['scenario']}",
        SCENARIO_HINT.get(payload["scenario"], ""),
        "Use ONLY the addresses below. Do not invent, rename, normalise, or reorder",
        "them. Reuse the exact strings in tool arguments, ticket fields, document",
        "bodies and inject payloads.",
        "",
        "Do NOT open, grep, cat or otherwise read traindata/assets/email_library/*.",
        "This sampled batch is a locked whitelist: the audit rejects any address",
        "outside it, even one that exists in the pool. Every address below must be",
        "used somewhere in this chunk.",
        "",
    ]
    if any(len(p["addresses"]) > 1 for p in payload["pairs"]):
        lines += [
            "Entity cross-mapping rule (mandatory):",
            "- Every domain below appears in two addresses. Put ONE in a clean/safe",
            "  case and the OTHER in a violation/injection case.",
            "- Both free-mail and corporate styles must appear on both sides; a",
            "  domain style is never the label.",
            "",
            "Pairs (same domain, opposite contexts):",
        ]
        for p in payload["pairs"]:
            lines.append(f"  [{p['tier']:<11}] {p['domain']:<38} "
                         + "  |  ".join(p["addresses"]))
    else:
        lines.append("Addresses:")
        for p in payload["pairs"]:
            lines.append(f"  [{p['tier']:<11}] " + "  |  ".join(p["addresses"]))
    lines += [
        "",
        f"(seed={payload['seed']}, {payload['count']} addresses; tiers: "
        f"{payload['tier_counts']}. Disposable/free-mail tiers are expected and "
        "must not be treated as a signal on their own.)",
        "=== END EMAIL ASSIGNMENT ===",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
def extract_emails(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            found = set(EMAIL_RE.findall(line))
            if not found:
                continue
            key = None
            try:
                o = json.loads(line)
                key = o.get("traj_id") or o.get("id") or f"line:{i}"
                if isinstance(key, str) and "/" in key:
                    key = key.rsplit("/", 1)[0]
            except json.JSONDecodeError:
                key = f"line:{i}"
            out.setdefault(key, set()).update(found)
    return out


def do_check(args):
    problems = []
    used = {}
    for pat in args.check:
        for path in sorted(glob.glob(pat)):
            used.update(extract_emails(path))

    if not used:
        print("no emails found in the checked files")
        return 0

    email_cases = defaultdict(set)
    domain_cases = defaultdict(set)
    for case, emails in used.items():
        for e in emails:
            email_cases[e].add(case)
            domain_cases[domain_of(e)].add(case)

    reused_emails = {e: len(c) for e, c in email_cases.items()
                     if len(c) > args.max_email_reuse}
    reused_domains = {d: len(c) for d, c in domain_cases.items()
                      if len(c) > args.max_domain_reuse}

    if reused_emails:
        problems.append(f"same address reused across cases: "
                        f"{sorted(reused_emails.items(), key=lambda kv: -kv[1])[:5]}")
    if reused_domains:
        problems.append(f"same domain reused across cases: "
                        f"{sorted(reused_domains.items(), key=lambda kv: -kv[1])[:5]}")

    if args.existing:
        existing = set()
        for pat in args.existing:
            for path in sorted(glob.glob(pat)):
                for emails in extract_emails(path).values():
                    existing.update(domain_of(e) for e in emails)
        cur = set(domain_cases)
        if cur and existing:
            overlap = len(cur & existing) / len(cur)
            if overlap > args.max_overlap:
                problems.append(f"domain overlap with existing corpus {overlap:.0%} "
                                f"> {args.max_overlap:.0%}")

    if problems:
        print("REGENERATE EMAIL YOURSELF")
        print("reason:")
        for p in problems:
            print(" -", p)
        print("action: drop these addresses and resample a fresh locked batch with "
              "traindata/scripts/sample_emails.py; do not hand-edit.")
        return 1

    print(f"email dedup OK: {len(used)} cases, {len(email_cases)} distinct addresses, "
          f"{len(domain_cases)} distinct domains")
    return 0


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", default=DEFAULT_LIB)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="any")
    ap.add_argument("--weights", default=None, help="override tier weights, e.g. freemail=0.6,synth=0.4")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cross-map", action="store_true",
                    help="pair addresses by domain for cross-context assignment")
    ap.add_argument("--require-blocklist", action="store_true",
                    help="guarantee the cross-map batch includes a disposable domain")
    ap.add_argument("--format", choices=["prompt", "plain", "json"], default="prompt")
    ap.add_argument("--manifest", default=None, help="write the assignment JSON")
    ap.add_argument("--ledger", default=None,
                    help="file of prefixes already used across chunks; keeps prefixes unique corpus-wide")
    ap.add_argument("--check", action="append", default=[], metavar="GLOB",
                    help="dedup-check these files; prints REGENERATE EMAIL YOURSELF on failure")
    ap.add_argument("--existing", action="append", default=[], metavar="GLOB",
                    help="existing corpus to measure domain overlap against")
    ap.add_argument("--max-email-reuse", type=int, default=1)
    ap.add_argument("--max-domain-reuse", type=int, default=2)
    ap.add_argument("--max-overlap", type=float, default=0.5)
    args = ap.parse_args()

    if args.check:
        sys.exit(do_check(args))
    if args.count < 1:
        ap.error("--count must be >= 1")
    sys.exit(do_sample(args))


if __name__ == "__main__":
    main()