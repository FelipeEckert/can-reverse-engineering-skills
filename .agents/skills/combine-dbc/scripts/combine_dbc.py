"""
combine_dbc.py - combine individual single-signal DBCs into one application DBC.

Scans an application's decoding-output folder for per-signal DBC files and merges
them into a single combined DBC at the application level. Re-runnable at any time
as new signals are confirmed.

Layout it expects (produced by the can-reverse-engineering skill):
    decoding-output/<application>/<signal>/<signal>.dbc   # inputs (one per signal)
    decoding-output/<application>/<application>.dbc        # combined output

Signals sharing a CAN frame id are merged into one message; a re-merged signal of
the same name replaces the previous definition (so re-running is idempotent).

Examples:
    .venv/bin/python .agents/skills/combine-dbc/scripts/combine_dbc.py --app sensor-to-can
    .venv/bin/python .agents/skills/combine-dbc/scripts/combine_dbc.py \
        --app-dir decoding-output/sensor-to-can --out combined.dbc
    .venv/bin/python .agents/skills/combine-dbc/scripts/combine_dbc.py \
        --app sensor-to-can --inputs a/a.dbc b/b.dbc
"""
from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path

import cantools
from cantools.database.can.formats.dbc_specifics import DbcSpecifics
from cantools.database.utils import start_bit


def _required_message_length(signals) -> int:
    """Return the byte extent using cantools' byte-order-aware geometry."""
    return max(
        ((start_bit(signal) + signal.length + 7) // 8 for signal in signals),
        default=0,
    )


def _rebuild_message(message, signals, *, minimum_length=0):
    """Rebuild ``message`` with new signals without losing message metadata."""
    length = max(
        message.length,
        minimum_length,
        _required_message_length(signals),
    )
    return cantools.database.can.Message(
        frame_id=message.frame_id,
        name=message.name,
        length=length,
        signals=signals,
        contained_messages=list(message.contained_messages),
        header_id=message.header_id,
        header_byte_order=message.header_byte_order,
        unused_bit_pattern=message.unused_bit_pattern,
        comment=message.comments,
        senders=list(message.senders),
        send_type=message.send_type,
        cycle_time=message.cycle_time,
        dbc_specifics=message.dbc,
        autosar_specifics=message.autosar,
        is_extended_frame=message.is_extended_frame,
        is_fd=message.is_fd,
        bus_name=message.bus_name,
        signal_groups=(
            list(message.signal_groups) if message.signal_groups is not None else None
        ),
        protocol=message.protocol,
    )


def _merge_dbc_specifics(databases) -> DbcSpecifics | None:
    """Carry cantools' DBC definitions needed by message/signal metadata."""
    specifics = [database.dbc for database in databases if database.dbc is not None]
    if not specifics:
        return None

    fields = (
        "attributes",
        "attribute_definitions",
        "environment_variables",
        "value_tables",
        "attributes_rel",
        "attribute_definitions_rel",
    )
    merged = {field: OrderedDict() for field in fields}
    for item in specifics:
        for field in fields:
            target = merged[field]
            for name, value in getattr(item, field).items():
                target.setdefault(name, deepcopy(value))

    return DbcSpecifics(**merged)


def merge_databases(paths: list[Path]) -> cantools.database.Database:
    """Merge several DBCs, combining signals per frame id (replace-by-name)."""
    by_id: dict[int, cantools.database.can.Message] = {}
    databases = []
    for p in paths:
        db = cantools.database.load_file(str(p))
        databases.append(db)
        for m in db.messages:
            if m.frame_id in by_id:
                existing = by_id[m.frame_id]
                names = {s.name for s in m.signals}
                kept = [s for s in existing.signals if s.name not in names]
                by_id[m.frame_id] = _rebuild_message(
                    existing,
                    kept + list(m.signals),
                    minimum_length=m.length,
                )
            else:
                by_id[m.frame_id] = m
    return cantools.database.Database(
        messages=list(by_id.values()),
        dbc_specifics=_merge_dbc_specifics(databases),
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--app", help="application name under --base, e.g. sensor-to-can")
    g.add_argument("--app-dir", help="explicit application folder path")
    ap.add_argument("--base", default="decoding-output",
                    help="base folder holding applications (default decoding-output)")
    ap.add_argument("--inputs", nargs="+",
                    help="explicit signal-DBC paths (relative to the app dir) "
                         "instead of auto-scanning */*.dbc")
    ap.add_argument("--out", help="combined DBC path (default <app-dir>/<app>.dbc)")
    args = ap.parse_args()

    app_dir = Path(args.app_dir) if args.app_dir else Path(args.base) / args.app
    if not app_dir.is_dir():
        print(f"ERROR: application folder not found: {app_dir}", file=sys.stderr)
        return 1
    app_name = app_dir.name

    out = Path(args.out) if args.out else app_dir / f"{app_name}.dbc"

    # Gather per-signal DBCs. Default: one folder level down (decoding-output/
    # <app>/<signal>/<signal>.dbc) — this naturally excludes the combined <app>.dbc
    # that sits at the app-dir root.
    if args.inputs:
        paths = [app_dir / p for p in args.inputs]
    else:
        paths = sorted(app_dir.glob("*/*.dbc"))
    paths = [p for p in paths if p.resolve() != out.resolve()]

    missing = [p for p in paths if not p.is_file()]
    if missing:
        print("ERROR: missing input DBC(s): " + ", ".join(str(m) for m in missing),
              file=sys.stderr)
        return 1
    if not paths:
        print(f"ERROR: no signal DBCs found under {app_dir} (looked for */*.dbc).",
              file=sys.stderr)
        return 1

    print(f"Combining {len(paths)} signal DBC(s) for '{app_name}':")
    for p in paths:
        print(f"  + {p}")

    db = merge_databases(paths)
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" preserves cantools' CRLF line endings verbatim; without it,
    # text-mode writing on Windows translates each \n to \r\n, yielding \r\r\n.
    out.write_text(db.as_dbc_string(), encoding="utf-8", newline="")

    total_sigs = sum(len(m.signals) for m in db.messages)
    print(f"\nWrote {out}: {len(db.messages)} message(s) / {total_sigs} signal(s).")
    for m in sorted(db.messages, key=lambda m: m.frame_id):
        names = ", ".join(s.name for s in m.signals)
        print(f"  0x{m.frame_id:X}: {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
