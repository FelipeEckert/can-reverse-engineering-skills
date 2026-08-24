"""Build a deterministic, read-only signal index from an organized opendbc corpus.

The corpus is described by ``manifest.tsv`` and contains symlinks to the final
DBCs.  This script deliberately opens those symlinks instead of the source paths
recorded in the manifest.  It never writes below the corpus (or into opendbc).

cantools is the primary parser.  Some upstream opendbc files contain permissive
syntax or overlapping/out-of-range declarations that cantools rejects, so the
script retries with ``strict=False`` and finally uses a small, read-only parser
for canonical BO_/SG_ declarations.  Every parser failure and every metadata
ambiguity is retained in the generated audit artifacts.

Output files (under the directory selected with ``--out-dir``):

    signals.tsv
    dbcs.tsv
    examples.tsv
    parse_failures.tsv
    metadata_ambiguities.tsv
    summary.json
    summary.md
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

import cantools


DEFAULT_OUT = Path("temp-output/opendbc-reference")
CORPUS_ENV = "CAN_RE_OPENDDBC_CORPUS"
MANIFEST_FIELDS = (
    "dbc_filename",
    "original_source_path",
    "corpus_symlink_path",
    "oem_family",
    "category",
    "origin_type",
)
SIGNAL_FIELDS = (
    "oem_family",
    "category",
    "origin_type",
    "dbc_filename",
    "message_name",
    "can_id",
    "is_extended",
    "signal_name",
    "start_bit",
    "length",
    "byte_order",
    "signedness",
    "factor",
    "offset",
    "unit",
    "minimum",
    "maximum",
    "parser_mode",
)
DBC_FIELDS = (
    "oem_family",
    "category",
    "origin_type",
    "dbc_filename",
    "corpus_symlink_path",
    "symlink_target",
    "parser_mode",
    "message_count",
    "source_signal_count",
    "signal_count",
    "parse_failure_count",
    "metadata_ambiguity_count",
)
FAILURE_FIELDS = ("dbc_filename", "stage", "recovered", "error")
AMBIGUITY_FIELDS = (
    "dbc_filename",
    "message_name",
    "can_id",
    "kind",
    "detail",
)
EXAMPLE_FIELDS = ("example_type",) + SIGNAL_FIELDS

_NUMBER = r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[Ee][+-]?\d+)?"
_BO_RE = re.compile(
    r"^\s*BO_\s+(?P<id>\d+)\s+(?P<name>\S+)\s*:\s*"
    r"(?P<length>\d+)(?:\s+.*)?$"
)
_SG_RE = re.compile(
    r"^\s*SG_\s+(?P<name>\S+)"
    r"(?:\s+(?P<mux>M|m(?:\d+)?M?))?\s*:\s*"
    r"(?P<start>\d+)\|(?P<length>\d+)@(?P<byte_order>[01])(?P<sign>[+-])\s*"
    rf"\(\s*(?P<factor>{_NUMBER})\s*,\s*(?P<offset>{_NUMBER})\s*\)\s*"
    rf"\[\s*(?P<minimum>{_NUMBER})\s*\|\s*(?P<maximum>{_NUMBER})\s*\]\s*"
    r'"(?P<unit>[^"]*)"(?:\s+.*)?$'
)
_RPM_RE = re.compile(r"(?:^|_)rpm(?:_|$)|engine.*speed|eng.*speed", re.IGNORECASE)
_SPEED_RE = re.compile(
    r"speed|velocity|(?:^|_)whl.*spd|(?:^|_)wheel.*spd|(?:^|_)veh.*spd",
    re.IGNORECASE,
)
_PEDAL_RE = re.compile(
    r"pedal|throttle|accelerator|(?:^|_)gas(?:_|$)|accel.*(?:pos|position)",
    re.IGNORECASE,
)


def _clean_error(exc: BaseException | str) -> str:
    return " ".join(str(exc).replace("\t", " ").split())


def resolve_corpus_path(explicit: Path | None) -> Path:
    """Resolve the read-only corpus path from CLI, then environment."""

    configured = explicit if explicit is not None else os.environ.get(CORPUS_ENV)
    if not configured:
        raise ValueError(
            "opendbc corpus is not configured; pass --corpus /path/to/opendbc-corpus "
            f"or set {CORPUS_ENV}"
        )
    return Path(configured).expanduser().resolve()


def _format_number(value: Any) -> str:
    """Return stable, compact numeric text for TSV keys and summaries."""
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(number)
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) < 1e21:
        return str(int(number))
    return format(number, ".15g")


def _hex_id(frame_id: int) -> str:
    return f"0x{frame_id:X}"


def _sort_signal_key(row: dict[str, str]) -> tuple[Any, ...]:
    return (
        row["oem_family"].casefold(),
        row["category"].casefold(),
        row["origin_type"],
        row["dbc_filename"].casefold(),
        int(row["can_id"], 16),
        row["message_name"].casefold(),
        int(row["start_bit"]),
        int(row["length"]),
        row["signal_name"].casefold(),
    )


def _write_tsv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(fields), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def _load_manifest(corpus: Path) -> list[dict[str, str]]:
    manifest = corpus / "manifest.tsv"
    if not manifest.is_file():
        raise ValueError(f"manifest not found: {manifest}")
    with manifest.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
            raise ValueError(
                f"unexpected manifest columns: {reader.fieldnames}; "
                f"expected {list(MANIFEST_FIELDS)}"
            )
        rows = [{key: (value or "").strip() for key, value in row.items()} for row in reader]
    if not rows:
        raise ValueError(f"manifest is empty: {manifest}")
    return rows


def _manifest_audit(
    rows: list[dict[str, str]], corpus: Path
) -> tuple[list[dict[str, str]], dict[int, list[dict[str, str]]]]:
    """Validate manifest and symlink metadata without changing either."""
    fatal: list[dict[str, str]] = []
    per_row: dict[int, list[dict[str, str]]] = defaultdict(list)
    corpus_real = corpus.resolve()

    for field in MANIFEST_FIELDS:
        seen: dict[str, int] = {}
        for index, row in enumerate(rows):
            value = row[field]
            if not value:
                fatal.append(
                    {
                        "dbc_filename": row.get("dbc_filename", ""),
                        "message_name": "",
                        "can_id": "",
                        "kind": "blank_manifest_field",
                        "detail": f"manifest row {index + 2}: {field} is blank",
                    }
                )
            if field in {"dbc_filename", "original_source_path", "corpus_symlink_path"}:
                if value in seen:
                    fatal.append(
                        {
                            "dbc_filename": row.get("dbc_filename", ""),
                            "message_name": "",
                            "can_id": "",
                            "kind": "duplicate_manifest_value",
                            "detail": (
                                f"manifest rows {seen[value] + 2} and {index + 2} "
                                f"duplicate {field}={value}"
                            ),
                        }
                    )
                seen[value] = index

    for index, row in enumerate(rows):
        name = row["dbc_filename"]
        path = Path(row["corpus_symlink_path"]).expanduser()
        issues = per_row[index]
        if row["origin_type"] not in {"direct", "generated"}:
            issues.append(
                {
                    "dbc_filename": name,
                    "message_name": "",
                    "can_id": "",
                    "kind": "unknown_origin_type",
                    "detail": f"origin_type={row['origin_type']!r}; expected direct/generated",
                }
            )
        try:
            path.relative_to(corpus_real)
        except ValueError:
            issues.append(
                {
                    "dbc_filename": name,
                    "message_name": "",
                    "can_id": "",
                    "kind": "symlink_outside_corpus",
                    "detail": f"manifest symlink path is not below corpus: {path}",
                }
            )
            continue
        if path.name != name:
            issues.append(
                {
                    "dbc_filename": name,
                    "message_name": "",
                    "can_id": "",
                    "kind": "filename_mismatch",
                    "detail": f"manifest filename differs from symlink basename {path.name}",
                }
            )
        if not path.is_symlink():
            issues.append(
                {
                    "dbc_filename": name,
                    "message_name": "",
                    "can_id": "",
                    "kind": "not_a_symlink",
                    "detail": f"organized corpus entry is not a symlink: {path}",
                }
            )
            continue
        if not path.exists():
            issues.append(
                {
                    "dbc_filename": name,
                    "message_name": "",
                    "can_id": "",
                    "kind": "broken_symlink",
                    "detail": f"organized corpus symlink target is missing: {path}",
                }
            )
            continue
        target = path.resolve()
        recorded_source = Path(row["original_source_path"]).expanduser().resolve()
        if target != recorded_source:
            issues.append(
                {
                    "dbc_filename": name,
                    "message_name": "",
                    "can_id": "",
                    "kind": "symlink_target_mismatch",
                    "detail": f"symlink resolves to {target}; manifest records {recorded_source}",
                }
            )
    return fatal, per_row


def _cantools_rows(
    db: Any, metadata: dict[str, str], parser_mode: str
) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    for message in db.messages:
        for signal in message.signals:
            rows.append(
                {
                    "oem_family": metadata["oem_family"],
                    "category": metadata["category"],
                    "origin_type": metadata["origin_type"],
                    "dbc_filename": metadata["dbc_filename"],
                    "message_name": message.name,
                    "can_id": _hex_id(message.frame_id),
                    "is_extended": "yes" if message.is_extended_frame else "no",
                    "signal_name": signal.name,
                    "start_bit": str(signal.start),
                    "length": str(signal.length),
                    "byte_order": signal.byte_order,
                    "signedness": "signed" if signal.is_signed else "unsigned",
                    "factor": _format_number(signal.scale),
                    "offset": _format_number(signal.offset),
                    "unit": signal.unit or "",
                    "minimum": _format_number(signal.minimum),
                    "maximum": _format_number(signal.maximum),
                    "parser_mode": parser_mode,
                }
            )
    return rows, len(db.messages)


def _fallback_rows(
    path: Path, metadata: dict[str, str]
) -> tuple[list[dict[str, str]], int, list[dict[str, str]], list[str]]:
    """Extract canonical BO_/SG_ declarations when cantools cannot load a DBC."""
    rows: list[dict[str, str]] = []
    ambiguities: list[dict[str, str]] = []
    errors: list[str] = []
    current: dict[str, Any] | None = None
    message_count = 0
    message_keys: set[tuple[int, str]] = set()
    signal_keys: set[tuple[int, str, str]] = set()

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\r\n")
            stripped = line.lstrip()
            if stripped.startswith("BO_ "):
                match = _BO_RE.match(line)
                if not match:
                    errors.append(f"line {line_number}: unparsed BO_ declaration: {line}")
                    current = None
                    continue
                raw_id = int(match.group("id"))
                has_extended_flag = bool(raw_id & 0x80000000)
                frame_id = raw_id & 0x7FFFFFFF if has_extended_flag else raw_id
                if has_extended_flag:
                    extended = "yes"
                elif frame_id <= 0x7FF:
                    extended = "no"
                elif frame_id <= 0x1FFFFFFF:
                    extended = "inferred"
                    ambiguities.append(
                        {
                            "dbc_filename": metadata["dbc_filename"],
                            "message_name": match.group("name"),
                            "can_id": _hex_id(frame_id),
                            "kind": "frame_format_inferred",
                            "detail": (
                                f"line {line_number}: ID exceeds 11 bits without the "
                                "DBC extended-frame flag; indexed as an inferred extended ID"
                            ),
                        }
                    )
                else:
                    extended = "ambiguous"
                    ambiguities.append(
                        {
                            "dbc_filename": metadata["dbc_filename"],
                            "message_name": match.group("name"),
                            "can_id": _hex_id(frame_id),
                            "kind": "can_id_out_of_range",
                            "detail": (
                                f"line {line_number}: raw DBC ID {raw_id} is above the "
                                "29-bit CAN range and has no extended-frame flag"
                            ),
                        }
                    )
                current = {
                    "name": match.group("name"),
                    "frame_id": frame_id,
                    "extended": extended,
                }
                message_count += 1
                message_key = (frame_id, current["name"])
                if message_key in message_keys:
                    ambiguities.append(
                        {
                            "dbc_filename": metadata["dbc_filename"],
                            "message_name": current["name"],
                            "can_id": _hex_id(frame_id),
                            "kind": "duplicate_message",
                            "detail": f"line {line_number}: duplicate message name/CAN ID",
                        }
                    )
                message_keys.add(message_key)
                continue

            if not stripped.startswith("SG_ "):
                continue
            match = _SG_RE.match(line)
            if not match:
                errors.append(f"line {line_number}: unparsed SG_ declaration: {line}")
                continue
            if current is None:
                errors.append(f"line {line_number}: SG_ declaration has no preceding BO_")
                continue
            signal_key = (current["frame_id"], current["name"], match.group("name"))
            if signal_key in signal_keys:
                ambiguities.append(
                    {
                        "dbc_filename": metadata["dbc_filename"],
                        "message_name": current["name"],
                        "can_id": _hex_id(current["frame_id"]),
                        "kind": "duplicate_signal",
                        "detail": (
                            f"line {line_number}: duplicate signal name "
                            f"{match.group('name')} in one message"
                        ),
                    }
                )
            signal_keys.add(signal_key)
            rows.append(
                {
                    "oem_family": metadata["oem_family"],
                    "category": metadata["category"],
                    "origin_type": metadata["origin_type"],
                    "dbc_filename": metadata["dbc_filename"],
                    "message_name": current["name"],
                    "can_id": _hex_id(current["frame_id"]),
                    "is_extended": current["extended"],
                    "signal_name": match.group("name"),
                    "start_bit": str(int(match.group("start"))),
                    "length": str(int(match.group("length"))),
                    "byte_order": (
                        "little_endian" if match.group("byte_order") == "1" else "big_endian"
                    ),
                    "signedness": "signed" if match.group("sign") == "-" else "unsigned",
                    "factor": _format_number(match.group("factor")),
                    "offset": _format_number(match.group("offset")),
                    "unit": match.group("unit"),
                    "minimum": _format_number(match.group("minimum")),
                    "maximum": _format_number(match.group("maximum")),
                    "parser_mode": "line-fallback",
                }
            )
    return rows, message_count, ambiguities, errors


def _signal_identity(row: dict[str, str]) -> tuple[str, ...]:
    """Identity used to compare cantools output with source SG_ declarations."""
    return (
        row["message_name"],
        row["can_id"],
        row["signal_name"],
        row["start_bit"],
        row["length"],
        row["byte_order"],
        row["signedness"],
    )


def _supplement_cantools_rows(
    path: Path,
    metadata: dict[str, str],
    cantools_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int, list[dict[str, str]], list[str]]:
    """Add source SG_ declarations that cantools silently leaves out.

    Per-identity queues are used because duplicate declarations are precisely the
    case where set-based reconciliation would lose data.  cantools remains authoritative for
    every declaration it returns; the source parser supplies only missing rows.
    """
    source_rows, _messages, _source_ambiguities, source_errors = _fallback_rows(
        path, metadata
    )
    supplemented = list(cantools_rows)
    remaining: dict[tuple[str, ...], deque[int]] = defaultdict(deque)
    for index, row in enumerate(supplemented):
        remaining[_signal_identity(row)].append(index)
    failures: list[dict[str, str]] = []
    for row in source_rows:
        identity = _signal_identity(row)
        if remaining[identity]:
            matched = supplemented[remaining[identity].popleft()]
            # cantools treats a DBC [0|0] range as unspecified (None).  The
            # source declaration still contains explicit bounds, and the index
            # contract is to preserve them rather than emit blank cells.
            for field in ("minimum", "maximum"):
                if not matched[field]:
                    matched[field] = row[field]
            continue
        extra = dict(row)
        extra["parser_mode"] = "line-supplement"
        supplemented.append(extra)
        failures.append(
            {
                "dbc_filename": metadata["dbc_filename"],
                "stage": "cantools-source-reconciliation",
                "recovered": "yes",
                "error": (
                    f"{row['message_name']} {row['can_id']}: cantools omitted source "
                    f"declaration {row['signal_name']} at {row['start_bit']}|"
                    f"{row['length']}; indexed via line supplement"
                ),
            }
        )
    return supplemented, len(source_rows), failures, source_errors


def _counter_rows(
    rows: list[dict[str, str]], field: str, dbc_by_oem: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    counts = Counter(row[field] if row[field] else "(blank)" for row in rows)
    result = []
    for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold())):
        entry: dict[str, Any] = {field: value, "signals": count}
        if dbc_by_oem is not None:
            entry["dbcs"] = dbc_by_oem.get(value, 0)
        result.append(entry)
    return result


def _example_kind(row: dict[str, str]) -> str | None:
    name = row["signal_name"]
    if _RPM_RE.search(name):
        return "rpm"
    if _PEDAL_RE.search(name):
        return "pedal_throttle"
    if _SPEED_RE.search(name):
        return "speed"
    return None


def _example_rank(kind: str, row: dict[str, str]) -> tuple[Any, ...]:
    name = row["signal_name"].casefold()
    unit = row["unit"].casefold().replace(" ", "")
    if kind == "rpm":
        physical_unit = unit in {"rpm", "rev/min", "1/min", "1/minute"}
        relevance = 0 if physical_unit and ("engine" in name or "rpm" in name) else 1
        relevance += 0 if physical_unit else 2
    elif kind == "speed":
        physical_unit = unit in {"km/h", "kph", "mph", "m/s", "meters/sec"}
        relevance = 0 if name in {"speed", "vehicle_speed", "vehicle_speed_kph"} else 1
        if "wheel" in name or "whl" in name:
            relevance += 1
        relevance += 0 if physical_unit else 2
    else:
        relevance = 0 if "pedal" in name or "throttle" in name else 1
        relevance += 0 if unit in {"%", "percent", "unit_percent"} else 1
    return (relevance,) + _sort_signal_key(row)


def _select_examples(rows: list[dict[str, str]], per_kind: int = 15) -> list[dict[str, str]]:
    candidates: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        kind = _example_kind(row)
        if kind:
            candidates[kind].append(row)
    selected: list[dict[str, str]] = []
    for kind in ("rpm", "speed", "pedal_throttle"):
        for row in sorted(candidates[kind], key=lambda item: _example_rank(kind, item))[:per_kind]:
            selected.append({"example_type": kind, **row})
    return selected


def _distribution(rows: list[dict[str, str]], field: str) -> list[dict[str, Any]]:
    return _counter_rows(rows, field)


def _build_summary(
    manifest_rows: list[dict[str, str]],
    signals: list[dict[str, str]],
    dbcs: list[dict[str, Any]],
    failures: list[dict[str, str]],
    ambiguities: list[dict[str, str]],
    examples: list[dict[str, str]],
) -> dict[str, Any]:
    dbc_by_oem = Counter(row["oem_family"] for row in manifest_rows)
    hyundai = [row for row in signals if row["oem_family"] == "Hyundai/Kia"]
    hyundai_dbcs = [row for row in manifest_rows if row["oem_family"] == "Hyundai/Kia"]
    hyundai_examples = _select_examples(hyundai, per_kind=8)
    hyundai_example_counts = Counter(
        kind for row in hyundai if (kind := _example_kind(row)) is not None
    )
    unrecovered = [row for row in failures if row["recovered"] == "no"]
    return {
        "totals": {
            "manifest_dbcs": len(manifest_rows),
            "indexed_dbcs": sum(row["parser_mode"] != "unparsed" for row in dbcs),
            "source_signal_declarations": sum(
                int(row["source_signal_count"]) for row in dbcs
            ),
            "signals": len(signals),
            "parse_failures": len(failures),
            "unrecovered_parse_failures": len(unrecovered),
            "metadata_ambiguities": len(ambiguities),
        },
        "dbc_parser_modes": [
            {"parser_mode": key, "dbcs": value}
            for key, value in sorted(Counter(row["parser_mode"] for row in dbcs).items())
        ],
        "signals_by_oem_family": _counter_rows(signals, "oem_family", dbc_by_oem),
        "common_bit_widths": _distribution(signals, "length"),
        "common_factors": _distribution(signals, "factor"),
        "common_offsets": _distribution(signals, "offset"),
        "common_units": _distribution(signals, "unit"),
        "hyundai_kia": {
            "dbcs": len(hyundai_dbcs),
            "signals": len(hyundai),
            "categories": _distribution(hyundai, "category"),
            "provenance": _distribution(hyundai, "origin_type"),
            "bit_widths": _distribution(hyundai, "length"),
            "factors": _distribution(hyundai, "factor"),
            "offsets": _distribution(hyundai, "offset"),
            "units": _distribution(hyundai, "unit"),
            "example_counts": dict(sorted(hyundai_example_counts.items())),
            "examples": hyundai_examples,
        },
        "examples": examples,
        "issue_counts": {
            "parse_failures_by_stage": dict(
                sorted(Counter(row["stage"] for row in failures).items())
            ),
            "metadata_ambiguities_by_kind": dict(
                sorted(Counter(row["kind"] for row in ambiguities).items())
            ),
        },
    }


def _markdown_table_count(items: list[dict[str, Any]], key: str, limit: int = 20) -> list[str]:
    lines = [f"| {key.replace('_', ' ').title()} | Signals |", "|---|---:|"]
    for item in items[:limit]:
        value = str(item[key]).replace("|", "\\|")
        lines.append(f"| {value} | {item['signals']} |")
    return lines


def _summary_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        "# opendbc reference index summary",
        "",
        "This report is deterministic: it intentionally contains no run timestamp or host-specific output path.",
        "",
        "## Totals",
        "",
        f"- DBCs in manifest: {totals['manifest_dbcs']}",
        f"- DBCs indexed: {totals['indexed_dbcs']}",
        f"- Signals indexed: {totals['signals']}",
        f"- Source `SG_` declarations audited: {totals['source_signal_declarations']}",
        f"- Parse-failure audit rows: {totals['parse_failures']} "
        f"({totals['unrecovered_parse_failures']} unrecovered)",
        f"- Metadata ambiguities: {totals['metadata_ambiguities']}",
        "",
        "### Parser modes",
        "",
        "| Parser mode | DBCs |",
        "|---|---:|",
    ]
    for item in summary["dbc_parser_modes"]:
        lines.append(f"| {item['parser_mode']} | {item['dbcs']} |")

    lines.extend(["", "## Signals by OEM/family", "", "| OEM/family | DBCs | Signals |", "|---|---:|---:|"])
    for item in summary["signals_by_oem_family"]:
        lines.append(f"| {item['oem_family']} | {item['dbcs']} | {item['signals']} |")

    for title, key, field in (
        ("Common bit widths", "common_bit_widths", "length"),
        ("Common factors", "common_factors", "factor"),
        ("Common offsets", "common_offsets", "offset"),
        ("Common units", "common_units", "unit"),
    ):
        lines.extend(["", f"## {title}", ""])
        lines.extend(_markdown_table_count(summary[key], field))

    hyundai = summary["hyundai_kia"]
    lines.extend(
        [
            "",
            "## Hyundai/Kia focus",
            "",
            f"- DBCs: {hyundai['dbcs']}",
            f"- Signals: {hyundai['signals']}",
            "",
            "### Categories",
            "",
        ]
    )
    lines.extend(_markdown_table_count(hyundai["categories"], "category"))
    for title, key, field in (
        ("Provenance", "provenance", "origin_type"),
        ("Bit widths", "bit_widths", "length"),
        ("Factors", "factors", "factor"),
        ("Offsets", "offsets", "offset"),
        ("Units", "units", "unit"),
    ):
        lines.extend(["", f"### {title}", ""])
        lines.extend(_markdown_table_count(hyundai[key], field, limit=15))

    lines.extend(
        [
            "",
            "### Named physical-signal matches",
            "",
            "| Kind | Matching signals |",
            "|---|---:|",
        ]
    )
    for kind in ("rpm", "speed", "pedal_throttle"):
        lines.append(f"| {kind} | {hyundai['example_counts'].get(kind, 0)} |")

    lines.extend(["", "## Signal examples", ""])
    for kind, title in (
        ("rpm", "RPM-like"),
        ("speed", "Speed-like"),
        ("pedal_throttle", "Pedal/throttle-like"),
    ):
        lines.extend(
            [
                f"### {title}",
                "",
                "| OEM/family | DBC | Message | CAN ID | Signal | Geometry | Factor | Offset | Unit |",
                "|---|---|---|---:|---|---|---:|---:|---|",
            ]
        )
        for row in summary["examples"]:
            if row["example_type"] != kind:
                continue
            geometry = f"{row['start_bit']}|{row['length']} {row['byte_order']} {row['signedness']}"
            lines.append(
                f"| {row['oem_family']} | {row['dbc_filename']} | {row['message_name']} | "
                f"{row['can_id']} | {row['signal_name']} | {geometry} | {row['factor']} | "
                f"{row['offset']} | {row['unit'] or '(blank)'} |"
            )
        lines.append("")

    lines.extend(["## Issues", ""])
    if summary["issue_counts"]["parse_failures_by_stage"]:
        lines.append("Parse-failure audit rows by stage:")
        lines.append("")
        for key, value in summary["issue_counts"]["parse_failures_by_stage"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("No parser failures.")
    lines.append("")
    if summary["issue_counts"]["metadata_ambiguities_by_kind"]:
        lines.append("Metadata ambiguities by kind:")
        lines.append("")
        for key, value in summary["issue_counts"]["metadata_ambiguities_by_kind"].items():
            lines.append(f"- {key}: {value}")
    else:
        lines.append("No ambiguous metadata.")
    lines.extend(
        [
            "",
            "See `parse_failures.tsv` and `metadata_ambiguities.tsv` for the complete audit details.",
            "",
        ]
    )
    return "\n".join(lines)


def _selftest() -> int:
    fixture = """BO_ 256 ENGINE: 8 ECU
 SG_ ENGINE_RPM : 7|16@0+ (.25,-500) [-500|1547.5] \"rpm\" ECU
 SG_ PEDAL m : 24|8@1- (0.4,0) [0|100] \"%\" ECU
