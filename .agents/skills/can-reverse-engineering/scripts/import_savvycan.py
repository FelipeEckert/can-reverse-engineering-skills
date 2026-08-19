"""
import_savvycan.py - convert SavvyCAN/GVRET CSV to the normalized trace CSV.

The input format is the 14-column classical-CAN CSV used by SavvyCAN/GVRET:

    Time Stamp,ID,Extended,Dir,Bus,LEN,D1,D2,D3,D4,D5,D6,D7,D8

Timestamps are integer microseconds on a relative clock. Their origin is kept
while the unit is converted to seconds. A backward jump is rejected: this tool
does not sort, unwrap, or otherwise repair timestamp resets.

SavvyCAN does not provide EDL, BRS, ESI, or RTR in this format. EDL/BRS/ESI are
set to 0 because this adapter accepts classical CAN payloads only. RTR is also
set to 0, so remote-frame status cannot be recovered from this CSV format.

Example:
    python import_savvycan.py --trace savvycan.csv --out temp-output/trace.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
from pathlib import Path


SAVVYCAN_COLUMNS = [
    "Time Stamp", "ID", "Extended", "Dir", "Bus", "LEN",
    "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8",
]

NORMALIZED_COLUMNS = [
    "TimestampEpoch", "BusChannel", "ID", "IDE", "DLC", "DataLength",
    "Dir", "EDL", "BRS", "ESI", "RTR", "DataBytes",
]

_HEX_ID = re.compile(r"^[0-9A-Fa-f]+$")
_HEX_BYTE = re.compile(r"^[0-9A-Fa-f]{2}$")


class SavvyCanImportError(ValueError):
    """Input cannot be converted without guessing or repairing data."""


def _drop_trailing_empty(row: list[str]) -> list[str]:
    """Ignore SavvyCAN's optional empty field after D8."""
    if len(row) == len(SAVVYCAN_COLUMNS) + 1 and row[-1] == "":
        return row[:-1]
    return row


def _boolean_bit(value: str, field: str, line: int) -> str:
    normalized = value.strip().lower()
    if normalized in {"false", "0"}:
        return "0"
    if normalized in {"true", "1"}:
        return "1"
    raise SavvyCanImportError(
        f"line {line}: invalid {field} value {value!r}; expected true/false"
    )


def _direction(value: str, line: int) -> str:
    normalized = value.strip().lower()
    if normalized == "rx":
        return "0"
    if normalized == "tx":
        return "1"
    raise SavvyCanImportError(
        f"line {line}: invalid Dir value {value!r}; expected Rx or Tx"
    )


def _relative_seconds(timestamp_us: int) -> str:
    """Format integer microseconds as exact relative seconds."""
    return f"{timestamp_us // 1_000_000}.{timestamp_us % 1_000_000:06d}"


def convert(trace: Path, out: Path) -> int:
    """Convert one SavvyCAN CSV atomically and return its frame count."""
    if trace.resolve() == out.resolve():
        raise SavvyCanImportError("input and output paths must be different")

    out.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    count = 0
    previous_timestamp: int | None = None

    try:
        with trace.open("r", encoding="utf-8-sig", newline="") as src:
            reader = csv.reader(src)
            try:
                header = _drop_trailing_empty(next(reader))
            except StopIteration as exc:
                raise SavvyCanImportError("input is empty") from exc
            if header != SAVVYCAN_COLUMNS:
                raise SavvyCanImportError(
                    f"unexpected header: {header!r}; expected {SAVVYCAN_COLUMNS!r}"
                )

            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", delete=False,
                dir=out.parent, prefix=f".{out.name}.", suffix=".tmp",
            ) as dst:
                temp_path = Path(dst.name)
                writer = csv.writer(dst, delimiter=";", lineterminator="\n")
                writer.writerow(NORMALIZED_COLUMNS)

                for row in reader:
                    if not row:
                        continue
                    line = reader.line_num
                    row = _drop_trailing_empty(row)
                    if len(row) != len(SAVVYCAN_COLUMNS):
                        raise SavvyCanImportError(
                            f"line {line}: expected 14 fields plus an optional "
                            f"trailing empty field, got {len(row)}"
                        )

                    try:
                        timestamp_us = int(row[0].strip(), 10)
                    except ValueError as exc:
                        raise SavvyCanImportError(
                            f"line {line}: invalid microsecond timestamp {row[0]!r}"
                        ) from exc
                    if timestamp_us < 0:
                        raise SavvyCanImportError(
                            f"line {line}: timestamp must be non-negative"
                        )
                    if (previous_timestamp is not None
                            and timestamp_us < previous_timestamp):
                        raise SavvyCanImportError(
                            f"line {line}: timestamp reset detected: {timestamp_us} < "
                            f"{previous_timestamp} microseconds; refusing to sort, "
                            "unwrap, or repair the trace"
                        )
                    previous_timestamp = timestamp_us

                    can_id = row[1].strip()
                    if not _HEX_ID.fullmatch(can_id):
                        raise SavvyCanImportError(
                            f"line {line}: invalid hexadecimal CAN ID {row[1]!r}"
                        )
                    ide = _boolean_bit(row[2], "Extended", line)
                    direction = _direction(row[3], line)

                    bus = row[4].strip()
                    try:
                        int(bus, 10)
                    except ValueError as exc:
                        raise SavvyCanImportError(
                            f"line {line}: invalid Bus value {row[4]!r}"
                        ) from exc

                    try:
                        length = int(row[5].strip(), 10)
                    except ValueError as exc:
                        raise SavvyCanImportError(
                            f"line {line}: invalid LEN value {row[5]!r}"
                        ) from exc
                    if not 0 <= length <= 8:
                        raise SavvyCanImportError(
                            f"line {line}: LEN={length} is not classical CAN (0..8)"
                        )

                    payload = [value.strip() for value in row[6:6 + length]]
                    for index, value in enumerate(payload, start=1):
                        if not _HEX_BYTE.fullmatch(value):
                            raise SavvyCanImportError(
                                f"line {line}: invalid D{index} byte {value!r}"
                            )

                    writer.writerow([
                        _relative_seconds(timestamp_us), bus, can_id, ide,
                        str(length), str(length), direction,
                        "0", "0", "0", "0", "".join(payload),
                    ])
                    count += 1

        os.replace(temp_path, out)
        temp_path = None
        return count
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trace", required=True, help="SavvyCAN/GVRET input CSV")
    parser.add_argument("--out", required=True, help="normalized output CSV")
    args = parser.parse_args()

    try:
        count = convert(Path(args.trace), Path(args.out))
    except (OSError, csv.Error, SavvyCanImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Converted {count} frames -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
