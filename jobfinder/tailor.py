"""
Resume tailoring: parses resume.txt into sections, asks Claude to re-focus it
on a job description (reorder / reword / re-prioritize only, never invent),
checks the result against the original, and renders a one-page PDF.
"""
import json
import re
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# Sections kept in resume.txt for the scorer but left off the PDF.
PDF_SKIP_SECTIONS = {"TARGET ROLES"}
# Plain-line sections Claude may rewrite. Everything else without bullets
# (education, etc.) is always copied from the original.
TAILORABLE_LINE_SECTIONS = {"SUMMARY", "SKILLS"}
# Bullet sections whose entries Claude may reorder. Experience stays chronological.
REORDERABLE_SECTIONS = {"PROJECTS"}
DATE_RE = re.compile(r"\b(19|20)\d\d\b|\bPresent\b", re.I)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


# ---------------------------------------------------------------- parsing
def is_section_title(line):
    return line == line.upper() and re.search(r"[A-Z]", line) and not line.startswith("-")


def parse_resume(text):
    """resume.txt -> {"name", "contact", "sections": [{"title", "entries": [{"header", "bullets"}]}]}"""
    lines = [l.rstrip() for l in text.strip().splitlines()]
    resume = {"name": lines[0].strip(), "contact": lines[1].strip(), "sections": []}
    section = None
    for line in lines[2:]:
        s = line.strip()
        if not s:
            continue
        if is_section_title(s):
            section = {"title": s, "entries": []}
            resume["sections"].append(section)
        elif section is None:
            continue
        elif s.startswith("- ") and section["entries"]:
            section["entries"][-1]["bullets"].append(s[2:].strip())
        else:
            section["entries"].append({"header": s, "bullets": []})
    return resume


def resume_to_text(resume):
    out = [resume["name"], resume["contact"]]
    for sec in resume["sections"]:
        out += ["", sec["title"]]
        for e in sec["entries"]:
            out.append(e["header"])
            out += [f"- {b}" for b in e["bullets"]]
    return "\n".join(out)


# ---------------------------------------------------------------- tailoring
TAILOR_PROMPT = """You are tailoring a candidate's resume to one job posting.

JOB POSTING
Company: {company}
Title: {title}
Description:
{description}

CANDIDATE RESUME (JSON):
{resume_json}

Return the same resume structure, re-focused on this job. Rules:
- Never invent anything. No new employers, projects, tools, skills, metrics,
  numbers, or responsibilities. Every claim must be supported by the original.
- You MAY reword bullets to lead with what this job cares about and to use the
  job's terminology where it truthfully describes the same work (e.g. "data
  pipelines" -> "ETL pipelines" only if the original work was ETL).
- You MAY reorder bullets within an entry, reorder entries in PROJECTS, and
  drop at most one weak bullet per entry to keep the resume to one page.
  Every entry keeps at least one bullet. Keep EXPERIENCE entries in their
  original (chronological) order.
- Copy EDUCATION exactly as given.
- Keep every section title and every entry "header" string exactly as given.
- SUMMARY: rewrite it (2-3 lines) to emphasize the overlap with this job.
- SKILLS: keep each line's "Label:" prefix; reorder items so the ones the job
  asks for come first. Only use items already present in the original skills.
- Keep bullets concise; don't make them longer than the originals.
- In "missing_skills", list requirements from the posting the candidate does
  not show on the resume (so they can prepare, NOT to add to the resume).
- In "changes", briefly say what you changed and why (one or two sentences)."""

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "header": {"type": "string"},
                                "bullets": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["header", "bullets"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "entries"],
                "additionalProperties": False,
            },
        },
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "string"},
    },
    "required": ["sections", "missing_skills", "changes"],
    "additionalProperties": False,
}


