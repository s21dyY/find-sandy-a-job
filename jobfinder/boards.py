"""
Job sources with public JSON APIs (Greenhouse, Lever, Ashby, SimplifyJobs list),
plus collect_jobs(), which pulls from every source in companies.yaml.

Every fetcher returns jobs in the same shape:
  id, company, title, location, url, description, posted (unix ts or None),
  employment, source
"""
from .ats import (DEFAULT_SEARCH_TERMS, fetch_icims, fetch_workable,
                  fetch_workday, parse_workday_url)
from .common import get_json, iso_to_ts, strip_html
from .screening import title_passes

SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
                "dev/.github/scripts/listings.json")


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
    companies = cfg["companies"]
    for source, fetch in (("greenhouse", fetch_greenhouse),
                          ("lever", fetch_lever),
                          ("ashby", fetch_ashby),
                          ("workable", fetch_workable)):
        for token in companies.get(source) or []:
            found = fetch(token)
            print(f"  {source:<10} {token:<20} {len(found)} jobs")
            jobs += found

    # Workday and iCIMS: keyword search, then only open postings whose title
    # passes your filters (big companies have thousands of postings).
    keep = lambda title: title_passes(title, cfg)
    terms = cfg.get("ats_search_terms") or DEFAULT_SEARCH_TERMS
    pages = cfg.get("ats_max_pages", 5)
    for url in companies.get("workday") or []:
        try:
            label = parse_workday_url(url)[2]
        except ValueError:
            label = url
        found = fetch_workday(url, keep, cfg.get("max_age_days", 30), terms, pages)
        print(f"  {'workday':<10} {label:<20} {len(found)} matching jobs")
        jobs += found
    for url in companies.get("icims") or []:
        found = fetch_icims(url, keep, terms, pages)
        label = url.split("//")[-1].split(".")[0]
        print(f"  {'icims':<10} {label:<20} {len(found)} matching jobs")
        jobs += found

    if cfg.get("use_simplify_list", True):
        found = fetch_simplify()
        print(f"  simplify   new-grad list        {len(found)} active jobs")
        jobs += found
    return jobs
