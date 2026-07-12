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

Each unique codec/bitrate is transcoded **once** per file (all tiers run
**concurrently across your CPU cores** — a full track analyzes in a few seconds),
then reused across every service that serves it.

> **AAC uses a real encoder, not ffmpeg's native one.** ffmpeg's built-in `aac`
> encoder overshoots true peak ~2 dB more than any encoder a real service uses, so
> on **macOS** every AAC tier is encoded with **Apple's CoreAudio AAC** (`aac_at`,
> the same encoder iTunes/Logic/Apple Music use) in **constrained-VBR** mode — the
> mode real encoders actually use. Falls back to FDK → `afconvert` → ffmpeg native
> AAC (a flagged proxy) on systems without those. Every result shows which encoder
> ran. *Validated: AAC and MP3 match iZotope Insight within 0.2 dB.*

---

## What it measures (and the spec it references)

Loudness and true peak follow **ITU-R BS.1770-4**. True peak is read from ffmpeg's
reference `ebur128` meter (4×-oversampled, full precision from its per-frame
metadata); lossy streams are decoded in 32-bit float so overshoot above 0 dBFS is
captured, not clipped. For every codec tier the app reports **both** peaks the
standard distinguishes:

- **Sample peak (dBFS)** — the raw *digital* peak; above 0 dBFS means the decoded
  stream clips in the playback engine.
- **True peak (dBTP)** — the *inter-sample* peak; above 0 dBTP means it clips the
  listener's DAC/hardware.

Spotify's own guidance,
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

The verdict reflects the true peak **at playback** — with each service's loudness
normalization on, which is the default listening case. (A loud master gets turned
*down* at playback, so its encoded stream can exceed 0 dBFS at unity gain yet be
perfectly safe for listeners.)

| | Meaning |
|---|---|
| 🟢 **PASS** | Safe at playback and within recommended headroom. |
| 🟠 **WARN** | Above the recommended ceiling, **or** the encoded stream exceeds 0 dBFS at unity gain (tagged `unity > 0 dBFS`) — audible only if a listener disables normalization, or in a non-normalized/downloaded copy. Common for loud masters. |
| 🔴 **FAIL** | The master already clips, or a codec clips **at playback** (after normalization) — audible even with normalization on. Mostly affects *quiet* masters lifted upward. |
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
=== loud_master.wav  [WARN] ===
  Master   integrated -10.41 LUFS · true peak -0.56 dBTP · sample -0.60 dBFS · inter-sample +0.04 dB
  Columns: dec = decoded true peak (unity) · over = codec overshoot · play = true peak after normalization (what listeners hear)
  Verdict reflects the PLAYBACK peak (normalization on, the default). 'unity>0' = stream exceeds 0 dBFS before normalization.
  Spotify          -14 LUFS  → plays ~-14.00 LUFS (gain -3.59 dB, down)   [WARN]
     Ogg Vorbis 96k        dec    1.59 over   +2.15 play   -2.00   WARN  unity>0
     Ogg Vorbis 320k       dec   -0.31 over   +0.25 play   -3.90   WARN
     AAC 128k              dec    0.72 over   +1.28 play   -2.87   WARN  · Apple AAC
     AAC 256k              dec   -0.11 over   +0.45 play   -3.70   WARN  · Apple AAC
