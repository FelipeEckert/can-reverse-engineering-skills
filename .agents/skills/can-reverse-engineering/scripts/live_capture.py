"""Backend-independent passive CAN capture helpers.

This module opens either the existing CANsub receive path or an already
configured Linux SocketCAN interface and writes received data frames directly
to the canonical normalized 12-column CSV format.  It contains no transmit,
replay, injection, bit-rate configuration, or interface-state operations.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import can

import common
from trace_validation import NORMALIZED_COLUMNS


@dataclass
class CaptureStats:
    """Counts collected during one passive capture."""

    frames: int = 0
    errors: int = 0
    ids: set[int] = field(default_factory=set)


class FrameCounter(can.Listener):
    """Count data and error frames without retaining message payloads."""

    def __init__(self) -> None:
        self.stats = CaptureStats()

    @property
    def n(self) -> int:
        """Compatibility alias used by the existing CANsub Flask controller."""
        return self.stats.frames

    @property
    def errors(self) -> int:
        return self.stats.errors

    @property
    def ids(self) -> set[int]:
        return self.stats.ids

    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_error_frame:
            self.stats.errors += 1
            return
        self.stats.frames += 1
        self.stats.ids.add(msg.arbitration_id)


class DataOnly(can.Listener):
    """Compatibility adapter that omits error frames from a wrapped listener."""

    def __init__(self, writer: can.Listener) -> None:
        self.writer = writer

    def on_message_received(self, msg: can.Message) -> None:
        if not msg.is_error_frame:
            self.writer.on_message_received(msg)

    def stop(self) -> None:
        self.writer.stop()


class NormalizedCSVWriter(can.Listener):
    """Write python-can messages to the canonical normalized trace format.

    ``bus_channel`` maps a backend channel name such as ``can0`` to the numeric
    channel required by the existing trace validator.  When it is omitted, the
    numeric ``Message.channel`` supplied by CANsub is retained.

    ``normalize_as_rx`` treats delivered frames as received from the capture
    application's perspective.  It is enabled only by the receive-only
    SocketCAN CLI path; CANsub retains the message's original direction.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        bus_channel: int | None = None,
        normalize_as_rx: bool = False,
    ) -> None:
        self.path = Path(path)
        self.bus_channel = bus_channel
        self.normalize_as_rx = normalize_as_rx
        self._file = self.path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file, delimiter=";", lineterminator="\n")
        self._writer.writerow(NORMALIZED_COLUMNS)
        self._stopped = False

    def _channel_for(self, msg: can.Message) -> int:
        channel: Any = self.bus_channel if self.bus_channel is not None else msg.channel
        if isinstance(channel, bool):
            raise ValueError("BusChannel must be a non-negative integer, not a boolean")
        try:
            normalized = int(channel)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"BusChannel must be numeric for normalized output; got {channel!r}"
            ) from exc
        if normalized < 0:
            raise ValueError("BusChannel must be non-negative")
        return normalized

    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_error_frame:
            return

        # RTR's DLC requests a response length but carries no payload.  Keep the
        # request in DLC and write the canonical/validator-compatible length 0.
        data_length = 0 if msg.is_remote_frame else len(msg.data)
        expected_length = msg.dlc if msg.is_fd else min(msg.dlc, 8)
        if not msg.is_remote_frame and expected_length != data_length:
            raise ValueError(
                f"message DLC/length {msg.dlc} does not match its "
                f"{data_length}-byte payload"
            )
        identifier = (
            f"{msg.arbitration_id:08X}"
            if msg.is_extended_id
            else f"{msg.arbitration_id:03X}"
        )
        self._writer.writerow(
            (
                f"{msg.timestamp:10.6f}",
                str(self._channel_for(msg)),
                identifier,
                "1" if msg.is_extended_id else "0",
                str(can.util.len2dlc(msg.dlc)),
                str(data_length),
                "0" if self.normalize_as_rx or msg.is_rx else "1",
                "1" if msg.is_fd else "0",
                "1" if msg.bitrate_switch else "0",
                "1" if msg.error_state_indicator else "0",
                "1" if msg.is_remote_frame else "0",
                msg.data.hex().upper(),
            )
        )

    def stop(self) -> None:
        if not self._stopped:
            self._file.flush()
            self._file.close()
            self._stopped = True


def open_cansub_bus(
    config: dict[str, Any],
    *,
    listen_only: bool,
    can_filters: list[dict[str, Any]] | None = None,
) -> can.BusABC:
    """Open the existing CANsub path with its established timing and mode."""

    timing = common.make_timing(
        config["nominal"], config["data"], config["sample_point"]
    )
    return can.Bus(
        interface=config["interface"],
        channel=config["channel"],
        timing=timing,
        listen_only=listen_only,
        error_frames=True,
        can_filters=can_filters,
    )


def open_socketcan_bus(
    channel: str,
    *,
    can_filters: list[dict[str, Any]] | None = None,
) -> can.BusABC:
    """Open an already configured SocketCAN interface for application RX only.

    ``fd=True`` enables receipt of CAN FD frames on interfaces that support them;
    it does not configure the controller or its bit rates.  ``ignore_config``
    prevents ambient python-can configuration from changing the selected backend.
    """

    if not channel.strip():
        raise RuntimeError("SocketCAN requires a non-empty interface name")
    try:
        return can.Bus(
            interface="socketcan",
            channel=channel,
            fd=True,
            receive_own_messages=False,
            ignore_config=True,
            can_filters=can_filters,
        )
    except (can.CanError, OSError) as exc:
        raise RuntimeError(
            f"could not open SocketCAN interface {channel!r}; configure and bring "
            f"it up externally before capture ({exc})"
        ) from exc


def capture_passively(
    bus: can.BusABC,
    output: str | Path,
    duration: float,
    *,
    bus_channel: int | None = None,
    normalize_as_rx: bool = False,
) -> CaptureStats:
    """Receive for ``duration`` seconds and write normalized data frames."""

    writer = NormalizedCSVWriter(
        output,
        bus_channel=bus_channel,
        normalize_as_rx=normalize_as_rx,
    )
    counter = FrameCounter()
    try:
        with can.Notifier([bus], [writer, counter]):
            time.sleep(duration)
    finally:
        writer.stop()
    return counter.stats
