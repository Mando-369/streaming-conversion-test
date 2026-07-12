#!/bin/bash
# Double-click launcher for the Streaming Conversion Test (macOS).
#
# The very first time, macOS Gatekeeper may block it: right-click this file,
# choose "Open", then confirm.  After that a normal double-click works.
#
# It finds a Python 3 with a WORKING Tk (8.6+) and runs the app.  Apple's
# /usr/bin/python3 (Xcode / Command Line Tools) ships Tk 8.5, which crashes when
# it opens a window, so it is deliberately rejected here.  No virtual environment
# is required — the app installs its helpers into a private per-user folder.

cd "$(dirname "$0")" || exit 1
APP="spotify_conversion_test_app.py"

if [ ! -f "$APP" ]; then
  osascript -e 'display alert "App file missing" message "spotify_conversion_test_app.py must sit next to this launcher."' >/dev/null 2>&1
  echo "Error: $APP not found next to this launcher." >&2
  exit 1
fi

# A Python is usable for the GUI only if tkinter imports AND Tk is 8.6+.
# (Reading TkVersion does NOT open a window, so this probe never crashes.)
has_working_tk() {
  "$1" -c 'import sys, tkinter; sys.exit(0 if float(tkinter.TkVersion) >= 8.6 else 1)' >/dev/null 2>&1
}

pick_gui_python() {
  for py in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    python3
  do
    if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then
      if has_working_tk "$py"; then echo "$py"; return 0; fi
    fi
  done
  return 1
}

any_python() {
  for py in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$py" >/dev/null 2>&1 || [ -x "$py" ]; then echo "$py"; return 0; fi
  done
  return 1
}

PY="$(pick_gui_python)"

# Testing hook: SCT_DRYRUN=1 prints the choice instead of launching.
if [ -n "$SCT_DRYRUN" ]; then
  echo "would run: ${PY:-<none: no Tk 8.6+ Python>} $APP"
  exit 0
fi

if [ -n "$PY" ]; then
  exec "$PY" "$APP"
fi

# No Python with a working Tk was found.
if any_python >/dev/null; then
  osascript -e 'display alert "Desktop app needs a newer Tk" message "None of your Pythons have a working Tk 8.6+. Apple’s /usr/bin/python3 ships the old, crash-prone Tk 8.5.\n\nInstall one of these once:\n   •  brew install python-tk@3.14\n   •  or Python from python.org (includes Tk)\n\nThe command line works with your current Python:\n   python3 spotify_conversion_test_app.py your_master.wav"' >/dev/null 2>&1
  echo "No Tk 8.6+ Python found. Fix: brew install python-tk@3.14 (or install python.org)." >&2
  echo "The command line works now:  python3 $APP your_master.wav" >&2
  exit 3
else
  osascript -e 'display alert "Python 3 not found" message "Install Python 3 from python.org (includes Tk) and try again."' >/dev/null 2>&1
  echo "Error: no Python 3 found. Install it from https://www.python.org/ and retry." >&2
  exit 1
fi
