"""Shared validation for CAN trace import and normalized webCAN CSV loading."""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


NORMALIZED_COLUMNS = (
    "TimestampEpoch",
    "BusChannel",
    "ID",
    "IDE",
    "DLC",
    "DataLength",
    "Dir",
    "EDL",
    "BRS",
    "ESI",
    "RTR",
    "DataBytes",
)

_HEX_ID = re.compile(r"^[0-9A-Fa-f]+$")
_HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{2}$")
_FD_DLC_TO_LENGTH = {
    0: 0,
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
    8: 8,
    9: 12,
    10: 16,
    11: 20,
    12: 24,
    13: 32,
    14: 48,
    15: 64,
}


class TraceValidationError(ValueError):
    """A trace row is malformed and cannot be interpreted without guessing."""


@dataclass(frozen=True)
class NormalizedFrame:
    """One validated normalized trace row in its original input position."""

    timestamp: float
    bus_channel: int
    can_id: int
    ide: bool
    dlc: int
    data_length: int
    direction: int
    edl: bool
    brs: bool
    esi: bool
    rtr: bool
    data: bytes


def parse_nonnegative_int(value: str, field: str, line: int) -> int:
    """Parse a decimal integer used by both source and normalized traces."""

    try:
        parsed = int(value.strip(), 10)
    except ValueError as exc:
        raise TraceValidationError(
            f"line {line}: invalid {field} value {value!r}; expected a decimal integer"
        ) from exc
    if parsed < 0:
        raise TraceValidationError(f"line {line}: {field} must be non-negative")
    return parsed


def parse_boolean_bit(
    value: str,
    field: str,
    line: int,
    *,
    allow_words: bool = False,
) -> int:
    """Parse a normalized 0/1 flag, optionally accepting source true/false."""

    normalized = value.strip().lower()
    if normalized in {"0", "1"}:
        return int(normalized)
    if allow_words and normalized in {"false", "true"}:
        return int(normalized == "true")
    expected = "0/1 or true/false" if allow_words else "0 or 1"
    raise TraceValidationError(
        f"line {line}: invalid {field} value {value!r}; expected {expected}"
    )


def parse_direction(
    value: str,
    line: int,
    *,
    source_words: bool = False,
) -> int:
    """Parse normalized 0/1 or the SavvyCAN source's Rx/Tx representation."""

    normalized = value.strip().lower()
    if source_words:
        if normalized in {"rx", "tx"}:
            return int(normalized == "tx")
        expected = "Rx or Tx"
    else:
        if normalized in {"0", "1"}:
            return int(normalized)
        expected = "0 (Rx) or 1 (Tx)"
    raise TraceValidationError(
        f"line {line}: invalid Dir value {value!r}; expected {expected}"
    )


def parse_can_id(value: str, ide: int | bool, line: int) -> int:
    """Parse a plain hexadecimal CAN ID and enforce the IDE-specific range."""

    normalized = value.strip()
    if not _HEX_ID.fullmatch(normalized):
        raise TraceValidationError(
            f"line {line}: invalid hexadecimal CAN ID {value!r}; "
            "expected hexadecimal digits without a 0x prefix"
        )
    can_id = int(normalized, 16)
    maximum = 0x1FFFFFFF if ide else 0x7FF
    if can_id > maximum:
        frame_type = "extended" if ide else "standard"
        raise TraceValidationError(
            f"line {line}: CAN ID 0x{can_id:X} exceeds the {frame_type} "
            f"IDE={int(bool(ide))} range (maximum 0x{maximum:X})"
        )
    return can_id


def validate_hex_byte(value: str, field: str, line: int) -> str:
    """Validate and return one two-digit payload byte without changing its text."""

    normalized = value.strip()
    if not _HEX_BYTE.fullmatch(normalized):
        raise TraceValidationError(
            f"line {line}: invalid {field} byte {value!r}; expected two hex digits"
        )
    return normalized


def parse_hex_payload(value: str, line: int) -> bytes:
    """Parse an unseparated normalized payload and identify its malformed byte."""

    normalized = value.strip()
    if len(normalized) % 2:
        raise TraceValidationError(
            f"line {line}: DataBytes has an odd number of hex digits"
        )
    for offset in range(0, len(normalized), 2):
        byte = normalized[offset : offset + 2]
        if not _HEX_BYTE.fullmatch(byte):
            raise TraceValidationError(
                f"line {line}: invalid DataBytes byte {offset // 2 + 1} "
                f"{byte!r}; expected two hex digits"
            )
    return bytes.fromhex(normalized)


def validate_normalized_header(header: list[str], path: Path) -> None:
    """Require the canonical prefix and allow only unambiguous trailing columns."""

    required = list(NORMALIZED_COLUMNS)
    if header[: len(required)] != required:
        raise TraceValidationError(
            f"unexpected CSV header in {path}: expected the first 12 columns to be "
            + ";".join(required)
        )
    extras = header[len(required) :]
    if any(not name.strip() for name in extras):
        raise TraceValidationError(
            f"unexpected CSV header in {path}: trailing extra columns must be named"
        )
    if len(header) != len(set(header)):
        raise TraceValidationError(
            f"unexpected CSV header in {path}: column names must be unique"
        )


