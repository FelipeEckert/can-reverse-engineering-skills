#!/usr/bin/env python3
"""Enumerate unconfirmed physical interpretations for discovered CAN fields.

This reference-free triage aid tests a small, documented set of automotive
factor/offset proposals. It never confirms a signal or calibrates a DBC.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from common import apply_sign, extract_be, extract_le, group_by_id, load_trace
from trace_validation import TraceValidationError

try:
    import opendbc_prior_lookup
except Exception as exc:  # Optional annotation must never block hypothesis generation.
    opendbc_prior_lookup = None
    OPENDBC_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    OPENDBC_IMPORT_ERROR = None


DISCLAIMER = (
    "HYPOTHESES ONLY: these interpretations are reference-free proposals, not signal "
    "identifications. Confirm geometry and calibration against an independent "
    "physical reference before creating a DBC."
)

CORPUS_INTERPRETATION = (
    "Exact opendbc corpus precedent is annotation only, not decoding confidence. "
    "It does not affect hypothesis ordering or filtering."
)
TORQUE_CORPUS_FAMILIES = (
    "torque.engine_torque", "torque.steering_torque", "torque.motor_torque",
    "torque.driveline_wheel_torque", "torque.request_command", "torque.limit",
)
CORPUS_FAMILY_MAP = {
    "engine speed": ("engine_rpm",),
    "percentage / pedal / throttle": ("pedal_throttle",),
    "temperature": ("temperature",),
    "vehicle or wheel speed": ("vehicle_speed", "wheel_speed"),
    "torque": TORQUE_CORPUS_FAMILIES,
    "torque percentage": TORQUE_CORPUS_FAMILIES,
    "low-voltage electrical": ("voltage.low_voltage_system",),
    "high-voltage electrical": ("voltage.hv_pack",),
}

# Stable generation order. These are encodings to inspect, not ranked beliefs.
HYPOTHESIS_ENCODINGS = [
    dict(name="engine speed", unit="rpm", factor=0.25, offset=0.0),
    dict(name="engine speed", unit="rpm", factor=0.125, offset=0.0),
    dict(name="engine speed", unit="rpm", factor=1.0, offset=0.0),
    dict(name="percentage / pedal / throttle", unit="%", factor="full_scale", offset=0.0),
    dict(name="percentage / pedal / throttle", unit="%", factor=0.4, offset=0.0),
    dict(name="percentage / pedal / throttle", unit="%", factor=1.0, offset=0.0),
    dict(name="temperature", unit="degC", factor=1.0, offset=-40.0),
    dict(name="temperature", unit="degC", factor=0.1, offset=-40.0),
    dict(name="vehicle or wheel speed", unit="km/h", factor=1 / 256, offset=0.0),
    dict(name="vehicle or wheel speed", unit="km/h", factor=1 / 64, offset=0.0),
    dict(name="vehicle or wheel speed", unit="km/h", factor=0.01, offset=0.0),
    dict(name="vehicle or wheel speed", unit="km/h", factor=0.1, offset=0.0),
    dict(name="pressure", unit="kPa", factor=1.0, offset=0.0),
    dict(name="pressure", unit="kPa", factor=2.0, offset=0.0),
    dict(name="pressure", unit="bar", factor=0.1, offset=0.0),
    dict(name="torque", unit="Nm", factor=0.1, offset="centered"),
    dict(name="torque", unit="Nm", factor=1.0, offset="centered"),
    dict(name="torque percentage", unit="%", factor=1.0, offset=-125.0),
    dict(name="low-voltage electrical", unit="V", factor=0.05, offset=0.0),
    dict(name="low-voltage electrical", unit="V", factor=0.01, offset=0.0),
    dict(name="high-voltage electrical", unit="V", factor=0.1, offset=0.0),
]


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


OBSERVED_BEHAVIOR_FIELDS = (
    "ac1", "wrap_rate", "jump_rate", "change_rate", "delta_entropy_norm", "distinct",
)


def observed_behavior(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return measured profiler properties without carrying its scalar rank score."""

    return {key: candidate[key] for key in OBSERVED_BEHAVIOR_FIELDS if key in candidate}


def factor_offset(encoding: dict[str, Any], length: int, signed: bool) -> tuple[float, float]:
    factor = (
        100.0 / ((1 << length) - 1)
        if encoding["factor"] == "full_scale"
        else float(encoding["factor"])
    )
    offset = (
        0.0 if signed else -factor * float(1 << (length - 1))
    ) if encoding["offset"] == "centered" else float(encoding["offset"])
    return factor, offset


def build_hypothesis(
    candidate: dict[str, Any], stats: dict[str, float], encoding: dict[str, Any]
) -> dict[str, Any]:
    length, signed = int(candidate["length_bits"]), bool(candidate.get("signed", False))
    factor, offset = factor_offset(encoding, length, signed)
    qlo, qhi = sorted(
        (offset + factor * stats["raw_q01"], offset + factor * stats["raw_q99"])
    )
    return {
        "label": "HYPOTHESIS",
        "hypothesis": encoding["name"],
        "unit": encoding["unit"],
        "factor": factor,
        "offset": offset,
        "decoded_physical_q01": qlo,
        "decoded_physical_q99": qhi,
    }


