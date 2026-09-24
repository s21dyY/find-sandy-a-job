#!/usr/bin/env python3
"""
Job finder: pulls jobs from Greenhouse / Lever / Ashby boards and the
SimplifyJobs new-grad list, filters by title, scores each new one against
your resume with Claude, and sends good matches to Discord.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from resume_tailor import parse_resume, render_pdf, tailor_resume

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")  # reads keys from .env locally; no-op on GitHub Actions
SEEN_FILE = ROOT / "seen_jobs.json"
MATCHES_FILE = ROOT / "matches.json"
TRACKER_FILE = ROOT / "tracker.json"
RESUME_DIR = ROOT / "resume"
HEADERS = {"User-Agent": "personal-job-finder/1.0"}
SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
                "dev/.github/scripts/listings.json")
SCORE = 70
POST_DAYS = 2

# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def strip_html(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def iso_to_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def get_json(url, params=None):
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 404:
            print(f"  ! 404 for {url} (company token is probably wrong)")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ! error fetching {url}: {e}")
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def job_key(job):
    # Same company + same title = same role (even if it shows up in two sources
    # or in several locations), so you only get alerted once.
    return f"{norm(job['company'])}|{norm(job['title'])}"


# ---------------------------------------------------------------- fetchers
def fetch_greenhouse(token):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                    {"content": "true"})
    if not data:
        return []
    return [{
        "id": j.get("id"),
        "company": token,
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "description": strip_html(j.get("content", "")),
        "posted": iso_to_ts(j.get("first_published") or j.get("updated_at") or ""),
        "employment": "",
        "source": "greenhouse",
    } for j in data.get("jobs", [])]


def fetch_lever(token):
    data = get_json(f"https://api.lever.co/v0/postings/{token}", {"mode": "json"})
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        lists = " ".join(f"{l.get('text', '')}: {strip_html(l.get('content', ''))}"
                         for l in (j.get("lists") or []))
        created = j.get("createdAt")
        jobs.append({
            "id": j.get("id"),
            "company": token,
            "title": j.get("text", ""),
            "location": cats.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "description": f"{j.get('descriptionPlain', '')} {lists}".strip(),
            "posted": created / 1000 if created else None,
            "employment": cats.get("commitment", ""),
            "source": "lever",
        })
    return jobs


def fetch_ashby(token):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    if not data:
        return []
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        jobs.append({
            "id": j.get("id"),
            "company": token,
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            "posted": iso_to_ts(j.get("publishedAt") or ""),
            "employment": j.get("employmentType", ""),
            "source": "ashby",
        })
    return jobs


def fetch_simplify():
    data = get_json(SIMPLIFY_URL)
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        if not j.get("active") or not j.get("is_visible", True):
            continue
        sponsor = j.get("sponsorship", "")
        jobs.append({
            "id": j.get("id"),
            "company": j.get("company_name", ""),
            "title": j.get("title", ""),
            "location": ", ".join(j.get("locations") or []),
            "url": j.get("url", ""),
            # Simplify doesn't include the description, so Claude scores these
            # from title/company/location plus the sponsorship note.
            "description": f"(Description not available.) Sponsorship: {sponsor}",
            "posted": j.get("date_posted"),
            "employment": "",
            "source": "simplify",
        })
    return jobs


def collect_jobs(cfg):
    jobs = []
    companies = cfg.get("companies", {})
    for source, fetch in (("greenhouse", fetch_greenhouse),
                          ("lever", fetch_lever),
                          ("ashby", fetch_ashby)):
        for token in companies.get(source) or []:
            found = fetch(token)
            print(f"  {source:<10} {token:<20} {len(found)} jobs")
            jobs += found
    if cfg.get("use_simplify_list", True):
        found = fetch_simplify()
        print(f"  simplify   new-grad list        {len(found)} active jobs")
        jobs += found
    return jobs


# ---------------------------------------------------------------- filtering
def word_in(word, text):
    return re.search(rf"\b{re.escape(word.lower())}\b", text) is not None


def passes_filters(job, cfg):
    title = job["title"].lower()
    if not any(w.lower() in title for w in cfg["title_include"]):
        return False
    if any(word_in(w, title) for w in cfg["title_exclude"]):
        return False
    emp = job.get("employment", "").lower()
    if emp and any(x in emp for x in ("intern", "contract", "part", "temporary")):
        return False
    locs = cfg.get("locations") or []
    if locs and job["location"]:
        loc = job["location"].lower()
        if not any(word_in(l, loc) for l in locs):
            return False
    return True


# ---------------------------------------------------------------- scoring
SCORING_PROMPT = """You are screening job postings for a job seeker.

