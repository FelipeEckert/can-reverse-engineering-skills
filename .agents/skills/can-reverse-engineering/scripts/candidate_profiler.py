#!/usr/bin/env python3
"""Profile reference-free CAN field candidates from a normalized trace.

This is an investigation-order aid, not signal identification or decoding
confidence.  It uses only observed frame behavior, survey classifications, and
field geometry.  It does not infer signal families, physical units, factors, or
offsets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

import common
import survey as survey_module


DEFAULT_OUT = Path("temp-output/candidate-analysis/candidate-ranking.json")
DEFAULT_MAX_LENGTH_BITS = 24
TIER_ORDER = {
    "varying": 0,
    "limited-state-or-low-activity": 1,
    "checksum-like": 2,
}
ORDER_DESCRIPTION = (
    "behavior tier, then lower wrap rate, lower jump rate, higher lag-1 "
    "autocorrelation, more observed states, higher change rate, and stable "
    "CAN-ID/geometry keys; no scalar score and no semantic evidence"
)


def entropy(values: np.ndarray) -> float:
    """Shannon entropy of observed values, in bits."""

    if values.size == 0:
        return 0.0
    _, counts = np.unique(values, return_counts=True)
    probabilities = counts.astype(float) / counts.sum()
    return float(-(probabilities * np.log2(probabilities)).sum())


def normalized_entropy(values: np.ndarray) -> float:
    """Observed entropy divided by its distinct-state maximum."""

    distinct = int(np.unique(values).size)
    if distinct <= 1:
        return 0.0
    return entropy(values) / math.log2(distinct)


def trajectory_metrics(raw_unsigned: np.ndarray, length_bits: int) -> dict[str, Any]:
    """Return transparent, scale-free diagnostics for one raw field."""

    raw = np.asarray(raw_unsigned, dtype=np.float64)
    delta = np.diff(raw)
    nonzero_delta = delta[delta != 0]
    distinct = int(np.unique(raw).size)
    change_rate = float(np.mean(delta != 0)) if delta.size else 0.0
    plausibility = common.plausibility(raw, length_bits)

    if nonzero_delta.size:
        counts = Counter(nonzero_delta.tolist())
        top8_delta_mass = sum(count for _, count in counts.most_common(8)) / nonzero_delta.size
    else:
        top8_delta_mass = 0.0

    modulus = 1 << length_bits
    if delta.size:
        modular_delta = np.mod(delta.astype(np.int64), modulus)
        counter_step_fraction = float(max(
            np.mean(modular_delta == 1),
            np.mean(modular_delta == modulus - 1),
        ))
    else:
        counter_step_fraction = 0.0

    code_low, code_high = 0.0, float(modulus - 1)
    q01, q99 = np.quantile(raw, [0.01, 0.99]) if raw.size else (0.0, 0.0)
    value_entropy = entropy(raw)
    unique_fraction = float(distinct / raw.size) if raw.size else 0.0
    checksum_like = bool(
        length_bits == 8
        and value_entropy >= 7.5
        and change_rate >= 0.9
    )

    return {
        "samples": int(raw.size),
        "raw_min": float(np.min(raw)) if raw.size else 0.0,
        "raw_q01": float(q01),
        "raw_q99": float(q99),
        "raw_max": float(np.max(raw)) if raw.size else 0.0,
        "raw_edge_fraction": (
            float(np.mean((raw == code_low) | (raw == code_high))) if raw.size else 0.0
        ),
        "distinct": distinct,
        "unique_fraction": unique_fraction,
        "change_rate": change_rate,
        "value_entropy": value_entropy,
        "delta_entropy_norm": normalized_entropy(nonzero_delta),
        "top8_delta_mass": float(top8_delta_mass),
        "counter_step_fraction": counter_step_fraction,
        "counter_like": counter_step_fraction >= 0.98,
        "ac1": float(plausibility["ac1"]),
        "wrap_rate": float(plausibility["wrap_rate"]),
        "jump_rate": float(plausibility["jump_rate"]),
        "checksum_like": checksum_like,
    }


def byte_span(start_bit: int, length_bits: int) -> set[int]:
    return set(range(start_bit // 8, (start_bit + length_bits - 1) // 8 + 1))


def normalized_signature(raw: np.ndarray) -> str:
    """Stable signature for affine-equivalent trajectories."""

    values = np.asarray(raw, dtype=np.float64)
    low = float(np.min(values))
    span = float(np.ptp(values))
    normalized = (values - low) / span if span else np.zeros_like(values)
    canonical = np.round(normalized, 12).astype("<f8", copy=False)
    return hashlib.sha256(canonical.tobytes()).hexdigest()


def candidate_tier(metrics: dict[str, Any], checksum_overlap: list[int]) -> str:
    if checksum_overlap or metrics["checksum_like"]:
        return "checksum-like"
    if metrics["distinct"] < 6 or metrics["change_rate"] < 0.002:
        return "limited-state-or-low-activity"
    return "varying"


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    """Deterministic investigation order based only on observed behavior."""

    return (
        TIER_ORDER[candidate["investigation_tier"]],
        candidate["wrap_rate"],
        candidate["jump_rate"],
        -candidate["ac1"],
        -candidate["distinct"],
        -candidate["change_rate"],
        candidate["id"],
        candidate["start_bit"],
        candidate["length_bits"],
        candidate["order"],
    )


def load_or_compute_survey(
    trace_frame: Any,
    survey_path: Path | None,
) -> tuple[list[dict[str, Any]], str]:
    """Use a supplied survey artifact, or compute it read-only in memory."""

    if survey_path is not None:
        rows = json.loads(survey_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"survey JSON must contain a list: {survey_path}")
        return rows, str(survey_path)
    rows, _ = survey_module.survey(trace_frame)
    return rows, "computed-in-memory"


def profile_candidates(
    trace_path: Path,
    survey_path: Path | None = None,
    max_length_bits: int = DEFAULT_MAX_LENGTH_BITS,
) -> dict[str, Any]:
    """Build a physical_hypotheses-compatible reference-free candidate document."""

    if max_length_bits < 8 or max_length_bits > 53:
        raise ValueError("--max-length-bits must be between 8 and 53")

    trace_frame = common.load_trace(str(trace_path))
    if trace_frame.empty:
        raise ValueError("normalized trace contains no RX frames")
    groups = common.group_by_id(trace_frame)
    survey_rows, survey_source = load_or_compute_survey(trace_frame, survey_path)
    survey_by_id = {int(row["id"]): row for row in survey_rows}

    generated: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    exclusions = {
        "messages_without_changing_bytes": 0,
        "static_geometry": 0,
        "survey_counter_overlap": 0,
        "trajectory_counter_like": 0,
        "duplicate_geometry": 0,
        "affine_equivalent_overlapping_geometry": 0,
    }

    def add_candidate(
        can_id: int,
        kind: str,
        field: str,
        start_bit: int,
        length_bits: int,
        order: str,
        raw: np.ndarray,
    ) -> None:
        key = (can_id, start_bit, length_bits, order)
        if key in generated:
            exclusions["duplicate_geometry"] += 1
            return

        survey_row = survey_by_id[can_id]
        touched = byte_span(start_bit, length_bits)
        counter_overlap = sorted(touched & set(survey_row.get("counter_bytes", [])))
        checksum_overlap = sorted(touched & set(survey_row.get("checksum_bytes", [])))
        if counter_overlap:
            exclusions["survey_counter_overlap"] += 1
            return

        metrics = trajectory_metrics(raw, length_bits)
        if metrics["distinct"] <= 1:
            exclusions["static_geometry"] += 1
            return
        if metrics["counter_like"]:
            exclusions["trajectory_counter_like"] += 1
            return

        period_ms = float(survey_row.get("period_ms", 0.0))
        candidate = {
            "id": can_id,
            "id_hex": f"0x{can_id:X}",
            "extended_id": bool(survey_row.get("ext", False)),
            "message_length_bytes": int(groups[can_id].length),
            "message_count": int(groups[can_id].n),
            "period_ms": period_ms,
            "message_rate_hz": (1000.0 / period_ms) if period_ms > 0 else 0.0,
            "field": field,
            "kind": kind,
            "start_bit": start_bit,
            "length_bits": length_bits,
            "order": order,
            "signed": False,
            "signedness_inferred": False,
            "counter_overlap": counter_overlap,
            "checksum_overlap": checksum_overlap,
            **metrics,
        }
        candidate["investigation_tier"] = candidate_tier(metrics, checksum_overlap)
        candidate["_signature"] = normalized_signature(raw)
        candidate["_span"] = touched
        generated[key] = candidate

    for can_id in sorted(survey_by_id):
        if can_id not in groups:
            continue
        survey_row = survey_by_id[can_id]
        group = groups[can_id]
        changing_bytes = set(int(value) for value in survey_row.get("changing_bytes", []))
        if not changing_bytes:
            exclusions["messages_without_changing_bytes"] += 1
            continue

        for byte in sorted(changing_bytes):
            add_candidate(
                can_id, "byte", f"byte {byte}", byte * 8, 8, "single-byte",
                common.extract_le(group.le_int, byte * 8, 8),
            )

        max_width = min(group.length, max_length_bits // 8)
        for width in range(2, max_width + 1):
            for byte in range(0, group.length - width + 1):
                touched = set(range(byte, byte + width))
                if not (touched & changing_bytes):
                    continue
                length_bits = width * 8
                add_candidate(
                    can_id, "aligned", f"bytes {byte}-{byte + width - 1} LE",
                    byte * 8, length_bits, "little",
                    common.extract_le(group.le_int, byte * 8, length_bits),
                )
                add_candidate(
                    can_id, "aligned", f"bytes {byte}-{byte + width - 1} BE",
                    byte * 8, length_bits, "big",
                    common.extract_be(group.be_int, group.length, byte, width),
                )

        for mapped in survey_row.get("field_map", []):
            start_bit = int(mapped["start_bit"])
            length_bits = int(mapped["length"])
            if (
                length_bits < 2
                or length_bits > max_length_bits
                or start_bit + length_bits > group.length * 8
            ):
                continue
            add_candidate(
                can_id, "survey-map", f"bits {start_bit}+{length_bits} LE",
                start_bit, length_bits, "little",
                common.extract_le(group.le_int, start_bit, length_bits),
            )

    # Parsimony only removes overlapping geometries from the same ID whose raw
    # trajectories differ solely by an affine transform. Non-overlapping mirrors
    # remain separate candidates.
    parsimonious: list[dict[str, Any]] = []
    by_shape: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for candidate in sorted(
        generated.values(),
        key=lambda item: (
            item["length_bits"], item["id"], item["start_bit"], item["order"]
        ),
    ):
        shape_key = (candidate["id"], candidate["_signature"])
        prior = by_shape.setdefault(shape_key, [])
        if any(candidate["_span"] & old["_span"] for old in prior):
            exclusions["affine_equivalent_overlapping_geometry"] += 1
            continue
        prior.append(candidate)
        parsimonious.append(candidate)

    candidates = sorted(parsimonious, key=candidate_sort_key)
    for investigation_order, candidate in enumerate(candidates, 1):
        candidate["investigation_order"] = investigation_order
        candidate.pop("_signature", None)
        candidate.pop("_span", None)

    tier_counts = {
        tier: sum(candidate["investigation_tier"] == tier for candidate in candidates)
        for tier in TIER_ORDER
    }
    return {
        "schema": "can-reverse-engineering/reference-free-candidates/v1",
        "interpretation": (
            "Candidate order is a reproducible investigation heuristic from observed CAN "
            "behavior and field geometry. It is not decoding confidence."
        ),
        "input": {
            "trace": str(trace_path),
            "survey": survey_source,
            "rx_frames": int(len(trace_frame)),
            "can_ids": int(len(groups)),
        },
        "geometry": {
            "maximum_length_bits": max_length_bits,
            "encodings": (
                "changing bytes; aligned 16..maximum-length little/big endian; "
                "little-endian survey field-map hints"
            ),
            "signedness": (
                "unsigned raw-code view only; signedness is not inferred without evidence"
            ),
        },
        "ordering": {
            "description": ORDER_DESCRIPTION,
            "tiers": list(TIER_ORDER),
            "checksum_note": (
                "checksum-like fields are retained last because a real wide signal byte "
                "can resemble a checksum"
            ),
        },
        "counts": {
            "generated_geometries": len(generated),
            "candidates": len(candidates),
            "by_tier": tier_counts,
            "excluded": exclusions,
        },
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, help="normalized 12-column CAN trace")
    parser.add_argument(
        "--survey",
        help=(
            "survey JSON for the same trace; when omitted, computes the same survey "
            "diagnostics in memory without writing another artifact"
        ),
    )
    parser.add_argument(
        "--max-length-bits", type=int, default=DEFAULT_MAX_LENGTH_BITS,
        help="maximum aligned/survey-map field length (default: 24)",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="candidate JSON output")
    parser.add_argument("--show", type=int, default=20, help="candidates to print")
    args = parser.parse_args()

    document = profile_candidates(
        Path(args.trace),
        Path(args.survey) if args.survey else None,
        args.max_length_bits,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    counts = document["counts"]
    print(document["interpretation"])
    print(
        f"Profiled {counts['generated_geometries']} non-static/non-counter geometries; "
        f"retained {counts['candidates']} after overlapping affine parsimony."
    )
    print("Investigation order: " + ORDER_DESCRIPTION + ".")
    for candidate in document["candidates"][:max(0, args.show)]:
        print(
            f"#{candidate['investigation_order']:3d} {candidate['id_hex']:>5} "
            f"{candidate['field']:<20} tier={candidate['investigation_tier']:<29} "
            f"states={candidate['distinct']:<5d} change={candidate['change_rate']:.1%} "
            f"ac1={candidate['ac1']:.4f} wrap={candidate['wrap_rate']:.2%} "
            f"jump={candidate['jump_rate']:.2%}"
        )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
