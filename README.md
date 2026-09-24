# Job Finder

Checks company job boards every 3 hours, keeps new-grad data roles that match
your resume, and pings you on Discord with a fit score and apply link.

## Setup

1. **Make a PRIVATE GitHub repo** (your resume will live in it) and upload all
   of these files, including the `.github` folder.
2. **Paste your resume** as plain text into `resume.txt`.
3. **Edit `config.yaml`**: fill in `candidate_notes` (grad date, work
   authorization), and add or remove companies, title keywords, and locations.
4. **Get a Claude API key** at https://console.anthropic.com (add a few dollars
   of credit; screening with Haiku costs very little per job).
5. **Make a Discord webhook**: in your own Discord server, go to
   Server Settings → Integrations → Webhooks → New Webhook → Copy URL.
6. **Add both as repo secrets**: repo Settings → Secrets and variables →
   Actions → New repository secret:
   - `ANTHROPIC_API_KEY`
   - `DISCORD_WEBHOOK_URL`
7. **Run it once**: Actions tab → job-finder → Run workflow. After that it runs
   on its own every 3 hours.

## Run it on your own computer

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # optional; without it you get keyword-only matches
export DISCORD_WEBHOOK_URL=https://...      # optional; without it results just print
python job_finder.py
```

## Add companies

1. Add company names to `companies.txt`, one per line.
2. `python discovery.py companies.txt` finds which job board each one uses
   (Greenhouse, Lever, Ashby, Workable) and writes `data/discovered.yaml`.
3. Paste the entries you want into `companies.yaml`.
4. `python discovery.py --check-config` finds dead or duplicate boards in
   `companies.yaml`.

Workday and iCIMS addresses can't be guessed, so discovery prints a search
link for those; paste the careers URL under `workday:` or `icims:`.

## Files

```
job_finder.py        run this: fetch -> filter -> score -> resumes -> alerts
discovery.py         run this: find which job board a company uses
config.yaml          filters, scoring, and resume settings
companies.yaml       the job boards to watch
companies.txt        company names for discovery.py
resume.txt           your resume as plain text (gitignored)
jobfinder/
  common.py          paths, config loading, shared helpers
  boards.py          Greenhouse, Lever, Ashby, SimplifyJobs + collect_jobs()
  ats.py             Workable, Workday, iCIMS
  screening.py       title/location filters and Claude scoring
  tailor.py          resume parsing, tailoring, PDF rendering
  tracker.py         picks the resume for each match, writes tracker.json
  alerts.py          console + Discord output
data/                seen_jobs.json, matches.json, tracker.json, discovered.yaml (gitignored)
resume/              generated PDFs (gitignored)
```

## How it works

1. Pulls jobs from every board in `companies.yaml` (Greenhouse, Lever, Ashby,
   Workable, Workday, iCIMS), plus the SimplifyJobs new-grad list.
2. Keeps titles matching `title_include`, drops `title_exclude` words
   (senior, intern, etc.), non-full-time roles, wrong locations, and old posts.
3. Skips anything already in `data/seen_jobs.json`.
4. Sends each new job plus your resume to Claude for a 0–100 fit score.
5. Alerts you about jobs at or above `min_score` and saves them to `data/matches.json`.
6. Picks a resume for each job scoring `tailor_min_score` or higher (this is
   separate from `min_score`) and logs it in `data/tracker.json`:
   - `original_resume_min_score`+ → your original resume
     (`resume/Sandy_YANG_Resume.pdf`, rebuilt from `resume.txt` whenever you edit it)
   - below that → Claude reorders and rewords your resume toward the job
     description and saves `resume/Sandy_YANG_<job title>_<company>.pdf`. It never
     adds skills, numbers, or experience that aren't in `resume.txt`; any section
     that fails that check falls back to your original wording. Skills the job asks
     for that you don't have show up in the Discord alert as "Not on resume".

   Thresholds, the model, and the file-name prefix are in `config.yaml`.

## Tuning tips

- Too many alerts? Raise `min_score` or tighten `title_include`.
- Too few? Lower `min_score`, add companies, or add title keywords.
- The first run has a big backlog, so it scores 40 jobs per run and works
  through the rest over the next few runs.
- To re-check everything from scratch, delete `data/seen_jobs.json`.
