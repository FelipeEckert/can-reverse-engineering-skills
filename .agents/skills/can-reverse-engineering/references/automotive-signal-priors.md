# Automotive physical-signal priors

Use this reference only for the optional, reference-free physical-hypothesis
stage. These priors enumerate interpretations worth testing; they do not identify
a signal. A matching range or familiar factor is weak evidence without an
independent physical reference.

## Inputs and output

`physical_hypotheses.py` consumes discovered field metadata plus raw-range and
observed-behavior metadata. Candidate JSON should provide CAN ID, start bit, bit
length, byte order, signedness when known, update period, and available observed
behavior metrics. It may also provide `raw_min`, `raw_max`, `raw_q01`,
`raw_q99`, and `raw_edge_fraction`. If raw statistics are absent, pass the
normalized trace and the script extracts them without modifying the trace.

Every result is labelled `HYPOTHESIS` and reports candidate geometry, proposed
signal family, factor/offset, decoded percentile range, concise observed behavior,
and exact opendbc precedent when available. Treat the output as a way to choose
the next experiment or reference, never as permission to name a DBC signal.

## Evaluation and deterministic ordering

There is no physical-hypothesis score or semantic ranking. The script preserves:

1. The upstream candidate profiler's order.
2. The declared hypothesis-generation order within each candidate.

The global `--limit` is applied after deterministic round-robin flattening: the first
declared interpretation for each candidate in profiler order, then the second for each,
and so on. This keeps competing candidates visible in bounded output while preserving
both orders; a partial final round favors earlier upstream candidates. Neither opendbc
support nor any other semantic evidence changes that order or filters the output. The
agent evaluates each interpretation from separate evidence: observed CAN behavior,
physical plausibility of the decoded values, exact corpus precedent, and independent
reference agreement. These dimensions are not collapsed into a scalar confidence value.

Short captures often exercise only a small part of a valid signal, while composite
or over-wide fields may use a tiny fraction of their available codes. Neither fact
alone proves or disproves an interpretation.

## Tested conventions

- Engine speed: 0.25, 0.125, and 1 rpm/bit.
- Percentage / pedal / throttle: unsigned full-scale 0–100%, 0.4%/bit, and
  integer percent.
- Temperature: raw minus 40 °C and 0.1 °C/bit with a −40 °C offset.
- Vehicle or wheel speed: 1/256, 1/64, 0.01, and 0.1 km/h per bit.
- Pressure: integer or 2 kPa/bit and 0.1 bar/bit.
- Torque: 0.1 or 1 Nm/bit with a midpoint bias, plus raw minus 125 percent.
- Voltage: 0.05 or 0.01 V/bit for low voltage and 0.1 V/bit for high voltage.

These are common conventions, not an exhaustive catalog. Exact opendbc evidence is
reported only through the read-only prior lookup as corpus precedent, not decoding
confidence.

The corpus index is optional and may live outside the working repository. Build it with:

```bash
.venv/bin/python .agents/skills/can-reverse-engineering/scripts/opendbc_reference.py \
  --corpus /path/to/opendbc-corpus \
  --out-dir /path/to/opendbc-reference
```

Then pass
`--opendbc-signals /path/to/opendbc-reference/signals.tsv` to
`physical_hypotheses.py`. Alternatively set `CAN_RE_OPENDDBC_CORPUS` for indexing and
`CAN_RE_OPENDDBC_SIGNALS` for lookup. Explicit CLI paths take precedence. With no
configured signals index, physical-hypothesis generation continues with corpus precedent
marked unavailable; no exact match is neutral evidence.

## Use in the workflow

Run this only after candidate geometry has been narrowed by survey/candidate
analysis or identified by bitsearch, and before reference-based calibration:

```bash
.venv/bin/python .agents/skills/can-reverse-engineering/scripts/physical_hypotheses.py \
  --candidates temp-output/candidate-analysis/candidate-ranking.json \
  --trace temp-output/trace_run_normalized.csv \
  --out temp-output/physical-hypotheses.json
```

Use the hypotheses to request a discriminating excitation or physical reference.
Then return to correlate/bitsearch as needed and calibrate only against that
reference. Do not copy a proposed factor or offset into a DBC merely because it
has corpus precedent or looks physically plausible.
