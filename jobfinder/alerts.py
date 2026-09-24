"""Console output and Discord alerts for new matches."""
import os
import time
from pathlib import Path

import requests


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
