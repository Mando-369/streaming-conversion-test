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
  * Apple Music encodes AAC with Apple's OWN encoder.  On macOS this tool uses
    the real thing (ffmpeg's AudioToolbox `aac_at`, or `/usr/bin/afconvert`) for
    the Apple Music tier; on Windows/Linux it falls back to ffmpeg's generic AAC
    as a proxy.  Each report/row shows which encoder actually ran.
  * Lossless tiers (FLAC/ALAC) round-trip bit-exactly, so they add no overshoot;
    the tool reports them as such rather than re-encoding them.
  * Normalization is applied by services at PLAYBACK — your uploaded file is
    never altered.  The "plays at" figure is what listeners hear.
"""

__version__ = "2.1.1"

import argparse
import datetime
import html
import importlib
import json as jsonmod
import os
import re
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


class TranscodeSpec:
    """One unique codec/bitrate round-trip we actually run through ffmpeg."""

    __slots__ = ("key", "label", "codec", "bitrate", "ext")

    def __init__(self, key, label, codec, bitrate, ext):
        self.key = key
        self.label = label
        self.codec = codec
        self.bitrate = bitrate
        self.ext = ext


# Apple Music uses its own AAC encoder.  On macOS we route this one tier through
# the real thing (ffmpeg's AudioToolbox `aac_at`, or `afconvert`), falling back
# to ffmpeg's generic AAC off macOS.  It is a distinct transcode from the generic
# `aac_256` the other services use.
APPLE_AAC_KEY = "aac_256_apple"

# The union of lossy codec tiers used by any service.  Each is transcoded ONCE
# per file; services then reference these results by key.
TRANSCODES = [
    TranscodeSpec("vorbis_96",  "Ogg Vorbis 96k",  "libvorbis",   "96k",  "ogg"),
    TranscodeSpec("vorbis_160", "Ogg Vorbis 160k", "libvorbis",   "160k", "ogg"),
    TranscodeSpec("vorbis_320", "Ogg Vorbis 320k", "libvorbis",   "320k", "ogg"),
    TranscodeSpec("aac_128",    "AAC 128k",        "aac",         "128k", "m4a"),
    TranscodeSpec("aac_256",    "AAC 256k",        "aac",         "256k", "m4a"),
    TranscodeSpec("opus_64",    "Opus 64k",        "libopus",     "64k",  "opus"),
    TranscodeSpec("opus_128",   "Opus 128k",       "libopus",     "128k", "opus"),
    TranscodeSpec("opus_160",   "Opus 160k",       "libopus",     "160k", "opus"),
    TranscodeSpec("mp3_128",    "MP3 128k",        "libmp3lame",  "128k", "mp3"),
    TranscodeSpec("mp3_320",    "MP3 320k",        "libmp3lame",  "320k", "mp3"),
    # Apple Music tier — encoder resolved at runtime (aac_at / afconvert / aac).
    TranscodeSpec("aac_256_apple", "AAC 256k",     "aac_at",      "256k", "m4a"),
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
        ("aac_256_apple", "AAC 256 VBR (standard)"),
        ("alac",          "Lossless / Hi-Res Lossless (ALAC)"),
    ], note="On macOS this uses Apple's real AAC encoder (AudioToolbox / afconvert); "
            "on Windows/Linux it falls back to ffmpeg's AAC as a proxy."),
    Service("youtube", "YouTube Music", -14.0, -1.0, -1.0, [
        ("opus_128", "Opus (typical)"),
        ("opus_160", "Opus (high)"),
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

# Encoders we need (to grade an ffmpeg build and to skip codecs cleanly).  The
# Apple tier is excluded: its encoder is resolved at runtime and always has a
# generic-AAC fallback, so it never forces a bundled-ffmpeg install.
REQUIRED_ENCODERS = tuple(dict.fromkeys(
    TRANSCODE_BY_KEY[k].codec for k in USED_TRANSCODE_KEYS if k != APPLE_AAC_KEY))

# --- Verdicts ----------------------------------------------------------------
PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
# SKIP (encoder unavailable) ranks below PASS so it never worsens the verdict.
_RANK = {SKIP: -1, PASS: 0, WARN: 1, FAIL: 2}

COLORS = {PASS: "#1db954", WARN: "#f5a623", FAIL: "#e0245e", SKIP: "#8a8a8a"}
LABELS = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "N/A"}


def worst(*verdicts):
    """Return the most severe verdict among the arguments."""
    return max(verdicts, key=lambda v: _RANK[v])


def effective_ceiling(service, integrated_lufs):
    """The recommended true-peak ceiling for a service given the master loudness."""
    if integrated_lufs is not None and integrated_lufs > service.target:
        return service.ceiling_hot
    return service.ceiling


def evaluate_peak(true_peak, ceiling):
    """Grade a true peak: FAIL if it clips (>0 dBFS), WARN if above the ceiling."""
    if true_peak is None:
        return PASS
    if true_peak > CLIP_CEILING:
        return FAIL
    if true_peak > ceiling:
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
    """Measure a file.

    Returns a dict with:
      integrated_lufs, true_peak (dBTP), lra, threshold, sample_peak (dBFS).
    Any value that cannot be measured (e.g. silence) is None.
    """
    result = {
        "integrated_lufs": None,
        "true_peak": None,
        "lra": None,
        "threshold": None,
        "sample_peak": None,
    }

    # loudnorm analysis -> JSON block on stderr with input_i / input_tp / input_lra.
    r = _run([ffmpeg, "-hide_banner", "-nostats", "-i", path,
              "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    match = re.search(r"\{[^{}]+\}", r.stderr, re.S)
    if match:
        try:
            data = jsonmod.loads(match.group(0))
            result["integrated_lufs"] = _to_float(data.get("input_i"))
            result["true_peak"] = _to_float(data.get("input_tp"))
            result["lra"] = _to_float(data.get("input_lra"))
            result["threshold"] = _to_float(data.get("input_thresh"))
        except jsonmod.JSONDecodeError:
            pass

    # astats -> overall sample peak.
    r2 = _run([ffmpeg, "-hide_banner", "-nostats", "-i", path,
               "-af", "astats=measure_perchannel=none:measure_overall=Peak_level",
               "-f", "null", "-"])
    sp = re.search(r"Peak level dB:\s*(-?\d+(?:\.\d+)?|-?inf)", r2.stderr)
    if sp:
        val = sp.group(1)
        result["sample_peak"] = None if "inf" in val else float(val)

    return result


def transcode_measure(ffmpeg, src, codec, bitrate, ext, workdir):
    """Encode `src` to codec/bitrate, then measure the decoded output.

    Returns the same dict shape as measure().  Raises FFmpegError on encode failure.
    """
    out = os.path.join(workdir, f"enc_{codec}_{bitrate}.{ext}")
    enc = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", src, "-c:a", codec, "-b:a", bitrate, out])
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


def resolve_apple_aac(encoders):
    """Choose the best AAC encoder for the Apple Music tier.

    Returns (kind, ffmpeg_codec, note):
      * ("ffmpeg",    "aac_at", ...) Apple's CoreAudio AAC via ffmpeg AudioToolbox
      * ("afconvert", None,     ...) Apple's CoreAudio AAC via /usr/bin/afconvert
      * ("ffmpeg",    "aac",    ...) ffmpeg's generic AAC (a proxy, off macOS)
    """
    if "aac_at" in encoders:
        return "ffmpeg", "aac_at", "Apple aac_at"
    if _afconvert_path():
        return "afconvert", None, "Apple afconvert"
    return "ffmpeg", "aac", "ffmpeg aac (proxy)"


def transcode_measure_afconvert(ffmpeg, src, bitrate, workdir):
    """Encode with macOS afconvert (real Apple AAC), then measure the decode."""
    afc = _afconvert_path()
    if not afc:
        raise FFmpegError("afconvert not available")
    # afconvert wants an uncompressed input; normalize to a temp wav first.
    wav = os.path.join(workdir, "afc_in.wav")
    dec = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", src, "-c:a", "pcm_s24le", wav])
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

def analyze_file(ffmpeg, path, available_encoders=None, progress=None):
    """Analyze one audio file across every modelled service.

    Each unique lossy tier is transcoded once, then every service is graded
    against its own normalization target and true-peak ceiling.

    `available_encoders`, if given, marks tiers whose encoder is missing as N/A.
    `progress`, if given, is called as progress(name, index, total, label).
    """
    name = os.path.basename(path)
    master = measure(ffmpeg, path)
    integ = master["integrated_lufs"]
    tp = master["true_peak"]

    # --- Run each unique lossy transcode once --------------------------------
    enc_set = available_encoders if available_encoders is not None else ffmpeg_encoders(ffmpeg)
    transcodes = {}  # key -> physics result
    total = len(USED_TRANSCODE_KEYS)
    with tempfile.TemporaryDirectory(prefix="strmconv_") as workdir:
        for i, tkey in enumerate(USED_TRANSCODE_KEYS):
            ts = TRANSCODE_BY_KEY[tkey]
            if progress:
                progress(name, i, total, ts.label)

            entry = {"key": tkey, "label": ts.label, "codec": ts.codec,
                     "bitrate": ts.bitrate, "decoded_tp": None, "decoded_lufs": None,
                     "overshoot": None, "loudness_drift": None,
                     "unity_verdict": PASS, "error": None, "unavailable": False,
                     "encoder_used": None}

            # Decide how to encode this tier.
            if tkey == APPLE_AAC_KEY:
                kind, codec, note = resolve_apple_aac(enc_set)
                entry["encoder_used"] = note
                entry["codec"] = codec or "afconvert"
            else:
                kind, codec = "ffmpeg", ts.codec
                if available_encoders is not None and codec not in enc_set:
                    entry.update(unavailable=True, unity_verdict=SKIP,
                                 error=f"'{codec}' encoder not available in this ffmpeg build")
                    transcodes[tkey] = entry
                    continue

            try:
                if kind == "afconvert":
                    m = transcode_measure_afconvert(ffmpeg, path, ts.bitrate, workdir)
                else:
                    m = transcode_measure(ffmpeg, path, codec, ts.bitrate, ts.ext, workdir)
                dtp = m["true_peak"]
                dlufs = m["integrated_lufs"]
                entry["decoded_tp"] = dtp
                entry["decoded_lufs"] = dlufs
                entry["overshoot"] = (dtp - tp) if (dtp is not None and tp is not None) else None
                entry["loudness_drift"] = (dlufs - integ) if (dlufs is not None and integ is not None) else None
                entry["unity_verdict"] = evaluate_peak(dtp, TP_CEILING_NORMAL)
            except Exception as exc:  # keep going even if one codec fails
                entry["unity_verdict"] = WARN
                entry["error"] = str(exc)
            transcodes[tkey] = entry

    # --- Grade every service against its own target/ceiling ------------------
    services = []
    overall = PASS
    for svc in SERVICES:
        ceiling = effective_ceiling(svc, integ)
        gain = normalization_gain(integ, tp, svc.target, ceiling)
        played = (integ + gain) if integ is not None else None
        master_verdict = evaluate_peak(tp, ceiling)

        tiers = []
        svc_worst = master_verdict
        any_clip = False
        overs = []
        for tkey, ctx in svc.tiers:
            if tkey in LOSSLESS:
                # Bit-exact: decoded == master, no added overshoot.
                dtp = tp
                over = 0.0 if tp is not None else None
                ptp = (dtp + gain) if dtp is not None else None
                v = evaluate_peak(dtp, ceiling)  # a clipping master still clips losslessly
                tiers.append({"key": tkey, "label": LOSSLESS[tkey], "context": ctx,
                              "lossless": True, "decoded_tp": dtp, "overshoot": over,
                              "playback_tp": ptp, "verdict": v, "error": None,
                              "encoder_used": None})
                if dtp is not None and dtp > CLIP_CEILING:
                    any_clip = True
            else:
                tr = transcodes[tkey]
                dtp = tr["decoded_tp"]
                over = tr["overshoot"]
                if tr["unavailable"]:
                    v = SKIP
                    ptp = None
                elif tr["error"]:
                    v = WARN
                    ptp = None
                else:
                    ptp = (dtp + gain) if dtp is not None else None
                    v = evaluate_peak(dtp, ceiling)
                    if dtp is not None and dtp > CLIP_CEILING:
                        any_clip = True
                    if over is not None:
                        overs.append(over)
                tiers.append({"key": tkey, "label": tr["label"], "context": ctx,
                              "lossless": False, "decoded_tp": dtp, "overshoot": over,
                              "playback_tp": ptp, "verdict": v, "error": tr["error"],
                              "encoder_used": tr.get("encoder_used")})
            svc_worst = worst(svc_worst, v)

        services.append({
            "key": svc.key, "name": svc.name, "target": svc.target,
            "ceiling": ceiling, "gain": gain, "played_lufs": played,
            "master_verdict": master_verdict, "verdict": svc_worst,
            "any_clip": any_clip,
            "max_overshoot": max(overs) if overs else None,
            "note": svc.note, "tiers": tiers,
        })
        overall = worst(overall, svc_worst)

    # --- Cross-service stability summary -------------------------------------
    all_overs = [t["overshoot"] for t in transcodes.values() if t["overshoot"] is not None]
    any_clip_any = any(t["decoded_tp"] is not None and t["decoded_tp"] > CLIP_CEILING
                       for t in transcodes.values())
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
        "worst_overshoot": max(all_overs) if all_overs else None,
        "any_clip": any_clip_any,
        "overall": overall,
    }


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
        tvc = COLORS[t["verdict"]]
        extra = LOSSLESS_TAG if t["lossless"] else ""
        if t.get("encoder_used"):
            extra += f'<span class="tag enc">{html.escape(t["encoder_used"])}</span>'
        err = f'<div class="err">{html.escape(t["error"])}</div>' if t["error"] else ""
        rows.append(
            f'<tr>'
            f'<td><b>{html.escape(t["label"])}</b>{extra}'
            f'<div class="ctx">{html.escape(t["context"])}</div>{err}</td>'
            f'<td class="num">{_fmt(t["decoded_tp"], " dBTP")}</td>'
            f'<td class="num" style="color:{tvc}"><b>{_signed(t["overshoot"], " dB")}</b></td>'
            f'<td class="num">{_fmt(t["playback_tp"], " dBTP")}</td>'
            f'<td>{_tp_bar(t["decoded_tp"], t["verdict"])}</td>'
            f'<td><span class="pill" style="background:{tvc}">{LABELS[t["verdict"]]}</span></td>'
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
          <th>Tier</th><th>Decoded TP</th><th>Overshoot</th>
          <th>At playback</th><th>Decoded TP (&minus;1 amber / 0 red)</th><th>Verdict</th>
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

    return f"""
    <section class="file">
      <div class="fhead">
        <h2>{name}</h2>
        <span class="badge" style="background:{badge_color}">{LABELS[verdict]}</span>
      </div>
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
          <div class="sub">across all lossy codecs</div></div>
        <div class="card"><div class="k">Loudness range</div>
          <div class="v">{_fmt(m['lra'], ' LU')}</div>
          <div class="sub">LRA</div></div>
        <div class="card"><div class="k">Any codec clips?</div>
          <div class="v" style="color:{COLORS[FAIL] if r['any_clip'] else COLORS[PASS]}">
            {'YES' if r['any_clip'] else 'no'}</div>
          <div class="sub">decoded TP &gt; 0 dBFS</div></div>
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
  "Decoded TP" is the codec's decoded true peak at unity gain; "Overshoot" is
  decoded minus master true peak; "At playback" adds each service's normalization
  gain.  Targets are publicly-reported and approximate.  ffmpeg encoders are a
  faithful proxy, not byte-identical to each service's build (notably Apple's
  AAC).  Informational — not a substitute for critical listening.
