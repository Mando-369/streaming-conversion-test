#!/usr/bin/env python3
"""
Streaming Conversion Test — single-file edition
===============================================

One self-contained file that takes your offline master audio and tells you
whether it will survive the lossy (and lossless) conversions online music
services apply.  It encodes your master through the codec/bitrate tiers those
services actually serve, decodes them back, and measures true-peak overshoot
(inter-sample clipping) and loudness/peak stability — then grades the result
against *each service's own* loudness-normalization target.

Services modelled (targets are publicly-reported, approximate, and change over
time):

    Spotify        -14 LUFS   Ogg Vorbis 96/160/320, AAC 128/256
    Apple Music    -16 LUFS   AAC 256, ALAC lossless
    YouTube Music  -14 LUFS   Opus 128/160, AAC 128
    Amazon Music   -14 LUFS   AAC 256, FLAC (HD/Ultra HD)
    Tidal          -14 LUFS   AAC 256, FLAC (HiFi/Max)
    Deezer         -15 LUFS   MP3 128/320, FLAC
    SoundCloud     -14 LUFS   Opus 64, AAC 256, MP3 128

Loudness/true-peak methodology follows ITU-R BS.1770 (via ffmpeg's loudnorm +
astats).  Spotify's own rules ("Loudness normalization on Spotify") are the
reference for the -14 LUFS / -1 dBTP guidance:
https://support.spotify.com/us/artists/article/loudness-normalization/

------------------------------------------------------------------------------
ONE-CLICK for end users
------------------------------------------------------------------------------
This file needs only:
  * Python 3.8+                (already installed if you can run this file)
  * ffmpeg                     (does the real transcoding + BS.1770 measurement)

On first run it checks for those and, if ffmpeg is missing OR your ffmpeg lacks
an encoder it needs (Ogg Vorbis, MP3, etc.), it installs a self-contained
ffmpeg for *you only* (via the pip package `imageio-ffmpeg`) into a per-user
folder — no admin rights, no Homebrew, no apt, nothing system-wide.  Optional
drag-and-drop support (`tkinterdnd2`) is installed the same way.

Usage
-----
    python3 spotify_conversion_test_app.py              # desktop app (GUI)
    python3 spotify_conversion_test_app.py master.wav   # command line
    python3 spotify_conversion_test_app.py folder/ --report out.html
    python3 spotify_conversion_test_app.py --setup      # just install deps
    python3 spotify_conversion_test_app.py --help

Anything installed by --setup / first run lives under a single per-user folder
and can be removed at any time (see --where).

Important accuracy notes
------------------------
  * ffmpeg's encoders are excellent references but are NOT byte-identical to
    each service's internal encoder builds.  Treat overshoot figures as a
    faithful, conservative simulation.
  * AAC: ffmpeg's *native* AAC encoder overshoots true peak ~2 dB more than any
    real-world AAC encoder, so on macOS every AAC tier is encoded with Apple's
    CoreAudio AAC (AudioToolbox `aac_at` — the same encoder iTunes / Logic / Apple
    Music use), falling back to FDK or `afconvert`, and only to ffmpeg's native
    AAC (a rough proxy) on systems without those.  Each row shows which encoder ran.
  * Lossless tiers (FLAC/ALAC) round-trip bit-exactly, so they add no overshoot;
    the tool reports them as such rather than re-encoding them.
  * Normalization is applied by services at PLAYBACK — your uploaded file is
    never altered.  The "plays at" figure is what listeners hear.
"""

__version__ = "2.6.1"

import argparse
import datetime
import html
import importlib
import json as jsonmod
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
import subprocess
import sys
import tempfile

# tkinter / DnD are imported lazily (only when the GUI actually launches) so the
# command-line path works on Python builds without Tk.  These module-level names
# are populated by run_gui().
tk = ttk = filedialog = messagebox = None
DND_FILES = TkinterDnD = None
_HAS_DND = False


# =============================================================================
#  Per-user app folder + dependency bootstrap (the "one-click" part)
# =============================================================================

def user_data_dir():
    """A per-user, per-OS folder where we keep pip-installed helpers."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "StreamingConversionTest")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "StreamingConversionTest")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "streaming-conversion-test")


DEPS_DIR = os.path.join(user_data_dir(), "pydeps")

# If we've installed helpers before, make them importable this run.
if os.path.isdir(DEPS_DIR) and DEPS_DIR not in sys.path:
    sys.path.insert(0, DEPS_DIR)


def _pip_install(*packages, log=print):
    """Install packages into our private DEPS_DIR (no admin, no system changes).

    Uses `pip install --target DEPS_DIR`, which sidesteps 'externally managed
    environment' restrictions and keeps everything in one removable folder.
    """
    os.makedirs(DEPS_DIR, exist_ok=True)
    base = [sys.executable, "-m", "pip", "install", "--upgrade",
            "--target", DEPS_DIR, *packages]

    def _try(cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            return proc.returncode == 0, proc
        except Exception as exc:  # pragma: no cover - pip missing entirely
            return False, exc

    ok, proc = _try(base)
    if not ok:
        # pip might be absent; try to bootstrap it once, then retry.
        try:
            subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                           capture_output=True, text=True)
        except Exception:
            pass
        ok, proc = _try(base)

    if ok:
        importlib.invalidate_caches()
        if DEPS_DIR not in sys.path:
            sys.path.insert(0, DEPS_DIR)
    else:
        detail = getattr(proc, "stderr", str(proc))
        log(f"  (could not install {', '.join(packages)}: {str(detail).strip()[:200]})")
    return ok


def ensure_import(import_name, pip_name, log=print):
    """Return the imported module, installing `pip_name` into DEPS_DIR if needed."""
    try:
        return importlib.import_module(import_name)
    except Exception:
        pass
    log(f"Installing '{pip_name}' (one-time, into your user folder)…")
    if not _pip_install(pip_name, log=log):
        return None
    try:
        return importlib.import_module(import_name)
    except Exception:
        return None


# =============================================================================
#  Codec tiers, services, and the loudness/true-peak specification
# =============================================================================

# --- Loudness / peak targets -------------------------------------------------
TARGET_LUFS = -14.0        # Spotify's integrated loudness target (ITU-R BS.1770)
TP_CEILING_NORMAL = -1.0   # dBTP ceiling recommended for lossy delivery
TP_CEILING_HOT = -2.0      # dBTP ceiling if master is louder than target (Spotify)
CLIP_CEILING = 0.0         # dBFS: a decoded peak above this is hard clipping

# Service delivery sample rates: a hi-res source is resampled to these BEFORE the
# lossy encode, matching what the services do to an upload.
DELIVERY_RATE_44K = 44100  # services deliver Vorbis/AAC/MP3 at 44.1 kHz
DELIVERY_RATE_48K = 48000  # Opus is always 48 kHz internally (YouTube)


class TranscodeSpec:
    """One unique codec/bitrate round-trip we actually run through ffmpeg.

    `primary` marks the high/standard-quality tiers most listeners actually get
    (they drive the verdict and the mastering recommendation).  Low-bitrate
    data-saver / fallback tiers are `primary=False`: still analyzed and shown, but
    treated as an informational, non-blocking notice — a −0.5 dBTP master should
    not be graded on what a data-saver stream does.
    """

    __slots__ = ("key", "label", "codec", "bitrate", "ext", "primary", "rate")

    def __init__(self, key, label, codec, bitrate, ext, primary=True,
                 rate=DELIVERY_RATE_44K):
        self.key = key
        self.label = label
        self.codec = codec
        self.bitrate = bitrate
        self.ext = ext
        self.primary = primary
        # The sample rate the service actually delivers this tier at.  A hi-res
        # (e.g. 48 kHz) upload is resampled to this BEFORE the lossy encode, so we
        # match that here rather than encoding at the source rate.
        self.rate = rate


# NOTE: ffmpeg's *native* `aac` encoder overshoots true peak far more than any
# real-world AAC encoder (~2 dB more).  Every AAC tier below is routed at runtime
# through the best available AAC encoder — Apple's CoreAudio AAC (`aac_at`, the
# same one iTunes/Logic/Apple Music use), else FDK, else `afconvert`, and only to
# ffmpeg's native `aac` as a rough proxy — see resolve_aac_encoder().

# The union of lossy codec tiers used by any service.  Each is transcoded ONCE
# per file; services then reference these results by key.
TRANSCODES = [
    # Primary tiers = the top streaming quality (what a quality-conscious release targets).
    TranscodeSpec("vorbis_320", "Ogg Vorbis 320k", "libvorbis",   "320k", "ogg",  primary=True),   # Spotify Very High
    TranscodeSpec("aac_256",    "AAC 256k",        "aac",         "256k", "m4a",  primary=True),   # Apple / Amazon / Tidal / YouTube High
    TranscodeSpec("mp3_320",    "MP3 320k",        "libmp3lame",  "320k", "mp3",  primary=True),   # Deezer High
    # Informational tiers = mid/low-bitrate, data-saver, fallback (non-blocking).
    TranscodeSpec("vorbis_160", "Ogg Vorbis 160k", "libvorbis",   "160k", "ogg",  primary=False),  # Spotify High/default
    TranscodeSpec("vorbis_96",  "Ogg Vorbis 96k",  "libvorbis",   "96k",  "ogg",  primary=False),  # Spotify Low
    TranscodeSpec("aac_128",    "AAC 128k",        "aac",         "128k", "m4a",  primary=False),  # free web
    TranscodeSpec("opus_160",   "Opus 160k",       "libopus",     "160k", "opus", primary=False, rate=DELIVERY_RATE_48K),  # YouTube standard
    TranscodeSpec("opus_128",   "Opus 128k",       "libopus",     "128k", "opus", primary=False, rate=DELIVERY_RATE_48K),  # below standard
    TranscodeSpec("opus_64",    "Opus 64k",        "libopus",     "64k",  "opus", primary=False, rate=DELIVERY_RATE_48K),  # data-saver
    TranscodeSpec("mp3_128",    "MP3 128k",        "libmp3lame",  "128k", "mp3",  primary=False),  # fallback
]
TRANSCODE_BY_KEY = {t.key: t for t in TRANSCODES}

# Lossless tiers: bit-exact round-trip, so no overshoot.  We do NOT re-encode
# these (it would only confirm zero overshoot); they're reported as lossless.
LOSSLESS = {
    "alac": "ALAC",
    "flac": "FLAC",
}


class Service:
    """A streaming service: its normalization target and the tiers it serves.

    `tiers` is a list of (tier_key, context_label).  tier_key is either a
    TRANSCODES key (lossy, re-encoded) or a LOSSLESS key (bit-exact).
    """

    __slots__ = ("key", "name", "target", "ceiling", "ceiling_hot", "tiers", "note")

    def __init__(self, key, name, target, ceiling, ceiling_hot, tiers, note=""):
        self.key = key
        self.name = name
        self.target = target
        self.ceiling = ceiling
        self.ceiling_hot = ceiling_hot
        self.tiers = tiers
        self.note = note


SERVICES = [
    Service("spotify", "Spotify", -14.0, -1.0, -2.0, [
        ("vorbis_96",  "Low quality / weak connection"),
        ("vorbis_160", "Free desktop/mobile"),
        ("vorbis_320", "Premium (very high)"),
        ("aac_128",    "Web player (free)"),
        ("aac_256",    "Web player / Google Cast (premium)"),
    ]),
    Service("apple", "Apple Music", -16.0, -1.0, -1.0, [
        ("aac_256", "AAC 256 VBR (standard)"),
        ("alac",    "Lossless / Hi-Res Lossless (ALAC)"),
    ]),
    Service("youtube", "YouTube Music", -14.0, -1.0, -1.0, [
        ("aac_256",  "High / Premium (256 AAC)"),
        ("opus_160", "Opus (standard)"),
        ("opus_128", "Opus (typical)"),
        ("aac_128",  "AAC (fallback)"),
    ]),
    Service("amazon", "Amazon Music", -14.0, -1.0, -1.0, [
        ("aac_256", "AAC (standard)"),
        ("flac",    "HD / Ultra HD (FLAC)"),
    ]),
    Service("tidal", "Tidal", -14.0, -1.0, -1.0, [
        ("aac_256", "AAC (lossy)"),
        ("flac",    "HiFi / Max (FLAC)"),
    ]),
    Service("deezer", "Deezer", -15.0, -1.0, -1.0, [
        ("mp3_128", "MP3 128 (standard)"),
        ("mp3_320", "MP3 320 (high)"),
        ("flac",    "HiFi (FLAC)"),
    ]),
    Service("soundcloud", "SoundCloud", -14.0, -1.0, -1.0, [
        ("opus_64",  "Opus 64 (streaming)"),
        ("aac_256",  "AAC 256 (Go+)"),
        ("mp3_128",  "MP3 128 (fallback)"),
    ]),
]

# Ordered, de-duplicated list of lossy tiers we must actually transcode.
USED_TRANSCODE_KEYS = []
for _svc in SERVICES:
    for _tkey, _ctx in _svc.tiers:
        if _tkey not in LOSSLESS and _tkey not in USED_TRANSCODE_KEYS:
            USED_TRANSCODE_KEYS.append(_tkey)

# Encoders we need (to grade an ffmpeg build and to skip codecs cleanly).  Native
# `aac` is the baseline requirement; resolve_aac_encoder() upgrades to aac_at/FDK
# when available, so a missing aac_at never forces a bundled-ffmpeg install.
REQUIRED_ENCODERS = tuple(dict.fromkeys(TRANSCODE_BY_KEY[k].codec for k in USED_TRANSCODE_KEYS))

# --- Verdicts ----------------------------------------------------------------
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
# SKIP (encoder unavailable) ranks below PASS so it never worsens the verdict.
_RANK = {SKIP: -1, PASS: 0, WARN: 1, FAIL: 2}

# "info" is a DISPLAY-only key for informational (data-saver) tiers — rendered
# muted and never rolled into a service/overall verdict.
INFO = "info"
COLORS = {PASS: "#1db954", WARN: "#f5a623", FAIL: "#e0245e", SKIP: "#8a8a8a", INFO: "#6f6f6f"}
LABELS = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "N/A", INFO: "info"}


def worst(*verdicts):
    """Return the most severe verdict among the arguments."""
    return max(verdicts, key=lambda v: _RANK[v])


def tier_display_key(t):
    """Verdict key to DISPLAY for a tier — informational (data-saver) tiers show
    muted 'info' instead of an alarming WARN, since they don't affect the verdict."""
    return INFO if not t.get("primary", True) else t["verdict"]


