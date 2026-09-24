#!/bin/bash
# Schedules job_finder.py with launchd (macOS).
#   ./scheduler/install.sh          # every 3 hours
#   ./scheduler/install.sh 6        # every 6 hours
#   ./scheduler/install.sh --dry-run  # just print the plist
# Stop it  with:  ./scheduler/install.sh --uninstall
set -e
LABEL="com.$(whoami).jobfinder"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAIN="gui/$(id -u)"

if [ "$1" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL."
  exit 0
fi

HOURS="${1:-3}"
[ "$1" = "--dry-run" ] && HOURS=3
case "$PROJECT" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    echo "Warning: $PROJECT is in a folder macOS blocks for background jobs."
    echo "Move the project (e.g. to ~/Projects) or runs will fail with 'Operation not permitted'."
    [ "$1" = "--dry-run" ] || exit 1 ;;
esac
[ -x "$PROJECT/venv/bin/python" ] || { echo "No venv at $PROJECT/venv. Create it first."; exit 1; }
chmod +x "$PROJECT/scheduler/run.sh"

XML="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>             <string>$LABEL</string>
  <key>ProgramArguments</key>  <array><string>$PROJECT/scheduler/run.sh</string></array>
  <key>StartInterval</key>     <integer>$((HOURS * 3600))</integer>
  <key>RunAtLoad</key>         <true/>
  <key>ProcessType</key>       <string>Background</string>
</dict>
</plist>"

if [ "$1" = "--dry-run" ]; then echo "$XML"; exit 0; fi

mkdir -p "$(dirname "$PLIST")"
echo "$XML" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Scheduled $LABEL every $HOURS hours (first run starting now)."
echo "Log: $PROJECT/data/job_finder.log"
