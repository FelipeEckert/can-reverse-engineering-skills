# CAN bus reverse engineering skills for Codex

This repository provides Codex skills and deterministic Python tools for turning
recorded raw CAN or CAN FD traffic into verified DBC signal definitions. The
workflow helps locate the CAN ID and bit field for a physical value, determine
byte order, signedness, scale, and offset, and verify the result before it is
accepted.

This is a **Codex-oriented port and evolution** of the original CAN
reverse-engineering skills from [CSS Electronics](https://www.csselectronics.com).
The original project, methodology, bundled reference databases, and CANsub
integration remain clearly attributed to CSS Electronics.

- **`.agents/` is the maintained Codex implementation.** It contains the current
  skills, scripts, references, and assets.
- **`.claude/` is preserved legacy/original reference.** It is not the maintained
  implementation and is not kept synchronized with `.agents/`.

The maintained Codex skills are:

- **`can-reverse-engineering`** — survey, candidate analysis, correlation,
  bit-field search, DBC generation, and verification.
- **`combine-dbc`** — combine confirmed per-signal DBC files into one
  application-level DBC.

This is an analysis workflow rather than a turnkey vehicle decoder. A
reference-free result is a hypothesis to investigate, not a confirmed signal;
shipping a DBC definition still requires independent evidence and verification.

## What works offline

After Python dependencies have been installed, the core workflow runs locally
without CAN hardware or internet access. It supports:

- Validation and analysis of normalized CAN/CAN FD CSV logs.
- Import of classical-CAN SavvyCAN/GVRET CSV logs into the normalized format.
- Bus survey, changing-bit analysis, counter/checksum hints, and candidate
  profiling.
- Reference-free candidate ordering and physical encoding hypotheses.
- Decoding an OBD2, GPS, or other machine reference from the same log when a DBC
  is available, followed by correlation, bit search, calibration, and verification.
- Building single-signal DBC files and combining them with `combine-dbc`.
- Optional local opendbc corpus indexing and exact-precedent annotation.
- Optional local video/OCR reference extraction and review.

Reference-free analysis deliberately stops short of claiming that a candidate is
speed, RPM, temperature, or another physical signal. It ranks fields for
investigation and enumerates plausible interpretations. Use a known physical
state, a decoded on-bus reference, or another independent measurement to confirm
geometry and calibration.

## Offline quickstart

Run these commands from the repository root. Python 3.10 or newer is required.
The example assumes that `path/to/log.csv` is already in the normalized format
described below.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-core.txt

.venv/bin/python .agents/skills/can-reverse-engineering/scripts/survey.py \
  --trace path/to/log.csv \
  --json temp-output/survey_log.json

.venv/bin/python .agents/skills/can-reverse-engineering/scripts/candidate_profiler.py \
  --trace path/to/log.csv \
  --survey temp-output/survey_log.json \
  --out temp-output/candidate-analysis/candidate-ranking.json

.venv/bin/python .agents/skills/can-reverse-engineering/scripts/physical_hypotheses.py \
  --candidates temp-output/candidate-analysis/candidate-ranking.json \
  --out temp-output/physical-hypotheses.json
```

On Windows, `install.bat` creates `.venv` and installs the full dependency set.
For a smaller core-only Windows environment, use:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-core.txt
```

Open the cloned repository in Codex, or launch Codex from the repository root or
a directory below it. Codex scans `.agents/skills/` from the current working
directory upward through the repository root, so the maintained skills are
available without copying them elsewhere. See the
[official skill documentation](https://learn.chatgpt.com/docs/build-skills).

Example prompts:

> Survey `path/to/log.csv` and rank reference-free CAN signal candidates. Treat all physical interpretations as hypotheses.

> Reverse engineer vehicle speed from `trip.csv`. It contains an OBD2 speed reference on the same bus.

> Import `savvycan.csv`, preserve the original, and survey the normalized result.

> Combine the confirmed DBCs for `mercedes-e350` into one application DBC.

Reference large logs by file path rather than pasting or attaching their contents
to the conversation.

## Input formats

### Normalized CSV

The analysis pipeline consumes a semicolon-delimited, 12-column format compatible
with webCAN and `python-can-cansub` capture output:

```text
TimestampEpoch;BusChannel;ID;IDE;DLC;DataLength;Dir;EDL;BRS;ESI;RTR;DataBytes
```

These columns must appear first and in this order. Uniquely named trailing
annotation columns are accepted and ignored. Frames are validated strictly;
timestamps must be non-negative and monotonically non-decreasing. The loader does
not sort or repair malformed traces.

### SavvyCAN/GVRET CSV

The repository imports the common 14-column classical-CAN SavvyCAN/GVRET format:

```text
Time Stamp,ID,Extended,Dir,Bus,LEN,D1,D2,D3,D4,D5,D6,D7,D8
```

Convert it without overwriting the source:

```bash
.venv/bin/python .agents/skills/can-reverse-engineering/scripts/import_savvycan.py \
  --trace path/to/savvycan.csv \
  --out temp-output/trace_savvycan_normalized.csv
```

This importer supports classical CAN payloads up to 8 bytes. The source format
does not carry CAN FD flags or recoverable RTR metadata, so it is not a general
CAN FD importer. SavvyCAN timestamps remain relative, converted from microseconds
to seconds; workflows that require an absolute clock, particularly video
alignment, need an independently established time anchor.

### CANedge MF4

Direct MF4 import is **not implemented**. Convert CANedge MF4 externally to the
normalized CSV format, for example with `mdf2csv`, and analyze the converted CSV.
The converter is not bundled or installed by this repository.

CANedge recordings can still provide excellent machine references after
conversion. The maintained skill bundles DBCs for CANedge internal GPS/IMU on
CAN9, CANmod.gps, and standard OBD2 references.

## Installation options

All Python dependencies are installed into the repository-local `.venv`.

| File | Includes | Use it for |
| --- | --- | --- |
| `requirements-core.txt` | `cantools`, NumPy, pandas, SciPy, matplotlib | Normalized-CSV analysis, SavvyCAN import, reference-free analysis, DBC-reference workflows, and `combine-dbc` |
| `requirements-live.txt` | Core plus `python-can`, `python-can-cansub`, Flask, requests | Live CANsub capture/UI and experimental headless SocketCAN capture |
| `requirements-vision.txt` | Core plus Flask, RapidOCR, ONNX Runtime, OpenCV | Local video/OCR reference extraction and review |
| `requirements.txt` | Live plus vision | Full installation of every maintained workflow |

Each feature-specific file is self-contained: live and vision both include core.
The full macOS/Linux installation is:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The vision workflow additionally requires the external FFmpeg **`ffprobe`**
executable on `PATH`. FFmpeg is an operating-system dependency and is not
installed by pip. If `ffprobe` cannot supply a usable video timestamp, the vision
tool requires an explicit `--start-epoch` or another documented time anchor.

## Optional opendbc reference support

Opendbc is optional. Core candidate ordering and physical-hypothesis generation
work without it. When configured, a local opendbc checkout can be organized into
a read-only corpus and indexed so hypotheses can report exact encoding precedent:

```bash
.venv/bin/python .agents/skills/can-reverse-engineering/scripts/setup_opendbc_corpus.py \
  --opendbc-root /path/to/opendbc \
  --corpus-out /path/to/opendbc-corpus

.venv/bin/python .agents/skills/can-reverse-engineering/scripts/opendbc_reference.py \
  --corpus /path/to/opendbc-corpus \
  --out-dir /path/to/opendbc-reference
```

Pass the generated `signals.tsv` to `physical_hypotheses.py` with
`--opendbc-signals`. Corpus matches are supporting precedent only: they do not
change candidate ordering, confirm a signal, or replace verification.

## Live capture: current support and limits

**CANsub remains the default full live backend**, through `python-can-cansub`
over USB or Ethernet. The live workflow can discover a CANsub via mDNS, probe
common classical CAN/CAN FD bit rates passively, record a normalized trace, and
collect a synchronized human reference through a local Flask UI.

An **experimental headless SocketCAN capture path** is also available for Linux
interfaces that the user has already configured and brought up externally. It is
selected explicitly, never used as a fallback, and only receives frames:

```bash
.venv/bin/python .agents/skills/can-reverse-engineering/scripts/capture.py \
  --backend socketcan --channel can0 --duration 15 --label baseline
```

The production capture code does not set a bit rate, bring interfaces up or
down, or expose transmit/replay/injection operations. It writes the canonical
normalized 12-column CSV directly, so `survey.py` and the remaining offline
pipeline consume it unchanged. Application receive-only does not itself disable
hardware ACK behavior; configure the SocketCAN controller's listen-only/ACK mode
externally when electrical silence is required. SocketCAN Flask/UI integration
is not implemented in this phase. ETAS ES582 has not yet been validated and is
not claimed as supported.

The live scripts never transmit CAN data frames. The default `vehicle` profile is
silent/listen-only; the explicit `bench` profile allows the CANsub to acknowledge
frames from a single-node bench setup. Do not have a driver operate the reference
UI while driving—use a passenger/operator or a controlled test setup.

For CANsub live capture, typical hardware is:

- A [CANsub.2](https://www.csselectronics.com/products/can-fd-usb-interface-ethernet-cansub-2) CAN FD interface.
- An [OBD2-DB9 adapter cable](https://www.csselectronics.com/products/obd2-db9-adapter-cable), with an optional [contactless CAN reader](https://www.csselectronics.com/products/contactless-can-bus-reader-adapter).

## Output structure and combine-dbc

Confirmed signals are stored separately so each result retains its DBC,
verification plot, and analysis evidence:

```text
decoding-output/
  <application>/
    <signal>/<signal>.dbc
    <signal>/<signal>.png
    <signal>/analysis-plots/
    <application>.dbc
```

Combine all confirmed signals for an application with the `combine-dbc` skill or
run its deterministic script directly:

```bash
.venv/bin/python .agents/skills/combine-dbc/scripts/combine_dbc.py \
  --app mercedes-e350
```

This writes `decoding-output/mercedes-e350/mercedes-e350.dbc`. It is safe to
re-run as more per-signal DBCs are confirmed.

## Current signal-decoding scope

- Classical CAN and CAN FD normalized traces are supported.
- Target signals are plain and non-multiplexed. Target-side J1939 PGN logic,
  ISO-TP/transport reassembly, and multiplexor discovery are not implemented.
- A multiplexed reference DBC, such as OBD2, is supported because `cantools`
  decodes the reference before target-field analysis.
- Arbitrary bit-packed little-endian fields are searched. Big-endian candidate
  search is currently limited to byte-aligned fields.
- Verification is a gate. An unconfirmed candidate should not be presented as a
  decoded signal merely because it correlates or resembles an OEM convention.

## Repository structure

```text
.agents/skills/can-reverse-engineering/   maintained Codex workflow
.agents/skills/combine-dbc/               maintained DBC-combine workflow
.claude/                                  preserved legacy/original reference
docs/                                     public documentation assets
requirements-core.txt                     offline/core dependency group
requirements-live.txt                     live CANsub dependency group
requirements-vision.txt                   local vision dependency group
requirements.txt                          full dependency set
install.bat                               full Windows setup
```

Generated work belongs at the repository root under `temp-output/` and confirmed
deliverables under `decoding-output/`; do not run the workflow from inside a
skill directory.

## CSS Electronics attribution and license

This Codex-oriented port/evolution is based on the original CSS Electronics CAN
reverse-engineering skills and methodology. The preserved `.claude/` tree records
that original/legacy implementation; the current maintained implementation is in
`.agents/`.

For the original methodology, sample data, and demonstration, see:

- [CAN bus reverse engineering with AI](https://www.csselectronics.com/pages/can-bus-reverse-engineering-ai-llm-claude)
- [CANsub CAN + OBD2 sample data](https://www.csselectronics.com/pages/ai-can-bus-sniffer-data-pack)

[![Original CSS Electronics CAN reverse-engineering demonstration](docs/vision-reference-demo.png)](https://cdn.shopify.com/videos/c/o/v/e384c5a75b7943e681dcbad2d10e230a.mp4)

The repository is available under the [MIT License](LICENSE). Preserve the
license notice and CSS Electronics attribution when redistributing or adapting
the project.