def load_corpus_document(
    signals_path: Path | None = None,
) -> tuple[dict[str, object] | None, str | None]:
    """Load optional corpus evidence through the lookup layer, failing open."""

    if opendbc_prior_lookup is None:
        return None, f"lookup layer unavailable: {OPENDBC_IMPORT_ERROR}"
    try:
        resolved = opendbc_prior_lookup.priors.resolve_signals_path(signals_path)
        if resolved is None:
            return None, (
                "indexed corpus not configured; pass --opendbc-signals "
                f"/path/to/signals.tsv or set {opendbc_prior_lookup.priors.SIGNALS_ENV}"
            )
        if not resolved.is_file():
            raise FileNotFoundError(f"indexed corpus not found: {resolved}")
        return opendbc_prior_lookup.load_prior_document(resolved), None
    except Exception as exc:  # Annotation failures must not alter normal generation.
        return None, f"corpus precedent unavailable: {type(exc).__name__}: {exc}"


def corpus_precedent(
    hypothesis: dict[str, Any],
    candidate: dict[str, Any],
    document: dict[str, object] | None,
    unavailable_reason: str | None,
) -> dict[str, Any]:
    """Annotate one hypothesis with exact family-specific corpus precedent."""

    annotation: dict[str, Any] = {
        "available": document is not None,
        "exact_match": None,
        "interpretation": CORPUS_INTERPRETATION,
        "family_results": [],
    }
    if document is None:
        annotation["unavailable_reason"] = unavailable_reason or "corpus precedent unavailable"
        return annotation

    families = CORPUS_FAMILY_MAP.get(str(hypothesis["hypothesis"]))
    if not families:
        annotation["available"] = False
        annotation["unavailable_reason"] = (
            f"no exact opendbc prior family for {hypothesis['hypothesis']!r}"
        )
        return annotation

    family_results = []
    try:
        for family in families:
            result = opendbc_prior_lookup.lookup_exact(
                document,
                family=family,
                bit_length=int(candidate["length_bits"]),
                signedness="signed" if candidate["signed"] else "unsigned",
                factor=str(hypothesis["factor"]),
                offset=str(hypothesis["offset"]),
                unit=str(hypothesis["unit"]) if hypothesis.get("unit") else None,
            )
            match = result["matches"][0] if result["matches"] else None
            family_results.append({
                "signal_family": family,
                "exact_match": bool(match),
                "qualitative_corpus_support": (
                    match["qualitative_corpus_support"] if match else None
                ),
                "independent_oem_family_count": (
                    match["independent_oem_family_count"] if match else 0
                ),
                "independent_oem_families": (
                    match["independent_oem_families"] if match else []
                ),
                "parser_provenance": (
                    match["parser_counts"]
                    if match else {"strict": 0, "lenient": 0, "fallback": 0}
                ),
                "matched_normalized_encoding_pattern": (
                    match["matched_normalized_encoding_pattern"] if match else None
                ),
            })
    except Exception as exc:
        annotation["available"] = False
        annotation["unavailable_reason"] = (
            f"exact lookup unavailable: {type(exc).__name__}: {exc}"
        )
        return annotation
    annotation["family_results"] = family_results
    annotation["exact_match"] = any(result["exact_match"] for result in family_results)
    return annotation


def corpus_console_summary(annotation: dict[str, Any]) -> str:
    if not annotation["available"]:
        return f"unavailable ({annotation['unavailable_reason']})"
    summaries = []
    for result in annotation["family_results"]:
        if result["exact_match"]:
            provenance = result["parser_provenance"]
            summaries.append(
                f"{result['signal_family']}: exact=yes, support={result['qualitative_corpus_support']}, "
                f"independent OEM/families={result['independent_oem_family_count']}, "
                f"strict/lenient/fallback={provenance['strict']}/{provenance['lenient']}/{provenance['fallback']}"
            )
        else:
            summaries.append(f"{result['signal_family']}: exact=no")
    return "; ".join(summaries)


def round_robin_hypotheses(
    hypotheses_by_candidate: list[list[dict[str, Any]]], limit: int
) -> list[dict[str, Any]]:
    """Apply a global limit in stable interpretation rounds across candidates."""

    emitted: list[dict[str, Any]] = []
    bounded_limit = max(1, limit)
    rounds = max((len(rows) for rows in hypotheses_by_candidate), default=0)
    for interpretation_index in range(rounds):
        for candidate_rows in hypotheses_by_candidate:
            if interpretation_index >= len(candidate_rows):
                continue
            emitted.append(candidate_rows[interpretation_index])
            if len(emitted) == bounded_limit:
                return emitted
    return emitted


