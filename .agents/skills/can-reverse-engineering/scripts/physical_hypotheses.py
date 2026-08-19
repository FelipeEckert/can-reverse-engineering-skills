#!/usr/bin/env python3
"""Rank unconfirmed physical interpretations for discovered CAN fields.

This reference-free triage aid tests a small, documented set of automotive
scale/offset priors. It never confirms a signal or calibrates a DBC.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from common import apply_sign, extract_be, extract_le, group_by_id, load_trace


DISCLAIMER = (
    "HYPOTHESES ONLY: these rankings are reference-free priors, not signal "
    "identifications. Confirm geometry and calibration against an independent "
    "physical reference before creating a DBC."
)
WEIGHTS = {
    "physical_range": 0.30, "bit_width": 0.15, "update_rate": 0.15,
    "dynamics": 0.20, "range_usage": 0.10, "common_encoding": 0.10,
}

# Broad physical bounds avoid rejecting valid signals from short captures.
# encoding is a fixed confidence in the convention itself, not in the signal ID.
PRIORS = [
    dict(name="engine speed", unit="rpm", scale=0.25, offset=0.0,
         bounds=(0, 12000), widths=(16,), periods=(5, 100), motion="fast",
         encoding=1.00, note="0.25 rpm/bit is a common 16-bit encoding"),
    dict(name="engine speed", unit="rpm", scale=0.125, offset=0.0,
         bounds=(0, 12000), widths=(16,), periods=(5, 100), motion="fast",
         encoding=0.80, note="0.125 rpm/bit is a common binary-fraction encoding"),
    dict(name="engine speed", unit="rpm", scale=1.0, offset=0.0,
         bounds=(0, 12000), widths=(8, 16), periods=(5, 100), motion="fast",
         encoding=0.70, note="integer rpm is simple but lower resolution"),
    dict(name="percentage / pedal / throttle", unit="%", scale="full_scale", offset=0.0,
         bounds=(0, 100), widths=(8, 10, 12, 16), periods=(5, 100), motion="control",
         encoding=1.00, note="unsigned full-scale maps the complete code range to 0..100%"),
    dict(name="percentage / pedal / throttle", unit="%", scale=0.4, offset=0.0,
         bounds=(0, 100), widths=(8,), periods=(5, 100), motion="control",
         encoding=0.95, note="0.4%/bit is a common 8-bit automotive convention"),
    dict(name="percentage / pedal / throttle", unit="%", scale=1.0, offset=0.0,
         bounds=(0, 100), widths=(8,), periods=(5, 100), motion="control",
         encoding=0.75, note="integer percent is a common simple encoding"),
    dict(name="temperature", unit="degC", scale=1.0, offset=-40.0,
         bounds=(-60, 300), widths=(8,), periods=(50, 2000), motion="slow",
         encoding=1.00, note="raw-40 degC is a common 8-bit temperature encoding"),
    dict(name="temperature", unit="degC", scale=0.1, offset=-40.0,
         bounds=(-60, 300), widths=(16,), periods=(20, 2000), motion="slow",
         encoding=0.85, note="0.1 degC/bit with a -40 offset is a common fine encoding"),
    dict(name="vehicle or wheel speed", unit="km/h", scale=1 / 256, offset=0.0,
         bounds=(0, 350), widths=(16,), periods=(5, 100), motion="fast",
         encoding=1.00, note="1/256 km/h is a common 16-bit wheel-speed convention"),
    dict(name="vehicle or wheel speed", unit="km/h", scale=1 / 64, offset=0.0,
         bounds=(0, 350), widths=(16,), periods=(5, 100), motion="fast",
         encoding=0.95, note="1/64 km/h is a common binary-fraction speed encoding"),
    dict(name="vehicle or wheel speed", unit="km/h", scale=0.01, offset=0.0,
         bounds=(0, 350), widths=(16,), periods=(5, 100), motion="fast",
         encoding=0.90, note="0.01 km/h/bit is a common decimal speed encoding"),
    dict(name="vehicle or wheel speed", unit="km/h", scale=0.1, offset=0.0,
         bounds=(0, 350), widths=(8, 16), periods=(5, 100), motion="fast",
         encoding=0.85, note="0.1 km/h/bit is a common decimal speed encoding"),
    dict(name="pressure", unit="kPa", scale=1.0, offset=0.0,
         bounds=(0, 3000), widths=(8, 16), periods=(5, 200), motion="moderate",
         encoding=0.90, note="integer kPa is a common pressure encoding"),
    dict(name="pressure", unit="kPa", scale=2.0, offset=0.0,
         bounds=(0, 3000), widths=(8, 16), periods=(5, 200), motion="moderate",
         encoding=1.00, note="2 kPa/bit is a common boost-pressure convention"),
    dict(name="pressure", unit="bar", scale=0.1, offset=0.0,
         bounds=(0, 30), widths=(8, 16), periods=(5, 200), motion="moderate",
         encoding=0.80, note="0.1 bar/bit is a common compact pressure encoding"),
    dict(name="torque", unit="Nm", scale=0.1, offset="centered",
         bounds=(-1500, 1500), widths=(16,), periods=(5, 100), motion="fast",
         encoding=0.85, note="0.1 Nm/bit with an unsigned midpoint bias allows signed torque"),
    dict(name="torque", unit="Nm", scale=1.0, offset="centered",
         bounds=(-1500, 1500), widths=(12, 16), periods=(5, 100), motion="fast",
         encoding=0.75, note="integer Nm with an unsigned midpoint bias allows signed torque"),
    dict(name="torque percentage", unit="%", scale=1.0, offset=-125.0,
         bounds=(-125, 125), widths=(8,), periods=(5, 100), motion="fast",
         encoding=1.00, note="raw-125% is a common engine-torque convention"),
    dict(name="low-voltage electrical", unit="V", scale=0.05, offset=0.0,
         bounds=(0, 60), widths=(8, 16), periods=(10, 1000), motion="slow",
         encoding=1.00, note="0.05 V/bit is a common battery-potential convention"),
    dict(name="low-voltage electrical", unit="V", scale=0.01, offset=0.0,
         bounds=(0, 60), widths=(12, 16), periods=(10, 1000), motion="slow",
         encoding=0.90, note="0.01 V/bit is a common fine low-voltage encoding"),
    dict(name="high-voltage electrical", unit="V", scale=0.1, offset=0.0,
         bounds=(0, 1000), widths=(12, 16), periods=(10, 1000), motion="moderate",
         encoding=0.90, note="0.1 V/bit is a common high-voltage encoding"),
]


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def parse_can_id(value: Any) -> int:
    return value if isinstance(value, int) else int(str(value), 0)


def load_candidates(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("candidates", data.get("ranked_candidates", []))
    if not isinstance(data, list):
        raise ValueError("candidate JSON must be a list or contain a candidates list")
    return data


def raw_stats(candidate: dict[str, Any], groups: dict[int, Any] | None) -> dict[str, float]:
    if "raw_min" in candidate and "raw_max" in candidate:
        lo, hi = float(candidate["raw_min"]), float(candidate["raw_max"])
        return {
            "raw_min": lo, "raw_q01": float(candidate.get("raw_q01", lo)),
            "raw_q99": float(candidate.get("raw_q99", hi)), "raw_max": hi,
            "raw_edge_fraction": float(candidate.get("raw_edge_fraction", 0.0)),
            "samples": int(candidate.get("samples", 0)),
        }
    if groups is None:
        raise ValueError("candidates lack raw_min/raw_max; provide --trace to extract them")
    can_id = parse_can_id(candidate.get("id", candidate.get("id_hex")))
    if can_id not in groups:
        raise ValueError(f"candidate CAN ID 0x{can_id:X} is absent from trace")
    group = groups[can_id]
    start, length = int(candidate["start_bit"]), int(candidate["length_bits"])
    order, signed = str(candidate.get("order", "little")).lower(), bool(candidate.get("signed", False))
    if order in ("little", "single-byte"):
        raw = extract_le(group.le_int, start, length)
    elif order == "big" and start % 8 == 0 and length % 8 == 0:
        raw = extract_be(group.be_int, group.length, start // 8, length // 8)
    else:
        raise ValueError(f"unsupported candidate geometry: order={order}, start={start}, length={length}")
    raw = apply_sign(raw, length, signed)
    code_lo = -(1 << (length - 1)) if signed else 0
    code_hi = (1 << (length - 1)) - 1 if signed else (1 << length) - 1
    q01, q99 = np.quantile(raw, [0.01, 0.99])
    return {
        "raw_min": float(np.min(raw)), "raw_q01": float(q01),
        "raw_q99": float(q99), "raw_max": float(np.max(raw)),
        "raw_edge_fraction": float(np.mean((raw == code_lo) | (raw == code_hi))),
        "samples": int(raw.size),
    }


def width_score(length: int, preferred: tuple[int, ...]) -> float:
    return clamp01(math.exp(-min(abs(length - width) for width in preferred) / 4.0))


def period_score(period_ms: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    if low <= period_ms <= high:
        return 1.0
    nearest = low if period_ms < low else high
    return clamp01(math.exp(-abs(math.log2(max(period_ms, 0.001) / nearest))))


def physical_range_score(qlo: float, qhi: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    width = high - low
    outside = max(0.0, low - qlo) + max(0.0, qhi - high)
    inside = math.exp(-5.0 * outside / max(width, 1e-9))
    span = clamp01(0.35 + 0.65 * max(0.0, qhi - qlo) / max(0.10 * width, 1e-9))
    return clamp01(inside * span)


def dynamics_score(candidate: dict[str, Any], motion: str) -> float:
    plaus = clamp01(float(candidate.get("plausibility", candidate.get("score", 0.5))))
    ac1 = clamp01((float(candidate.get("ac1", 0.0)) + 1.0) / 2.0)
    jump = clamp01(1.0 - float(candidate.get("jump_rate", 0.0)))
    structure = clamp01(1.0 - float(candidate.get("delta_entropy_norm", 0.5)))
    change = clamp01(float(candidate.get("change_rate", 0.0)))
    if motion == "slow":
        activity = 1.0 if change <= 0.30 else clamp01(1.0 - (change - 0.30) / 0.70)
    elif motion == "control":
        activity = clamp01(0.55 + min(change, 0.45))
    elif motion == "fast":
        activity = clamp01(0.50 + min(change, 0.50))
    else:
        activity = clamp01(0.65 + min(change, 0.35))
    return 0.30 * plaus + 0.25 * ac1 + 0.20 * jump + 0.15 * structure + 0.10 * activity


def usage_score(stats: dict[str, float], length: int) -> tuple[float, float]:
    usage = clamp01((stats["raw_q99"] - stats["raw_q01"]) / max(float((1 << length) - 1), 1.0))
    coverage = clamp01((math.log10(max(usage, 1e-8)) + 5.0) / 3.0)
    saturation = clamp01(1.0 - stats["raw_edge_fraction"] / 0.50)
    return 0.70 * coverage + 0.30 * saturation, usage


def scale_offset(prior: dict[str, Any], length: int, signed: bool) -> tuple[float, float]:
    scale = 100.0 / ((1 << length) - 1) if prior["scale"] == "full_scale" else float(prior["scale"])
    offset = (0.0 if signed else -scale * float(1 << (length - 1))) if prior["offset"] == "centered" else float(prior["offset"])
    return scale, offset


def score_hypothesis(candidate: dict[str, Any], stats: dict[str, float], prior: dict[str, Any]) -> dict[str, Any]:
    length, signed = int(candidate["length_bits"]), bool(candidate.get("signed", False))
    period_ms = float(candidate.get("period_ms", 0.0))
    scale, offset = scale_offset(prior, length, signed)
    qlo, qhi = sorted((offset + scale * stats["raw_q01"], offset + scale * stats["raw_q99"]))
    use_score, usage = usage_score(stats, length)
    parts = {
        "physical_range": physical_range_score(qlo, qhi, prior["bounds"]),
        "bit_width": width_score(length, prior["widths"]),
        "update_rate": period_score(period_ms, prior["periods"]),
        "dynamics": dynamics_score(candidate, prior["motion"]),
        "range_usage": use_score, "common_encoding": float(prior["encoding"]),
    }
    score = 100.0 * sum(WEIGHTS[key] * parts[key] for key in WEIGHTS)
    reasoning = (
        f"range {parts['physical_range']:.2f}: observed {qlo:.4g}..{qhi:.4g} {prior['unit']} "
        f"vs broad {prior['bounds'][0]}..{prior['bounds'][1]}; "
        f"width {parts['bit_width']:.2f}: {length}b vs preferred {list(prior['widths'])}; "
        f"rate {parts['update_rate']:.2f}: {period_ms:g} ms vs {prior['periods'][0]}..{prior['periods'][1]}; "
        f"dynamics {parts['dynamics']:.2f}: plaus={float(candidate.get('plausibility', 0.5)):.2f}, "
        f"ac1={float(candidate.get('ac1', 0.0)):.2f}, change={float(candidate.get('change_rate', 0.0)):.1%}; "
        f"usage {parts['range_usage']:.2f}: {usage:.3%} of raw codes, edge={stats['raw_edge_fraction']:.1%}; "
        f"encoding {parts['common_encoding']:.2f}: {prior['note']}"
    )
    return {
        "label": "HYPOTHESIS", "hypothesis": prior["name"], "unit": prior["unit"],
        "scale": scale, "offset": offset, "score": round(score, 4),
        "observed_physical_q01": qlo, "observed_physical_q99": qhi,
        "score_breakdown": {key: round(value, 4) for key, value in parts.items()},
        "reasoning": reasoning,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, help="candidate JSON with field metadata")
    parser.add_argument("--trace", help="normalized webCAN CSV; required if raw ranges are absent")
    parser.add_argument("--candidate-limit", type=int, default=12)
    parser.add_argument("--top", type=int, default=20, help="global hypotheses to keep")
    parser.add_argument("--out", default="temp-output/physical-hypotheses.json")
    args = parser.parse_args()

    candidates = load_candidates(Path(args.candidates))[:max(1, args.candidate_limit)]
    groups = group_by_id(load_trace(args.trace)) if args.trace else None
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        stats = raw_stats(candidate, groups)
        can_id = parse_can_id(candidate.get("id", candidate.get("id_hex")))
        identity = {
            "id_hex": candidate.get("id_hex") or f"0x{can_id:X}",
            "field": candidate.get("field", "unnamed field"),
            "order": candidate.get("order", "little"),
            "start_bit": int(candidate["start_bit"]), "length_bits": int(candidate["length_bits"]),
            "signed": bool(candidate.get("signed", False)),
            "period_ms": float(candidate.get("period_ms", 0.0)), **stats,
        }
        for prior in PRIORS:
            ranked.append({"candidate": identity, **score_hypothesis(candidate, stats, prior)})

    ranked.sort(key=lambda row: (
        -row["score"], row["candidate"]["id_hex"], row["candidate"]["start_bit"],
        row["candidate"]["length_bits"], row["hypothesis"], row["scale"], row["offset"],
    ))
    ranked = ranked[:max(1, args.top)]
    for index, row in enumerate(ranked, 1):
        row["rank"] = index

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "label": "HYPOTHESES ONLY", "disclaimer": DISCLAIMER,
        "method": "deterministic weighted automotive priors", "weights": WEIGHTS,
        "candidates_considered": len(candidates), "ranked_hypotheses": ranked,
    }, indent=2) + "\n")
    csv_out = out.with_suffix(".csv")
    columns = [
        "rank", "id_hex", "field", "start_bit", "length_bits", "order", "signed",
        "period_ms", "raw_min", "raw_max", "hypothesis", "unit", "scale", "offset",
        "score", "observed_physical_q01", "observed_physical_q99", "reasoning",
    ]
    with csv_out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in ranked:
            writer.writerow({key: row["candidate"].get(key, row.get(key)) for key in columns})

    print(DISCLAIMER)
    print(f"Scored {len(candidates)} candidate fields with fixed weights: {WEIGHTS}")
    for row in ranked[:min(10, len(ranked))]:
        c = row["candidate"]
        print(f"#{row['rank']:2d} HYPOTHESIS {c['id_hex']} {c['field']} -> {row['hypothesis']} "
              f"[{row['score']:.1f}/100], scale={row['scale']:.8g}, "
              f"offset={row['offset']:.8g} {row['unit']}")
        print(f"    {row['reasoning']}")
    print(f"Wrote {out}")
    print(f"Wrote {csv_out}")


if __name__ == "__main__":
    main()