def effective_ceiling(service, integrated_lufs):
    """The recommended true-peak ceiling for a service given the master loudness."""
    if integrated_lufs is not None and integrated_lufs > service.target:
        return service.ceiling_hot
    return service.ceiling


def evaluate_peak(true_peak, ceiling):
    """Grade a true peak by what actually happens: FAIL only if it CLIPS (>0 dBFS).

    Being above the recommended `ceiling` (e.g. -1 dBTP) but still under 0 dBFS is
    NOT a warning — it is "above recommended headroom, but not clipping", an
    advisory the caller surfaces separately (see `above_recommended`).  A hot but
    clean master (say -0.5 dBTP) plays back safely; the -1 dBTP figure is a
    guideline, not a clip line, so it never drives the verdict on its own.
    """
    if true_peak is None:
        return PASS
    if true_peak > CLIP_CEILING:
        return FAIL
    return PASS


def above_recommended(true_peak, ceiling):
    """True when a peak sits above the recommended headroom but does not clip —
    the "above recommended headroom, but not clipping" advisory case."""
    return (true_peak is not None
            and ceiling < true_peak <= CLIP_CEILING)


def evaluate_tier(decoded_tp, playback_tp):
    """Grade a codec tier the way listeners actually experience it.

    These services apply loudness normalization at playback (the default), so
    what matters most is the true peak AFTER that gain:

      * FAIL  — clips at playback (playback true peak > 0 dBFS): audibly clips
                even with normalization on.  Happens mainly to quiet masters
                that get lifted up.
      * WARN  — the encoded stream exceeds 0 dBFS at UNITY gain (decoded true
                peak > 0): only audible if a listener disables normalization, or
                in a downloaded/un-normalized copy.  Common for loud masters,
                which get turned DOWN at playback and are safe there.
      * PASS  — otherwise, INCLUDING a stream that sits above the -1 dBTP
                recommended headroom but still under 0 dBFS: it doesn't clip, so
                it isn't a warning (just "above recommended headroom").
    """
    if decoded_tp is None:
        return PASS
    if playback_tp is not None and playback_tp > CLIP_CEILING:
        return FAIL
    if decoded_tp > CLIP_CEILING:
        return WARN
    return PASS


def normalization_gain(integrated_lufs, true_peak, target, cap_ceiling=TP_CEILING_NORMAL):
    """Gain (dB) a service applies at playback to hit `target`.

    Downward gain (louder master) is applied in full and introduces no clipping.
    Upward gain (quieter master) is capped so the true peak stays at or below the
    ceiling, matching the "leave headroom for lossy encodings" rule.
    """
    if integrated_lufs is None:
        return 0.0
    desired = target - integrated_lufs
    if desired <= 0:
        return desired
    if true_peak is None:
        return desired
    cap = cap_ceiling - true_peak
    return max(0.0, min(desired, cap))


# =============================================================================
#  ffmpeg: locate/validate, measure loudness+peaks, run codec round-trips
# =============================================================================

class FFmpegError(Exception):
    pass


# Common install locations to check if ffmpeg is not on PATH (macOS Homebrew, etc.)
_FALLBACK_PATHS = (
    "/opt/homebrew/bin/ffmpeg",   # Apple Silicon Homebrew
    "/usr/local/bin/ffmpeg",      # Intel Homebrew
    "/usr/bin/ffmpeg",
    r"C:\ffmpeg\bin\ffmpeg.exe",  # common Windows manual install
)


def _imageio_ffmpeg_exe():
    """Path to a pip-bundled ffmpeg if `imageio-ffmpeg` is already importable."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def candidate_ffmpegs():
    """Every ffmpeg binary we might use, best-known-good order, de-duplicated."""
    found = []

    def add(path):
        if path and path not in found:
            found.append(path)

    add(shutil.which("ffmpeg"))
    for p in _FALLBACK_PATHS:
        if os.path.exists(p):
            add(p)
    add(_imageio_ffmpeg_exe())
    return found


def ffmpeg_version(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-version"],
                             capture_output=True, text=True)
        return out.stdout.splitlines()[0] if out.stdout else "unknown"
    except Exception:
        return "unknown"


def ffmpeg_encoders(ffmpeg):
    """The set of encoder names compiled into this ffmpeg build."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True)
    except Exception:
        return set()
    names = set()
    for line in out.stdout.splitlines():
        m = re.match(r"\s*[A-Za-z.]{6}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return names


def encoder_available(encoders, codec):
    """True if the given ffmpeg encoder (e.g. 'libvorbis') is compiled in."""
    return codec in encoders


def resolve_ffmpeg(auto_install=True, log=print):
    """Pick the best ffmpeg to use.

    Prefers a build that has ALL required encoders.  If none on the system
    qualifies (the classic 'my ffmpeg has no libvorbis/libmp3lame' gap), and
    auto_install is on, installs a complete bundled ffmpeg via pip.

    Returns (ffmpeg_path_or_None, available_encoders_set, missing_encoders_list).
    """
    best = None  # (path, encoders, missing)

    for ff in candidate_ffmpegs():
        encs = ffmpeg_encoders(ff)
        missing = [e for e in REQUIRED_ENCODERS if e not in encs]
        if not missing:
            return ff, encs, []
        if best is None or len(missing) < len(best[2]):
            best = (ff, encs, missing)

    if auto_install:
        if best is None:
            log("No ffmpeg found — installing a private copy (one-time)…")
        else:
            log(f"Your ffmpeg is missing: {', '.join(best[2])} — installing a complete "
                f"bundled ffmpeg so all codecs work (one-time)…")
        mod = ensure_import("imageio_ffmpeg", "imageio-ffmpeg", log=log)
        if mod is not None:
            try:
                ff = mod.get_ffmpeg_exe()
                encs = ffmpeg_encoders(ff)
                missing = [e for e in REQUIRED_ENCODERS if e not in encs]
                if best is None or len(missing) <= len(best[2]):
                    return ff, encs, missing
            except Exception as exc:
                log(f"  (bundled ffmpeg install did not complete: {exc})")

    if best is not None:
        return best  # a partial ffmpeg: usable, some codecs will be marked N/A
    return None, set(), list(REQUIRED_ENCODERS)


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _to_float(value):
    """Parse an ffmpeg-reported dB value, treating -inf / very-low as None (silence)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f <= -90.0 else f


def measure(ffmpeg, path):
    """Measure a file per ITU-R BS.1770.

    Returns a dict with:
      integrated_lufs, true_peak (dBTP), lra, threshold, sample_peak (dBFS).
    Any value that cannot be measured (e.g. silence) is None.

    True peak comes from ffmpeg's `ebur128` filter (the reference BS.1770-4
    true-peak meter, 4x oversampled) — read from its per-frame metadata for full
    precision, not the 0.1 dB-rounded summary.  Integrated loudness / LRA come from
    the same pass's summary; the raw sample peak comes from `astats`.
    """
    result = {
        "integrated_lufs": None,
        "true_peak": None,
        "lra": None,
        "threshold": None,
        "sample_peak": None,
    }

    # True peak + loudness from ffmpeg's `ebur128` — the reference BS.1770-4
    # true-peak meter (the same standard MAAT / iZotope Insight implement).  Read
    # from the per-frame metadata for full precision, not the 0.1 dB summary.
    # (ffmpeg's ebur128 is fixed at the spec's 4x oversampling; you cannot raise
    # it — pre-upsampling the input leaves its true-peak output unchanged.  A
    # resampler-as-meter can oversample more but is a non-standard filter, so we
    # stay on the real BS.1770 filter here.)
    r = _run([ffmpeg, "-hide_banner", "-nostats", "-i", path,
              "-af", "ebur128=peak=true:metadata=1,"
                     "ametadata=mode=print:key=lavfi.r128.true_peak:file=-",
              "-f", "null", "-"])
    peaks = [float(x) for x in re.findall(r"lavfi\.r128\.true_peak=([0-9.]+)", r.stdout)]
    if peaks:
        mx = max(peaks)
        result["true_peak"] = (20.0 * math.log10(mx)) if mx > 0 else None
    summary = r.stderr.rsplit("Summary:", 1)[-1]
    mi = re.search(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", summary)
    if mi:
        result["integrated_lufs"] = _to_float(mi.group(1))
    ml = re.search(r"\bLRA:\s*(-?\d+(?:\.\d+)?)\s*LU", summary)
    if ml:
        result["lra"] = _to_float(ml.group(1))
    mt = re.search(r"\bThreshold:\s*(-?\d+(?:\.\d+)?)\s*LUFS", summary)
    if mt:
        result["threshold"] = _to_float(mt.group(1))

    # astats -> overall raw sample peak (the "digital" peak, dBFS).
    r2 = _run([ffmpeg, "-hide_banner", "-nostats", "-i", path,
               "-af", "astats=measure_perchannel=none:measure_overall=Peak_level",
               "-f", "null", "-"])
    sp = re.search(r"Peak level dB:\s*(-?\d+(?:\.\d+)?|-?inf)", r2.stderr)
    if sp:
        val = sp.group(1)
        result["sample_peak"] = None if "inf" in val else float(val)

    return result


def transcode_measure(ffmpeg, src, codec, bitrate, ext, workdir, extra_args=None,
                      rate=DELIVERY_RATE_44K):
    """Encode `src` to codec/bitrate, then measure the decoded output.

    `extra_args` are extra ffmpeg encoder options (e.g. the AAC rate-control mode).
    `rate` is the service's delivery sample rate: the source is resampled to it
    (soxr) BEFORE encoding, matching what the service does to a hi-res upload.
    Returns the same dict shape as measure().  Raises FFmpegError on encode failure.
    """
    out = os.path.join(workdir, f"enc_{codec}_{bitrate}.{ext}")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
           "-af", f"aresample={rate}:osf=fltp",
           "-c:a", codec, "-b:a", bitrate]
    if extra_args:
        cmd += list(extra_args)
    cmd.append(out)
    enc = _run(cmd)
    if enc.returncode != 0 or not os.path.exists(out):
        raise FFmpegError(f"encode failed for {codec} {bitrate}: {enc.stderr.strip()[:300]}")
    try:
        return measure(ffmpeg, out)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


# --- Apple Music: use the real Apple AAC encoder when available --------------

def _afconvert_path():
    """Path to macOS's built-in afconvert (the real Apple AAC encoder), or None."""
    if sys.platform != "darwin":
        return None
    p = shutil.which("afconvert") or "/usr/bin/afconvert"
    return p if os.path.exists(p) else None


def resolve_aac_encoder(encoders):
    """Choose the best AAC encoder available for ALL AAC tiers.

    ffmpeg's native `aac` overshoots true peak ~2 dB more than real-world AAC
    encoders, so it is only a last resort.  Prefer Apple's CoreAudio AAC (the
    encoder iTunes / Logic / Apple Music use) or Fraunhofer FDK.

    Returns (kind, ffmpeg_codec, note):
      * ("ffmpeg",    "aac_at",     ...) Apple CoreAudio AAC via ffmpeg AudioToolbox
      * ("ffmpeg",    "libfdk_aac", ...) Fraunhofer FDK AAC
      * ("afconvert", None,         ...) Apple CoreAudio AAC via /usr/bin/afconvert
      * ("ffmpeg",    "aac",        ...) ffmpeg native AAC (rough proxy, overshoots)
    """
    if "aac_at" in encoders:
        return "ffmpeg", "aac_at", "Apple AAC"
    if "libfdk_aac" in encoders:
        return "ffmpeg", "libfdk_aac", "FDK AAC"
    if _afconvert_path():
        return "afconvert", None, "Apple AAC"
    return "ffmpeg", "aac", "ffmpeg AAC (proxy)"


def transcode_measure_afconvert(ffmpeg, src, bitrate, workdir, rate=DELIVERY_RATE_44K):
    """Encode with macOS afconvert (real Apple AAC), then measure the decode.

    `rate` is the service delivery rate; the pre-decode wav is resampled to it
    (soxr) so afconvert encodes at the same rate the service would."""
    afc = _afconvert_path()
    if not afc:
        raise FFmpegError("afconvert not available")
    # afconvert wants an uncompressed input; normalize to a temp wav at the
    # delivery sample rate first (soxr).
    wav = os.path.join(workdir, "afc_in.wav")
    dec = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                "-af", f"aresample={rate}",
                "-c:a", "pcm_s24le", wav])
    if dec.returncode != 0 or not os.path.exists(wav):
        raise FFmpegError(f"afconvert pre-decode failed: {dec.stderr.strip()[:200]}")
    out = os.path.join(workdir, f"enc_apple_{bitrate}.m4a")
    bps = str(int(str(bitrate).rstrip("k")) * 1000)
    enc = _run([afc, "-d", "aac", "-f", "m4af", "-b", bps, wav, out])
    try:
        if enc.returncode != 0 or not os.path.exists(out):
            raise FFmpegError(f"afconvert encode failed: {enc.stderr.strip()[:200]}")
        return measure(ffmpeg, out)
    finally:
        for f in (wav, out):
            try:
                os.remove(f)
            except OSError:
                pass


# =============================================================================
#  Per-file analysis orchestration (multi-service)
# =============================================================================