CANDIDATE NOTES:
{notes}

CANDIDATE RESUME:
{resume}

JOB POSTING:
Company: {company}
Title: {title}
Location: {location}
Description: {description}

Judge how well this job fits the candidate. Be realistic:
- Use the candidate notes to decide how much experience is too much. If the notes
  don't say, treat roles requiring 3+ years of full-time experience as a poor fit.
- "New grad", "entry level", "associate", "early career", or 0-2 years is a good sign.
- Weigh skill overlap (SQL, Python, dbt, Spark, BI tools, cloud, etc.).
- Flag dealbreakers such as no visa sponsorship (if the candidate needs it),
  citizenship/clearance requirements, or a start date before the candidate graduates.

Respond with ONLY a JSON object, no other text:
{{"score": <0-100>, "level_fit": "new_grad" | "entry" | "too_senior" | "unclear",
  "reason": "<one or two sentences>", "dealbreakers": ["<short item>", ...]}}"""


def score_job(client, model, resume, notes, job):
    prompt = SCORING_PROMPT.format(
        notes=notes, resume=resume, company=job["company"], title=job["title"],
        location=job["location"], description=job["description"][:6000])
    msg = client.messages.create(model=model, max_tokens=400,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"no JSON in response: {text[:200]}")
    return json.loads(match.group(0))


# ---------------------------------------------------------------- resumes
def job_id(job):
    return f"{job['source']}-{job['id']}" if job.get("id") else job_key(job)


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def fetch_page_text(url):
    # Simplify jobs have no description; try the posting page itself.
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        return strip_html(page)
    except Exception as e:
        print(f"  ! couldn't fetch description from {url}: {e}")
        return ""


def original_pdf(cfg, resume_data):
    path = RESUME_DIR / f"{cfg.get('resume_file_prefix', 'Resume')}_Resume.pdf"
    if not path.exists() or path.stat().st_mtime < (ROOT / cfg.get("resume_file", "resume.txt")).stat().st_mtime:
        render_pdf(resume_data, path)
    return path


def prepare_resume(client, cfg, resume_data, job, score):
    """Picks the original resume (high scores) or makes a tailored one.
    Returns fields to add to the match."""
    original = original_pdf(cfg, resume_data)
    use_original = {"resume": str(original.relative_to(ROOT)), "resume_type": "original"}
    if score >= cfg.get("original_resume_min_score", 80):
        return use_original

    if job["source"] == "simplify":
        job = job | {"description": fetch_page_text(job["url"])}
    if len(job["description"]) < 400:
        print(f"  ! no usable description for {job['title']} @ {job['company']}; using original resume")
        return use_original | {"resume_type": "original (no job description to tailor to)"}

    try:
        tailored, info = tailor_resume(client, cfg.get("tailor_model"), resume_data, job)
    except Exception as e:
        print(f"  ! tailoring failed for {job['title']} @ {job['company']}: {e}")
        return use_original | {"resume_type": "original (tailoring failed)"}

    prefix = cfg.get("resume_file_prefix", "Resume")
    path = RESUME_DIR / f"{prefix}_{safe_name(job['title'])}_{safe_name(job['company'])}.pdf"
    pages = render_pdf(tailored, path)
    if info["kept_original_sections"]:
        print(f"  ! kept original {', '.join(info['kept_original_sections'])} for "
              f"{job['title']} @ {job['company']} (tailored version failed checks)")
    if pages > 1:
        print(f"  ! {path.name} runs to {pages} pages")
    return {"resume": str(path.relative_to(ROOT)), "resume_type": "tailored",
            "missing_skills": info["missing_skills"], "resume_changes": info["changes"]}


def record_in_tracker(tracker, job, match, now):
    tracker.append({
        "job_id": job_id(job),
        "company": job["company"],
        "title": job["title"],
        "score": match.get("score"),
        "url": job["url"],
        "application_date": datetime.fromtimestamp(now).strftime("%Y-%m-%d"),
        "resume": match["resume"],
        "resume_type": match["resume_type"],
        "status": "to_apply",
    })


# ---------------------------------------------------------------- alerts
def format_match(m):
    score = m.get("score")
    head = f"**{score}/100**" if score is not None else "**(unscored)**"
    lines = [f"{head}  {m['title']} @ {m['company']}",
             f"📍 {m['location'] or 'n/a'}  ·  via {m['source']}",
             m.get("reason", "")]
    if m.get("dealbreakers"):
        lines.append("⚠️ " + "; ".join(m["dealbreakers"]))
    if m.get("resume"):
        lines.append(f"📄 {Path(m['resume']).name} ({m['resume_type']})")
    if m.get("missing_skills"):
        lines.append("🧩 Not on resume: " + ", ".join(m["missing_skills"]))
    lines.append(m["url"])
    return "\n".join(l for l in lines if l)


def notify(matches):
    if not matches:
        print("\nNo new matches this run.")
        return
    print(f"\n{len(matches)} new match(es):\n")
    for m in matches:
        print(format_match(m) + "\n")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    for m in matches:
        try:
            requests.post(webhook, json={"content": format_match(m)[:1990]}, timeout=15)
            time.sleep(1)  # stay under Discord rate limits
        except Exception as e:
            print(f"  ! Discord error: {e}")


# ---------------------------------------------------------------- main
def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    resume_path = ROOT / cfg.get("resume_file", "resume.txt")
    if not resume_path.exists():
        sys.exit(f"Resume not found at {resume_path}. Paste your resume text there.")
    resume = resume_path.read_text()
    notes = cfg.get("candidate_notes", "")
    seen = load_json(SEEN_FILE, {})
    now = time.time()

    print("Fetching jobs...")
    jobs = collect_jobs(cfg)
    print(f"Fetched {len(jobs)} jobs total.")

    max_age = cfg.get("max_age_days", POST_DAYS) * 86400
    candidates, keys = [], set()
    for job in jobs:
        if job.get("posted") and now - job["posted"] > max_age:
            continue
        if not passes_filters(job, cfg):
            continue
        key = job_key(job)
        if key in seen or key in keys:
            continue
        keys.add(key)
        candidates.append(job)
    candidates.sort(key=lambda j: j.get("posted") or 0, reverse=True)
    print(f"{len(candidates)} new jobs passed the title/location filters.")

    limit = cfg.get("max_llm_calls_per_run", 50)
    min_score = cfg.get("min_score", SCORE)
    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        from anthropic import Anthropic
        client = Anthropic()
    else:
        print("No ANTHROPIC_API_KEY set: skipping AI scoring, alerting on keyword matches.")

    resume_data = parse_resume(resume)
    tailor_min = cfg.get("tailor_min_score", 52)
    tracker = load_json(TRACKER_FILE, [])
    tracked = {t["job_id"] for t in tracker}

    matches = []
    for job in candidates[:limit]:
        if client:
            try:
                result = score_job(client, cfg.get("model"), resume, notes, job)
            except Exception as e:
                print(f"  ! scoring failed for {job['title']} @ {job['company']}: {e}")
                continue  # not marked seen, so it retries next run
        else:
            result = {"score": None, "level_fit": "unclear",
                      "reason": "Keyword match (no AI scoring).", "dealbreakers": []}

        seen[job_key(job)] = {"t": int(now), "score": result.get("score")}
        score = result.get("score")
        if score is None or (score >= min_score and result.get("level_fit") != "too_senior"):
            match = {k: v for k, v in job.items() if k != "description"} | result
            if (client and score is not None and score >= tailor_min
                    and job_id(job) not in tracked):
                match |= prepare_resume(client, cfg, resume_data, job, score)
                record_in_tracker(tracker, job, match, now)
                tracked.add(job_id(job))
            matches.append(match)

    leftover = len(candidates) - min(len(candidates), limit)
    if leftover:
        print(f"{leftover} jobs left to score; they'll be handled next run.")

    matches.sort(key=lambda m: m.get("score") or 0, reverse=True)
    notify(matches)

    # Forget jobs older than 180 days so the file stays small.
    cutoff = now - 180 * 86400
    seen = {k: v for k, v in seen.items() if v.get("t", now) > cutoff}
    SEEN_FILE.write_text(json.dumps(seen, indent=1))
    history = load_json(MATCHES_FILE, []) + [m | {"found_at": int(now)} for m in matches]
    MATCHES_FILE.write_text(json.dumps(history[-500:], indent=1))
    TRACKER_FILE.write_text(json.dumps(tracker, indent=1))


if __name__ == "__main__":
    main()