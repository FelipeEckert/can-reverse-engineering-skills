"""Backend-independent passive CAN capture helpers.

This module opens either the existing CANsub receive path or an already
configured Linux SocketCAN interface and writes received data frames directly
to the canonical normalized 12-column CSV format.  It contains no transmit,
replay, injection, bit-rate configuration, or interface-state operations.
"""
from __future__ import annotations

import csv
import select
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import can
from can.interfaces.socketcan import socketcan as python_can_socketcan

import common
from trace_validation import NORMALIZED_COLUMNS


NETDEVICE_RX_COUNTERS = (
    "rx_packets",
    "rx_dropped",
    "rx_errors",
    "rx_fifo_errors",
    "rx_over_errors",
    "rx_missed_errors",
)

SO_RXQ_OVFL = 40
SO_RXQ_OVFL_STRUCT = struct.Struct("@I")


class SocketcanBusWithOverflow(python_can_socketcan.SocketcanBus):
    """python-can SocketCAN bus with per-socket RX queue drop accounting."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.socket_rx_queue_overflow_total: int | None = None
        self._socket_rx_queue_overflow_raw = 0
        super().__init__(*args, **kwargs)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
        except OSError:
            return
        self.socket_rx_queue_overflow_total = 0

    def _recv_internal(
        self, timeout: float | None
    ) -> tuple[can.Message | None, bool]:
        try:
            ready, _, _ = select.select([self.socket], [], [], timeout)
        except OSError as error:
            raise can.CanOperationError(
                f"Failed to receive: {error.strerror}", error.errno
            ) from error
        if not ready:
            return None, self._is_filtered

        ancillary_buffer_size = socket.CMSG_SPACE(
            python_can_socketcan.RECEIVED_TIMESTAMP_STRUCT.size
        ) + socket.CMSG_SPACE(SO_RXQ_OVFL_STRUCT.size)
        try:
            frame, ancillary, msg_flags, addr = self.socket.recvmsg(
                python_can_socketcan.constants.CANFD_MTU,
                ancillary_buffer_size,
            )
        except OSError as error:
            raise can.CanOperationError(
                f"Error receiving: {error.strerror}", error.errno
            ) from error
        if msg_flags & socket.MSG_CTRUNC:
            raise can.CanOperationError("SocketCAN ancillary data was truncated")

        timestamp_data = None
        overflow_data = None
        for cmsg_level, cmsg_type, cmsg_data in ancillary:
            if (
                cmsg_level == socket.SOL_SOCKET
                and cmsg_type == python_can_socketcan.constants.SO_TIMESTAMPNS
            ):
                if timestamp_data is not None:
                    raise can.CanOperationError("duplicate SocketCAN timestamp")
                timestamp_data = cmsg_data
            elif cmsg_level == socket.SOL_SOCKET and cmsg_type == SO_RXQ_OVFL:
                if overflow_data is not None:
                    raise can.CanOperationError("duplicate SO_RXQ_OVFL value")
                overflow_data = cmsg_data
            else:
                raise can.CanOperationError(
                    "unexpected SocketCAN ancillary message "
                    f"level={cmsg_level} type={cmsg_type}"
                )

        try:
            if timestamp_data is None:
                raise struct.error("missing timestamp")
            seconds, nanoseconds = (
                python_can_socketcan.RECEIVED_TIMESTAMP_STRUCT.unpack_from(
                    timestamp_data
                )
            )
            overflow = (
                SO_RXQ_OVFL_STRUCT.unpack(overflow_data)[0]
                if overflow_data is not None
                else None
            )
        except struct.error as error:
            raise can.CanOperationError(
                f"invalid SocketCAN ancillary data: {error}"
            ) from error
        if nanoseconds >= 1_000_000_000:
            raise can.CanOperationError(
                "Timestamp nanoseconds field was out of range: "
                f"{nanoseconds} not less than 1e9"
            )
        if overflow is not None:
            if self.socket_rx_queue_overflow_total is None:
                self.socket_rx_queue_overflow_total = 0
            self.socket_rx_queue_overflow_total += (
                overflow - self._socket_rx_queue_overflow_raw
            ) & 0xFFFFFFFF
            self._socket_rx_queue_overflow_raw = overflow

        can_id, can_dlc, flags, data = python_can_socketcan.dissect_can_frame(frame)
        is_extended = bool(can_id & python_can_socketcan.constants.CAN_EFF_FLAG)
        channel = None
        if self.channel == "":
            channel = addr[0] if isinstance(addr, tuple) else addr
        message = can.Message(
            timestamp=seconds + nanoseconds * 1e-9,
            channel=channel,
            arbitration_id=can_id & (0x1FFFFFFF if is_extended else 0x000007FF),
            is_extended_id=is_extended,
            is_remote_frame=bool(can_id & python_can_socketcan.constants.CAN_RTR_FLAG),
            is_error_frame=bool(can_id & python_can_socketcan.constants.CAN_ERR_FLAG),
            is_fd=len(frame) == python_can_socketcan.constants.CANFD_MTU,
            is_rx=not bool(msg_flags & socket.MSG_DONTROUTE),
            bitrate_switch=bool(flags & python_can_socketcan.constants.CANFD_BRS),
            error_state_indicator=bool(
                flags & python_can_socketcan.constants.CANFD_ESI
            ),
            dlc=can_dlc,
            data=data,
        )
        if not message.channel and self.channel:
            message.channel = self.channel
        return message, self._is_filtered


@dataclass
class CaptureStats:
    """Counts collected during one passive capture."""

    # ``frames`` counts non-error/data frames; error frames are separate.
    frames: int = 0
    rows_written: int = 0
    errors: int = 0
    ids: set[int] = field(default_factory=set)
    elapsed_seconds: float = 0.0
    average_fps: float = 0.0
    socket_receive_buffer: int | None = None
    # SO_RXQ_OVFL delta observed through the last delivered packet, not an exact
    # wall-clock capture-window drop count.
    socket_rx_queue_overflow_drops: int | None = None
    netdevice_rx_before: dict[str, int | None] = field(default_factory=dict)
    netdevice_rx_after: dict[str, int | None] = field(default_factory=dict)
    netdevice_rx_deltas: dict[str, int | None] = field(default_factory=dict)


class CaptureError(RuntimeError):
    """A receive-thread failure with the aggregate stats collected so far."""

    def __init__(self, message: str, stats: CaptureStats) -> None:
        super().__init__(message)
        self.stats = stats


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
        self.rows_written = 0
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
        self.rows_written += 1

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
    it does not configure the controller or its bit rates.  Direct construction
    keeps ambient python-can configuration from changing the selected backend.
    """

    if not channel.strip():
        raise RuntimeError("SocketCAN requires a non-empty interface name")
    try:
        return SocketcanBusWithOverflow(
            channel=channel,
            fd=True,
            receive_own_messages=False,
            can_filters=can_filters,
        )
    except (can.CanError, OSError) as exc:
        raise RuntimeError(
            f"could not open SocketCAN interface {channel!r}; configure and bring "
            f"it up externally before capture ({exc})"
        ) from exc