def _run_transcode(ffmpeg, path, tkey, enc_set, strict, workdir):
    """Encode+measure one tier.  Returns its physics entry (no master-relative
    fields yet — overshoot/drift are filled in once the master is measured).

    Safe to run concurrently: each tier writes a uniquely named temp file.
    """
    ts = TRANSCODE_BY_KEY[tkey]
    entry = {"key": tkey, "label": ts.label, "codec": ts.codec,
             "bitrate": ts.bitrate, "decoded_tp": None, "decoded_sample_peak": None,
             "decoded_lufs": None, "overshoot": None, "loudness_drift": None,
             "unity_verdict": PASS, "error": None, "unavailable": False,
             "encoder_used": None}

    if ts.codec == "aac":
        kind, codec, note = resolve_aac_encoder(enc_set)
        entry["encoder_used"] = note
        entry["codec"] = codec or "afconvert"
    else:
        kind, codec = "ffmpeg", ts.codec
        if strict and codec not in enc_set:
            entry.update(unavailable=True, unity_verdict=SKIP,
                         error=f"'{codec}' encoder not available in this ffmpeg build")
            return entry

    try:
        if kind == "afconvert":
            m = transcode_measure_afconvert(ffmpeg, path, ts.bitrate, workdir, rate=ts.rate)
        else:
            # Apple's AAC (aac_at): use constrained VBR — real-world AAC (Logic,
            # Apple Music) is VBR-family, which peaks ~0.7 dB lower than the CBR
            # ffmpeg picks by default, while still respecting the bitrate tier.
            extra = ["-aac_at_mode", "cvbr"] if codec == "aac_at" else None
            m = transcode_measure(ffmpeg, path, codec, ts.bitrate, ts.ext, workdir,
                                  extra_args=extra, rate=ts.rate)
        entry["decoded_tp"] = m["true_peak"]
        entry["decoded_sample_peak"] = m["sample_peak"]
        entry["decoded_lufs"] = m["integrated_lufs"]
        entry["unity_verdict"] = evaluate_peak(m["true_peak"], TP_CEILING_NORMAL)
    except Exception as exc:  # keep going even if one codec fails
        entry["unity_verdict"] = WARN
        entry["error"] = str(exc)
    return entry


def analyze_file(ffmpeg, path, available_encoders=None, progress=None, max_workers=None):
    """Analyze one audio file across every modelled service.

    Each unique lossy tier is transcoded once (all tiers plus the master measure
    run concurrently across CPU cores), then every service is graded against its
    own normalization target and true-peak ceiling.

    `available_encoders`, if given, marks tiers whose encoder is missing as N/A.
    `progress`, if given, is called as progress(name, done, total, label).
    `max_workers` caps concurrency (default: CPU count).
    """
    name = os.path.basename(path)
    enc_set = available_encoders if available_encoders is not None else ffmpeg_encoders(ffmpeg)
    strict = available_encoders is not None

    # --- Measure the master and run every tier concurrently ------------------
    transcodes = {}  # key -> physics result
    total = len(USED_TRANSCODE_KEYS)
    workers = max_workers or min(total + 1, max(2, (os.cpu_count() or 4)))
    with tempfile.TemporaryDirectory(prefix="strmconv_") as workdir:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_master = ex.submit(measure, ffmpeg, path)
            futs = {ex.submit(_run_transcode, ffmpeg, path, k, enc_set, strict, workdir): k
                    for k in USED_TRANSCODE_KEYS}
            done = 0
            for fut in as_completed(futs):
                k = futs[fut]
                transcodes[k] = fut.result()
                done += 1
                if progress:
                    progress(name, done, total, TRANSCODE_BY_KEY[k].label)
            master = fut_master.result()

    integ = master["integrated_lufs"]
    tp = master["true_peak"]

    # Fill in master-relative fields now that the master is measured.
    for e in transcodes.values():
        if e["decoded_tp"] is not None and tp is not None:
            e["overshoot"] = e["decoded_tp"] - tp
        if e["decoded_lufs"] is not None and integ is not None:
            e["loudness_drift"] = e["decoded_lufs"] - integ

    # --- Grade every service against its own target/ceiling ------------------
    services = []
    overall = PASS
    for svc in SERVICES:
        ceiling = effective_ceiling(svc, integ)
        gain = normalization_gain(integ, tp, svc.target, ceiling)
        played = (integ + gain) if integ is not None else None
        master_verdict = evaluate_peak(tp, ceiling)

        tiers = []
        svc_worst = master_verdict   # only PRIMARY tiers roll into the service verdict
        any_clip = False             # PRIMARY tier clips at PLAYBACK (audible, normalization on)
        any_unity_clip = False       # PRIMARY tier exceeds 0 dBFS at unity gain
        info_overshoot = False       # an informational (data-saver) tier exceeds 0 dBFS at unity
        overs = []
        for tkey, ctx in svc.tiers:
            if tkey in LOSSLESS:
                # Bit-exact: decoded == master, no added overshoot.  Always primary.
                is_primary = True
                dtp = tp
                over = 0.0 if tp is not None else None
                ptp = (dtp + gain) if dtp is not None else None
                v = evaluate_tier(dtp, ptp)
                unity_clip = dtp is not None and dtp > CLIP_CEILING
                tiers.append({"key": tkey, "label": LOSSLESS[tkey], "context": ctx,
                              "lossless": True, "primary": True, "decoded_tp": dtp,
                              "sample_peak": master["sample_peak"], "overshoot": over,
                              "playback_tp": ptp, "verdict": v, "error": None,
                              "encoder_used": None, "unity_clip": unity_clip})
            else:
                is_primary = TRANSCODE_BY_KEY[tkey].primary
                tr = transcodes[tkey]
                dtp = tr["decoded_tp"]
                over = tr["overshoot"]
                unity_clip = False
                if tr["unavailable"]:
                    v = SKIP
                    ptp = None
                elif tr["error"]:
                    v = WARN
                    ptp = None
                else:
                    ptp = (dtp + gain) if dtp is not None else None
                    v = evaluate_tier(dtp, ptp)
                    unity_clip = dtp is not None and dtp > CLIP_CEILING
                    if over is not None and is_primary:
                        overs.append(over)
                tiers.append({"key": tkey, "label": tr["label"], "context": ctx,
                              "lossless": False, "primary": is_primary, "decoded_tp": dtp,
                              "sample_peak": tr.get("decoded_sample_peak"), "overshoot": over,
                              "playback_tp": ptp, "verdict": v, "error": tr["error"],
                              "encoder_used": tr.get("encoder_used"), "unity_clip": unity_clip})

            if is_primary:
                if ptp is not None and ptp > CLIP_CEILING:
                    any_clip = True
                if unity_clip:
                    any_unity_clip = True
                svc_worst = worst(svc_worst, v)
            elif unity_clip:
                info_overshoot = True

        services.append({
            "key": svc.key, "name": svc.name, "target": svc.target,
            "ceiling": ceiling, "gain": gain, "played_lufs": played,
            "master_verdict": master_verdict, "verdict": svc_worst,
            "any_clip": any_clip, "any_unity_clip": any_unity_clip,
            "info_overshoot": info_overshoot,
            "max_overshoot": max(overs) if overs else None,
            "note": svc.note, "tiers": tiers,
        })
        overall = worst(overall, svc_worst)

    # --- Cross-service stability summary (primary tiers only for verdicts) ----
    prim_keys = {k for k in USED_TRANSCODE_KEYS if TRANSCODE_BY_KEY[k].primary}
    all_overs = [transcodes[k]["overshoot"] for k in prim_keys
                 if transcodes[k]["overshoot"] is not None]
    any_unity_clip_any = any(transcodes[k]["decoded_tp"] is not None and transcodes[k]["decoded_tp"] > CLIP_CEILING
                             for k in prim_keys)
    any_info_overshoot = any(transcodes[k]["decoded_tp"] is not None and transcodes[k]["decoded_tp"] > CLIP_CEILING
                             for k in USED_TRANSCODE_KEYS if k not in prim_keys)
    any_playback_clip = any(s["any_clip"] for s in services)
    inter_sample = (tp - master["sample_peak"]) if (tp is not None and master["sample_peak"] is not None) else None

    return {
        "file": path,
        "name": name,
        "master": master,
        "master_lufs": integ,
        "master_true_peak": tp,
        "inter_sample_margin": inter_sample,
        "transcodes": list(transcodes.values()),
        "services": services,
        "worst_overshoot": max(all_overs) if all_overs else None,   # primary tiers only
        "any_clip": any_playback_clip,          # primary tier clips at playback
        "any_unity_clip": any_unity_clip_any,    # primary tier exceeds 0 dBFS at unity
        "any_info_overshoot": any_info_overshoot,  # data-saver tier exceeds 0 dBFS (informational)
        # Master sits above the -1 dBTP recommended headroom but still under 0 dBFS:
        # advisory only ("above recommended headroom, but not clipping"), not a verdict.
        "above_recommended": above_recommended(tp, TP_CEILING_NORMAL),
        "overall": overall,
    }


def build_advice(result):
    """Actionable mastering guidance for one result.

    Judged on the PRIMARY streaming tiers only (the quality most listeners get) —
    low-bitrate data-saver tiers are demoted to a non-blocking notice.  Returns
    (level, text) where level is 'safe' | 'warn' | 'fail', or None.
    """
    tp = result["master_true_peak"]
    if tp is None:
        return None

    # Worst decoded true peak at unity among the PRIMARY lossy tiers.
    worst_dec, worst_tier = None, None
    for s in result["services"]:
        for t in s["tiers"]:
            if t["lossless"] or not t.get("primary", True) or t["decoded_tp"] is None:
                continue
            if worst_dec is None or t["decoded_tp"] > worst_dec:
                worst_dec, worst_tier = t["decoded_tp"], t["label"]

    # Notice about informational (data-saver) tiers, appended when the master is fine.
    notice = ""
    if result.get("any_info_overshoot"):
        notice = (" Notice: low-bandwidth fallback streams (e.g. Opus 64k, AAC 128k) show "
                  "overshoot at unity — normal for data-saver tiers and eliminated by loudness "
                  "normalization at playback; no action needed.")

    if tp > CLIP_CEILING:
        return ("fail",
                f"Your master already clips — true peak {tp:+.2f} dBTP, above 0 dBFS. "
                f"Pull its true-peak ceiling below 0 (ideally to −1 dBTP) and re-export before "
                f"anything else.")

    if worst_dec is not None and worst_dec > CLIP_CEILING:
        rec = tp - worst_dec  # master ceiling that brings the worst primary tier to 0 dBFS
        return ("warn",
                f"A primary streaming tier ({worst_tier}) reaches {worst_dec:+.2f} dBTP at unity — "
                f"above 0 dBFS. To keep the main streams under 0 dBFS even with normalization OFF, "
                f"lower your master's true peak to about {rec:.1f} dBTP and re-export. With "
                f"normalization ON (the streaming default) it's turned down and already plays "
                f"safely.{notice}")

    headroom = f", {abs(worst_dec):.2f} dB of headroom to spare" if worst_dec is not None else ""
    if result.get("above_recommended"):
        # Above the -1 dBTP guideline but nothing clips — advisory, not a warning.
        return ("safe",
                f"Above recommended headroom, but not clipping — at {tp:+.2f} dBTP this master sits "
                f"above the −1 dBTP guideline, yet the primary streaming tiers (Ogg Vorbis 320, "
                f"AAC 256, MP3 320, lossless) all stay under 0 dBFS{headroom}. It survives "
                f"conversion intact and plays back safely; pulling down to −1 dBTP is optional, "
                f"not required.{notice}")
    return ("safe",
            f"Safe — the primary streaming tiers (Ogg Vorbis 320, AAC 256, MP3 320, lossless) all "
            f"stay under 0 dBFS{headroom}, comfortably within the −1 dBTP recommended headroom at "
            f"{tp:+.2f} dBTP. This master survives conversion intact; no re-export required.{notice}")


# =============================================================================
#  Self-contained HTML report (no external libraries or CDNs)
# =============================================================================

def _fmt(value, unit="", digits=2):
    if value is None:
        return "&mdash;"
    return f"{value:+.{digits}f}{unit}" if unit == " dB" else f"{value:.{digits}f}{unit}"


def _signed(value, unit=""):
    if value is None:
        return "&mdash;"
    return f"{value:+.2f}{unit}"


def _tp_bar(decoded_tp, verdict):
    """Inline SVG bar showing a decoded true peak on a -3..+1.5 dBTP scale,
    with markers at the -1 dBTP ceiling and the 0 dBFS clip line."""
    lo, hi = -3.0, 1.5
    width = 200
    height = 16

    def x(db):
        db = max(lo, min(hi, db))
        return (db - lo) / (hi - lo) * width

    ceiling_x = x(TP_CEILING_NORMAL)
    clip_x = x(CLIP_CEILING)
    color = COLORS[verdict]

    if decoded_tp is None:
        return '<span style="color:#888">&mdash;</span>'

    val_x = x(decoded_tp)
    bar = min(val_x, width)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'style="vertical-align:middle">'
        f'<rect x="0" y="4" width="{width}" height="8" rx="2" fill="#2a2a2a"/>'
        f'<rect x="0" y="4" width="{bar:.1f}" height="8" rx="2" fill="{color}"/>'
        f'<line x1="{ceiling_x:.1f}" y1="1" x2="{ceiling_x:.1f}" y2="15" '
        f'stroke="#f5a623" stroke-width="1.5" stroke-dasharray="2,1"/>'
        f'<line x1="{clip_x:.1f}" y1="1" x2="{clip_x:.1f}" y2="15" '
        f'stroke="#e0245e" stroke-width="1.5"/>'
        f'</svg>'
    )


