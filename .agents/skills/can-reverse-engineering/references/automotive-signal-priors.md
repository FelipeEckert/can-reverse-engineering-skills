# Automotive physical-signal priors

Use this reference only for the optional, reference-free physical-hypothesis
stage. These priors rank interpretations worth testing; they do not identify a
signal. A matching range or familiar scale is weak evidence without an
independent physical reference.

## Inputs and output

`physical_hypotheses.py` consumes discovered field metadata plus raw-range and
dynamics metadata. Candidate JSON should provide CAN ID, start bit, bit length,
byte order, signedness when known, update period, and the survey/candidate
smoothness metrics. It may also provide `raw_min`, `raw_max`, `raw_q01`,
`raw_q99`, and `raw_edge_fraction`. If raw statistics are absent, pass the
normalized trace and the script extracts them without modifying the trace.

Every result is labelled `HYPOTHESIS`. Treat the output as a way to choose the
next experiment or reference, never as permission to name a DBC signal.

## Deterministic score

The total score is a fixed weighted sum on a 0–100 scale:

| Component | Weight | Evidence |
|---|---:|---|
| Physical range | 30% | Observed 1st–99th percentile after scale/offset vs a broad physical domain |
| Bit width | 15% | Distance from widths commonly used for that quantity |
| Update rate | 15% | Message period vs a broad quantity-specific period band |
| Smoothness/dynamics | 20% | Candidate plausibility, lag-1 autocorrelation, jump rate, delta structure, and activity |
| Saturation/range usage | 10% | Fraction of available raw codes exercised plus time at the field extrema |
| Common encoding | 10% | Fixed prior for how conventional the tested scale/offset is |

All component values, weights, observed ranges, and a one-line explanation are
written to JSON. Ties use stable candidate geometry and hypothesis keys, so the
same inputs produce the same ranking.

The physical bounds are intentionally broad. Short captures often exercise only
a small part of a valid signal, while composite or over-wide fields tend to use a
tiny fraction of their available codes. Neither fact alone proves or disproves an
interpretation.

## Tested conventions

- Engine speed: 0.25, 0.125, and 1 rpm/bit.
- Percentage / pedal / throttle: unsigned full-scale 0–100%, 0.4%/bit, and
  integer percent.
- Temperature: raw minus 40 °C and 0.1 °C/bit with a −40 °C offset.
- Vehicle or wheel speed: 1/256, 1/64, 0.01, and 0.1 km/h per bit.
- Pressure: integer or 2 kPa/bit and 0.1 bar/bit.
- Torque: 0.1 or 1 Nm/bit with a midpoint bias, plus raw minus 125 percent.
- Voltage: 0.05 or 0.01 V/bit for low voltage and 0.1 V/bit for high voltage.

These are common conventions, not an exhaustive catalog. Do not add
manufacturer-specific databases or `opendbc` evidence at this stage.

## Use in the workflow

Run this only after candidate geometry has been narrowed by survey/candidate
analysis or identified by bitsearch, and before reference-based calibration:

```bash
.venv/bin/python .agents/skills/can-reverse-engineering/scripts/physical_hypotheses.py \
  --candidates temp-output/candidate-analysis/candidate-ranking.json \
  --trace temp-output/trace_run_normalized.csv \
  --out temp-output/physical-hypotheses.json
```

Use the ranking to request a discriminating excitation or physical reference.
Then return to correlate/bitsearch as needed and calibrate only against that
reference. Do not copy a hypothesis scale or offset into a DBC merely because it
ranked highly.