def _validate_lengths(
    *,
    dlc: int,
    data_length: int,
    edl: int,
    brs: int,
    esi: int,
    rtr: int,
    payload_length: int,
    line: int,
) -> None:
    if dlc > 15:
        raise TraceValidationError(f"line {line}: DLC={dlc} is outside 0..15")
    if data_length > 64:
        raise TraceValidationError(
            f"line {line}: DataLength={data_length} is outside 0..64"
        )
    if payload_length != data_length:
        raise TraceValidationError(
            f"line {line}: DataLength={data_length} but DataBytes contains "
            f"{payload_length} bytes"
        )

    if edl:
        if rtr:
            raise TraceValidationError(
                f"line {line}: RTR=1 is invalid for a CAN FD frame (EDL=1)"
            )
        expected = _FD_DLC_TO_LENGTH[dlc]
        if data_length != expected:
            raise TraceValidationError(
                f"line {line}: CAN FD DLC={dlc} requires DataLength={expected}, "
                f"got {data_length}"
            )
        return

    if brs or esi:
        field = "BRS" if brs else "ESI"
        raise TraceValidationError(
            f"line {line}: {field}=1 requires a CAN FD frame (EDL=1)"
        )
    # Classical CAN retains the raw four-bit DLC. Codes 9..15 still carry at
    # most eight payload bytes; for RTR they request a length but carry none.
    expected = 0 if rtr else min(dlc, 8)
    if data_length != expected:
        detail = "RTR frames carry no payload" if rtr else "DLC and DataLength must match"
        raise TraceValidationError(
            f"line {line}: classical CAN DLC={dlc}, DataLength={data_length}; {detail}"
        )


def _next_csv_row(reader: Any, trace: Path) -> list[str]:
    try:
        return next(reader)
    except csv.Error as exc:
        raise TraceValidationError(
            f"invalid normalized CSV near line {reader.line_num} in {trace}: {exc}"
        ) from exc


def validate_normalized_trace(path: str | Path) -> list[NormalizedFrame]:
    """Validate a normalized trace and return frames without sorting or repair.

    The canonical 12 columns must be first and in order. Uniquely named trailing
    columns are accepted for annotations and ignored by analysis.
    """

    trace = Path(path)
    frames: list[NormalizedFrame] = []
    previous_timestamp: float | None = None
    with trace.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";", strict=True)
        try:
            header = _next_csv_row(reader, trace)
        except StopIteration as exc:
            raise TraceValidationError(f"normalized trace is empty: {trace}") from exc
        validate_normalized_header(header, trace)

        while True:
            try:
                row = _next_csv_row(reader, trace)
            except StopIteration:
                break
            if not row:
                continue
            line = reader.line_num
            if len(row) != len(header):
                raise TraceValidationError(
                    f"line {line}: expected {len(header)} fields, got {len(row)}"
                )
            values = dict(zip(header, row))

            raw_timestamp = values["TimestampEpoch"]
            try:
                timestamp = float(raw_timestamp.strip())
            except ValueError as exc:
                raise TraceValidationError(
                    f"line {line}: invalid TimestampEpoch {raw_timestamp!r}; "
                    "expected a finite non-negative number"
                ) from exc
            if not math.isfinite(timestamp) or timestamp < 0:
                raise TraceValidationError(
                    f"line {line}: invalid TimestampEpoch {raw_timestamp!r}; "
                    "expected a finite non-negative number"
                )
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise TraceValidationError(
                    f"line {line}: timestamp reset detected: {timestamp:g} < "
                    f"{previous_timestamp:g}; refusing to sort, unwrap, or repair "
                    "the trace"
                )
            previous_timestamp = timestamp

            bus_channel = parse_nonnegative_int(values["BusChannel"], "BusChannel", line)
            ide = parse_boolean_bit(values["IDE"], "IDE", line)
            can_id = parse_can_id(values["ID"], ide, line)
            dlc = parse_nonnegative_int(values["DLC"], "DLC", line)
            data_length = parse_nonnegative_int(
                values["DataLength"], "DataLength", line
            )
            direction = parse_direction(values["Dir"], line)
            edl = parse_boolean_bit(values["EDL"], "EDL", line)
            brs = parse_boolean_bit(values["BRS"], "BRS", line)
            esi = parse_boolean_bit(values["ESI"], "ESI", line)
            rtr = parse_boolean_bit(values["RTR"], "RTR", line)
            data = parse_hex_payload(values["DataBytes"], line)
            _validate_lengths(
                dlc=dlc,
                data_length=data_length,
                edl=edl,
                brs=brs,
                esi=esi,
                rtr=rtr,
                payload_length=len(data),
                line=line,
            )

            frames.append(
                NormalizedFrame(
                    timestamp=timestamp,
                    bus_channel=bus_channel,
                    can_id=can_id,
                    ide=bool(ide),
                    dlc=dlc,
                    data_length=data_length,
                    direction=direction,
                    edl=bool(edl),
                    brs=bool(brs),
                    esi=bool(esi),
                    rtr=bool(rtr),
                    data=data,
                )
            )
    return frames