</footer>
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


# =============================================================================
#  Command-line interface
# =============================================================================

AUDIO_EXTS = (".wav", ".wave", ".aif", ".aiff", ".flac")

_ANSI = {PASS: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m"}
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
    print("  Columns: dec = decoded true peak (unity) · over = codec overshoot · "
          "play = true peak after that service's normalization")

    skipped = set()
    for s in r["services"]:
        gain = s["gain"]
        gdir = "down" if gain < 0 else ("up" if gain > 0 else "flat")
        head = (f"  {s['name']:<15} {s['target']:>4.0f} LUFS  → plays "
                f"~{_v(s['played_lufs'],' LUFS')} (gain {_v(gain,' dB',signed=True)}, {gdir})   ")
        print(paint(s["verdict"], head + f"[{LABELS[s['verdict']]}]"))
        for t in s["tiers"]:
            tag = " (lossless)" if t["lossless"] else ""
            enc = f"  · {t['encoder_used']}" if t.get("encoder_used") else ""
            line = (f"     {t['label']+tag:<22}"
                    f"dec {_v(t['decoded_tp'],'',digits=2):>7} "
                    f"over {_v(t['overshoot'],'',digits=2,signed=True):>7} "
                    f"play {_v(t['playback_tp'],'',digits=2):>7}   ")
            print(line + paint(t["verdict"], LABELS[t["verdict"]]) + enc)
            if t["verdict"] == SKIP:
                skipped.add(t["label"])

    if r["any_clip"]:
        print(paint(FAIL, "  ! At least one codec pushes the decoded true peak above 0 dBFS (clipping)."))
    if skipped:
        print(paint(SKIP, f"  note: {', '.join(sorted(skipped))} skipped — encoder not in this ffmpeg "
                          f"build (run with --setup to install a complete bundled ffmpeg)."))


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

BG = "#121212"
PANEL = "#181818"
CARD = "#202020"
FG = "#e8e8e8"
MUTED = "#8a8a8a"
ACCENT = "#1db954"


def _g(value, unit="", signed=False):
    if value is None:
        return "—"
    sign = "+" if signed else ""
    return f"{value:{sign}.2f}{unit}"


class App:
    def __init__(self, root, ffmpeg, encoders, missing):
        self.root = root
        self.ffmpeg = ffmpeg
        self.encoders = encoders
        self.missing = missing
        self.results = []
        import queue
        self.events = queue.Queue()
        self._queue_empty = queue.Empty
        self.busy = False

        root.title("Streaming Conversion Test")
        root.geometry("1040x720")
        root.minsize(860, 580)
        root.configure(bg=BG)
        self._build_style()
        self._build_ui()
        self._poll()

    # ------------------------------------------------------------------ styling
    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview",
                        background=PANEL, fieldbackground=PANEL, foreground=FG,
                        rowheight=25, borderwidth=0)
        style.configure("Treeview.Heading",
                        background=CARD, foreground=MUTED, borderwidth=0,
                        font=("Helvetica", 10, "bold"))
        style.map("Treeview", background=[("selected", "#2d3a30")])
        style.configure("TProgressbar", background=ACCENT, troughcolor=CARD, borderwidth=0)

    # ------------------------------------------------------------------- layout
    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text="Streaming Conversion Test", bg=BG, fg=FG,
                 font=("Helvetica", 18, "bold")).pack(anchor="w")
        tk.Label(header,
                 text="True-peak overshoot & loudness per service  ·  "
                      "Spotify · Apple Music · YouTube · Amazon · Tidal · Deezer · SoundCloud",
                 bg=BG, fg=MUTED, font=("Helvetica", 11)).pack(anchor="w")

        self.status = tk.Label(self.root, bg=BG, anchor="w", font=("Helvetica", 10))
        self.status.pack(fill="x", padx=16)
        self._refresh_ffmpeg_status()

        self.drop = tk.Label(
            self.root,
            text=("⬇  Drop master files here"
                  if _HAS_DND else "\U0001f4c1  Click to add master files"),
            bg=CARD, fg=FG, font=("Helvetica", 13, "bold"),
            height=3, cursor="hand2", relief="flat", bd=0)
        self.drop.pack(fill="x", padx=16, pady=10, ipady=14)
        self.drop.bind("<Button-1>", lambda e: self.add_files())
        if _HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)
        tk.Label(self.root,
                 text=("Drag & drop enabled  ·  accepts .wav .aiff .flac  ·  "
                       "folders are scanned recursively"
                       if _HAS_DND else
                       "Tip: install 'tkinterdnd2' for true drag & drop  ·  "
                       "accepts .wav .aiff .flac"),
                 bg=BG, fg=MUTED, font=("Helvetica", 9)).pack()

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=8)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="FILES", bg=BG, fg=MUTED,
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        cols = ("lufs", "tp", "over", "clip", "verdict")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", height=8)
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=200, anchor="w")
        for cid, text, w in (("lufs", "LUFS", 66), ("tp", "True pk", 66),
                             ("over", "Worst OS", 70), ("clip", "Clip?", 50),
                             ("verdict", "Verdict", 66)):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, pady=(2, 0))
        for v, col in COLORS.items():
            self.tree.tag_configure(v, foreground=col)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = tk.Frame(body, bg=BG, width=460)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        tk.Label(right, text="PER-SERVICE BREAKDOWN", bg=BG, fg=MUTED,
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        dcols = ("tp", "over", "play", "verdict")
        self.detail = ttk.Treeview(right, columns=dcols, show="tree headings", height=8)
        self.detail.heading("#0", text="Service / tier")
        self.detail.column("#0", width=230, anchor="w")
        for cid, text, w in (("tp", "Decoded TP", 78), ("over", "Overshoot", 76),
                             ("play", "Playback", 70), ("verdict", "", 56)):
            self.detail.heading(cid, text=text)
            self.detail.column(cid, width=w, anchor="center")
        self.detail.pack(fill="both", expand=True, pady=(2, 0))
        for v, col in COLORS.items():
            self.detail.tag_configure(v, foreground=col)
        self.detail.tag_configure("svc", foreground=FG, font=("Helvetica", 10, "bold"))
        self.summary = tk.Label(right, text="", bg=BG, fg=MUTED, justify="left",
                                font=("Helvetica", 10), anchor="w")
        self.summary.pack(fill="x", pady=(6, 0))

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=16, pady=(4, 12))
        self.progress = ttk.Progressbar(foot, mode="determinate")
        self.progress.pack(fill="x", side="top", pady=(0, 8))
        self.prog_label = tk.Label(foot, text="Ready.", bg=BG, fg=MUTED,
                                   anchor="w", font=("Helvetica", 10))
        self.prog_label.pack(fill="x", side="top")

        btns = tk.Frame(foot, bg=BG)
        btns.pack(fill="x", pady=(8, 0))
        self._button(btns, "Add files…", self.add_files, primary=True).pack(side="left")
        self._button(btns, "Clear", self.clear).pack(side="left", padx=6)
        self.report_btn = self._button(btns, "Save HTML report…", self.save_report)
        self.report_btn.pack(side="right")

    def _button(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=ACCENT if primary else CARD,
                         fg="#08210f" if primary else FG,
                         activebackground="#1ed760" if primary else "#2a2a2a",
                         activeforeground=FG, relief="flat", bd=0,
                         font=("Helvetica", 11, "bold" if primary else "normal"),
                         padx=14, pady=7, cursor="hand2")

    def _refresh_ffmpeg_status(self):
        if self.ffmpeg and not self.missing:
            self.status.config(text=f"✓ ffmpeg ready: {ffmpeg_version(self.ffmpeg)}", fg=ACCENT)
        elif self.ffmpeg and self.missing:
            self.status.config(
                text=f"⚠ ffmpeg found but missing {', '.join(self.missing)} — those tiers "
                     f"show as N/A. Re-run with --setup to enable all codecs.",
                fg=COLORS[WARN])
        else:
            self.status.config(
                text="✗ ffmpeg not found — install it (macOS: brew install ffmpeg), then reopen.",
                fg=COLORS[FAIL])

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
        self.results.append(r)
        m = r["master"]
        vals = (_g(m["integrated_lufs"]), _g(m["true_peak"]),
                _g(r["worst_overshoot"], signed=True),
                "YES" if r["any_clip"] else "no",
                LABELS[r["overall"]])
        iid = self.tree.insert("", "end", text=r["name"], values=vals, tags=(r["overall"],))
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self._show_detail(r)

    # ----------------------------------------------------------------- detail
    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.results):
            self._show_detail(self.results[idx])

    def _show_detail(self, r):
        self.detail.delete(*self.detail.get_children())
        for s in r["services"]:
            head = (f"{s['name']}   {s['target']:.0f} LUFS → "
                    f"{_g(s['played_lufs'],' LUFS')}")
            parent = self.detail.insert("", "end", text=head,
                                        values=("", "", "", LABELS[s["verdict"]]),
                                        tags=("svc", s["verdict"]), open=True)
            for t in s["tiers"]:
                lbl = t["label"] + ("  (lossless)" if t["lossless"] else "")
                if t.get("encoder_used"):
                    lbl += f"  · {t['encoder_used']}"
                self.detail.insert(
                    parent, "end", text="   " + lbl,
                    values=(_g(t["decoded_tp"], " dBTP"),
                            _g(t["overshoot"], " dB", signed=True),
                            _g(t["playback_tp"], " dBTP"),
                            LABELS[t["verdict"]]),
                    tags=(t["verdict"],))
        m = r["master"]
        self.summary.config(
            text=(f"Sample peak {_g(m['sample_peak'],' dBFS')}   "
                  f"(inter-sample {_g(r['inter_sample_margin'],' dB',signed=True)})   ·   "
                  f"worst overshoot {_g(r['worst_overshoot'],' dB',signed=True)}\n"
                  f"dec = decoded true peak (unity) · over = codec overshoot · "
                  f"play = true peak after that service's normalization"))

    # ------------------------------------------------------------------ actions
    def clear(self):
        if self.busy:
            return
        self.results.clear()
        self.tree.delete(*self.tree.get_children())
        self.detail.delete(*self.detail.get_children())
        self.summary.config(text="")
        self.progress.config(value=0)
        self.prog_label.config(text="Ready.")

    def save_report(self):
        if not self.results:
            messagebox.showinfo("Nothing to save", "Analyze some files first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save HTML report", defaultextension=".html",
            initialfile="streaming_conversion_report.html",
            filetypes=[("HTML", "*.html")])
        if not path:
            return
        build_report(self.results, path, ffmpeg_version(self.ffmpeg))
        try:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(path))
        except Exception:
            pass
        self.prog_label.config(text=f"Report saved: {path}")


def run_gui(no_install=False):
    """Launch the desktop app, importing Tk (and optional DnD) lazily."""
    global tk, ttk, filedialog, messagebox, DND_FILES, TkinterDnD, _HAS_DND

    try:
        import tkinter as _tk
        from tkinter import filedialog as _fd, messagebox as _mb, ttk as _ttk
    except Exception:
        msg = ("This desktop app needs Python's Tk (tkinter), which isn't available "
               "in this Python build.\n"
               "  • macOS (Homebrew Python):  brew install python-tk\n"
               "  • Or use the official installer from python.org (includes Tk)\n\n"
               "You can still use the command line:\n"
               "  python3 spotify_conversion_test_app.py your_master.wav")
        print(msg, file=sys.stderr)
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