def tailor_resume(client, model, resume, job):
    """Returns (tailored_resume, info). Raises on API/parse failure."""
    pdf_sections = [s for s in resume["sections"] if s["title"] not in PDF_SKIP_SECTIONS]
    prompt = TAILOR_PROMPT.format(
        company=job["company"], title=job["title"],
        description=job["description"][:12000],
        resume_json=json.dumps({"sections": pdf_sections}, indent=1))
    msg = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"format": {"type": "json_schema", "schema": TAILOR_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if msg.stop_reason == "refusal":
        raise ValueError("model declined the tailoring request")
    if msg.stop_reason == "max_tokens":
        raise ValueError("tailoring response was cut off (max_tokens)")
    text = "".join(b.text for b in msg.content if b.type == "text")
    data = json.loads(text)
    tailored, rejected = merge_checked(resume, data["sections"])
    info = {"missing_skills": data.get("missing_skills", []),
            "changes": data.get("changes", ""),
            "kept_original_sections": rejected}
    return tailored, info


# ---------------------------------------------------------------- safety checks
def numbers_in(text):
    return {n.replace(",", "") for n in NUM_RE.findall(text)}


def skill_items(line):
    label, _, rest = line.partition(":")
    return label.strip(), [i.strip() for i in rest.split(",") if i.strip()]


def check_section(orig, new, allowed_numbers):
    """Returns the cleaned new section, or None if it breaks a rule."""
    title = orig["title"]
    has_bullets = any(e["bullets"] for e in orig["entries"])
    new_text = " ".join(e["header"] + " " + " ".join(e["bullets"]) for e in new["entries"])
    if not numbers_in(new_text) <= allowed_numbers:
        return None  # a number appeared that isn't in the original resume

    if has_bullets:
        orig_headers = [e["header"] for e in orig["entries"]]
        by_header = {e["header"]: e for e in new["entries"]}
        if sorted(by_header) != sorted(orig_headers):
            return None
        if any(not by_header[h]["bullets"] for h in orig_headers):
            return None
        order = ([e["header"] for e in new["entries"]] if title in REORDERABLE_SECTIONS
                 else orig_headers)
        return {"title": title, "entries": [by_header[h] for h in order]}

    if title not in TAILORABLE_LINE_SECTIONS:
        return None
    if title == "SKILLS":
        orig_items = {}
        for e in orig["entries"]:
            label, items = skill_items(e["header"])
            orig_items[label] = set(items)
        new_labels = []
        for e in new["entries"]:
            label, items = skill_items(e["header"])
            if label not in orig_items or not set(items) <= orig_items[label]:
                return None
            new_labels.append(label)
        if sorted(new_labels) != sorted(orig_items):
            return None
    return {"title": title,
            "entries": [{"header": e["header"], "bullets": []} for e in new["entries"]]}


def merge_checked(resume, new_sections):
    """Section by section, keep Claude's version only if it passes the checks."""
    allowed_numbers = numbers_in(resume_to_text(resume))
    new_by_title = {s["title"]: s for s in new_sections}
    out = dict(resume, sections=[])
    rejected = []
    for orig in resume["sections"]:
        new = new_by_title.get(orig["title"])
        checked = check_section(orig, new, allowed_numbers) if new else None
        if new and checked is None and new != orig:
            rejected.append(orig["title"])
        out["sections"].append(checked or orig)
    return out, rejected


# ---------------------------------------------------------------- PDF
def split_dates(header):
    """'Role | Org | City | Jan 2024 - Present' -> ('Role | Org | City', 'Jan 2024 - Present')"""
    parts = [p.strip() for p in header.split(" | ")]
    if len(parts) > 1 and DATE_RE.search(parts[-1]):
        return " | ".join(parts[:-1]), parts[-1]
    return header, ""


def build_story(resume, size):
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=size, leading=size * 1.22)
    styles = {
        "name": ParagraphStyle("name", parent=base, fontName="Helvetica-Bold",
                               fontSize=size + 7, leading=size + 10, alignment=TA_CENTER),
        "contact": ParagraphStyle("contact", parent=base, alignment=TA_CENTER),
        "section": ParagraphStyle("section", parent=base, fontName="Helvetica-Bold",
                                  fontSize=size + 1, spaceBefore=size * 0.7),
        "body": base,
        "right": ParagraphStyle("right", parent=base, alignment=2),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=12, bulletIndent=3),
    }
    width = LETTER[0] - 1.1 * inch
    story = [Paragraph(escape(resume["name"]), styles["name"]),
             Paragraph(escape(resume["contact"]), styles["contact"])]

    def header_row(left, right, bold_all):
        if bold_all:
            left_html = f"<b>{escape(left)}</b>"
        else:  # bold only the first part (e.g. the school name)
            first, sep, rest = left.partition(" | ")
            left_html = f"<b>{escape(first)}</b>{escape(sep + rest)}"
        if not right:
            return Paragraph(left_html, styles["body"])
        t = Table([[Paragraph(left_html, styles["body"]), Paragraph(escape(right), styles["right"])]],
                  colWidths=[width * 0.8, width * 0.2])
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return t

    for sec in resume["sections"]:
        if sec["title"] in PDF_SKIP_SECTIONS:
            continue
        story.append(Paragraph(escape(sec["title"]), styles["section"]))
        story.append(HRFlowable(width="100%", thickness=0.6, spaceBefore=1, spaceAfter=3))
        for e in sec["entries"]:
            left, right = split_dates(e["header"])
            if e["bullets"]:
                story.append(Spacer(1, size * 0.25))
                story.append(header_row(left, right, bold_all=True))
                for b in e["bullets"]:
                    story.append(Paragraph(escape(b), styles["bullet"], bulletText="•"))
            elif right:
                story.append(header_row(left, right, bold_all=False))
            else:
                label, sep, rest = e["header"].partition(": ")
                if sep and len(label) < 40:
                    story.append(Paragraph(f"<b>{escape(label)}:</b> {escape(rest)}", styles["body"]))
                else:
                    story.append(Paragraph(escape(e["header"]), styles["body"]))
    return story


def render_pdf(resume, path):
    """Renders to one page if possible by stepping the font size down."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for size in (10, 9.5, 9, 8.5):
        doc = SimpleDocTemplate(str(path), pagesize=LETTER, title=resume["name"],
                                author=resume["name"],
                                leftMargin=0.55 * inch, rightMargin=0.55 * inch,
                                topMargin=0.45 * inch, bottomMargin=0.45 * inch)
        doc.build(build_story(resume, size))
        if doc.page <= 1:
            break
    return doc.page
