"""
Finds which job-board system each company uses and prints companies.yaml lines.

Probes Greenhouse, Lever, Ashby and Workable with a few slug guesses per name
("dbt Labs" -> dbtlabs, dbt-labs, dbt_labs, dbt, ...).

Usage:
  python discovery.py companies.txt           # one company name per line
  python discovery.py "dbt Labs" Hex "Sigma Computing"
  python discovery.py --check-config          # re-test boards already in companies.yaml

Results are printed and saved to data/discovered.yaml; paste the lines you want
into companies.yaml (this script never edits it, so your comments stay safe).

Workday and iCIMS URLs contain server numbers and site names that can't be
guessed, so companies not found here are listed at the end with search links
for finding their careers page by hand.
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote_plus

import requests

from jobfinder.common import DISCOVERED_FILE, HEADERS, load_config, norm

SUFFIXES = {"inc", "incorporated", "llc", "ltd", "corp", "corporation", "co",
            "company", "technologies", "technology", "labs", "group", "holdings", "hq"}
# Boards whose address can be guessed from the company name. Workday and iCIMS
# URLs can't, so those are added to companies.yaml by hand.
PLATFORMS = ["greenhouse", "lever", "ashby", "workable"]


def slug_candidates(name):
    """Most likely slugs first. Returns [(slug, is_first_word_guess)]."""
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    core = [w for w in words if w not in SUFFIXES] or words
    cands = []
    for ws in (words, core):
        cands += ["".join(ws), "-".join(ws), "_".join(ws)]
    cands += ["".join(core) + "hq", "".join(core) + "inc"]
    out, seen = [], set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append((c, False))
    if len(core) > 1 and core[0] not in seen and len(core[0]) > 2:
        out.append((core[0], True))  # risky: could be a different company
    return out


# ---------------------------------------------------------------- probes
def _get(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def probe(platform, slug):
    """Returns {"jobs": n, "board_name": str|None} if the board exists, else None."""
    if platform == "greenhouse":
        board = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if not isinstance(board, dict) or "name" not in board:
            return None
        jobs = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs") or {}
        return {"jobs": len(jobs.get("jobs", [])), "board_name": board.get("name")}
    if platform == "lever":
        data = _get(f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
        return {"jobs": len(data), "board_name": None} if isinstance(data, list) else None
    if platform == "ashby":
        data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if not isinstance(data, dict) or "jobs" not in data:
            return None
        return {"jobs": len(data["jobs"]), "board_name": None}
    if platform == "workable":
        data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
        if not isinstance(data, dict) or "jobs" not in data:
            return None
        return {"jobs": len(data["jobs"]), "board_name": data.get("name")}
    raise ValueError(platform)


def discover(name):
    """Checks every platform; returns a list of hits for this company."""
    hits = []
    for platform in PLATFORMS:
        for slug, first_word in slug_candidates(name):
            res = probe(platform, slug)
            if res:
                warn = []
                if first_word:
                    warn.append("matched on first word only")
                bn = res["board_name"]
                if bn and norm(name) not in norm(bn) and norm(bn) not in norm(name):
                    warn.append(f"board is named '{bn}'")
                if res["jobs"] == 0:
                    warn.append("0 open jobs")
                hits.append({"company": name, "platform": platform, "slug": slug,
                             "jobs": res["jobs"], "warn": warn})
                break  # first working slug on this platform is enough
    return hits


# ---------------------------------------------------------------- config
def existing_tokens(cfg):
    comps = cfg["companies"]
    return {p: set(comps.get(p) or []) for p in PLATFORMS}


def check_config(cfg):
    comps = cfg["companies"]
    tasks = [(p, t) for p in PLATFORMS for t in dict.fromkeys(comps.get(p) or [])]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda pt: (pt, probe(*pt)), tasks))
    print("Checking boards in companies.yaml...\n")
    dead = []
    for (platform, token), res in results:
        if res is None:
            dead.append((platform, token))
            print(f"  DEAD   {platform:<10} {token}")
        elif res["jobs"] == 0:
            print(f"  EMPTY  {platform:<10} {token}  (board exists, 0 jobs)")
    for platform in PLATFORMS:
        items = comps.get(platform) or []
        for dup in sorted({t for t in items if items.count(t) > 1}):
            print(f"  DUPE   {platform:<10} {dup}  (listed more than once)")
    print(f"\n{len(tasks)} tokens checked, {len(dead)} dead. Remove dead ones from companies.yaml.")


# ---------------------------------------------------------------- main
def read_names(args):
    names = []
    for a in args:
        p = Path(a)
        if p.suffix == ".txt" and p.exists():
            for line in p.read_text().splitlines():
                line = line.split("#")[0].strip()
                if line:
                    names.append(line)
        else:
            names.append(a)
    return list(dict.fromkeys(names))


def main():
    args = sys.argv[1:]
    cfg = load_config()
    if not args:
        sys.exit(__doc__)
    if args == ["--check-config"]:
        check_config(cfg)
        return

    names = read_names(args)
    have = existing_tokens(cfg)
    print(f"Probing {len(names)} companies on {', '.join(PLATFORMS)}...\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        all_hits = dict(zip(names, pool.map(discover, names)))

    chosen = {p: [] for p in PLATFORMS}
    extra_lines, already, missing = [], [], []
    for name in names:
        hits = all_hits[name]
        if not hits:
            missing.append(name)
            continue
        # Best board = most open jobs; others are usually old or unused.
        hits.sort(key=lambda h: h["jobs"], reverse=True)
        best = hits[0]
        if best["slug"] in have[best["platform"]]:
            already.append(f"{name} ({best['platform']}: {best['slug']})")
            continue
        chosen[best["platform"]].append(best)
        for h in hits[1:]:
            extra_lines.append(f"# {name}: also has a {h['platform']} board '{h['slug']}' "
                               f"with {h['jobs']} jobs")

    lines = ["# Paste the entries you want under the matching heading in companies.yaml."]
    for platform in PLATFORMS:
        if not chosen[platform]:
            continue
        lines.append(f"{platform}:")
        for h in chosen[platform]:
            note = f"{h['company']}, {h['jobs']} jobs"
            if h["warn"]:
                note += "  CHECK: " + "; ".join(h["warn"])
            lines.append(f"  - {h['slug']:<24} # {note}")
    snippet = "\n".join(lines + ([""] + extra_lines if extra_lines else []))

    print(snippet)
    DISCOVERED_FILE.parent.mkdir(exist_ok=True)
    DISCOVERED_FILE.write_text(snippet + "\n")
    found = sum(len(v) for v in chosen.values())
    print(f"\nFound {found} new boards (saved to data/discovered.yaml).")
    if already:
        print(f"\nAlready in companies.yaml: {', '.join(already)}")
    if missing:
        print(f"\nNot found on {', '.join(PLATFORMS)} ({len(missing)}). These probably use "
              "Workday, iCIMS or their own site. Search for their careers page:")
        for name in missing:
            print(f"  {name:<28} https://www.google.com/search?q="
                  f"{quote_plus(name + ' careers (myworkdayjobs OR icims)')}")


if __name__ == "__main__":
    main()