```

- **dec** — decoded true peak at unity gain. On a *loud* master this can exceed
  0 dBFS (`unity>0`) yet still be safe at playback, because normalization turns
  the track down.
- **samp** — the decoded **sample** peak (digital clip, dBFS).
- **over** — `decoded − master` true peak: how much this codec added.
- **play** — the true peak **after** the service's normalization; the **verdict is
  based on this** (what listeners actually hear with normalization on).

Every result ends with an **actionable recommendation**, computed on *your* file —
either "✓ Safe — survives conversion even at −0.5 dBTP, no need to pull down to −1"
or "→ lower your true-peak ceiling to about *X* dBTP" (the exact number that keeps
the worst codec under 0 dBFS), with the normalization caveat spelled out. That's the
answer to the "how hot can I master?" question, measured rather than guessed.

---

## How reliable is it?

| Claim | Confidence |
|---|---|
| Loudness & true peak | 🟢 High — ITU-R BS.1770-4 via ffmpeg's `ebur128` (float decode, 4× oversampled) |
| Overshoot direction & clipping flags | 🟢 High — a real encode→decode round-trip |
| Relative comparisons (codec vs codec, master vs master, lossless = clean) | 🟢 High |
| **AAC & MP3 on macOS** | 🟢 High — real encoders, validated within 0.2 dB vs iZotope Insight |
| AAC on Windows/Linux (no `aac_at`/FDK) | 🟠 Proxy — ffmpeg native AAC, flagged in the row |
| Ogg Vorbis / Opus | 🟢 Good — `libvorbis`/`libopus`, the reference encoders those services use |
| Absolute peak vs another tool/meter | 🟠 ±~0.3 dB — normal true-peak meter variance (see below) |
| Service parameters (targets, bitrates) | 🟠 Medium — approximate, change over time |

Use it as a **relative pre-delivery check** — "does my master have enough
true-peak headroom to survive lossy streaming, and which services/codecs are the
riskiest." It is a faithful, conservative **simulation**, not a bit-exact
prediction of any one service's encoder.

**On comparing to other tools:** two correct true-peak meters routinely disagree by
**~0.2–0.4 dB** — BS.1770 only mandates a *minimum* 4× oversampling, and meters
(ffmpeg, iZotope Insight, Sonnox ListenHub…) use anywhere from 4× to 16×. A
sub-0.5 dB gap between this tool and your meter is agreement, not error. Also
**measure the encoded file directly** in a file-based meter — importing into a DAW
resamples it to the project sample rate (a real artifact), and measuring real-time
playback adds the system audio path. The tool does **not** model service-side
pre-processing or resample to each service's delivery rate (a ~0.1 dB effect), and
it's never a substitute for critical listening.

This same reliability sheet is printed at the top of every HTML report and
summarised in the command-line output, so anyone reading a result knows exactly
how much to trust it.

---

## Free tools it builds on

- **ffmpeg** — `libvorbis`, native `aac`, `libopus`, `libmp3lame` encoders for the
  real round-trips (plus Apple's `aac_at` / AudioToolbox on macOS); `loudnorm`
  (ITU-R BS.1770 integrated loudness + 4× oversampled true peak) and `astats`
  (sample peak) for measurement.
- **macOS AudioToolbox / `afconvert`** — Apple's real CoreAudio AAC encoder
  (`aac_at`, constrained-VBR) for **all** AAC tiers, on macOS.
- **Python standard library** (incl. `tkinter`) for the app and report.
- **imageio-ffmpeg** / **tkinterdnd2** — optional, auto-installed on demand for the
  bundled ffmpeg and true drag-and-drop.

---

## Notes & limitations

- **AAC:** ffmpeg's *native* AAC encoder overshoots ~2 dB more than real encoders,
  so all AAC tiers use Apple's CoreAudio AAC (`aac_at`, constrained-VBR) on macOS,
  falling back to FDK → `afconvert` → native AAC (flagged) elsewhere. AAC true peak
  is inherently encoder-mode-dependent (±~1 dB); each row shows which encoder ran.
- **Vorbis / Opus / MP3** use `libvorbis` / `libopus` / LAME — the reference
  encoders those services actually use — in their normal quality/VBR modes.
- Lossless tiers (FLAC/ALAC) round-trip bit-exactly, so they add no overshoot; the
  tool reports them as such rather than re-encoding.
- Normalization is applied at **playback** — your uploaded file is never altered —
  so the "plays at" figure is what listeners hear, not a change to your master.
- ffmpeg's encoders are excellent references but not byte-identical to each
  service's internal builds; treat figures as a faithful, conservative simulation.

---

## License

MIT — see [LICENSE](LICENSE).