BO_ 419385573 EXTENDED: 8 ECU
 SG_ VEHICLE_SPEED : 0|16@1+ (0.01,0) [0|655.35] \"km/h\" ECU
"""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.dbc"
        path.write_text(fixture, encoding="utf-8")
        metadata = {
            "oem_family": "Test",
            "category": "test",
            "origin_type": "direct",
            "dbc_filename": path.name,
        }
        rows, messages, ambiguities, errors = _fallback_rows(path, metadata)
    assert messages == 2
    assert len(rows) == 3
    assert not errors
    assert rows[0]["factor"] == "0.25"
    assert rows[0]["byte_order"] == "big_endian"
    assert rows[1]["signedness"] == "signed"
    assert rows[2]["can_id"] == "0x18FF50E5"
    assert rows[2]["is_extended"] == "inferred"
    assert len(ambiguities) == 1
    assert _example_kind(rows[0]) == "rpm"
    assert _example_kind(rows[1]) == "pedal_throttle"
    assert _example_kind(rows[2]) == "speed"
    print("opendbc_reference selftest: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python "
            ".agents/skills/can-reverse-engineering/scripts/opendbc_reference.py "
            "--corpus /data/opendbc-corpus "
            "--out-dir /data/opendbc-reference\n"
            f"  {CORPUS_ENV}=/data/opendbc-corpus .venv/bin/python "
            ".agents/skills/can-reverse-engineering/scripts/opendbc_reference.py "
            "--out-dir /data/opendbc-reference"
        ),
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        help=(
            "organized read-only corpus containing manifest.tsv; takes precedence "
            f"over {CORPUS_ENV}"
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT,
        help=(
            "artifact directory outside the corpus; may be a stable external path "
            f"(default: {DEFAULT_OUT})"
        ),
    )
    parser.add_argument("--selftest", action="store_true", help="run parser smoke tests and exit")
    args = parser.parse_args()
    if args.selftest:
        return _selftest()

    try:
        corpus = resolve_corpus_path(args.corpus)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    out_dir = args.out_dir.expanduser().resolve()
    try:
        out_dir.relative_to(corpus)
    except ValueError:
        pass
    else:
        print(
            f"ERROR: --out-dir must be outside the read-only corpus: {out_dir}",
            file=sys.stderr,
        )
        return 1

    try:
        manifest_rows = _load_manifest(corpus)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    fatal_manifest, per_manifest_row = _manifest_audit(manifest_rows, corpus)
    if fatal_manifest:
        for issue in fatal_manifest:
            print(f"ERROR: {issue['kind']}: {issue['detail']}", file=sys.stderr)
        return 1

    signals: list[dict[str, str]] = []
    dbcs: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    ambiguities: list[dict[str, str]] = []
    unrecovered = False

    for index, metadata in enumerate(manifest_rows):
        path = Path(metadata["corpus_symlink_path"]).expanduser()
        dbc_failures: list[dict[str, str]] = []
        dbc_ambiguities = list(per_manifest_row[index])
        parser_mode = ""
        message_count = 0
        source_signal_count = 0
        dbc_signals: list[dict[str, str]] = []

        if any(issue["kind"] in {"not_a_symlink", "broken_symlink", "symlink_outside_corpus"}
               for issue in dbc_ambiguities):
            error = "organized corpus entry is not a readable in-corpus symlink"
            dbc_failures.append(
                {
                    "dbc_filename": metadata["dbc_filename"],
                    "stage": "symlink-validation",
                    "recovered": "no",
                    "error": error,
                }
            )
            parser_mode = "unparsed"
            unrecovered = True
        else:
            try:
                db = cantools.database.load_file(path, strict=True)
                parser_mode = "cantools-strict"
                dbc_signals, message_count = _cantools_rows(db, metadata, parser_mode)
                dbc_signals, source_signal_count, supplemental_failures, source_errors = (
                    _supplement_cantools_rows(path, metadata, dbc_signals)
                )
                dbc_failures.extend(supplemental_failures)
                for error in source_errors:
                    dbc_failures.append(
                        {
                            "dbc_filename": metadata["dbc_filename"],
                            "stage": "source-declaration-audit",
                            "recovered": "no",
                            "error": _clean_error(error),
                        }
                    )
                if source_errors:
                    unrecovered = True
            except Exception as strict_exc:  # noqa: BLE001 - audit third-party parser failures
                dbc_failures.append(
                    {
                        "dbc_filename": metadata["dbc_filename"],
                        "stage": "cantools-strict",
                        "recovered": "yes",
                        "error": _clean_error(strict_exc),
                    }
                )
                try:
                    db = cantools.database.load_file(path, strict=False)
                    parser_mode = "cantools-lenient"
                    dbc_signals, message_count = _cantools_rows(db, metadata, parser_mode)
                    dbc_signals, source_signal_count, supplemental_failures, source_errors = (
                        _supplement_cantools_rows(path, metadata, dbc_signals)
                    )
                    dbc_failures.extend(supplemental_failures)
                    for error in source_errors:
                        dbc_failures.append(
                            {
                                "dbc_filename": metadata["dbc_filename"],
                                "stage": "source-declaration-audit",
                                "recovered": "no",
                                "error": _clean_error(error),
                            }
                        )
                    if source_errors:
                        unrecovered = True
                except Exception as lenient_exc:  # noqa: BLE001 - audit then fall back
                    dbc_failures.append(
                        {
                            "dbc_filename": metadata["dbc_filename"],
                            "stage": "cantools-lenient",
                            "recovered": "yes",
                            "error": _clean_error(lenient_exc),
                        }
                    )
                    parser_mode = "line-fallback"
                    dbc_signals, message_count, fallback_ambiguities, fallback_errors = (
                        _fallback_rows(path, metadata)
                    )
                    source_signal_count = len(dbc_signals)
                    dbc_ambiguities.extend(fallback_ambiguities)
                    for error in fallback_errors:
                        dbc_failures.append(
                            {
                                "dbc_filename": metadata["dbc_filename"],
                                "stage": "line-fallback",
                                "recovered": "no",
                                "error": _clean_error(error),
                            }
                        )
                    if fallback_errors:
                        unrecovered = True

        failures.extend(dbc_failures)
        ambiguities.extend(dbc_ambiguities)
        signals.extend(dbc_signals)
        target = str(path.resolve()) if path.is_symlink() and path.exists() else ""
        dbcs.append(
            {
                "oem_family": metadata["oem_family"],
                "category": metadata["category"],
                "origin_type": metadata["origin_type"],
                "dbc_filename": metadata["dbc_filename"],
                "corpus_symlink_path": str(path),
                "symlink_target": target,
                "parser_mode": parser_mode,
                "message_count": message_count,
                "source_signal_count": source_signal_count,
                "signal_count": len(dbc_signals),
                "parse_failure_count": len(dbc_failures),
                "metadata_ambiguity_count": len(dbc_ambiguities),
            }
        )

    signals.sort(key=_sort_signal_key)
    dbcs.sort(key=lambda row: (row["oem_family"].casefold(), row["dbc_filename"].casefold()))
    failures.sort(key=lambda row: (row["dbc_filename"].casefold(), row["stage"], row["error"]))
    ambiguities.sort(
        key=lambda row: (
            row["dbc_filename"].casefold(),
            row["kind"],
            row["can_id"],
            row["message_name"].casefold(),
            row["detail"],
        )
    )
    examples = _select_examples(signals)
    summary = _build_summary(manifest_rows, signals, dbcs, failures, ambiguities, examples)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_tsv(out_dir / "signals.tsv", SIGNAL_FIELDS, signals)
    _write_tsv(out_dir / "dbcs.tsv", DBC_FIELDS, dbcs)
    _write_tsv(out_dir / "examples.tsv", EXAMPLE_FIELDS, examples)
    _write_tsv(out_dir / "parse_failures.tsv", FAILURE_FIELDS, failures)
    _write_tsv(out_dir / "metadata_ambiguities.tsv", AMBIGUITY_FIELDS, ambiguities)
    with (out_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    (out_dir / "summary.md").write_text(_summary_markdown(summary), encoding="utf-8")

    totals = summary["totals"]
    print(f"Indexed {totals['indexed_dbcs']}/{totals['manifest_dbcs']} DBCs and "
          f"{totals['signals']} signals through corpus symlinks.")
    print(
        "Parser modes: "
        + ", ".join(
            f"{row['parser_mode']}={row['dbcs']}" for row in summary["dbc_parser_modes"]
        )
    )
    print(
        f"Parse audit: {totals['parse_failures']} failure rows, "
        f"{totals['unrecovered_parse_failures']} unrecovered."
    )
    print(f"Metadata ambiguities: {totals['metadata_ambiguities']}.")
    print(f"Wrote deterministic artifacts to {out_dir}")
    return 2 if unrecovered else 0


if __name__ == "__main__":
    raise SystemExit(main())