def socketcan_receive_buffer(bus: can.BusABC) -> int | None:
    """Return the effective SocketCAN SO_RCVBUF without changing it."""

    bus_socket = getattr(bus, "socket", None)
    if bus_socket is None:
        return None
    try:
        return int(bus_socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def snapshot_netdevice_rx_counters(
    channel: str,
    *,
    sysfs_root: str | Path = "/sys/class/net",
) -> dict[str, int | None]:
    """Read supported netdevice RX counters without changing interface state."""

    statistics = Path(sysfs_root) / channel / "statistics"
    snapshot: dict[str, int | None] = {}
    for name in NETDEVICE_RX_COUNTERS:
        try:
            snapshot[name] = int((statistics / name).read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            snapshot[name] = None
    return snapshot


def netdevice_rx_counter_deltas(
    before: dict[str, int | None],
    after: dict[str, int | None],
) -> dict[str, int | None]:
    """Subtract two netdevice snapshots, preserving unavailable counters."""

    deltas: dict[str, int | None] = {}
    for name in NETDEVICE_RX_COUNTERS:
        earlier = before.get(name)
        later = after.get(name)
        deltas[name] = (
            later - earlier if earlier is not None and later is not None else None
        )
    return deltas


def capture_passively(
    bus: can.BusABC,
    output: str | Path,
    duration: float,
    *,
    bus_channel: int | None = None,
    normalize_as_rx: bool = False,
) -> CaptureStats:
    """Receive for ``duration`` seconds and write normalized data frames.

    The overflow metric is the SO_RXQ_OVFL delta observed through the last
    delivered packet, not a guaranteed exact wall-clock capture-window count.
    """

    if duration < 0:
        raise ValueError("duration must be non-negative")

    writer = NormalizedCSVWriter(
        output,
        bus_channel=bus_channel,
        normalize_as_rx=normalize_as_rx,
    )
    counter = FrameCounter()
    notifier: can.Notifier | None = None
    overflow_before = getattr(bus, "socket_rx_queue_overflow_total", None)
    started = time.monotonic()
    ended = started
    try:
        # Count before writing so a writer failure still leaves an accurate
        # received-frame total in the surfaced CaptureError stats.
        notifier = can.Notifier([bus], [counter, writer])
        with notifier:
            deadline = started + duration
            while True:
                if notifier.exception is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))
        # Sample after Notifier.stop() has joined its receive thread, so frames
        # dispatched during shutdown share the same boundary as final counts.
        ended = time.monotonic()
    finally:
        writer.stop()

    stats = counter.stats
    stats.rows_written = writer.rows_written
    overflow_after = getattr(bus, "socket_rx_queue_overflow_total", None)
    if isinstance(overflow_before, int) and isinstance(overflow_after, int):
        stats.socket_rx_queue_overflow_drops = overflow_after - overflow_before
    stats.elapsed_seconds = max(0.0, ended - started)
    if stats.elapsed_seconds > 0:
        stats.average_fps = stats.frames / stats.elapsed_seconds

    if notifier is not None and notifier.exception is not None:
        raise CaptureError(
            f"CAN receive thread failed: {notifier.exception}", stats
        ) from notifier.exception
    return stats
