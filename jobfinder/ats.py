"""
Job sources without a simple public API: Workday, Workable, and iCIMS.

Every fetch_* function returns jobs in the same shape job_finder uses:
  id, company, title, location, url, description, posted (unix ts or None),
  employment, source

Workday and iCIMS boards at big companies can hold thousands of postings, so
those fetchers run keyword searches and take a `keep(title) -> bool` filter.
The full description (one extra request per job) is only downloaded for
titles that pass it.
"""
import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from .common import iso_to_ts, strip_html

# Workday and iCIMS often reject obvious bot user agents.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_SEARCH_TERMS = ["data", "analyst", "analytics", "business intelligence",
                        "data engineer",  "data scientist"
                        "software engineer", "machine learning", "forward deploy engineer"]
PAUSE = 0.3  # seconds between list-page requests, to be polite


# ---------------------------------------------------------------- helpers
def _session():
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _get(session, url, params=None, as_json=True):
    try:
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 404:
            print(f"  ! 404 for {url} (board address is probably wrong)")
            return None
        r.raise_for_status()
        return r.json() if as_json else r.text
    except Exception as e:
        print(f"  ! error fetching {url}: {e}")
        return None


def _post_json(session, url, body):
    try:
        r = session.post(url, json=body, timeout=30)
        if r.status_code == 404:
            print(f"  ! 404 for {url} (check the Workday URL in companies.yaml)")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ! error fetching {url}: {e}")
        return None


# ---------------------------------------------------------------- Workday
def parse_workday_url(url):
    """Careers URL -> (api_base, public_base, tenant).

    Handles both URL styles:
      https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One
      https://wd1.myworkdaysite.com/recruiting/acme/External
    """
    p = urlparse(url.strip())
    host = p.netloc.lower()
    parts = [x for x in p.path.split("/") if x]
    parts = [x for x in parts if not re.fullmatch(r"[a-z]{2}-[A-Za-z]{2}", x)]  # drop en-US
    if "myworkdaysite.com" in host:
        if len(parts) < 3 or parts[0] != "recruiting":
            raise ValueError(f"can't read tenant/site from {url}")
        tenant, site = parts[1], parts[2]
        public = f"https://{host}/recruiting/{tenant}/{site}"
    else:
        if not parts:
            raise ValueError(f"no site name in {url} (it should end in /SiteName)")
        tenant, site = host.split(".")[0], parts[0]
        public = f"https://{host}/{site}"
    return f"https://{host}/wday/cxs/{tenant}/{site}", public, tenant


def workday_posted(text, now):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)(\+?)\s*day", t)
        if not m:
            return None
        days = int(m.group(1)) + (1 if m.group(2) else 0)
    return now - days * 86400


def fetch_workday(url, keep, max_age_days=30, search_terms=None, max_pages=5):
    try:
        api, public, tenant = parse_workday_url(url)
    except ValueError as e:
        print(f"  ! {e}")
        return []
    session = _session()
    now = time.time()
    seen_paths, jobs = set(), []

    for term in search_terms or DEFAULT_SEARCH_TERMS:
        for page in range(max_pages):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term}
            data = _post_json(session, f"{api}/jobs", body)
            if not data:
                break
            postings = data.get("jobPostings") or []
            for p in postings:
                path, title = p.get("externalPath"), p.get("title", "")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                posted = workday_posted(p.get("postedOn"), now)
                if posted and now - posted > max_age_days * 86400:
                    continue
                if not keep(title):
                    continue
                jobs.append(_workday_job(session, api, public, tenant, p, posted))
            if len(postings) < 20:  # last page ("total" is only sent on page 1)
                break
            time.sleep(PAUSE)
    return jobs


def _workday_job(session, api, public, tenant, posting, posted):
    path = posting["externalPath"]
    info = (_get(session, api + path) or {}).get("jobPostingInfo") or {}
    # The list only says "3 Locations"; the detail page has the real ones.
    locations = [info.get("location")] + list(info.get("additionalLocations") or [])
    location = "; ".join(l for l in locations if l) or posting.get("locationsText", "")
    if info.get("remoteType"):
        location += f"; {info['remoteType']}"
    bullets = posting.get("bulletFields") or []
    return {
        "id": info.get("jobReqId") or (bullets[0] if bullets else path),
        "company": tenant,
        "title": info.get("title") or posting.get("title", ""),
        "location": location,
        "url": info.get("externalUrl") or public + path,
        "description": strip_html(info.get("jobDescription", "")),
        "posted": iso_to_ts(info.get("startDate")) or posted,
        "employment": info.get("timeType", ""),
        "source": "workday",
    }


