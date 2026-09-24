"""Title/location filters and Claude fit scoring."""
import json
import re


# ---------------------------------------------------------------- filtering
def word_in(word, text):
    return re.search(rf"\b{re.escape(word.lower())}\b", text) is not None


def title_passes(title, cfg):
    title = (title or "").lower()
    if not any(w.lower() in title for w in cfg["title_include"]):
        return False
    return not any(word_in(w, title) for w in cfg["title_exclude"])


def passes_filters(job, cfg):
    if not title_passes(job["title"], cfg):
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
