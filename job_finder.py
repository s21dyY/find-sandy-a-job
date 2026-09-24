#!/usr/bin/env python3
"""
Job finder: pulls jobs from every board in companies.yaml (Greenhouse, Lever,
Ashby, Workable, Workday, iCIMS) plus the SimplifyJobs new-grad list, filters
by title, scores each new one against your resume with Claude, prepares a
resume for good fits, and sends matches to Discord.

  python job_finder.py
"""
import os
import sys
import time

from dotenv import load_dotenv

from jobfinder.alerts import notify
from jobfinder.boards import collect_jobs
from jobfinder.common import (MATCHES_FILE, ROOT, SEEN_FILE, TRACKER_FILE,
                              job_key, load_config, load_json, save_json)
from jobfinder.screening import passes_filters, score_job
from jobfinder.tailor import parse_resume
from jobfinder.tracker import job_id, prepare_resume, record_in_tracker

load_dotenv(ROOT / ".env")  # reads keys from .env locally; no-op on GitHub Actions


def main():
    cfg = load_config()
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

    max_age = cfg.get("max_age_days", 30) * 86400
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
    min_score = cfg.get("min_score", 70)
    tailor_min = cfg.get("tailor_min_score", 52)
    client = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        from anthropic import Anthropic
        client = Anthropic()
    else:
        print("No ANTHROPIC_API_KEY set: skipping AI scoring, alerting on keyword matches.")

    resume_data = parse_resume(resume)
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
        match = {k: v for k, v in job.items() if k != "description"} | result
        if score is None:  # keyword-only mode
            matches.append(match)
            continue
        if result.get("level_fit") == "too_senior":
            continue
        # Resumes and alerts have separate thresholds (tailor_min_score, min_score).
        if score >= tailor_min and job_id(job) not in tracked:
            match |= prepare_resume(client, cfg, resume_data, job, score)
            record_in_tracker(tracker, job, match, now)
            tracked.add(job_id(job))
        if score >= min_score:
            matches.append(match)

    leftover = len(candidates) - min(len(candidates), limit)
    if leftover:
        print(f"{leftover} jobs left to score; they'll be handled next run.")

    matches.sort(key=lambda m: m.get("score") or 0, reverse=True)
    notify(matches)

    # Forget jobs older than 180 days so the file stays small.
    cutoff = now - 180 * 86400
    seen = {k: v for k, v in seen.items() if v.get("t", now) > cutoff}
    save_json(SEEN_FILE, seen)
    history = load_json(MATCHES_FILE, []) + [m | {"found_at": int(now)} for m in matches]
    save_json(MATCHES_FILE, history[-500:])
    save_json(TRACKER_FILE, tracker)


if __name__ == "__main__":
    main()
