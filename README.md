# Streaming Conversion Test

A single-file desktop + command-line tool that takes your **offline master audio**
and tells you whether it will survive the conversions that online music services
apply — specifically whether their lossy codecs introduce **true-peak overshoot**
(inter-sample clipping) and how **stable** loudness and peaks stay through
transcoding, graded against **each service's own loudness-normalization target.**

It does the *real* thing: it encodes your master through the codec/bitrate tiers
those services actually serve (Ogg Vorbis, AAC, Opus, MP3, plus lossless
FLAC/ALAC), decodes them back, and measures the result — no approximation of the
transcode itself.

**No manual setup.** The whole app is a single Python file
(`spotify_conversion_test_app.py`); on first launch it installs everything it
needs into a private per-user folder — no admin rights, no Homebrew/apt, no
virtual environment, nothing system-wide. On macOS a bundled `run.command`
launcher lets you **double-click to open** it.

---

## Services it models

Loudness targets are publicly-reported, approximate, and change over time:

| Service | Target | Tiers tested |
|---|---|---|
| **Spotify** | −14 LUFS | Ogg Vorbis 96 / 160 / 320, AAC 128 / 256 |
| **Apple Music** | −16 LUFS | AAC 256, ALAC lossless |
| **YouTube Music** | −14 LUFS | Opus 128 / 160, AAC 128 |
| **Amazon Music** | −14 LUFS | AAC 256, FLAC (HD / Ultra HD) |
| **Tidal** | −14 LUFS | AAC 256, FLAC (HiFi / Max) |
| **Deezer** | −15 LUFS | MP3 128 / 320, FLAC |
| **SoundCloud** | −14 LUFS | Opus 64, AAC 256, MP3 128 |

Each unique codec/bitrate is transcoded **once** per file, then reused across every
service that serves it.

> **Apple Music uses Apple's own AAC encoder.** On **macOS** this tool encodes the
> Apple Music tier with the real thing — ffmpeg's AudioToolbox `aac_at`, or the
> built-in `/usr/bin/afconvert` — so that row is high-fidelity, not a proxy. On
> Windows/Linux it falls back to ffmpeg's generic AAC. Every result shows which
> encoder actually ran.

---

## What it measures (and the spec it references)