def main() -> None:
    signals_env = (
        opendbc_prior_lookup.priors.SIGNALS_ENV
        if opendbc_prior_lookup is not None else "CAN_RE_OPENDDBC_SIGNALS"
    )
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"Alternatively set {signals_env}=/path/to/signals.tsv.",
    )
    parser.add_argument("--candidates", required=True, help="candidate JSON with field metadata")
    parser.add_argument("--trace", help="normalized webCAN CSV; required if raw ranges are absent")
    parser.add_argument("--candidate-limit", type=int, default=12)
    parser.add_argument(
        "--limit", type=int, default=20,
        help="maximum hypotheses to emit globally after round-robin candidate balancing",
    )
    parser.add_argument("--out", default="temp-output/physical-hypotheses.json")
    parser.add_argument(
        "--opendbc-signals", type=Path,
        help=(
            "optional indexed signals.tsv for exact corpus precedent; takes "
            f"precedence over {signals_env}"
        ),
    )
    args = parser.parse_args()

    candidates = load_candidates(Path(args.candidates))[:max(1, args.candidate_limit)]
    groups = group_by_id(load_trace(args.trace)) if args.trace else None
    corpus_document, corpus_unavailable_reason = load_corpus_document(args.opendbc_signals)
    hypotheses_by_candidate: list[list[dict[str, Any]]] = []
    for candidate_index, candidate in enumerate(candidates, 1):
        stats = raw_stats(candidate, groups)
        can_id = parse_can_id(candidate.get("id", candidate.get("id_hex")))
        identity = {
            "upstream_candidate_order": candidate_index,
            "id_hex": candidate.get("id_hex") or f"0x{can_id:X}",
            "field": candidate.get("field", "unnamed field"),
            "order": candidate.get("order", "little"),
            "start_bit": int(candidate["start_bit"]), "length_bits": int(candidate["length_bits"]),
            "signed": bool(candidate.get("signed", False)),
            "period_ms": float(candidate.get("period_ms", 0.0)),
            "observed_behavior": observed_behavior(candidate),
            **stats,
        }
        candidate_hypotheses: list[dict[str, Any]] = []
        for encoding in HYPOTHESIS_ENCODINGS:
            hypothesis = build_hypothesis(candidate, stats, encoding)
            hypothesis["corpus_precedent"] = corpus_precedent(
                hypothesis, identity, corpus_document, corpus_unavailable_reason
            )
            candidate_hypotheses.append({"candidate": identity, **hypothesis})
        hypotheses_by_candidate.append(candidate_hypotheses)

    hypotheses = round_robin_hypotheses(hypotheses_by_candidate, args.limit)
    candidates_represented = len({
        row["candidate"]["upstream_candidate_order"] for row in hypotheses
    })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "label": "HYPOTHESES ONLY", "disclaimer": DISCLAIMER,
        "method": "deterministic enumeration of automotive encoding hypotheses",
        "ordering": (
            "stable hypothesis-generation rounds, each in upstream candidate input order; "
            "no semantic or opendbc-based ranking"
        ),
        "corpus_precedent": {
            "available": corpus_document is not None,
            "interpretation": CORPUS_INTERPRETATION,
            "unavailable_reason": corpus_unavailable_reason,
        },
        "candidates_considered": len(candidates),
        "candidates_represented": candidates_represented,
        "hypotheses_emitted": len(hypotheses),
        "physical_hypotheses": hypotheses,
    }, indent=2) + "\n")
    csv_out = out.with_suffix(".csv")
    columns = [
        "upstream_candidate_order", "id_hex", "field", "start_bit", "length_bits", "order", "signed",
        "period_ms", "raw_min", "raw_max", "hypothesis", "unit", "factor", "offset",
        "decoded_physical_q01", "decoded_physical_q99", "observed_behavior", "corpus_precedent",
    ]
    with csv_out.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in hypotheses:
            csv_row = {key: row["candidate"].get(key, row.get(key)) for key in columns}
            csv_row["observed_behavior"] = json.dumps(
                row["candidate"]["observed_behavior"], sort_keys=True, separators=(",", ":")
            )
            csv_row["corpus_precedent"] = json.dumps(
                row["corpus_precedent"], sort_keys=True, separators=(",", ":")
            )
            writer.writerow(csv_row)

    print(DISCLAIMER)
    print(
        f"Emitted {len(hypotheses)} hypotheses representing "
        f"{candidates_represented}/{len(candidates)} considered candidates in stable "
        "hypothesis-generation rounds and upstream candidate order."
    )
    preview_count = min(len(hypotheses), max(10, candidates_represented))
    for row in hypotheses[:preview_count]:
        c = row["candidate"]
        print(
            f"HYPOTHESIS candidate {c['upstream_candidate_order']}: "
            f"{c['id_hex']} {c['field']} -> {row['hypothesis']}, "
            f"factor={row['factor']:.8g}, offset={row['offset']:.8g} {row['unit']}, "
            f"decoded q01..q99={row['decoded_physical_q01']:.4g}.."
            f"{row['decoded_physical_q99']:.4g}"
        )
        print(f"    corpus precedent: {corpus_console_summary(row['corpus_precedent'])}")
    print(f"Wrote {out}")
    print(f"Wrote {csv_out}")


if __name__ == "__main__":
    try:
        main()
    except TraceValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
