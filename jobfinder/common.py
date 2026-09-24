"""Paths, config loading, and small HTTP/text helpers shared by every module."""
import html
import json
import re
from datetime import datetime
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"            # run state; gitignored
RESUME_DIR = ROOT / "resume"        # generated PDFs; gitignored
CONFIG_FILE = ROOT / "config.yaml"
COMPANIES_FILE = ROOT / "companies.yaml"
SEEN_FILE = DATA_DIR / "seen_jobs.json"
MATCHES_FILE = DATA_DIR / "matches.json"
TRACKER_FILE = DATA_DIR / "tracker.json"
DISCOVERED_FILE = DATA_DIR / "discovered.yaml"

HEADERS = {"User-Agent": "personal-job-finder/1.0"}
PLATFORMS = ["greenhouse", "lever", "ashby", "workable", "workday", "icims"]


# ---------------------------------------------------------------- config
def load_config():
    """config.yaml settings, with companies.yaml merged in as cfg["companies"]."""
    cfg = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    companies = {}
    if COMPANIES_FILE.exists():
        companies = yaml.safe_load(COMPANIES_FILE.read_text()) or {}
    companies = companies.get("companies", companies)  # tolerate a "companies:" wrapper
    unknown = set(companies) - set(PLATFORMS)
    if unknown:
        print(f"  ! companies.yaml: unknown platform(s) {', '.join(sorted(unknown))} "
              f"(expected {', '.join(PLATFORMS)})")
    cfg["companies"] = {p: companies.get(p) or [] for p in PLATFORMS}
    return cfg


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


# ---------------------------------------------------------------- text
def strip_html(text):
    text = html.unescape(text or "")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def iso_to_ts(value):
    if not value:
        return None
    try:
        value = str(value).replace("Z", "+00:00")
        if len(value) == 10:  # plain date, e.g. 2026-09-20
            value += "T00:00:00+00:00"
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def job_key(job):
    # Same company + same title = same role (even if it shows up in two sources
    # or in several locations), so you only get alerted once.
    return f"{norm(job['company'])}|{norm(job['title'])}"


# ---------------------------------------------------------------- http
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