# ---------------------------------------------------------------- Workable
def fetch_workable(account):
    """account = the part after apply.workable.com/ in the careers URL."""
    session = _session()
    data = _get(session, f"https://apply.workable.com/api/v1/widget/accounts/{account}",
                {"details": "true"})
    if not data:
        return []
    jobs = []
    for j in data.get("jobs") or []:
        locs = []
        for l in j.get("locations") or []:
            if isinstance(l, dict) and not l.get("hidden"):
                locs.append(", ".join(x for x in (l.get("city"), l.get("region"),
                                                  l.get("country")) if x))
        if not locs:
            locs.append(", ".join(x for x in (j.get("city"), j.get("state"),
                                              j.get("country")) if x))
        if j.get("telecommuting"):
            locs.append("Remote")
        jobs.append({
            "id": j.get("shortcode") or j.get("id"),
            "company": account,
            "title": j.get("title", ""),
            "location": "; ".join(l for l in locs if l),
            "url": j.get("url") or j.get("shortlink") or j.get("application_url", ""),
            "description": strip_html(j.get("description", "")),
            "posted": iso_to_ts(j.get("published_on") or j.get("created_at")),
            "employment": j.get("employment_type", ""),
            "source": "workable",
        })
    return jobs


# ---------------------------------------------------------------- iCIMS
# iCIMS has no public JSON API, so this reads the portal's HTML search pages.
# It works on the standard careers-<company>.icims.com portals; companies that
# put iCIMS behind a heavily customized site may return nothing.
ICIMS_LINK_RE = re.compile(
    r'<a[^>]+href="([^"]*/jobs/(\d+)/([^"/?]*)/job)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
ICIMS_LDJSON_RE = re.compile(
    r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
ICIMS_JUNK_TITLES = {"apply", "apply now", "view job", "more", "read more", "details"}


def fetch_icims(base_url, keep, search_terms=None, max_pages=5):
    """base_url like https://careers-acme.icims.com"""
    p = urlparse(base_url.strip())
    base = f"{p.scheme or 'https'}://{p.netloc}"
    company = re.sub(r"^(careers|jobs|uscareers|external)-", "", p.netloc.split(".")[0])
    session = _session()
    found = {}  # job id -> (url, title)

    for term in search_terms or DEFAULT_SEARCH_TERMS:
        for page in range(max_pages):
            text = _get(session, f"{base}/jobs/search", as_json=False,
                        params={"ss": 1, "searchKeyword": term, "pr": page, "in_iframe": 1})
            if not text:
                break
            new = 0
            for m in ICIMS_LINK_RE.finditer(text):
                href, jid, slug, inner = m.groups()
                heading = re.search(r"<h\d[^>]*>(.*?)</h\d>", inner, re.S | re.I)
                title = strip_html(heading.group(1) if heading else inner)
                if len(title) < 4 or title.lower() in ICIMS_JUNK_TITLES:
                    title = slug.replace("-", " ").title()
                if jid not in found:
                    found[jid] = (urljoin(base, href), title)
                    new += 1
            if not new:
                break
            time.sleep(PAUSE)

    jobs = []
    for jid, (url, title) in found.items():
        if keep(title):
            jobs.append(_icims_job(session, company, jid, url, title))
            time.sleep(PAUSE)
    return jobs


def _icims_job(session, company, jid, url, title):
    page = _get(session, url, params={"in_iframe": 1}, as_json=False) or ""
    posting = _find_jobposting(page)
    job = {"id": jid, "company": company, "title": title, "location": "", "url": url,
           "description": "", "posted": None, "employment": "", "source": "icims"}
    if posting:
        emp = posting.get("employmentType") or ""
        job |= {
            "title": posting.get("title") or title,
            "location": _ld_location(posting),
            "description": strip_html(posting.get("description", "")),
            "posted": iso_to_ts(posting.get("datePosted")),
            "employment": ", ".join(emp) if isinstance(emp, list) else emp,
        }
    if not job["description"]:
        job["description"] = strip_html(page)[:8000]
    return job


def _find_jobposting(page):
    """Returns the schema.org JobPosting dict embedded in the page, if any."""
    for block in ICIMS_LDJSON_RE.findall(page):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _ld_location(posting):
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    out = []
    for place in places:
        addr = (place or {}).get("address") or {}
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            out.append(", ".join(x for x in (addr.get("addressLocality"),
                                              addr.get("addressRegion"), country) if x))
    if posting.get("jobLocationType") == "TELECOMMUTE":
        out.append("Remote")
    return "; ".join(x for x in out if x)