def _service_block(svc):
    vc = COLORS[svc["verdict"]]
    gain = svc["gain"]
    gain_dir = "down" if gain < 0 else ("up" if gain > 0 else "no change")

    rows = []
    for t in svc["tiers"]:
        dk = tier_display_key(t)
        tvc = COLORS[dk]
        row_cls = ' class="ds"' if not t.get("primary", True) else ""
        extra = LOSSLESS_TAG if t["lossless"] else ""
        if not t.get("primary", True):
            extra += '<span class="tag ds">data-saver</span>'
        if t.get("encoder_used"):
            extra += f'<span class="tag enc">{html.escape(t["encoder_used"])}</span>'
        if t.get("unity_clip"):
            extra += '<span class="tag unity">unity &gt; 0 dBFS</span>'
        err = f'<div class="err">{html.escape(t["error"])}</div>' if t["error"] else ""
        rows.append(
            f'<tr{row_cls}>'
            f'<td><b>{html.escape(t["label"])}</b>{extra}'
            f'<div class="ctx">{html.escape(t["context"])}</div>{err}</td>'
            f'<td class="num">{_fmt(t.get("sample_peak"), " dBFS")}</td>'
            f'<td class="num">{_fmt(t["decoded_tp"], " dBTP")}</td>'
            f'<td class="num" style="color:{tvc}"><b>{_signed(t["overshoot"], " dB")}</b></td>'
            f'<td class="num">{_fmt(t["playback_tp"], " dBTP")}</td>'
            f'<td>{_tp_bar(t["decoded_tp"], dk)}</td>'
            f'<td><span class="pill" style="background:{tvc}">{LABELS[dk]}</span></td>'
            f'</tr>'
        )

    note = f'<div class="snote">{html.escape(svc["note"])}</div>' if svc["note"] else ""
    return f"""
    <div class="svc">
      <div class="shead">
        <span class="sname">{html.escape(svc["name"])}</span>
        <span class="pill" style="background:{vc}">{LABELS[svc["verdict"]]}</span>
        <span class="starget">target {svc['target']:.0f} LUFS &middot;
          plays ~{_fmt(svc['played_lufs'], ' LUFS')} (gain {_signed(gain, ' dB')}, {gain_dir})
          &middot; ceiling {svc['ceiling']:.0f} dBTP</span>
      </div>{note}
      <table>
        <thead><tr>
          <th>Tier</th><th title="digital / codec peak">Sample pk</th>
          <th title="inter-sample / hardware peak">True peak</th><th>Overshoot</th>
          <th>At playback</th><th>True peak (&minus;1 amber / 0 red)</th><th>Verdict</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """


LOSSLESS_TAG = '<span class="tag">lossless</span>'

# Reliability "sheet" shown near the top of every report so users know how much
# to trust the numbers.  Plain string (no f-string) — safe to embed verbatim.
RELIABILITY_BOX = """
<section class="reliability">
  <h3>How reliable is this?</h3>
  <table>
    <tr><td class="hi">High</td><td>Loudness math (LUFS, normalization gain, "plays at") — deterministic ITU-R BS.1770.</td></tr>
    <tr><td class="hi">High</td><td>Overshoot direction &amp; clipping flags — a real encode&rarr;decode round-trip, not a formula.</td></tr>
    <tr><td class="hi">High</td><td>Relative comparisons (codec vs codec, master vs master; lossless adds nothing).</td></tr>
    <tr><td class="hi">High</td><td>Apple Music on macOS — uses Apple's real AAC encoder (AudioToolbox / afconvert).</td></tr>
    <tr><td class="mid">Approx</td><td>Absolute overshoot magnitude per service — &plusmn;~0.3 dB; ffmpeg encoders are faithful proxies.</td></tr>
    <tr><td class="mid">Approx</td><td>Service parameters (targets, bitrates) — publicly reported and change over time.</td></tr>
  </table>
  <div class="snote">Use it as a relative, conservative pre-delivery check &mdash; not a bit-exact prediction
  of any one service's encoder, and never a substitute for critical listening.</div>
</section>
"""


def _file_section(r):
    m = r["master"]
    verdict = r["overall"]
    badge_color = COLORS[verdict]
    name = html.escape(r["name"])

    pills = "".join(
        f'<span class="spill" style="background:{COLORS[s["verdict"]]}">'
        f'{html.escape(s["name"])} {LABELS[s["verdict"]]}</span>'
        for s in r["services"]
    )
    blocks = "\n".join(_service_block(s) for s in r["services"])

    advice = build_advice(r)
    advice_html = ""
    if advice:
        level, text = advice
        ac = COLORS[{"safe": PASS, "warn": WARN, "fail": FAIL}[level]]
        icon = {"safe": "&#10003;", "warn": "&rarr;", "fail": "&#10007;"}[level]
        advice_html = (f'<div class="advice" style="border-color:{ac}">'
                       f'<span class="aicon" style="color:{ac}">{icon}</span>'
                       f'{html.escape(text)}</div>')

    return f"""
    <section class="file">
      <div class="fhead">
        <h2>{name}</h2>
        <span class="badge" style="background:{badge_color}">{LABELS[verdict]}</span>
      </div>
      {advice_html}
      <div class="grid">
        <div class="card"><div class="k">Integrated loudness</div>
          <div class="v">{_fmt(m['integrated_lufs'], ' LUFS')}</div>
          <div class="sub">measured (BS.1770)</div></div>
        <div class="card"><div class="k">Master true peak</div>
          <div class="v">{_fmt(m['true_peak'], ' dBTP')}</div>
          <div class="sub">4&times; oversampled</div></div>
        <div class="card"><div class="k">Sample peak</div>
          <div class="v">{_fmt(m['sample_peak'], ' dBFS')}</div>
          <div class="sub">inter-sample {_signed(r['inter_sample_margin'], ' dB')}</div></div>
        <div class="card"><div class="k">Worst overshoot</div>
          <div class="v">{_signed(r['worst_overshoot'], ' dB')}</div>
          <div class="sub">across primary tiers</div></div>
        <div class="card"><div class="k">Loudness range</div>
          <div class="v">{_fmt(m['lra'], ' LU')}</div>
          <div class="sub">LRA</div></div>
        <div class="card"><div class="k">Clips at playback?</div>
          <div class="v" style="color:{COLORS[FAIL] if r['any_clip'] else COLORS[PASS]}">
            {'YES' if r['any_clip'] else 'no'}</div>
          <div class="sub">audible with normalization on</div></div>
        <div class="card"><div class="k">Exceeds 0 dBFS at unity?</div>
          <div class="v" style="color:{COLORS[WARN] if r.get('any_unity_clip') else COLORS[PASS]}">
            {'YES' if r.get('any_unity_clip') else 'no'}</div>
          <div class="sub">only if normalization off</div></div>
      </div>
      <div class="spills">{pills}</div>
      {blocks}
    </section>
    """


def build_report(results, out_path, ffmpeg_version="", title="Streaming Conversion Test"):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    n_fail = sum(1 for r in results if r["overall"] == FAIL)
    n_warn = sum(1 for r in results if r["overall"] == WARN)
    n_pass = sum(1 for r in results if r["overall"] == PASS)

    sections = "\n".join(_file_section(r) for r in results)

    css = """
    :root{color-scheme:dark}
    *{box-sizing:border-box}
    body{margin:0;background:#121212;color:#e8e8e8;
      font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
    header{padding:28px 32px;background:linear-gradient(135deg,#1db954,#0d6b30);color:#fff}
    header h1{margin:0 0 4px;font-size:22px}
    header .meta{opacity:.9;font-size:12px}
    .summary{display:flex;gap:12px;padding:18px 32px;flex-wrap:wrap}
    .stat{background:#1e1e1e;border-radius:10px;padding:12px 18px;min-width:90px}
    .stat .n{font-size:22px;font-weight:700}.stat .l{font-size:11px;opacity:.7;text-transform:uppercase;letter-spacing:.5px}
    main{padding:0 32px 40px}
    section.file{background:#181818;border:1px solid #262626;border-radius:14px;padding:20px 22px;margin:18px 0}
    .fhead{display:flex;align-items:center;gap:12px;margin-bottom:14px}
    .fhead h2{margin:0;font-size:17px;word-break:break-all}
    .badge,.pill{color:#fff;font-weight:700;border-radius:20px;padding:3px 12px;font-size:11px;letter-spacing:.5px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
    .card{background:#202020;border-radius:10px;padding:10px 12px}
    .card .k{font-size:11px;opacity:.65;text-transform:uppercase;letter-spacing:.4px}
    .card .v{font-size:18px;font-weight:700;margin:2px 0}
    .card .sub{font-size:11px;opacity:.55}
    .advice{background:#1c1c1c;border-left:4px solid #1db954;border-radius:8px;
      padding:11px 14px;margin:0 0 14px;font-size:13.5px;line-height:1.5}
    .advice .aicon{font-weight:700;margin-right:8px}
    .spills{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 14px}
    .spill{color:#fff;font-weight:700;border-radius:6px;padding:3px 9px;font-size:11px}
    .svc{background:#161616;border:1px solid #242424;border-radius:10px;padding:12px 14px;margin:10px 0}
    .shead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
    .sname{font-size:15px;font-weight:700}
    .starget{font-size:12px;opacity:.6}
    .snote{font-size:11px;opacity:.6;font-style:italic;margin-bottom:6px}
    .tag{display:inline-block;margin-left:6px;font-size:9px;text-transform:uppercase;letter-spacing:.5px;
      background:#2a2a2a;color:#9ad;border-radius:4px;padding:1px 5px;vertical-align:middle}
    .tag.enc{color:#7ddf9f;text-transform:none;letter-spacing:0}
    .tag.unity{color:#f5a623;background:#3a2f1a}
    .tag.ds{color:#9a9a9a;background:#262626}
    tr.ds td{opacity:.6}
    .reliability{margin:6px 32px 4px;background:#161616;border:1px solid #242424;border-radius:12px;padding:14px 18px}
    .reliability h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;opacity:.7}
    .reliability table{width:auto;margin:0;font-size:12.5px}
    .reliability td{border:0;padding:3px 14px 3px 0;vertical-align:top}
    .reliability td.hi{color:#1db954;font-weight:700;white-space:nowrap}
    .reliability td.mid{color:#f5a623;font-weight:700;white-space:nowrap}
    table{width:100%;border-collapse:collapse;margin-top:6px;font-size:13px}
    th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #242424;vertical-align:middle}
    th{font-size:11px;text-transform:uppercase;letter-spacing:.4px;opacity:.6}
    td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
    .ctx{font-size:11px;opacity:.5}
    .err{font-size:11px;color:#e0245e}
    .pill{font-size:10px;padding:2px 9px}
    footer{padding:20px 32px;opacity:.5;font-size:12px;border-top:1px solid #262626}
    a{color:#1db954}
    """

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{css}</style></head>
<body>
<header>
  <h1>Streaming Conversion Test</h1>
  <div class="meta">Per-service transcode stability &amp; true-peak overshoot &middot;
   generated {now} &middot; {html.escape(ffmpeg_version)}</div>
</header>
<div class="summary">
  <div class="stat"><div class="n">{len(results)}</div><div class="l">files</div></div>
  <div class="stat" style="color:{COLORS[PASS]}"><div class="n">{n_pass}</div><div class="l">pass</div></div>
  <div class="stat" style="color:{COLORS[WARN]}"><div class="n">{n_warn}</div><div class="l">warn</div></div>
  <div class="stat" style="color:{COLORS[FAIL]}"><div class="n">{n_fail}</div><div class="l">fail</div></div>
