"""Picks the resume for each match (original or tailored) and logs it in tracker.json."""
import re
from datetime import datetime

import requests

from .common import HEADERS, RESUME_DIR, ROOT, job_key, strip_html
from .tailor import render_pdf, tailor_resume


def job_id(job):
    return f"{job['source']}-{job['id']}" if job.get("id") else job_key(job)


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


def fetch_page_text(url):
    # Simplify jobs have no description; try the posting page itself.
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return strip_html(r.text)
    except Exception as e:
        print(f"  ! couldn't fetch description from {url}: {e}")
        return ""


def file_prefix(cfg, resume_data):
    """resume_file_prefix from config, else built from the resume's name line
    ("SANDY YANG" -> "Sandy_YANG")."""
    if cfg.get("resume_file_prefix"):
        return safe_name(str(cfg["resume_file_prefix"]))
    words = resume_data["name"].split()
    if not words:
        return "Resume"
    return safe_name("_".join([words[0].title()] + [w.upper() for w in words[1:]]))


def original_pdf(cfg, resume_data):
    path = RESUME_DIR / f"{file_prefix(cfg, resume_data)}_Resume.pdf"
    resume_file = ROOT / cfg.get("resume_file", "resume.txt")
    if not path.exists() or path.stat().st_mtime < resume_file.stat().st_mtime:
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

    prefix = file_prefix(cfg, resume_data)
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
