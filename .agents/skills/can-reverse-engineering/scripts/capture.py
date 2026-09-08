"""
capture.py - passively capture CAN to a canonical normalized CSV.

The default ``cansub`` backend retains the existing discovery, bit timing, and
silent-vs-ACK behavior described below.  The experimental ``socketcan`` backend
opens an already configured Linux interface such as can0 or vcan0 for
application-level receive only.  It never configures the interface or bit rate.
SocketCAN controller listen-only/ACK behavior must be configured externally.

The silent-vs-ACK mode comes from the PROFILE chosen at detect_bus.py and stored
in temp-output/bus.json:
  * vehicle profile -> SILENT (listen_only): the CANsub never transmits; other
    ECUs on the bus provide the ACK. Use on a real car/truck/bike/machine.
  * bench profile -> NORMAL (ACK): the CANsub acknowledges frames. Required for a
    SINGLE node on a desk - with no ACK a lone node hits ACK errors and gets stuck
    retransmitting (it won't update cleanly).
Override per-run with --listen-only (force silent) or --normal (force ACK).
If bus.json predates profiles, the default is SILENT (safe).

The bus is opened with error_frames=True; error frames are counted and reported
(they signal a bus problem) but are kept out of the CSV, which has no error-frame
representation. Bit-rate auto-detect (detect_bus.py) is always listen_only.

For CANsub, reads device + bit-rate from temp-output/bus.json (run detect_bus.py
first), or takes them as flags. SocketCAN does not read bus.json.

Examples:
    .venv/bin/python .agents/skills/can-reverse-engineering/scripts/capture.py \
        --duration 15 --label baseline
    .venv/bin/python .agents/skills/can-reverse-engineering/scripts/capture.py \
        --duration 60 --label run --ids 0x123,0x200
    .venv/bin/python .agents/skills/can-reverse-engineering/scripts/capture.py \
        --duration 15 --label sniff --listen-only   # force silent
    .venv/bin/python .agents/skills/can-reverse-engineering/scripts/capture.py \
        --backend socketcan --channel can0 --duration 15 --label baseline
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import common
import live_capture


def parse_ids(s: str | None) -> list[int] | None:
    if not s:
        return None
    return [int(x, 0) for x in s.split(",") if x.strip()]


# Kept as compatibility aliases for flask_sync.py phase 1.  The Flask controller
# still uses its existing CANsub-specific logger/session and is intentionally not
# integrated with the new passive layer yet.
_Counter = live_capture.FrameCounter
_DataOnly = live_capture.DataOnly


def resolve_config(bus_json: str = "temp-output/bus.json", device: str | None = None,
                   channel: int | None = None, bitrate: int | None = None,
                   data_bitrate: int | None = None) -> dict:
    """Resolve {interface, channel, nominal, data, sample_point, listen_only,
    profile} for a capture.

    Reads temp-output/bus.json (written by detect_bus.py) and applies optional
    CLI-style overrides. Used by both capture.py's main() and flask_sync.py's
    in-process capture controller so they share one source of truth. `listen_only`
    comes from the recorded profile (default True/safe if bus.json predates it).
    """
    cfg = {}
    p = Path(bus_json)
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    if bitrate:
        cfg["nominal"] = bitrate
        cfg["data"] = data_bitrate or bitrate
        cfg.setdefault("sample_point", 80.0)
    nominal = cfg.get("nominal", cfg.get("bitrate"))
    if nominal is None:
        raise RuntimeError("no bit-rate. Run detect_bus.py or pass --bitrate.")
    data = cfg.get("data", cfg.get("data_bitrate", nominal))
    sample_point = cfg.get("sample_point", 80.0)
    interface = cfg.get("interface")
    chan = cfg.get("channel")
    if device or channel or chan is None:
        picked = common.pick_config(device, channel)
        interface, chan = picked["interface"], picked["channel"]
    return {"interface": interface, "channel": chan, "nominal": nominal,
            "data": data, "sample_point": sample_point,
            "listen_only": cfg.get("listen_only", True),
            "profile": cfg.get("profile")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, required=True, help="seconds to capture")
    ap.add_argument("--backend", choices=("cansub", "socketcan"), default="cansub",
                    help="live backend (default: cansub)")
    ap.add_argument("--label", default="capture", help="trace label (filename stem)")
    ap.add_argument("--out", help="output CSV (default temp-output/trace_<label>.csv)")
    ap.add_argument("--bus-json", default="temp-output/bus.json")
    ap.add_argument("--device")
    ap.add_argument("--channel", help="CANsub channel number, or SocketCAN interface "
                    "name (default can0 for --backend socketcan)")
    ap.add_argument("--bitrate", type=int)
    ap.add_argument("--data-bitrate", type=int)
    ap.add_argument("--ids", help="comma-separated hardware ID filter, e.g. 0x123,0x200")
    mode_grp = ap.add_mutually_exclusive_group()
    mode_grp.add_argument("--listen-only", dest="mode", action="store_const",
                          const="silent", help="force SILENT (no ACK) - multi-node bus")
    mode_grp.add_argument("--normal", dest="mode", action="store_const",
                          const="ack", help="force NORMAL/ACK - single-node bench")
    args = ap.parse_args(argv)

    out = Path(args.out) if args.out else Path(f"temp-output/trace_{args.label}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    can_filters = None
    ids = parse_ids(args.ids)
    if ids:
        can_filters = [{"can_id": i, "can_mask": 0x1FFFFFFF} for i in ids]

    if args.backend == "socketcan":
        incompatible = []
        if args.device is not None:
            incompatible.append("--device")
        if args.bitrate is not None:
            incompatible.append("--bitrate")
        if args.data_bitrate is not None:
            incompatible.append("--data-bitrate")
        if args.mode == "silent":
            incompatible.append("--listen-only")
        elif args.mode == "ack":
            incompatible.append("--normal")
        if incompatible:
            print(
                "ERROR: SocketCAN uses an externally configured interface; "
                + ", ".join(incompatible)
                + " cannot be applied with --backend socketcan.",
                file=sys.stderr,
            )
            return 1

        channel = args.channel or "can0"
        print(
            f"Capturing {args.duration:.0f}s on SocketCAN {channel} "
            f"(application receive-only) -> {out}",
            file=sys.stderr,
        )
        print(
            "SocketCAN bit rate, interface state, and hardware ACK/listen-only "
            "controller mode are configured externally.",
            file=sys.stderr,
        )
        stats = None
        capture_error = None
        rx_buffer = None
        rx_before = {}
        rx_after = {}
        try:
            with live_capture.open_socketcan_bus(
                channel, can_filters=can_filters
            ) as bus:
                rx_buffer = live_capture.socketcan_receive_buffer(bus)
                rx_before = live_capture.snapshot_netdevice_rx_counters(channel)
                try:
                    stats = live_capture.capture_passively(
                        bus,
                        out,
                        args.duration,
                        bus_channel=0,
                        normalize_as_rx=True,
                    )
                except live_capture.CaptureError as exc:
                    stats = exc.stats
                    capture_error = exc
                finally:
                    rx_after = live_capture.snapshot_netdevice_rx_counters(channel)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        stats.socket_receive_buffer = rx_buffer
        stats.netdevice_rx_before = rx_before
        stats.netdevice_rx_after = rx_after
        stats.netdevice_rx_deltas = live_capture.netdevice_rx_counter_deltas(
            rx_before, rx_after
        )

        summary = (
            f"Captured {stats.frames} data frames, wrote {stats.rows_written} CSV rows, "
            f"{len(stats.ids)} unique IDs in {stats.elapsed_seconds:.3f}s"
        )
        if stats.errors:
            summary += f"  [!] {stats.errors} error frames seen (bus problem?)"
        print(f"{summary} -> {out}")
        print(f"Rate: average={stats.average_fps:.1f} data frames/s")
        if stats.socket_receive_buffer is None:
            print("SocketCAN SO_RCVBUF: unavailable")
        else:
            print(f"SocketCAN SO_RCVBUF: {stats.socket_receive_buffer} bytes")
        overflow = stats.socket_rx_queue_overflow_drops
        print(
            "SocketCAN RX queue overflow drops: "
            f"{overflow if overflow is not None else 'unavailable'}"
        )
        delta_parts = [
            f"{name}={value if value is not None else 'unavailable'}"
            for name, value in stats.netdevice_rx_deltas.items()
        ]
        print("Netdevice RX deltas: " + ", ".join(delta_parts))
        if stats.rows_written != stats.frames:
            print(
                f"[!] received/written mismatch: {stats.frames} data frames vs "
                f"{stats.rows_written} CSV rows",
                file=sys.stderr,
            )
        if capture_error is not None:
            print(f"ERROR: {capture_error}", file=sys.stderr)
            return 1
        return 0

    # Resolve config: bus.json then CLI overrides.
    try:
        cansub_channel = int(args.channel) if args.channel is not None else None
    except ValueError:
        print("ERROR: CANsub --channel must be a numeric channel index.", file=sys.stderr)
        return 1
    try:
        cfg = resolve_config(
            args.bus_json,
            args.device,
            cansub_channel,
            args.bitrate,
            args.data_bitrate or common.DEFAULT_DATA_BITRATE,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    nominal, data, sample_point = cfg["nominal"], cfg["data"], cfg["sample_point"]

    # Effective mode: explicit flag overrides the recorded profile (bus.json).
    if args.mode == "silent":
        listen_only = True
    elif args.mode == "ack":
        listen_only = False
    else:
        listen_only = cfg["listen_only"]

    fd = f"/{data}" if data != nominal else ""
    mode = "silent/listen_only" if listen_only else "normal/ACK"
    prof = f" [{cfg['profile']}]" if cfg.get("profile") and args.mode is None else ""
    print(f"Capturing {args.duration:.0f}s on {cfg['channel']} @ {nominal}{fd} bps "
          f"({mode}{prof}) -> {out}", file=sys.stderr)

    capture_error = None
    with live_capture.open_cansub_bus(
        cfg, listen_only=listen_only, can_filters=can_filters
    ) as bus:
        try:
            stats = live_capture.capture_passively(bus, out, args.duration)
        except live_capture.CaptureError as exc:
            stats = exc.stats
            capture_error = exc

    summary = f"Captured {stats.frames} data frames, {len(stats.ids)} unique IDs"
    if stats.errors:
        summary += f"  [!] {stats.errors} error frames seen (bus problem?)"
    print(f"{summary} -> {out}")
    if capture_error is not None:
        print(f"ERROR: {capture_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