Loudness and true peak follow **ITU-R BS.1770** (integrated LUFS + 4×-oversampled
true peak). Spotify's own guidance,
[*Loudness normalization on Spotify*](https://support.spotify.com/us/artists/article/loudness-normalization/),
is the reference for the −14 LUFS / −1 dBTP rules:

| Spec | Value | Why it matters |
|---|---|---|
| Loudness target | **−14 LUFS integrated** (Spotify) | Services normalize to this at playback. |
| True-peak ceiling | **≤ −1 dBTP** for lossy | Leaves the transcoder room so it doesn't clip. |
| Hotter than target? | **≤ −2 dBTP** (Spotify) | Loud masters distort more easily when encoded. |
| Positive-gain headroom | **1 dB** preserved on upward normalization | Quiet tracks are only lifted until the peak reaches −1 dBTP. |

### The two things it reports

- **Overshoot** — lossy encoding reconstructs the waveform and routinely adds
  ~0.3–0.5 dB of true peak. A master sitting at −0.3 dBTP can come out of the
  encoder **above 0 dBFS and clip.** For each codec it reports
  `overshoot = decoded true peak − master true peak`.
- **Conversion stability** — how consistently loudness and peaks hold up, per
  service: the worst-case overshoot, whether *any* tier pushes the decoded true
  peak above 0 dBFS, and the effective true peak *after* each service's
  normalization gain.

### Verdicts

| | Meaning |
|---|---|
| 🟢 **PASS** | Within spec; no codec for that service exceeds −1 dBTP. |
| 🟠 **WARN** | A decoded peak lands between −1 and 0 dBTP — above recommended headroom but not clipping. |
| 🔴 **FAIL** | The master already clips, or a codec pushes the decoded true peak above 0 dBFS. |
| **N/A** | That codec's encoder isn't available in the current ffmpeg build. |

---

## Requirements

- **Python 3.8+** — already present if you can run the file (macOS/Linux usually
  ship it; on Windows install from [python.org](https://www.python.org/), which
  includes Tk for the GUI).
- **ffmpeg** — does the real transcoding and BS.1770 measurement. **You don't have
  to install it yourself:** if it's missing (or your ffmpeg lacks a needed encoder
  like Ogg Vorbis or MP3), the app installs a complete, self-contained ffmpeg for
  *you only* via the pip package `imageio-ffmpeg`.

Everything it installs lives under one per-user folder (`--where` prints it) and
can be removed at any time.

**No virtual environment needed.** Helpers are installed with `pip install --target`
into that private folder — it never touches your system Python's packages, needs
no `sudo`, and sidesteps the "externally-managed-environment" restriction, so
there's nothing to set up or activate.

> **GUI note:** the desktop window needs **Tk 8.6+**. The python.org installers
> include it; on Homebrew run `brew install python-tk@3.14`. **Do not use Apple's
> `/usr/bin/python3` for the GUI** — it ships a deprecated Tk 8.5 that crashes on
> window open (the app detects this and prints guidance instead of crashing). The
> command line works with any Python, no Tk needed.

---

## Running it

**Easiest (macOS) — double-click `run.command`.** Keep it in the same folder as
`spotify_conversion_test_app.py`. The first time, macOS Gatekeeper may block it —
**right-click → Open**, then confirm; after that a normal double-click works. It
automatically finds a Tk-capable Python and opens the desktop app.

**Or launch the desktop app from a terminal:**

```bash
python3 spotify_conversion_test_app.py
```

> If you see a Tk message instead of a window, your `python3` doesn't have a
> working Tk. On macOS, get one with `brew install python-tk@3.14` (then use
> Homebrew's `python3`) or the python.org installer. Avoid `/usr/bin/python3` for
> the GUI — its Tk 8.5 crashes.

Pick one or more master files — or a whole folder. Results appear per file with a
per-service, codec-by-codec breakdown; click **Save HTML report…** for a
shareable, fully self-contained report.

**Command line (batch / scripting):**

```bash
# single file
python3 spotify_conversion_test_app.py master.wav

# a whole folder, plus an HTML report
python3 spotify_conversion_test_app.py /path/to/masters --report report.html

# machine-readable
python3 spotify_conversion_test_app.py master.wav --json
```

The CLI exits non-zero if any file **FAILs**, so it drops into a pre-delivery
check or CI step.

Accepted inputs: `.wav`, `.aiff`, `.flac` (folders are scanned recursively).

**Utility flags:**

```bash
python3 spotify_conversion_test_app.py --setup        # install/verify deps, then exit
python3 spotify_conversion_test_app.py --where         # print the per-user helper folder
python3 spotify_conversion_test_app.py --no-install    # never auto-install; use what's present
python3 spotify_conversion_test_app.py --help
```

---

## How to read the output

```
=== hot.wav  [FAIL] ===
  Master   integrated -0.25 LUFS · true peak -0.08 dBTP · sample -0.04 dBFS · inter-sample -0.04 dB
  Columns: dec = decoded true peak (unity) · over = codec overshoot · play = true peak after that service's normalization
  Spotify          -14 LUFS  → plays ~-14.00 LUFS (gain -13.75 dB, down)   [FAIL]
     Ogg Vorbis 96k        dec    0.36 over   +0.44 play  -13.39   FAIL
     ...
  Apple Music      -16 LUFS  → plays ~-16.00 LUFS (gain -15.75 dB, down)   [FAIL]
     AAC 256k              dec    0.04 over   +0.12 play  -15.71   FAIL  · Apple aac_at
     ALAC (lossless)       dec   -0.08 over   +0.00 play  -15.83   WARN
```

- **dec** — the codec's decoded true peak at unity gain. Above 0 dBFS is real
  clipping in the delivered stream.
- **over** — `decoded − master` true peak: how much this codec pushed the peak up.
- **play** — the effective true peak *after* that service applies its
  normalization gain (what a listener's DAC sees with normalization on).

The takeaway is the usual mastering guidance, now measured on *your* file: if a
codec is clipping, pull your master's true-peak ceiling down (−1 dBTP, or −2 dBTP
if you're hotter than −14 LUFS) and re-test.

---

## How reliable is it?

| Claim | Confidence |
|---|---|
| Loudness / normalization math (LUFS, gain, "plays at") | 🟢 High — deterministic BS.1770 |
| Overshoot direction & clipping flags | 🟢 High — a real encode→decode round-trip |
| Relative comparisons (codec vs codec, master vs master, lossless = clean) | 🟢 High |
| **Apple Music on macOS** | 🟢 High — uses Apple's real AAC encoder (`aac_at` / `afconvert`) |
| Apple Music on Windows/Linux | 🟠 Proxy — ffmpeg's generic AAC |
| Absolute overshoot magnitude per service | 🟠 Medium — ±~0.3 dB |
| Service parameters (targets, bitrates) | 🟠 Medium — approximate, change over time |

Use it as a **relative pre-delivery check** — "does my master have enough
true-peak headroom to survive lossy streaming, and which services/codecs are the
riskiest." It is a faithful, conservative **simulation**, not a bit-exact
prediction of any one service's encoder. It does **not** model encoder
quality/VBR settings, service-side pre-processing, sample-rate conversion, or
perceptual quality — and it's never a substitute for critical listening.

This same reliability sheet is printed at the top of every HTML report and
summarised in the command-line output, so anyone reading a result knows exactly
how much to trust it.

---

## Free tools it builds on

- **ffmpeg** — `libvorbis`, native `aac`, `libopus`, `libmp3lame` encoders for the
  real round-trips (plus Apple's `aac_at` / AudioToolbox on macOS); `loudnorm`
  (ITU-R BS.1770 integrated loudness + 4× oversampled true peak) and `astats`
  (sample peak) for measurement.
- **macOS AudioToolbox / `afconvert`** — Apple's real AAC encoder for the Apple
  Music tier, on macOS only.
- **Python standard library** (incl. `tkinter`) for the app and report.
- **imageio-ffmpeg** / **tkinterdnd2** — optional, auto-installed on demand for the
  bundled ffmpeg and true drag-and-drop.

---

## Notes & limitations

- ffmpeg's encoders are excellent references but are not byte-identical to each
  service's internal encoder builds; treat overshoot figures as a faithful,
  conservative simulation. On macOS the **Apple Music** tier uses Apple's real AAC
  encoder (AudioToolbox `aac_at`, or `afconvert`); elsewhere it falls back to
  ffmpeg's AAC as a proxy, and each report row shows which encoder ran.
- Lossless tiers (FLAC/ALAC) round-trip bit-exactly, so they add no overshoot; the
  tool reports them as such rather than re-encoding.
- Normalization is applied at **playback** — your uploaded file is never altered —
  so the "plays at" figure is what listeners hear, not a change to your master.

---

## License

MIT — see [LICENSE](LICENSE).
