#!/bin/bash
# Runs job_finder.py once and appends the output to data/job_finder.log.
# launchd calls this on a schedule; you can also run it by hand.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S')"
  PYTHONUNBUFFERED=1 venv/bin/python job_finder.py
  echo "===== exit code $?"
} >> data/job_finder.log 2>&1