</div>
{RELIABILITY_BOX}
<main>{sections}</main>
<footer>
  <b>The verdict is judged on the PRIMARY streaming tiers</b> (Ogg Vorbis 320,
  AAC 256, MP3 320, lossless — the top streaming quality) at PLAYBACK, with each
  service's loudness normalization on. Tiers tagged
  <span class="tag ds">data-saver</span> are low-bitrate fallbacks shown for information
  only; they never affect the verdict. "Sample pk" is the raw digital peak (dBFS);
  "True peak" is the inter-sample peak (dBTP); "At playback" adds the service's
  normalization gain. A tier marked
  <span class="tag unity">unity &gt; 0 dBFS</span> exceeds full scale in the raw stream
  but is turned down at playback — it only clips if a listener disables normalization,
  or in a non-normalized/downloaded copy. Low-bitrate codecs genuinely overshoot more on
  loud, high-frequency-dense masters; the fix is true-peak headroom in the master.
  Targets are publicly-reported and approximate; ffmpeg encoders are a faithful proxy
  (notably Apple's AAC). Informational — not a substitute for critical listening.
</footer>
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


# =============================================================================
#  Command-line interface
# =============================================================================

AUDIO_EXTS = (".wav", ".wave", ".aif", ".aiff", ".flac")

_ANSI = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m", INFO: "\033[90m"}
_RESET = "\033[0m"


def gather(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in sorted(names):
                    if n.lower().endswith(AUDIO_EXTS):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"warning: not found: {p}", file=sys.stderr)
    return files


def _v(value, unit="", digits=2, signed=False):
    if value is None:
        return "  --  "
    sign = "+" if signed else ""
    return f"{value:{sign}.{digits}f}{unit}"


def print_text(r, color=True):
    def paint(verdict, text):
        return f"{_ANSI[verdict]}{text}{_RESET}" if color else text

    print()
    print(paint(r["overall"], f"=== {r['name']}  [{LABELS[r['overall']]}] ==="))
    m = r["master"]
    print(f"  Master   integrated {_v(m['integrated_lufs'],' LUFS')} · "
          f"true peak {_v(m['true_peak'],' dBTP')} · "
          f"sample {_v(m['sample_peak'],' dBFS')} · "
          f"inter-sample {_v(r['inter_sample_margin'],' dB',signed=True)}")
    print("  Columns: samp = decoded sample peak (digital clip, dBFS) · dec = decoded true "
          "peak (ISP/hardware clip, dBTP)")
    print("           over = codec overshoot · play = true peak after normalization "
          "(what listeners hear)")
    print("  Verdict is judged on PRIMARY tiers at the PLAYBACK peak (normalization on). "
          "'(data-saver)' tiers are")
    print("  informational only. 'unity>0' = stream exceeds 0 dBFS before normalization.")

    skipped = set()
    for s in r["services"]:
        gain = s["gain"]
        gdir = "down" if gain < 0 else ("up" if gain > 0 else "flat")
        head = (f"  {s['name']:<15} {s['target']:>4.0f} LUFS  → plays "
                f"~{_v(s['played_lufs'],' LUFS')} (gain {_v(gain,' dB',signed=True)}, {gdir})   ")
        print(paint(s["verdict"], head + f"[{LABELS[s['verdict']]}]"))
        for t in s["tiers"]:
            tag = (" (lossless)" if t["lossless"]
                   else ("" if t.get("primary", True) else " (data-saver)"))
            enc = f"  · {t['encoder_used']}" if t.get("encoder_used") else ""
            dk = tier_display_key(t)
            line = (f"     {t['label']+tag:<24}"
                    f"samp {_v(t['sample_peak'],'',digits=2):>7} "
                    f"dec {_v(t['decoded_tp'],'',digits=2):>7} "
                    f"over {_v(t['overshoot'],'',digits=2,signed=True):>7} "
                    f"play {_v(t['playback_tp'],'',digits=2):>7}   ")
            mark = "  unity>0" if t.get("unity_clip") else ""
            print(line + paint(dk, LABELS[dk]) + enc + mark)
            if t["verdict"] == SKIP:
                skipped.add(t["label"])

    if r["any_clip"]:
        print(paint(FAIL, "  ! Clips at PLAYBACK on at least one tier — audible even with "
                          "loudness normalization on."))
    elif r.get("any_unity_clip"):
        print(paint(WARN, "  · Some tiers exceed 0 dBFS at unity gain. Safe at playback "
                          "(normalization turns this master down); would clip only if a "
                          "listener disables normalization, or in a non-normalized copy."))
    if skipped:
        print(paint(SKIP, f"  note: {', '.join(sorted(skipped))} skipped — encoder not in this ffmpeg "
                          f"build (run with --setup to install a complete bundled ffmpeg)."))
    advice = build_advice(r)
    if advice:
        level, text = advice
        icon = {"safe": "✓", "warn": "→", "fail": "✗"}[level]
        vmap = {"safe": PASS, "warn": WARN, "fail": FAIL}
        import textwrap
        wrapped = textwrap.fill(text, width=92, initial_indent="  ", subsequent_indent="    ")
        print(paint(vmap[level], f"  {icon} {wrapped.strip()}"))


def run_cli(args):
    ffmpeg, encoders, missing = resolve_ffmpeg(auto_install=not args.no_install,
                                               log=lambda s: print(s, file=sys.stderr))
    if not ffmpeg:
        print("ERROR: ffmpeg not found and could not be installed automatically.\n"
              "Install it (macOS: brew install ffmpeg) or re-run with network access.",
              file=sys.stderr)
        return 2
    if missing:
        print(f"warning: this ffmpeg lacks {', '.join(missing)}; those tiers show as N/A.",
              file=sys.stderr)

    files = gather(args.paths)
    if not files:
        print("No audio files found.", file=sys.stderr)
        return 1

    results = []
    for f in files:
        print(f"Analyzing {os.path.basename(f)} ...", file=sys.stderr)
        results.append(analyze_file(ffmpeg, f, available_encoders=encoders))

    if args.json:
        print(jsonmod.dumps(results, indent=2))
    else:
        for r in results:
            print_text(r, color=not args.no_color)
        print("\nReliability: a relative, conservative pre-delivery check. Loudness math and "
              "clipping\nflags are solid; absolute per-service overshoot is a faithful proxy "
              "(±~0.3 dB).\nApple Music uses its real encoder on macOS. Not a substitute for "
              "critical listening.", file=sys.stderr)

    if args.report:
        build_report(results, args.report, ffmpeg_version(ffmpeg))
        print(f"\nReport written: {args.report}", file=sys.stderr)

    return 1 if any(r["overall"] == FAIL for r in results) else 0


# =============================================================================
#  Drag-and-drop desktop GUI (Tkinter) — imported lazily
# =============================================================================

# --- Design-handoff palette (dark, high-contrast; see design/ handoff) --------
# Tk widget backgrounds can't be translucent, so the design's rgba-over-dark
# fills are pre-blended here into solid hex approximations over the window bg.
DESK      = "#0c0d10"   # desktop behind the window
BG        = "#0f1116"   # window surface
RAIL      = "#0d0f13"   # left rail
TITLEBAR  = "#191c22"

CARD      = "#13151a"   # ~white .015 over bg — cards/meters
CARD2     = "#14161b"   # ~white .02  — tiles, column-header strips
GRP       = "#16181d"   # ~white .028 — service group header
BORDER    = "#202227"   # ~white .07  — card/tile borders
BORDER2   = "#1d1f24"   # ~white .06  — rail edge
HAIR      = "#1a1c21"   # ~white .045 — row hairlines
SEP       = "#2e313a"   # lighter grey — vertical column separators
DASH      = "#33363c"   # ~white .16  — dashed drop-zone border
HOVER     = "#14161b"   # row hover
SEL_BG    = "#101f1c"   # selected files-list row (green tint over rail)

# text ramp
T_PRIMARY = "#f0f2f5"
T_PRIMARY2= "#e6e8ec"
T_SEC     = "#c7ccd4"
T_SEC2    = "#9aa0aa"
T_MUTED   = "#8a909a"
T_FAINT   = "#6b7078"
T_DISABLED= "#5a5f68"

# signal colors
GREEN     = "#35c98d"
GREEN_TXT = "#7fd9b0"
GREEN_DEEP= "#2ea677"
AMBER     = "#e8b45a"
AMBER_BAR = "#e0a53a"
AMBER_MUT = "#b98a3e"
AMBER_TXT = "#d8b877"
RED       = "#e5564e"
RED_VAL   = "#e8635b"
RED_MUT   = "#c86a63"

# GUI verdict rendering (distinct labels from the CLI/report: OK / CLIP).
GUI_VCOLOR = {PASS: GREEN, WARN: AMBER, FAIL: RED_VAL, SKIP: T_FAINT, INFO: T_FAINT}
GUI_VLABEL = {PASS: "OK", WARN: "WARN", FAIL: "CLIP", SKIP: "N/A", INFO: "info"}
# pre-blended pill backgrounds (signal color at ~0.15 over the window bg)
GUI_PILLBG = {PASS: "#152c24", WARN: "#2e2718", FAIL: "#2f1b1e", SKIP: "#191b20", INFO: "#191b20"}

# Kept for any legacy references; the facelift uses the ramp above.
PANEL, FG, MUTED, ACCENT = RAIL, T_PRIMARY2, T_MUTED, GREEN

# Font families to try, best-first; the last is a safe generic fallback.
_UI_FAMILIES  = ["Hanken Grotesk", "Inter", "Helvetica Neue", "Helvetica", "Arial"]
_MONO_FAMILIES = ["JetBrains Mono", "SF Mono", "Menlo", "Monaco", "Consolas",
                  "DejaVu Sans Mono", "Courier New", "Courier"]


def _pick_family(root, candidates):
    """First installed family from `candidates`, else the last (generic)."""
    try:
        import tkinter.font as tkfont
        installed = set(tkfont.families(root))
        for c in candidates:
            if c in installed:
                return c
    except Exception:
        pass
    return candidates[-1]


def _g(value, unit="", signed=False):
    if value is None:
        return "—"
    sign = "+" if signed else ""
    return f"{value:{sign}.2f}{unit}"


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """Draw a rounded rectangle on a Tk canvas (smoothed polygon)."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class App:
    def __init__(self, root, ffmpeg, encoders, missing):
        self.root = root
        self.ffmpeg = ffmpeg
        self.encoders = encoders
        self.missing = missing
        self.results = []
        self.file_rows = []      # custom Files-list row frames (parallel to results)
        self.sel_index = None
        self.hide_datasaver = False   # toggle: hide the low-bitrate data-saver tiers
        import queue
        self.events = queue.Queue()
        self._queue_empty = queue.Empty
        self.busy = False

        # Resolve fonts once (fall back gracefully when the design fonts aren't
        # installed).  self.F(size, weight, mono) returns a Tk font tuple.
        self.ui_family = _pick_family(root, _UI_FAMILIES)
        self.mono_family = _pick_family(root, _MONO_FAMILIES)

        root.title("Streaming Conversion Test")
        root.geometry("1460x860")
        root.minsize(1200, 720)
        root.configure(bg=BG)
        self._build_style()
        self._build_ui()
        self._poll()

    def F(self, size, weight="normal", mono=False):
        return (self.mono_family if mono else self.ui_family, size, weight)

    # ------------------------------------------------------------------ styling
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Slim green progress bar.
        style.configure("Green.Horizontal.TProgressbar",
                        background=GREEN, troughcolor="#0c0e12", bordercolor="#0c0e12",
                        lightcolor=GREEN, darkcolor=GREEN, thickness=7)
        # Dark, minimal vertical scrollbar for the breakdown canvas.
        style.configure("Dark.Vertical.TScrollbar",
                        background=CARD2, troughcolor=BG, bordercolor=BG,
                        arrowcolor=T_FAINT, darkcolor=CARD2, lightcolor=CARD2,
                        borderwidth=0, gripcount=0, arrowsize=12)
        style.map("Dark.Vertical.TScrollbar", background=[("active", BORDER)])

    # ------------------------------------------------------------------- layout
    # ------------------------------------------------------- reusable widgets
    _SB_W = 14   # reserved width for the breakdown scrollbar (header alignment)
    ROWH = 36    # per-service breakdown row height (so column rules span it fully)

    def _bordered(self, parent, bg, border=BORDER):
        """1px-'border' via an outer(border)+inner(bg) frame.  Returns (outer, inner)."""
        outer = tk.Frame(parent, bg=border)
        inner = tk.Frame(outer, bg=bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def _section_label(self, parent, text):
        return tk.Label(parent, text=text.upper(), bg=parent["bg"], fg=T_FAINT,
                        font=self.F(10, "bold", mono=True), anchor="w")

    def _fixed_cell(self, parent, width, text, fg, *, size=12, anchor="e",
                    weight="normal", height=22):
        # Fix the WIDTH (for column alignment) but keep enough HEIGHT for the glyphs:
        # pack_propagate(False) freezes both dims, so height must be set explicitly or
        # the frame collapses and clips the text vertically.
        bg = parent["bg"]
        f = tk.Frame(parent, bg=bg, width=width, height=height)
        f.pack_propagate(False)
        tk.Label(f, text=text, bg=bg, fg=fg, font=self.F(size, weight, mono=True),
                 anchor=anchor).pack(fill="both", expand=True)
        return f

    def _col(self, parent, width, height, sep=True):
        """A fixed-width column cell (breakdown table) with a light grey vertical
        rule on its left edge, packed to the right.  Returns the cell frame."""
        f = tk.Frame(parent, bg=parent["bg"], width=width, height=height)
        f.pack_propagate(False)
        f.pack(side="right", fill="y")
        if sep:
            tk.Frame(f, bg=SEP, width=1).pack(side="left", fill="y")
        return f

    def _num_cell(self, parent, width, height, text, fg, *, size=10):
        """A right-aligned numeric cell with a left rule and margin on both sides."""
        f = self._col(parent, width, height)
        tk.Label(f, text=text, bg=parent["bg"], fg=fg, font=self.F(size, mono=True),
                 anchor="e").pack(side="right", fill="both", expand=True, padx=(8, 11))
        return f

    def _bar(self, parent, headroom):
        """Headroom bar: dB below the 0 dBTP playback ceiling → width + traffic color."""
        bg = parent["bg"]
        w, h = 80, 6
        c = tk.Canvas(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0)
        c.create_rectangle(0, 0, w, h, fill=BORDER, outline="")
        if headroom is not None:
            frac = min(1.0, max(0.07, headroom / 6.0)) if headroom > 0 else 0.07
            col = GREEN if headroom >= 3 else (AMBER_BAR if headroom >= 1.5 else RED)
            c.create_rectangle(0, 0, max(3, int(w * frac)), h, fill=col, outline="")
        return c

    def _round_button(self, parent, text, command, *, font, radius=8,
                      fill=RAIL, border=BORDER, fg=T_SEC2, hover_fill=None, hover_fg=None,
                      disabled_border="#1a1c21", disabled_fg=T_DISABLED,
                      pad_x=16, pad_y=9, min_width=0):
        """A flat, rounded-corner button drawn on a canvas (Tk's native buttons
        can't do rounded corners).  Redraws to its actual size, so it may be
        packed with fill='x'.  The returned canvas gains .set_text/.set_enabled."""
        import tkinter.font as tkfont
        fo = tkfont.Font(root=parent, family=font[0], size=font[1],
                         weight=(font[2] if len(font) > 2 else "normal"))
        w0 = max(min_width, fo.measure(text) + 2 * pad_x)
        h0 = fo.metrics("linespace") + 2 * pad_y
        c = tk.Canvas(parent, width=w0, height=h0, bg=parent["bg"],
                      highlightthickness=0, bd=0, cursor="hand2")
        st = {"text": text, "enabled": True, "hover": False}
        hf, hg = hover_fill or fill, hover_fg or fg

        def draw():
            c.delete("all")
            w, h = (c.winfo_width() or w0), (c.winfo_height() or h0)
            if not st["enabled"]:
                f, b, t = fill, disabled_border, disabled_fg
            elif st["hover"]:
                f, b, t = hf, border, hg
            else:
                f, b, t = fill, border, fg
            _round_rect(c, 1, 1, w - 1, h - 1, radius, fill=f, outline=b, width=1)
            c.create_text(w // 2, h // 2, text=st["text"], fill=t, font=font)

        c.bind("<Button-1>", lambda e: (st["enabled"] and command) and command())
        c.bind("<Enter>", lambda e: (st.update(hover=True), draw()))
        c.bind("<Leave>", lambda e: (st.update(hover=False), draw()))
        c.bind("<Configure>", lambda e: draw())
        c.set_text = lambda t: (st.update(text=t), draw())
        c.set_enabled = lambda on: (st.update(enabled=on),
                                    c.config(cursor="hand2" if on else "arrow"), draw())
        draw()
        return c

    def _checkbox(self, parent, checked, command, size=17):
        """A small rounded checkbox (canvas).  command(is_on) fires on toggle.
        The returned canvas gains .set_checked(on) and .redraw() (redraw picks up
        the current canvas bg, so it survives row selection re-tinting)."""
        c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"],
                      highlightthickness=0, bd=0, cursor="hand2")
        st = {"on": bool(checked)}

        def draw():
            c.delete("all")
            if st["on"]:
                _round_rect(c, 1, 1, size - 1, size - 1, 4, fill="#153027",
                            outline=GREEN, width=1)
                c.create_line(4, size // 2, size // 2 - 1, size - 5, fill=GREEN, width=2)
                c.create_line(size // 2 - 1, size - 5, size - 4, 4, fill=GREEN, width=2)
            else:
                _round_rect(c, 1, 1, size - 1, size - 1, 4, fill=c["bg"],
                            outline=BORDER, width=1)

        def toggle(_e):
            st["on"] = not st["on"]
            draw()
            command(st["on"])

        c.bind("<Button-1>", toggle)
        c.redraw = draw
        c.set_checked = lambda on: (st.update(on=on), draw())
        draw()
        return c

    def _icon_button(self, parent, glyph, command, *, fg=T_FAINT, hover_fg=RED_VAL,
                     size=20, font=None):
        """A borderless glyph button (e.g. a row's remove ✕)."""
        c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"],
                      highlightthickness=0, bd=0, cursor="hand2")
        st = {"hover": False}
        fnt = font or self.F(13)

        def draw():
            c.delete("all")
            c.create_text(size // 2, size // 2, text=glyph,
                          fill=(hover_fg if st["hover"] else fg), font=fnt)

        c.bind("<Button-1>", lambda e: command())
        c.bind("<Enter>", lambda e: (st.update(hover=True), draw()))
        c.bind("<Leave>", lambda e: (st.update(hover=False), draw()))
        c.redraw = draw
        draw()
        return c

    def _attach_tooltip(self, widget, text, *, only_if_truncated=True):
        """Show a small dark tooltip with `text` while hovering `widget`.  When
        only_if_truncated, it appears only if the widget's text doesn't fully fit
        (so full filenames show on hover without redundant tips on short names)."""
        st = {"win": None, "job": None}

        def show():
            st["job"] = None
            if st["win"] or not text:
                return
            if only_if_truncated and widget.winfo_reqwidth() <= widget.winfo_width():
                return
            win = tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            try:
                win.wm_attributes("-topmost", True)
            except tk.TclError:
                pass
            win.wm_geometry(f"+{widget.winfo_pointerx() + 14}+{widget.winfo_pointery() + 18}")
            win.configure(bg=BORDER)
            tk.Label(win, text=text, bg=CARD2, fg=T_PRIMARY2, font=self.F(11),
                     justify="left", padx=9, pady=5).pack(padx=1, pady=1)
            st["win"] = win

        def hide(_e=None):
            if st["job"]:
                widget.after_cancel(st["job"])
                st["job"] = None
            if st["win"]:
                st["win"].destroy()
                st["win"] = None

        widget.bind("<Enter>", lambda e: st.update(job=widget.after(400, show)), add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<Destroy>", hide, add="+")

    def _drag_divider(self, event):
        """Resize the left rail by dragging the divider; the right panel takes the
        rest.  Clamped so neither side collapses."""
        total = self.main.winfo_width()
        new_w = event.x_root - self.main.winfo_rootx()
        new_w = max(360, min(new_w, total - 520))
        self.railwrap.config(width=new_w)

    # ------------------------------------------------------------------- layout
    def _build_ui(self):
        self.root.configure(bg=DESK)
        self.main = main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True)

        # =========================== LEFT RAIL ===============================
        self.railwrap = railwrap = tk.Frame(main, bg=RAIL, width=452)
        railwrap.pack(side="left", fill="y")
        railwrap.pack_propagate(False)   # width is controlled by us / the drag divider
        # Draggable divider — lets the user resize the left column (drag left/right).
        self.divider = tk.Frame(main, bg=BORDER2, width=5, cursor="sb_h_double_arrow")
        self.divider.pack(side="left", fill="y")
        self.divider.bind("<Enter>", lambda e: self.divider.config(bg="#3b3f47"))
        self.divider.bind("<Leave>", lambda e: self.divider.config(bg=BORDER2))
        self.divider.bind("<B1-Motion>", self._drag_divider)
        left = tk.Frame(railwrap, bg=RAIL)
        left.pack(fill="both", expand=True, padx=24, pady=22)

        # 1) Brand block
        tk.Label(left, text="Streaming Conversion Test", bg=RAIL, fg=T_PRIMARY,
                 font=self.F(21, "bold"), anchor="w").pack(fill="x")
        tk.Label(left, text="True-peak overshoot & loudness, per service",
                 bg=RAIL, fg=T_SEC2, font=self.F(13), anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(left, text="Spotify · Apple · YouTube · Amazon · Tidal · Deezer · SoundCloud",
                 bg=RAIL, fg=T_FAINT, font=self.F(11), anchor="w",
                 wraplength=404, justify="left").pack(fill="x", pady=(3, 0))

        # 2) ffmpeg status (dot + mono text)
        self.status = tk.Label(left, bg=RAIL, anchor="w", font=self.F(12, mono=True),
                               wraplength=404, justify="left")
        self.status.pack(fill="x", pady=(14, 0))
        self._refresh_ffmpeg_status()

        # 3) Drop zone (dashed canvas)
        self.drop = tk.Canvas(left, height=86, bg=RAIL, highlightthickness=0, bd=0,
                              cursor="hand2")
        self.drop.pack(fill="x", pady=(14, 0))
        self.drop.bind("<Configure>", lambda e: self._paint_drop())
        self.drop.bind("<Button-1>", lambda e: self.add_files())
        if _HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)

        # 4) Action buttons
        btns = tk.Frame(left, bg=RAIL)
        btns.pack(fill="x", pady=(14, 0))
        self.add_btn = self._round_button(
            btns, "Add files…", self.add_files, font=self.F(13, "bold"), min_width=150,
            fill="#122723", border="#1b503e", fg=GREEN_TXT,
            hover_fill="#173a2e", hover_fg="#8fe6c0")
        self.add_btn.pack(side="left", fill="x", expand=True)
        self.clear_btn = self._round_button(
            btns, "Clear", self.clear, font=self.F(13),
            fill=RAIL, border=BORDER, fg=T_SEC2, hover_fill=CARD2, hover_fg=T_PRIMARY2)
        self.clear_btn.pack(side="left", padx=(8, 0))
        self.report_btn = self._round_button(
            btns, "Report", self.save_report, font=self.F(13),
            fill=RAIL, border=BORDER, fg=T_SEC2, hover_fill=CARD2, hover_fg=T_PRIMARY2)
        self.report_btn.pack(side="left", padx=(8, 0))
        self._set_buttons_enabled(False)

        # 5) Progress row
        prow = tk.Frame(left, bg=RAIL)
        prow.pack(fill="x", pady=(14, 0))
        self.progress = ttk.Progressbar(prow, mode="determinate",
                                        style="Green.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.prog_label = tk.Label(prow, text="Ready", bg=RAIL, fg=T_SEC2,
                                   anchor="e", font=self.F(12, mono=True))
        self.prog_label.pack(side="left", padx=(10, 0))

        # 7) Master meters (pinned to the bottom of the rail)
        mwrap = tk.Frame(left, bg=RAIL)
        mwrap.pack(side="bottom", fill="x", pady=(16, 0))
        self._section_label(mwrap, "Master meters · selected file").pack(fill="x")
        mcard_outer, self.meters_card = self._bordered(mwrap, CARD)
        mcard_outer.pack(fill="x", pady=(6, 0))
        self._clear_meters()

        # 6) Files list (fills the middle, scrolls)
        fhead = tk.Frame(left, bg=RAIL)
        fhead.pack(fill="x", pady=(16, 0))
        self._section_label(fhead, "Files").pack(side="left")
        self.files_count = tk.Label(fhead, text="0 loaded", bg=RAIL, fg=T_FAINT,
                                    font=self.F(10, mono=True))
        self.files_count.pack(side="right")

        flist_outer, flist = self._bordered(left, RAIL)
        flist_outer.pack(fill="both", expand=True, pady=(6, 0))
        # column header strip
        fcols = tk.Frame(flist, bg=CARD2)
        fcols.pack(fill="x")
        tk.Frame(fcols, bg=CARD2, width=28).pack(side="right")   # over the remove ✕
        for text, w in (("Verdict", 52), ("Pk", 50), ("LUFS", 60)):
            self._fixed_cell(fcols, w, text.upper(), T_FAINT, size=9).pack(side="right")
        tk.Frame(fcols, bg=CARD2, width=26).pack(side="left")    # over the checkbox
        tk.Label(fcols, text="FILE", bg=CARD2, fg=T_FAINT, font=self.F(9, mono=True),
                 anchor="w").pack(side="left", fill="x", expand=True, padx=(8, 0))
        # scrollable body
        fbody = tk.Frame(flist, bg=RAIL)
        fbody.pack(fill="both", expand=True)
        fcanvas = tk.Canvas(fbody, bg=RAIL, highlightthickness=0, bd=0)
        fsb = ttk.Scrollbar(fbody, orient="vertical", command=fcanvas.yview,
                            style="Dark.Vertical.TScrollbar")
        fcanvas.configure(yscrollcommand=fsb.set)
        fsb.pack(side="right", fill="y")
        fcanvas.pack(side="left", fill="both", expand=True)
        self.files_body = tk.Frame(fcanvas, bg=RAIL)
        self._fwin = fcanvas.create_window((0, 0), window=self.files_body, anchor="nw")
        self.files_body.bind("<Configure>",
                             lambda e: fcanvas.configure(scrollregion=fcanvas.bbox("all")))
        fcanvas.bind("<Configure>", lambda e: fcanvas.itemconfigure(self._fwin, width=e.width))
        self._bind_wheel(fcanvas)

        # =========================== RIGHT PANEL =============================
        right = tk.Frame(main, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=24, pady=22)

        # 1) Summary tiles
        self.tiles = tk.Frame(right, bg=BG)
        self.tiles.pack(fill="x")
        for i in range(5):
            self.tiles.grid_columnconfigure(i, weight=1, uniform="tiles")
        self._render_tiles(None)

        # 2) Per-service breakdown card
        bcard_outer, bcard = self._bordered(right, CARD, BORDER)
        bcard_outer.pack(fill="both", expand=True, pady=(16, 0))
        bhead = tk.Frame(bcard, bg=CARD)
        bhead.pack(fill="x", padx=18, pady=(12, 8))
        tk.Label(bhead, text="PER-SERVICE BREAKDOWN", bg=CARD, fg=T_MUTED,
                 font=self.F(11, "bold", mono=True), anchor="w").pack(side="left")
        self.ds_toggle = self._round_button(
            bhead, "Hide data-saver tiers", self._toggle_datasaver, font=self.F(11, "bold"),
            radius=7, pad_x=14, pad_y=6, fill="#122723", border="#1b503e", fg=GREEN_TXT,
            hover_fill="#173a2e", hover_fg="#8fe6c0")
        self.ds_toggle.pack(side="left", padx=(14, 0))
        tk.Label(bhead, text="bar = headroom to the 0 dBTP ceiling at playback",
                 bg=CARD, fg=T_FAINT, font=self.F(11), anchor="e").pack(side="right")
        # column header strip
        chead = tk.Frame(bcard, bg=CARD2)
        chead.pack(fill="x", padx=(0, self._SB_W))
        for text, w in (("Verdict", 84), ("At playback", 162), ("Overshoot", 92),
                        ("True dBTP", 100), ("Sample dBFS", 100)):
            self._num_cell(chead, w, 28, text.upper(), T_FAINT, size=9)
        tk.Label(chead, text="SERVICE / TIER", bg=CARD2, fg=T_FAINT,
                 font=self.F(9, mono=True), anchor="w").pack(side="left", fill="x",
                                                             expand=True, padx=(18, 0))
        # scrollable body
        dbody = tk.Frame(bcard, bg=CARD)
        dbody.pack(fill="both", expand=True)
        dcanvas = tk.Canvas(dbody, bg=CARD, highlightthickness=0, bd=0)
        dsb = ttk.Scrollbar(dbody, orient="vertical", command=dcanvas.yview,
                            style="Dark.Vertical.TScrollbar")
        dcanvas.configure(yscrollcommand=dsb.set)
        dsb.pack(side="right", fill="y")
        dcanvas.pack(side="left", fill="both", expand=True)
        self.detail_body = tk.Frame(dcanvas, bg=CARD)
        self._dwin = dcanvas.create_window((0, 0), window=self.detail_body, anchor="nw")
        self.detail_body.bind("<Configure>",
                             lambda e: dcanvas.configure(scrollregion=dcanvas.bbox("all")))
        dcanvas.bind("<Configure>", lambda e: dcanvas.itemconfigure(self._dwin, width=e.width))
        self._bind_wheel(dcanvas)
        self._empty_detail()

        # 3) Footnote
        foot = tk.Label(right,
                 text="Sample dBFS = digital/codec peak · True dBTP = inter-sample (hardware) peak.  "
                      "Verdict = true peak at PLAYBACK (normalization on, the default): a loud master "
                      "whose stream exceeds 0 dBFS at unity but is turned down at playback is a WARN, "
                      "not a clip.  'data-saver' tiers (low-bitrate fallbacks) are informational and "
                      "don't affect the verdict.  At playback = decoded TP + this service's gain.",
                 bg=BG, fg=T_FAINT, font=self.F(11), anchor="w", justify="left")
        foot.pack(fill="x", pady=(12, 0))
        self._wrap_on_resize(foot)

        # 4) Advisory callout
        self.advice_outer, self.advice_card = self._bordered(right, CARD, BORDER)
        self.advice_outer.pack(fill="x", pady=(12, 0))
        self.advice_icon = tk.Label(self.advice_card, text="", bg=CARD, fg=AMBER,
                                    font=self.F(15, "bold"), anchor="nw")
        self.advice_icon.pack(side="left", padx=(16, 10), pady=14)
        self.advice = tk.Label(self.advice_card, text="", bg=CARD, fg=AMBER_TXT, anchor="w",
                               justify="left", font=self.F(12), wraplength=720)
        self.advice.pack(side="left", fill="x", expand=True, padx=(0, 16), pady=14)
        self._wrap_on_resize(self.advice)
        self.advice_outer.pack_forget()

    def _wrap_on_resize(self, label):
        """Keep a wrapping label's wraplength synced to its own width (no layout loop —
        the label fills x, so setting wraplength changes only its height)."""
        label.bind("<Configure>",
                   lambda e: e.width > 20 and label.config(wraplength=e.width - 6))

    # ------------------------------------------------------------- wheel + drop
    def _bind_wheel(self, canvas):
        def scroll(delta):
            canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        canvas.bind("<Enter>", lambda e: (
            canvas.bind_all("<MouseWheel>", lambda ev: scroll(ev.delta)),
            canvas.bind_all("<Button-4>", lambda ev: scroll(1)),
            canvas.bind_all("<Button-5>", lambda ev: scroll(-1))))
        canvas.bind("<Leave>", lambda e: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>")))

    def _paint_drop(self):
        c = self.drop
        c.delete("all")
        w = c.winfo_width() or 400
        h = int(c["height"])
        _round_rect(c, 1, 1, w - 1, h - 1, 11, outline=DASH, dash=(4, 3), width=1.5)
        # icon tile
        _round_rect(c, 18, h // 2 - 19, 56, h // 2 + 19, 9, fill="#132a22", outline="")
        c.create_text(37, h // 2, text="↓", fill=GREEN, font=self.F(18, "bold"))
        title = "Drop master files here" if _HAS_DND else "Click to add master files"
        c.create_text(72, h // 2 - 9, text=title, fill=T_PRIMARY2, anchor="w",
                      font=self.F(15, "bold"))
        c.create_text(72, h // 2 + 11,
                      text=".wav .aiff .flac · folders scanned recursively",
                      fill=T_FAINT, anchor="w", font=self.F(11))

    def _set_buttons_enabled(self, on):
        for b in (getattr(self, "clear_btn", None), getattr(self, "report_btn", None)):
            if b is not None:
                b.set_enabled(on)

    def _refresh_ffmpeg_status(self):
        if self.ffmpeg and not self.missing:
            self.status.config(text=f"●  ffmpeg ready · {ffmpeg_version(self.ffmpeg)}", fg=GREEN_TXT)
        elif self.ffmpeg and self.missing:
            self.status.config(
                text=f"●  ffmpeg found, missing {', '.join(self.missing)} — those tiers show N/A. "
                     f"Re-run with --setup to enable all codecs.",
                fg=AMBER)
        else:
            self.status.config(
                text="●  ffmpeg not found — install it (macOS: brew install ffmpeg), then reopen.",
                fg=RED_VAL)

    # -------------------------------------------------------------- file intake
    def _on_drop(self, event):
        self.enqueue_paths(self.root.tk.splitlist(event.data))

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select master files",
            filetypes=[("Audio", "*.wav *.wave *.aif *.aiff *.flac"), ("All files", "*.*")])
        if paths:
            self.enqueue_paths(paths)

    def enqueue_paths(self, paths):
        if not self.ffmpeg:
            messagebox.showerror("ffmpeg required",
                                 "ffmpeg was not found on this system.\n\n"
                                 "Install it and reopen the app:\n  macOS:  brew install ffmpeg")
            return
        files = self._expand(paths)
        if not files:
            messagebox.showinfo("No audio", "No .wav/.aiff/.flac files were found in that drop.")
            return
        if self.busy:
            messagebox.showinfo("Busy", "Still analyzing — please wait for the current batch.")
            return
        self._analyze(files)

    def _expand(self, paths):
        files = []
        for p in paths:
            p = p.strip()
            if os.path.isdir(p):
                for root, _, names in os.walk(p):
                    for n in sorted(names):
                        if n.lower().endswith(AUDIO_EXTS):
                            files.append(os.path.join(root, n))
            elif os.path.isfile(p) and p.lower().endswith(AUDIO_EXTS):
                files.append(p)
        return files

    # ------------------------------------------------------------- analysis run
    def _analyze(self, files):
        self.busy = True
        self.progress.config(maximum=len(files) * len(USED_TRANSCODE_KEYS), value=0)
        self.prog_label.config(text=f"Analyzing {len(files)} file(s)…")
        import threading
        threading.Thread(target=self._worker, args=(files,), daemon=True).start()

    def _worker(self, files):
        step = 0
        total = len(files) * len(USED_TRANSCODE_KEYS)
        for f in files:
            def progress(name, i, n, label, base=step):
                self.events.put(("progress", base + i, total, f"{name}: {label}"))
            try:
                result = analyze_file(self.ffmpeg, f, available_encoders=self.encoders,
                                      progress=progress)
                self.events.put(("result", result))
            except Exception as exc:
                self.events.put(("error", f, str(exc)))
            step += len(USED_TRANSCODE_KEYS)
        self.events.put(("done", None))

    def _poll(self):
        try:
            while True:
                kind, *payload = self.events.get_nowait()
                if kind == "progress":
                    value, total, text = payload
                    self.progress.config(value=value)
                    self.prog_label.config(text=text)
                elif kind == "result":
                    self._add_result(payload[0])
                elif kind == "error":
                    f, msg = payload
                    self.prog_label.config(text=f"Error on {os.path.basename(f)}: {msg}")
                elif kind == "done":
                    self.busy = False
                    self.progress.config(value=self.progress["maximum"])
                    self.prog_label.config(text="Done.")
        except self._queue_empty:
            pass
        self.root.after(80, self._poll)

    def _add_result(self, r):
        r.setdefault("_include", True)
        self.results.append(r)
        self._add_file_row(r, len(self.results) - 1)
        self.files_count.config(text=f"{len(self.results)} loaded")
        self._set_buttons_enabled(True)
        self._select_file(len(self.results) - 1)

    # -------------------------------------------------------------- files list
    def _add_file_row(self, r, index):
        if index > 0:
            tk.Frame(self.files_body, bg=HAIR, height=1).pack(fill="x")
        m = r["master"]
        row = tk.Frame(self.files_body, bg=RAIL, cursor="hand2")
        row.pack(fill="x")
        accent = tk.Frame(row, bg=RAIL, width=2)
        accent.pack(side="left", fill="y")
        inner = tk.Frame(row, bg=RAIL)
        inner.pack(side="left", fill="x", expand=True, padx=(8, 10), pady=8)
        # include-in-report checkbox (left)
        chk = self._checkbox(inner, r.get("_include", True),
                             lambda on, i=index: self._set_include(i, on))
        chk._no_select = True
        chk.pack(side="left", padx=(0, 9))
        # remove ✕ (far right)
        rm = self._icon_button(inner, "✕", lambda i=index: self._remove_file(i))
        rm._no_select = True
        rm.pack(side="right", padx=(8, 0))
        v = r["overall"]
        self._fixed_cell(inner, 52, GUI_VLABEL[v], GUI_VCOLOR[v],
                         size=11, weight="bold").pack(side="right")
        self._fixed_cell(inner, 50, _g(m["true_peak"]), T_SEC2, size=12).pack(side="right")
        self._fixed_cell(inner, 60, _g(m["integrated_lufs"]), T_SEC, size=12).pack(side="right")
        name_lbl = tk.Label(inner, text=r["name"], bg=RAIL, fg=T_PRIMARY2,
                            font=self.F(13), anchor="w")
        name_lbl.pack(side="left", fill="x", expand=True)
        self._attach_tooltip(name_lbl, r["name"])
        self._bind_click(row, index)
        self.file_rows.append({"row": row, "accent": accent, "check": chk})

    def _bind_click(self, widget, index):
        # Skip interactive controls (checkbox, remove ✕) so their own bindings win.
        if getattr(widget, "_no_select", False):
            return
        widget.bind("<Button-1>", lambda e, i=index: self._select_file(i))
        for c in widget.winfo_children():
            self._bind_click(c, index)

    def _recolor(self, widget, bg):
        try:
            widget.config(bg=bg)
        except tk.TclError:
            pass
        for c in widget.winfo_children():
            self._recolor(c, bg)

    def _select_file(self, index):
        if not (0 <= index < len(self.results)):
            return
        self.sel_index = index
        for i, entry in enumerate(self.file_rows):
            sel = (i == index)
            self._recolor(entry["row"], SEL_BG if sel else RAIL)
            entry["accent"].config(bg=GREEN if sel else RAIL)
            entry["check"].redraw()   # match the checkbox fill to the new row tint
        self._show_detail(self.results[index])

    def _set_include(self, index, on):
        if 0 <= index < len(self.results):
            self.results[index]["_include"] = on

    def _remove_file(self, index):
        if self.busy or not (0 <= index < len(self.results)):
            return
        del self.results[index]
        if not self.results:
            self.sel_index = None
        elif self.sel_index is not None and index < self.sel_index:
            self.sel_index -= 1
        elif self.sel_index == index:
            self.sel_index = min(index, len(self.results) - 1)
        self._rebuild_file_list()

    def _rebuild_file_list(self):
        for w in self.files_body.winfo_children():
            w.destroy()
        self.file_rows = []
        for i, r in enumerate(self.results):
            self._add_file_row(r, i)
        self.files_count.config(text=f"{len(self.results)} loaded")
        if self.results:
            if self.sel_index is None:
                self.sel_index = 0
            self._set_buttons_enabled(True)
            self._select_file(self.sel_index)
        else:
            self.sel_index = None
            self._render_tiles(None)
            self._empty_detail()
            self._clear_meters()
            self.advice_outer.pack_forget()
            self._set_buttons_enabled(False)

    # ----------------------------------------------------------------- detail
    def _show_detail(self, r):
        self._render_tiles(r)
        self._render_breakdown(r)
        self._render_meters(r)
        self._render_advice(r)

    def _tile(self, parent, label, value, sub="", *, border=BORDER, bg=CARD2,
              label_fg=T_FAINT, value_fg=T_PRIMARY2):
        outer, inner = self._bordered(parent, bg, border)
        pad = tk.Frame(inner, bg=bg)
        pad.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(pad, text=label.upper(), bg=bg, fg=label_fg,
                 font=self.F(10, "bold", mono=True), anchor="w").pack(fill="x")
        tk.Label(pad, text=value, bg=bg, fg=value_fg,
                 font=self.F(22, "bold", mono=True), anchor="w").pack(fill="x", pady=(4, 0))
        tk.Label(pad, text=sub, bg=bg, fg=T_FAINT, font=self.F(9, mono=True),
                 anchor="w").pack(fill="x")
        return outer

    def _summary_stats(self, r):
        labels = set()
        warnings = 0
        tightest = tightest_svc = None      # smallest headroom at playback (primary)
        worst_os = worst_os_tier = None     # largest overshoot across all tiers (info)
        for s in r["services"]:
            for t in s["tiers"]:
                primary = t.get("primary", True)
                labels.add(t["label"])
                if primary and t["verdict"] == WARN:
                    warnings += 1
                if primary and t["playback_tp"] is not None:
                    head = -t["playback_tp"]
                    if tightest is None or head < tightest:
                        tightest, tightest_svc = head, s["name"]
                # Worst overshoot tracks what's VISIBLE: skip data-saver tiers when
                # they're hidden, so the tile matches the breakdown on screen.
                if self.hide_datasaver and not primary:
                    continue
                if t["overshoot"] is not None and (worst_os is None or t["overshoot"] > worst_os):
                    worst_os, worst_os_tier = t["overshoot"], t["label"]
        return dict(services=len(r["services"]), formats=len(labels), warnings=warnings,
                    tightest=tightest, tightest_svc=tightest_svc,
                    worst_os=worst_os, worst_os_tier=worst_os_tier)

    def _render_tiles(self, r):
        for w in self.tiles.winfo_children():
            w.destroy()
        if r is None:
            tiles = [(l, "—", "", BORDER, CARD2, T_FAINT, T_SEC2)
                     for l in ("Services", "Formats", "Warnings", "Tightest", "Worst OS")]
        else:
            st = self._summary_stats(r)
            tiles = [("Services", str(st["services"]), "", BORDER, CARD2, T_FAINT, T_PRIMARY2),
                     ("Formats", str(st["formats"]), "", BORDER, CARD2, T_FAINT, T_PRIMARY2)]
            if st["warnings"] > 0:
                tiles.append(("Warnings", str(st["warnings"]), "",
                              "#4a3a20", "#211d15", AMBER_MUT, AMBER))
            else:
                tiles.append(("Warnings", "0", "", BORDER, CARD2, T_FAINT, GREEN))
            if st["tightest"] is not None:
                h = st["tightest"]
                col = GREEN if h >= 3 else (AMBER if h >= 1.5 else RED_VAL)
                bd = "#194537" if h >= 3 else ("#4a3a20" if h >= 1.5 else "#4a2426")
                bg = "#12201d" if h >= 3 else ("#211d15" if h >= 1.5 else "#20161a")
                lf = GREEN if h >= 3 else (AMBER_MUT if h >= 1.5 else RED_MUT)
                tiles.append(("Tightest", f"{-h:+.2f}", f"dBTP · {st['tightest_svc']}",
                              bd, bg, lf, col))
            else:
                tiles.append(("Tightest", "—", "", BORDER, CARD2, T_FAINT, T_SEC2))
            if st["worst_os"] is not None:
                vf = AMBER if st["worst_os"] > 0 else GREEN
                tiles.append(("Worst OS", f"{st['worst_os']:+.2f}",
                              f"dB · {st['worst_os_tier']}", BORDER, CARD2, T_FAINT, vf))
            else:
                tiles.append(("Worst OS", "—", "", BORDER, CARD2, T_FAINT, T_SEC2))
        for i, (l, v, sub, bd, bg, lf, vf) in enumerate(tiles):
            self._tile(self.tiles, l, v, sub, border=bd, bg=bg, label_fg=lf,
                       value_fg=vf).grid(row=0, column=i, sticky="nsew",
                                         padx=(0 if i == 0 else 6, 0 if i == 4 else 6))

    def _render_breakdown(self, r):
        body = self.detail_body
        for w in body.winfo_children():
            w.destroy()
        for s in r["services"]:
            tiers = [t for t in s["tiers"]
                     if t.get("primary", True) or not self.hide_datasaver]
            self._service_group(body, s)
            for i, t in enumerate(tiers):
                self._tier_row(body, t, first=(i == 0))

    def _toggle_datasaver(self):
        self.hide_datasaver = not self.hide_datasaver
        self.ds_toggle.set_text(
            "Show data-saver tiers" if self.hide_datasaver else "Hide data-saver tiers")
        if self.sel_index is not None and 0 <= self.sel_index < len(self.results):
            self._render_tiles(self.results[self.sel_index])   # Worst OS follows the toggle
            self._render_breakdown(self.results[self.sel_index])

    def _service_group(self, parent, s):
        row = tk.Frame(parent, bg=GRP)
        row.pack(fill="x")
        inner = tk.Frame(row, bg=GRP)
        inner.pack(fill="x", padx=18, pady=6)
        v = s["verdict"]
        tk.Label(inner, text=GUI_VLABEL[v], bg=GRP, fg=GUI_VCOLOR[v],
                 font=self.F(10, "bold", mono=True)).pack(side="right", padx=(0, self._SB_W))
        tk.Label(inner, text="●", bg=GRP, fg=GUI_VCOLOR[v], font=self.F(9)).pack(side="left")
        tk.Label(inner, text=s["name"], bg=GRP, fg=T_PRIMARY,
                 font=self.F(13, "bold")).pack(side="left", padx=(8, 10))
        gain = s["gain"]
        if gain is None or abs(gain) < 0.05:
            move = "no change"
        else:
            move = f"turned {'down' if gain < 0 else 'up'} {abs(gain):.1f} dB"
        tk.Label(inner,
                 text=f"normalizes to {s['target']:.0f} LUFS · {move} → plays at "
                      f"{_g(s['played_lufs'], ' LUFS')}",
                 bg=GRP, fg=T_MUTED, font=self.F(10, mono=True)).pack(side="left")

    def _tier_row(self, parent, t, first=False):
        tk.Frame(parent, bg=HAIR, height=1).pack(fill="x")
        row = tk.Frame(parent, bg=CARD, height=self.ROWH)
        row.pack(fill="x")
        row.pack_propagate(False)   # freeze height so the column rules span the full row
        dk = tier_display_key(t)
        info = (dk == INFO)

        vcell = self._col(row, 84, self.ROWH)
        if info:
            tk.Label(vcell, text="info", bg=CARD, fg=T_FAINT,
                     font=self.F(9, mono=True), anchor="e").pack(side="right", padx=(0, 16))
        else:
            v = t["verdict"]
            tk.Label(vcell, text=GUI_VLABEL[v], bg=GUI_PILLBG[v], fg=GUI_VCOLOR[v],
                     font=self.F(9, "bold", mono=True), padx=7, pady=2).pack(
                         side="right", padx=(0, 16))

        pcell = self._col(row, 162, self.ROWH)
        tk.Label(pcell, text=_g(t["playback_tp"]), bg=CARD, fg=T_SEC,
                 font=self.F(10, mono=True), width=7, anchor="e").pack(side="right", padx=(0, 11))
        head = (-t["playback_tp"]) if t["playback_tp"] is not None else None
        self._bar(pcell, head).pack(side="right", padx=(8, 6))

        dtp, sp = t["decoded_tp"], t.get("sample_peak")
        self._num_cell(row, 92, self.ROWH, _g(t["overshoot"], " dB", signed=True), T_SEC2)
        self._num_cell(row, 100, self.ROWH, _g(dtp, " dBTP"),
                       AMBER if (dtp is not None and dtp >= 0) else T_SEC2)
        self._num_cell(row, 100, self.ROWH, _g(sp, " dBFS"),
                       AMBER if (sp is not None and sp >= 0) else T_SEC2)

        note = (" · lossless" if t["lossless"]
                else (f" · {t['encoder_used']}" if t.get("encoder_used")
                      else (" · data-saver" if not t.get("primary", True) else "")))
        nf = tk.Frame(row, bg=CARD, height=self.ROWH)
        nf.pack(side="left", fill="both", expand=True)
        ninner = tk.Frame(nf, bg=CARD)
        ninner.pack(side="left", fill="both", expand=True, padx=(18, 8))
        tk.Label(ninner, text=t["label"], bg=CARD, fg=(T_FAINT if info else T_SEC),
                 font=self.F(12)).pack(side="left")
        if note:
            tk.Label(ninner, text=note, bg=CARD, fg=T_FAINT, font=self.F(10)).pack(side="left")

    def _render_meters(self, r):
        card = self.meters_card
        for w in card.winfo_children():
            w.destroy()
        pad = tk.Frame(card, bg=CARD)
        pad.pack(fill="x", padx=14, pady=12)
        m = r["master"]
        wos = r["worst_overshoot"]
        rows = [("Integrated", _g(m["integrated_lufs"], " LUFS"), T_PRIMARY2),
                ("True peak", _g(m["true_peak"], " dBTP"), T_PRIMARY2),
                ("Sample peak", _g(m["sample_peak"], " dBFS"), T_PRIMARY2),
                ("Inter-sample", _g(r["inter_sample_margin"], " dB", signed=True), T_PRIMARY2),
                ("LRA", _g(m["lra"], " LU"), T_PRIMARY2),
                ("Worst OS", _g(wos, " dB", signed=True),
                 AMBER if (wos is not None and wos > 0) else T_PRIMARY2)]
        for label, val, vf in rows:
            rr = tk.Frame(pad, bg=CARD)
            rr.pack(fill="x", pady=1)
            tk.Label(rr, text=label, bg=CARD, fg=T_MUTED,
                     font=self.F(12, mono=True)).pack(side="left")
            tk.Label(rr, text=val, bg=CARD, fg=vf,
                     font=self.F(12, mono=True)).pack(side="right")
        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=8)
        stat = tk.Frame(pad, bg=CARD)
        stat.pack(fill="x")
        clip, unity = r["any_clip"], r.get("any_unity_clip")
        tk.Label(stat, text="Clips @ play ", bg=CARD, fg=T_MUTED,
                 font=self.F(12, mono=True)).pack(side="left")
        tk.Label(stat, text="YES" if clip else "no", bg=CARD,
                 fg=RED_VAL if clip else GREEN,
                 font=self.F(12, "bold", mono=True)).pack(side="left")
        tk.Label(stat, text="    ·    Unity >0 ", bg=CARD, fg=T_MUTED,
                 font=self.F(12, mono=True)).pack(side="left")
        tk.Label(stat, text="YES" if unity else "no", bg=CARD,
                 fg=AMBER if unity else GREEN,
                 font=self.F(12, "bold", mono=True)).pack(side="left")

    def _clear_meters(self):
        for w in self.meters_card.winfo_children():
            w.destroy()
        tk.Label(self.meters_card, text="Select a file to see its master meters.",
                 bg=CARD, fg=T_FAINT, font=self.F(12), anchor="w").pack(
                     fill="x", padx=14, pady=16)

    def _render_advice(self, r):
        advice = build_advice(r)
        if not advice:
            self.advice_outer.pack_forget()
            return
        level, text = advice
        icon = {"safe": "✓", "warn": "→", "fail": "✗"}[level]
        col = {"safe": GREEN, "warn": AMBER, "fail": RED_VAL}[level]
        border = {"safe": "#25463a", "warn": "#4a3a20", "fail": "#4a2426"}[level]
        txt = {"safe": GREEN_TXT, "warn": AMBER_TXT, "fail": "#e6a39d"}[level]
        self.advice_outer.config(bg=border)
        self.advice_icon.config(text=icon, fg=col)
        self.advice.config(text=text, fg=txt)
        self.advice_outer.pack(fill="x", pady=(12, 0))

    def _empty_detail(self):
        for w in self.detail_body.winfo_children():
            w.destroy()
        tk.Label(self.detail_body, text="Drop a master file to see the per-service breakdown.",
                 bg=CARD, fg=T_FAINT, font=self.F(13), anchor="w").pack(
                     fill="x", padx=18, pady=20)

    # ------------------------------------------------------------------ actions
    def clear(self):
        if self.busy:
            return
        self.results.clear()
        self.file_rows.clear()
        self.sel_index = None
        for w in self.files_body.winfo_children():
            w.destroy()
        self.files_count.config(text="0 loaded")
        self._render_tiles(None)
        self._empty_detail()
        self._clear_meters()
        self.advice_outer.pack_forget()
        self._set_buttons_enabled(False)
        self.progress.config(value=0)
        self.prog_label.config(text="Ready")

    def save_report(self):
        if not self.results:
            messagebox.showinfo("Nothing to save", "Analyze some files first.")
            return
        included = [r for r in self.results if r.get("_include", True)]
        if not included:
            messagebox.showinfo(
                "Nothing selected",
                "No files are ticked for the report.\n\nUse the checkbox on each file "
                "in the list to include it, then save again.")
            return
        path = filedialog.asksaveasfilename(
            title="Save HTML report", defaultextension=".html",
            initialfile="streaming_conversion_report.html",
            filetypes=[("HTML", "*.html")])
        if not path:
            return
        build_report(included, path, ffmpeg_version(self.ffmpeg))
        try:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(path))
        except Exception:
            pass
        n = len(included)
        self.prog_label.config(text=f"Report saved ({n} file{'s' if n != 1 else ''}): {path}")


def run_gui(no_install=False):
    """Launch the desktop app, importing Tk (and optional DnD) lazily."""
    global tk, ttk, filedialog, messagebox, DND_FILES, TkinterDnD, _HAS_DND

    try:
        import tkinter as _tk
        from tkinter import filedialog as _fd, messagebox as _mb, ttk as _ttk
    except Exception:
        msg = ("This desktop app needs Python's Tk (tkinter), which isn't available "
               "in this Python build.\n"
               "  • macOS (Homebrew Python):  brew install python-tk@3.14\n"
               "  • Or use the official installer from python.org (includes Tk)\n\n"
               "You can still use the command line:\n"
               "  python3 spotify_conversion_test_app.py your_master.wav")
        print(msg, file=sys.stderr)
        return 3

    # Apple's system / Xcode Python ships a deprecated Tk 8.5 that imports fine
    # but ABORTS the process the moment it opens a window (Tcl_Panic in TkpInit).
    # A C-level abort can't be caught in Python, so refuse to launch on it and
    # point to a working interpreter instead of crashing.
    if sys.platform == "darwin" and float(_tk.TkVersion) < 8.6:
        print("This Python's Tk is Apple's deprecated version 8.5, which crashes when it opens\n"
              "a window (typically /usr/bin/python3 from Xcode / Command Line Tools).\n"
              "Use a Python with Tk 8.6+ for the desktop app:\n"
              "  • brew install python-tk@3.14      then run Homebrew's python3\n"
              "  • or install Python from python.org (bundles Tk 8.6)\n\n"
              "The command line works with any Python:\n"
              "  python3 spotify_conversion_test_app.py your_master.wav", file=sys.stderr)
        return 3

    tk, ttk, filedialog, messagebox = _tk, _ttk, _fd, _mb

    mod = None
    try:
        mod = importlib.import_module("tkinterdnd2")
    except Exception:
        mod = ensure_import("tkinterdnd2", "tkinterdnd2",
                            log=lambda s: print(s, file=sys.stderr))
    if mod is not None:
        try:
            DND_FILES = mod.DND_FILES
            TkinterDnD = mod.TkinterDnD
            _HAS_DND = True
        except Exception:
            _HAS_DND = False

    ffmpeg, encoders, missing = resolve_ffmpeg(auto_install=not no_install,
                                               log=lambda s: print(s, file=sys.stderr))

    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    App(root, ffmpeg, encoders, missing)
    root.mainloop()
    return 0


# =============================================================================
#  Setup / entry point
# =============================================================================

def run_setup():
    print("Streaming Conversion Test — checking dependencies…\n")
    print(f"Python           : {sys.version.split()[0]} ({sys.executable})")
    print(f"User helper dir  : {DEPS_DIR}")

    ffmpeg, encoders, missing = resolve_ffmpeg(auto_install=True, log=lambda s: print("  " + s))
    if ffmpeg:
        print(f"ffmpeg           : {ffmpeg}")
        print(f"                   {ffmpeg_version(ffmpeg)}")
        have = [e for e in REQUIRED_ENCODERS if e in encoders]
        print(f"encoders present : {', '.join(have) if have else '(none)'}")
        if missing:
            print(f"encoders MISSING : {', '.join(missing)} (those tiers will show as N/A)")
    else:
        print("ffmpeg           : NOT available and could not be installed.\n"
              "                   Install it manually — macOS: brew install ffmpeg")

    dnd = None
    try:
        dnd = importlib.import_module("tkinterdnd2")
    except Exception:
        dnd = ensure_import("tkinterdnd2", "tkinterdnd2", log=lambda s: print("  " + s))
    print(f"drag-and-drop    : {'available' if dnd else 'optional (not installed)'}")

    try:
        import tkinter  # noqa: F401
        print("tkinter (GUI)    : available")
    except Exception:
        print("tkinter (GUI)    : NOT available — GUI disabled; CLI still works.\n"
              "                   macOS (Homebrew): brew install python-tk")

    ok = bool(ffmpeg) and not missing
    print("\n" + ("All set — you're ready to go." if ok
                  else "Setup finished with warnings (see above)."))
    return 0 if ffmpeg else 2


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="streaming-conversion-test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Test master audio files for streaming transcode stability and "
                    "true-peak overshoot across Spotify, Apple Music, YouTube, Amazon,\n"
                    "Tidal, Deezer and SoundCloud.  Run with no file arguments to open "
                    "the desktop app.")
    ap.add_argument("paths", nargs="*", help="audio files or folders (wav/aiff/flac)")
    ap.add_argument("--gui", action="store_true", help="force the desktop app")
    ap.add_argument("--setup", action="store_true",
                    help="check/install dependencies (ffmpeg, drag-and-drop) and exit")
    ap.add_argument("--where", action="store_true",
                    help="print the per-user folder where helpers are installed and exit")
    ap.add_argument("--no-install", action="store_true",
                    help="never auto-install anything; use only what's already present")
    ap.add_argument("--report", metavar="FILE.html", help="write an HTML report")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--no-color", action="store_true", help="disable colored output")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv=None):
    ap = build_arg_parser()
    args = ap.parse_args(argv)

    if args.where:
        print(user_data_dir())
        return 0
    if args.setup:
        return run_setup()
    if args.paths and not args.gui:
        return run_cli(args)
    return run_gui(no_install=args.no_install)


if __name__ == "__main__":
    sys.exit(main())
