#!/bin/bash
# Double-click launcher for the Streaming Conversion Test (macOS).
#
# The very first time, macOS Gatekeeper may block it: right-click this file,
# choose "Open", then confirm.  After that a normal double-click works.
#
# It finds a Python 3 that has Tk (needed for the desktop window), then runs the
# app.  No virtual environment is required — the app installs its helpers into a
# private per-user folder on first run (see README).

cd "$(dirname "$0")" || exit 1
APP="spotify_conversion_test_app.py"

if [ ! -f "$APP" ]; then
  osascript -e 'display alert "App file missing" message "spotify_conversion_test_app.py must sit next to this launcher."' >/dev/null 2>&1
  echo "Error: $APP not found next to this launcher." >&2
  exit 1
fi

# Prefer a Python 3 that can import tkinter (so the GUI opens).
pick_python() {
  for py in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /usr/bin/python3 \
    python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3
  do
    if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
      if "$py" -c 'import tkinter' >/dev/null 2>&1; then
        echo "$py"; return 0
      fi
    fi
  done
  # No Tk-capable Python found: fall back to any python3 (the app will show
  # guidance for enabling Tk; the command line still works).
  if command -v python3 >/dev/null 2>&1; then echo "python3"; return 0; fi
  return 1
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  osascript -e 'display alert "Python 3 not found" message "Install Python 3 (from python.org, which includes Tk) and try again."' >/dev/null 2>&1
  echo "Error: no Python 3 found. Install it from https://www.python.org/ and retry." >&2
  exit 1
fi

# Testing hook: SCT_DRYRUN=1 prints the choice instead of launching.
if [ -n "$SCT_DRYRUN" ]; then
  echo "would run: $PY $APP"
  exit 0
fi

exec "$PY" "$APP"
