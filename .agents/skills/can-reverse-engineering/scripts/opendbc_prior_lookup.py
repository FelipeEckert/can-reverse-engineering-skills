#!/usr/bin/env python3
"""Read-only exact lookup against the structured opendbc encoding priors.

The lookup rebuilds the normalized prior document from ``signals.tsv`` through
``opendbc_priors``.  It does not depend on the generated prior JSON, write any
artifacts, rank near matches, or calculate decoding confidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence

import opendbc_priors as priors


INTERPRETATION = (
    "Qualitative support describes exact cross-OEM corpus precedent; it is not "
    "decoding confidence and cannot confirm a candidate signal."
)


def load_prior_document(signals_path: Path) -> dict[str, object]:
    """Build the existing normalized prior model without writing artifacts."""

    rows, _ = priors.read_rows(signals_path)
    return priors.build_document(rows, signals_path, priors.sha256_file(signals_path))


def family_sections(document: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """Flatten ordinary families and requested torque/voltage subfamilies."""

    families = document.get("families")
    if not isinstance(families, Mapping):
        raise ValueError("prior document has no families mapping")

    sections: dict[str, Mapping[str, object]] = {}
    for key, _, _ in priors.FAMILY_MATCHERS:
        section = families.get(key)
        if not isinstance(section, Mapping):
            raise ValueError(f"prior document is missing family {key!r}")
        sections[key] = section

    for group_key in ("torque", "voltage"):
        group = families.get(group_key)
        if not isinstance(group, Mapping):
            raise ValueError(f"prior document is missing family group {group_key!r}")
        subfamilies = group.get("subfamilies")
        if not isinstance(subfamilies, Mapping):
            raise ValueError(f"prior document has no {group_key} subfamilies mapping")
        for subfamily_key, section in subfamilies.items():
            if not isinstance(subfamily_key, str) or not isinstance(section, Mapping):
                raise ValueError(f"invalid {group_key} subfamily entry")
            sections[f"{group_key}.{subfamily_key}"] = section
    return sections


def normalized_query(
    family: str,
    bit_length: int,
    signedness: str,
    factor: str,
    offset: str,
    unit: str | None = None,
) -> dict[str, object]:
    """Normalize query fields with the same helpers used by prior generation."""

    if bit_length <= 0:
        raise ValueError("bit length must be positive")
    normalized_signedness = signedness.strip().lower()
    if normalized_signedness not in {"signed", "unsigned"}:
        raise ValueError("signedness must be 'signed' or 'unsigned'")
    return {
        "family": family,
        "bit_length": bit_length,
        "signedness": normalized_signedness,
        "factor": priors.decimal_text(str(factor)),
        "offset": priors.decimal_text(str(offset)),
        "unit": None if unit is None else priors.canonical_unit(unit, family),
    }


def result_pattern(pattern: Mapping[str, object]) -> dict[str, object]:
    """Project a prior pattern into the stable read-only lookup response."""

    return {
        "qualitative_corpus_support": pattern["strength"],
        "independent_oem_family_count": pattern["independent_oem_family_count"],
        "independent_oem_families": pattern["independent_oem_families"],
        "oem_families": pattern["oem_families"],
        "declaration_count": pattern["declaration_count"],
        "unique_dbc_count": pattern["unique_dbc_count"],
        "parser_counts": pattern["parser_counts"],
        "matched_normalized_encoding_pattern": {
            "bit_length": pattern["bit_length"],
            "signedness": pattern["signedness"],
            "factor": pattern["factor"],
            "offset": pattern["offset"],
            "unit": pattern["unit"],
        },
        "byte_order_diagnostic_counts": pattern["byte_order_counts"],
        "strength_basis": pattern["strength_basis"],
    }


def lookup_exact(
    document: Mapping[str, object],
    *,
    family: str,
    bit_length: int,
    signedness: str,
    factor: str,
    offset: str,
    unit: str | None = None,
) -> dict[str, object]:
    """Return all exact matches for the supplied normalized encoding fields.

    If ``unit`` is omitted, unit is unconstrained and every otherwise-exact unit
    variant is returned.  Results are sorted only by canonical unit, not evidence
    volume or OEM identity.
    """

    sections = family_sections(document)
    if family not in sections:
        available = ", ".join(sorted(sections))
        raise ValueError(f"unknown family {family!r}; available families: {available}")
    query = normalized_query(family, bit_length, signedness, factor, offset, unit)
    patterns = sections[family].get("patterns")
    if not isinstance(patterns, list):
        raise ValueError(f"prior family {family!r} has no patterns list")

    matches = []
    for pattern in patterns:
        if not isinstance(pattern, Mapping):
            raise ValueError(f"prior family {family!r} contains an invalid pattern")
        same_structure = (
            pattern.get("bit_length") == query["bit_length"]
            and pattern.get("signedness") == query["signedness"]
            and pattern.get("factor") == query["factor"]
            and pattern.get("offset") == query["offset"]
        )
        same_unit = query["unit"] is None or pattern.get("unit") == query["unit"]
        if same_structure and same_unit:
            matches.append(result_pattern(pattern))
    matches.sort(key=lambda match: str(match["matched_normalized_encoding_pattern"]["unit"]))

    return {
        "schema_version": 1,
        "lookup_type": "exact",
        "interpretation": INTERPRETATION,
        "query": query,
        "unit_constrained": unit is not None,
        "exact_pattern_exists": bool(matches),
        "match_count": len(matches),
        "matches": matches,
    }


def selftest(signals_path: Path) -> None:
    document = load_prior_document(signals_path)
    rpm = lookup_exact(
        document, family="engine_rpm", bit_length=16, signedness="unsigned",
        factor="0.2500", offset="-0", unit="RPM",
    )
    assert rpm["exact_pattern_exists"] and rpm["match_count"] == 1
    assert rpm["matches"][0]["qualitative_corpus_support"] == "strong"
    assert rpm["matches"][0]["independent_oem_family_count"] == 4
    assert rpm == lookup_exact(
        document, family="engine_rpm", bit_length=16, signedness="unsigned",
        factor="0.25", offset="0", unit="rpm",
    )

    pedal = lookup_exact(
        document, family="pedal_throttle", bit_length=8, signedness="unsigned",
        factor="0.4", offset="0", unit="Percent",
    )
    assert pedal["exact_pattern_exists"] and pedal["match_count"] == 1
    assert pedal["matches"][0]["qualitative_corpus_support"] == "moderate"

    temperature = lookup_exact(
        document, family="temperature", bit_length=8, signedness="unsigned",
        factor="0.5", offset="-40", unit="DegreeCelsius",
    )
    assert temperature["exact_pattern_exists"] and temperature["match_count"] == 1
    assert temperature["matches"][0]["qualitative_corpus_support"] == "strong"

    absent = lookup_exact(
        document, family="engine_rpm", bit_length=7, signedness="signed",
        factor="123.456", offset="789", unit="rpm",
    )
    assert not absent["exact_pattern_exists"] and absent["match_count"] == 0
    assert absent["matches"] == []

    unit_optional = lookup_exact(
        document, family="temperature", bit_length=8, signedness="unsigned",
        factor="0.75", offset="-48",
    )
    assert unit_optional["exact_pattern_exists"] and unit_optional["match_count"] == 2
    assert [
        match["matched_normalized_encoding_pattern"]["unit"]
        for match in unit_optional["matches"]
    ] == ["", "degC"]
    print("opendbc_prior_lookup self-test: ok")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"Alternatively set {priors.SIGNALS_ENV}=/path/to/signals.tsv.",
    )
    parser.add_argument(
        "--signals", type=Path,
        help=f"input signals.tsv; takes precedence over {priors.SIGNALS_ENV}",
    )
    parser.add_argument("--family", help="canonical prior family key")
    parser.add_argument("--bit-length", type=int)
    parser.add_argument("--signedness", choices=("signed", "unsigned"))
    parser.add_argument("--factor")
    parser.add_argument("--offset")
    parser.add_argument("--unit", help="optional unit; normalized with the prior family's rules")
    parser.add_argument("--selftest", action="store_true", help="run deterministic corpus checks and exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    signals_path = priors.resolve_signals_path(args.signals)
    if signals_path is None:
        print(
            "error: indexed opendbc signals are not configured; pass "
            f"--signals /path/to/signals.tsv or set {priors.SIGNALS_ENV}",
            file=sys.stderr,
        )
        return 2
    if not signals_path.is_file():
        print(f"error: input not found: {signals_path}", file=sys.stderr)
        return 2
    try:
        if args.selftest:
            selftest(signals_path)
            return 0
        required = {
            "--family": args.family,
            "--bit-length": args.bit_length,
            "--signedness": args.signedness,
            "--factor": args.factor,
            "--offset": args.offset,
        }
        missing = [flag for flag, value in required.items() if value is None]
        if missing:
            raise ValueError(f"missing required query arguments: {', '.join(missing)}")
        document = load_prior_document(signals_path)
        result = lookup_exact(
            document,
            family=args.family,
            bit_length=args.bit_length,
            signedness=args.signedness,
            factor=args.factor,
            offset=args.offset,
            unit=args.unit,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
