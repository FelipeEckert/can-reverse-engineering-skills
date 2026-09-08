from __future__ import annotations

import ast
import importlib
import io
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import can


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import capture  # noqa: E402
import common  # noqa: E402
import live_capture  # noqa: E402
import survey  # noqa: E402
from trace_validation import NORMALIZED_COLUMNS, validate_normalized_trace  # noqa: E402


class NormalizedWriterTests(unittest.TestCase):
    def write_messages(
        self,
        messages: list[can.Message],
        *,
        bus_channel: int | None = None,
        normalize_as_rx: bool = False,
    ) -> tuple[str, list]:
        with tempfile.TemporaryDirectory(prefix="live_capture_writer_") as directory:
            path = Path(directory) / "trace.csv"
            writer = live_capture.NormalizedCSVWriter(
                path,
                bus_channel=bus_channel,
                normalize_as_rx=normalize_as_rx,
            )
            for message in messages:
                writer.on_message_received(message)
            writer.stop()
            return path.read_text(encoding="utf-8"), validate_normalized_trace(path)

    def test_classical_frame_has_deterministic_canonical_format(self) -> None:
        text, frames = self.write_messages(
            [
                can.Message(
                    timestamp=1_700_000_000.125,
                    channel=1,
                    arbitration_id=0x123,
                    is_extended_id=False,
                    is_rx=True,
                    data=bytes.fromhex("01 AB 00 FF"),
                )
            ]
        )
        self.assertEqual(
            text,
            ";".join(NORMALIZED_COLUMNS)
            + "\n1700000000.125000;1;123;0;4;4;0;0;0;0;0;01AB00FF\n",
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].data, bytes.fromhex("01 AB 00 FF"))

    def test_extended_identifier_is_zero_padded_to_eight_hex_digits(self) -> None:
        text, frames = self.write_messages(
            [
                can.Message(
                    timestamp=1_700_000_001.0,
                    channel=2,
                    arbitration_id=0x1ABCDE,
                    is_extended_id=True,
                    data=b"\x5a",
                )
            ]
        )
        self.assertIn(";001ABCDE;1;1;1;0;0;0;0;0;5A\n", text)
        self.assertTrue(frames[0].ide)
        self.assertEqual(frames[0].can_id, 0x1ABCDE)

    def test_rtr_preserves_requested_dlc_and_writes_no_payload(self) -> None:
        text, frames = self.write_messages(
            [
                can.Message(
                    timestamp=1_700_000_002.0,
                    channel=1,
                    arbitration_id=0x456,
                    is_extended_id=False,
                    is_remote_frame=True,
                    dlc=8,
                    data=b"",
                )
            ]
        )
        self.assertIn(";456;0;8;0;0;0;0;0;1;\n", text)
        self.assertTrue(frames[0].rtr)
        self.assertEqual(frames[0].dlc, 8)
        self.assertEqual(frames[0].data_length, 0)

    def test_can_fd_fields_and_length_code(self) -> None:
        payload = bytes(range(12))
        text, frames = self.write_messages(
            [
                can.Message(
                    timestamp=1_700_000_003.0,
                    channel=1,
                    arbitration_id=0x321,
                    is_extended_id=False,
                    is_fd=True,
                    bitrate_switch=True,
                    error_state_indicator=True,
                    dlc=len(payload),
                    data=payload,
                )
            ]
        )
        self.assertIn(
            ";321;0;9;12;0;1;1;1;0;000102030405060708090A0B\n", text
        )
        self.assertTrue(frames[0].edl)
        self.assertTrue(frames[0].brs)
        self.assertTrue(frames[0].esi)
        self.assertEqual(frames[0].data_length, 12)

    def test_error_frames_are_counted_but_excluded(self) -> None:
        error = can.Message(
            timestamp=1_700_000_004.0,
            is_error_frame=True,
            is_extended_id=False,
        )
        data = can.Message(
            timestamp=1_700_000_005.0,
            channel="vcan0",
            arbitration_id=0x100,
            is_extended_id=False,
            data=b"\x01",
        )
        text, frames = self.write_messages([error, data], bus_channel=0)

        counter = live_capture.FrameCounter()
        counter.on_message_received(error)
        counter.on_message_received(data)

        self.assertNotIn("1700000004", text)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].bus_channel, 0)
        self.assertEqual(counter.errors, 1)
        self.assertEqual(counter.n, 1)
        self.assertEqual(counter.ids, {0x100})

    def test_socketcan_capture_normalizes_delivered_local_frame_as_rx(self) -> None:
        message = can.Message(
            timestamp=1_700_000_006.0,
            channel="vcan0",
            arbitration_id=0x123,
            is_extended_id=False,
            is_rx=False,
            data=b"\x01",
        )
        text, frames = self.write_messages(
            [message], bus_channel=0, normalize_as_rx=True
        )

        self.assertIn(";123;0;1;1;0;0;0;0;0;01\n", text)
        self.assertEqual(frames[0].direction, 0)
        self.assertFalse(message.is_rx)

    def test_default_writer_preserves_cansub_message_direction(self) -> None:
        text, frames = self.write_messages(
            [
                can.Message(
                    timestamp=1_700_000_006.5,
                    channel=1,
                    arbitration_id=0x124,
                    is_extended_id=False,
                    is_rx=False,
                    data=b"\x02",
                )
            ]
        )

        self.assertIn(";124;0;1;1;1;0;0;0;0;02\n", text)
        self.assertEqual(frames[0].direction, 1)

    def test_writer_counts_data_rows_separately_from_header_and_errors(self) -> None:
        error = can.Message(is_error_frame=True, is_extended_id=False)
        data = can.Message(
            timestamp=1_700_000_007.0,
            channel=0,
            arbitration_id=0x125,
            is_extended_id=False,
            data=b"\x03",
        )
        with tempfile.TemporaryDirectory(prefix="live_capture_row_count_") as directory:
            writer = live_capture.NormalizedCSVWriter(Path(directory) / "trace.csv")
            writer.on_message_received(error)
            writer.on_message_received(data)
            writer.stop()

        self.assertEqual(writer.rows_written, 1)

    def test_normalized_output_feeds_existing_loader_and_survey(self) -> None:
        messages = [
            can.Message(
                timestamp=1_700_000_010.0 + index,
                channel="can0",
                arbitration_id=0x222,
                is_extended_id=False,
                data=bytes((index, index + 1)),
            )
            for index in range(4)
        ]
        with tempfile.TemporaryDirectory(prefix="live_capture_pipeline_") as directory:
            path = Path(directory) / "trace.csv"
            writer = live_capture.NormalizedCSVWriter(path, bus_channel=0)
            for message in messages:
                writer.on_message_received(message)
            writer.stop()
            loaded = common.load_trace(str(path))
            rows, _ = survey.survey(loaded)

        self.assertEqual(len(loaded), 4)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 0x222)

    def test_matches_cansub_writer_for_supported_data_frames_when_installed(self) -> None:
        try:
            cansub = importlib.import_module("python_can_cansub")
        except ImportError:
            self.skipTest("python-can-cansub is not installed in this environment")

        messages = [
            can.Message(
                timestamp=1_700_000_006.25,
                channel=1,
                arbitration_id=0x123,
                is_extended_id=False,
                data=b"\x01\x02",
            ),
            can.Message(
                timestamp=1_700_000_007.5,
                channel=2,
                arbitration_id=0x18FF50E5,
                is_extended_id=True,
                is_fd=True,
                bitrate_switch=True,
                data=bytes(range(12)),
            ),
        ]
        with tempfile.TemporaryDirectory(prefix="cansub_writer_regression_") as directory:
            root = Path(directory)
            actual = root / "actual.csv"
            expected = root / "expected.csv"
            writer = live_capture.NormalizedCSVWriter(actual)
            reference = cansub.CanSubCSVWriter(expected)
            for message in messages:
                writer.on_message_received(message)
                reference.on_message_received(message)
            writer.stop()
            reference.stop()
            self.assertEqual(
                actual.read_text(encoding="utf-8"),
                expected.read_text(encoding="utf-8"),
            )


class BackendTests(unittest.TestCase):
    @staticmethod
    def _receive_bus(
        ancillary: list[tuple[int, int, bytes]], *, msg_flags: int = 0
    ) -> tuple[live_capture.SocketcanBusWithOverflow, can.Message]:
        bus = object.__new__(live_capture.SocketcanBusWithOverflow)
        bus.socket = mock.Mock()
        bus.channel = "vcan0"
        bus._is_filtered = True
        bus.socket_rx_queue_overflow_total = 0
        bus._socket_rx_queue_overflow_raw = 0
        frame = live_capture.python_can_socketcan.build_can_frame(
            can.Message(
                arbitration_id=0x123,
                is_extended_id=False,
                data=b"\x01\x02",
            )
        )
        bus.socket.recvmsg.return_value = (
            frame,
            ancillary,
            msg_flags,
            ("vcan0",),
        )
        with mock.patch.object(
            live_capture.select, "select", return_value=([bus.socket], [], [])
        ):
            message, already_filtered = bus._recv_internal(1.0)
        assert message is not None
        assert already_filtered
        return bus, message

    def test_socketcan_bus_enables_rx_queue_overflow_reporting(self) -> None:
        bus_socket = mock.Mock()

        def initialize(instance, *args, **kwargs) -> None:
            instance.socket = bus_socket

        with mock.patch.object(
            live_capture.python_can_socketcan.SocketcanBus,
            "__init__",
            new=initialize,
        ):
            bus = live_capture.SocketcanBusWithOverflow(channel="vcan0")

        bus_socket.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET, live_capture.SO_RXQ_OVFL, 1
        )
        self.assertEqual(bus.socket_rx_queue_overflow_total, 0)

    def test_socketcan_receive_parses_timestamp_and_overflow_ancillary_data(
        self,
    ) -> None:
        timestamp = live_capture.python_can_socketcan.RECEIVED_TIMESTAMP_STRUCT.pack(
            1_700_000_000, 125_000_000
        )
        overflow = live_capture.SO_RXQ_OVFL_STRUCT.pack(7)

        bus, message = self._receive_bus(
            [
                (socket.SOL_SOCKET, live_capture.SO_RXQ_OVFL, overflow),
                (
                    socket.SOL_SOCKET,
                    live_capture.python_can_socketcan.constants.SO_TIMESTAMPNS,
                    timestamp,
                ),
            ]
        )

        self.assertEqual(message.arbitration_id, 0x123)
        self.assertEqual(message.data, bytearray(b"\x01\x02"))
        self.assertAlmostEqual(message.timestamp, 1_700_000_000.125)
        self.assertEqual(bus.socket_rx_queue_overflow_total, 7)
        bus.socket.recvmsg.assert_called_once_with(
            live_capture.python_can_socketcan.constants.CANFD_MTU,
            live_capture.SOCKETCAN_ANCILLARY_BUFFER_SIZE,
        )

    def test_socketcan_overflow_counter_accumulates_across_u32_wrap(self) -> None:
        timestamp = live_capture.python_can_socketcan.RECEIVED_TIMESTAMP_STRUCT.pack(
            1_700_000_000, 0
        )
        overflow = live_capture.SO_RXQ_OVFL_STRUCT.pack(3)
        bus = object.__new__(live_capture.SocketcanBusWithOverflow)
        bus.socket = mock.Mock()
        bus.channel = "vcan0"
        bus._is_filtered = True
        bus.socket_rx_queue_overflow_total = 10
        bus._socket_rx_queue_overflow_raw = 0xFFFFFFFE
        bus.socket.recvmsg.return_value = (
            live_capture.python_can_socketcan.build_can_frame(
                can.Message(
                    arbitration_id=0x123,
                    is_extended_id=False,
                    data=b"\x01",
                )
            ),
            [
                (
                    socket.SOL_SOCKET,
                    live_capture.python_can_socketcan.constants.SO_TIMESTAMPNS,
                    timestamp,
                ),
                (socket.SOL_SOCKET, live_capture.SO_RXQ_OVFL, overflow),
            ],
            0,
            ("vcan0",),
        )

        with mock.patch.object(
            live_capture.select, "select", return_value=([bus.socket], [], [])
        ):
            bus._recv_internal(1.0)

        self.assertEqual(bus.socket_rx_queue_overflow_total, 15)

    def test_socketcan_receive_rejects_truncated_ancillary_data(self) -> None:
        bus = object.__new__(live_capture.SocketcanBusWithOverflow)
        bus.socket = mock.Mock()
        bus.channel = "vcan0"
        bus._is_filtered = True
        bus.socket_rx_queue_overflow_total = 0
        bus._socket_rx_queue_overflow_raw = 0
        bus.socket.recvmsg.return_value = (b"", [], socket.MSG_CTRUNC, ("vcan0",))

        with (
            mock.patch.object(
                live_capture.select, "select", return_value=([bus.socket], [], [])
            ),
            self.assertRaisesRegex(
                can.CanOperationError, "ancillary data was truncated"
            ),
        ):
            bus._recv_internal(1.0)

    def test_socketcan_receive_buffer_is_read_without_setting_it(self) -> None:
        bus_socket = mock.Mock()
        bus_socket.getsockopt.return_value = 212_992
        bus = mock.Mock(socket=bus_socket)

        self.assertEqual(live_capture.socketcan_receive_buffer(bus), 212_992)
        bus_socket.getsockopt.assert_called_once_with(
            socket.SOL_SOCKET, socket.SO_RCVBUF
        )
        self.assertFalse(
            hasattr(bus_socket, "setsockopt") and bus_socket.setsockopt.called
        )

    def test_socketcan_receive_buffer_unavailable_does_not_fail(self) -> None:
        bus_socket = mock.Mock()
        bus_socket.getsockopt.side_effect = OSError("unsupported")
        self.assertIsNone(
            live_capture.socketcan_receive_buffer(mock.Mock(socket=bus_socket))
        )
        self.assertIsNone(live_capture.socketcan_receive_buffer(object()))

    def test_netdevice_rx_snapshots_and_deltas_tolerate_missing_counters(self) -> None:
        with tempfile.TemporaryDirectory(prefix="netdevice_stats_") as directory:
            statistics = Path(directory) / "vcan0" / "statistics"
            statistics.mkdir(parents=True)
            (statistics / "rx_packets").write_text("100\n", encoding="ascii")
            (statistics / "rx_dropped").write_text("not-a-number\n", encoding="ascii")
            before = live_capture.snapshot_netdevice_rx_counters(
                "vcan0", sysfs_root=directory
            )
            (statistics / "rx_packets").write_text("125\n", encoding="ascii")
            after = live_capture.snapshot_netdevice_rx_counters(
                "vcan0", sysfs_root=directory
            )

        self.assertEqual(before["rx_packets"], 100)
        self.assertEqual(after["rx_packets"], 125)
        self.assertIsNone(before["rx_dropped"])
        self.assertTrue(
            all(
                before[name] is None
                for name in live_capture.NETDEVICE_RX_COUNTERS
                if name != "rx_packets"
            )
        )
        deltas = live_capture.netdevice_rx_counter_deltas(before, after)
        self.assertEqual(deltas["rx_packets"], 25)
        self.assertIsNone(deltas["rx_dropped"])

    def test_capture_reports_rows_and_non_integral_average_rate(self) -> None:
        messages = [
            can.Message(
                timestamp=1_700_000_020.0 + index,
                channel="vcan0",
                arbitration_id=0x120 + index,
                is_extended_id=False,
                data=bytes((index,)),
            )
            for index in range(3)
        ]
        error = can.Message(is_error_frame=True, is_extended_id=False)

        class Clock:
            now = 100.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        class FakeNotifier:
            def __init__(self, buses, listeners) -> None:
                self.listeners = listeners
                self.exception = None

            def __enter__(self):
                for message in [*messages, error]:
                    for listener in self.listeners:
                        listener(message)
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                return None

        clock = Clock()
        with (
            tempfile.TemporaryDirectory(prefix="capture_stats_") as directory,
            mock.patch.object(live_capture.can, "Notifier", FakeNotifier),
            mock.patch.object(live_capture.time, "monotonic", clock.monotonic),
            mock.patch.object(live_capture.time, "sleep", clock.sleep),
        ):
            trace = Path(directory) / "trace.csv"
            stats = live_capture.capture_passively(
                object(), trace, 2.5, bus_channel=0, normalize_as_rx=True
            )
            validated = validate_normalized_trace(trace)

        self.assertEqual(stats.frames, 3)
        self.assertEqual(stats.rows_written, 3)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.elapsed_seconds, 2.5)
        self.assertEqual(stats.average_fps, 1.2)
        self.assertIsNone(stats.socket_rx_queue_overflow_drops)
        self.assertEqual(len(validated), 3)

    def test_capture_reports_overflow_delta_at_final_notifier_boundary(self) -> None:
        class Bus:
            socket_rx_queue_overflow_total = 2

        class Notifier:
            def __init__(self, buses, listeners) -> None:
                self.bus = buses[0]
                self.exception = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                self.bus.socket_rx_queue_overflow_total = 7

        with (
            tempfile.TemporaryDirectory(prefix="capture_overflow_delta_") as directory,
            mock.patch.object(live_capture.can, "Notifier", Notifier),
        ):
            stats = live_capture.capture_passively(
                Bus(), Path(directory) / "trace.csv", 0.0, bus_channel=0
            )

        self.assertEqual(stats.socket_rx_queue_overflow_drops, 5)

    def test_subsecond_capture_uses_actual_elapsed_time_for_average(self) -> None:
        messages = [
            can.Message(
                timestamp=1_700_000_025.0 + index,
                channel="vcan0",
                arbitration_id=0x125,
                is_extended_id=False,
                data=bytes((index,)),
            )
            for index in range(2)
        ]

        class Clock:
            now = 100.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        class FakeNotifier:
            def __init__(self, buses, listeners) -> None:
                self.listeners = listeners
                self.exception = None

            def __enter__(self):
                for message in messages:
                    for listener in self.listeners:
                        listener(message)
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                return None

        clock = Clock()
        with (
            tempfile.TemporaryDirectory(prefix="capture_subsecond_") as directory,
            mock.patch.object(live_capture.can, "Notifier", FakeNotifier),
            mock.patch.object(live_capture.time, "monotonic", clock.monotonic),
            mock.patch.object(live_capture.time, "sleep", clock.sleep),
        ):
            stats = live_capture.capture_passively(
                object(), Path(directory) / "trace.csv", 0.25, bus_channel=0
            )

        self.assertEqual(stats.elapsed_seconds, 0.25)
        self.assertEqual(stats.average_fps, 8.0)

    def test_shutdown_dispatch_is_included_in_counts_and_elapsed_boundary(self) -> None:
        message = can.Message(
            timestamp=1_700_000_028.0,
            channel="vcan0",
            arbitration_id=0x128,
            is_extended_id=False,
            data=b"\x01",
        )

        class Clock:
            now = 100.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = Clock()

        class ShutdownDispatchNotifier:
            def __init__(self, buses, listeners) -> None:
                self.listeners = listeners
                self.exception = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                clock.now += 0.1
                for listener in self.listeners:
                    listener(message)

        with (
            tempfile.TemporaryDirectory(prefix="capture_shutdown_dispatch_") as directory,
            mock.patch.object(
                live_capture.can, "Notifier", ShutdownDispatchNotifier
            ),
            mock.patch.object(live_capture.time, "monotonic", clock.monotonic),
            mock.patch.object(live_capture.time, "sleep", clock.sleep),
        ):
            trace = Path(directory) / "trace.csv"
            stats = live_capture.capture_passively(
                object(), trace, 0.25, bus_channel=0
            )
            validated = validate_normalized_trace(trace)

        self.assertEqual(stats.frames, 1)
        self.assertEqual(stats.rows_written, 1)
        self.assertAlmostEqual(stats.elapsed_seconds, 0.35)
        self.assertAlmostEqual(stats.average_fps, 1.0 / 0.35)
        self.assertEqual(len(validated), 1)

    def test_capture_surfaces_notifier_exception_with_partial_stats(self) -> None:
        message = can.Message(
            timestamp=1_700_000_030.0,
            channel="vcan0",
            arbitration_id=0x123,
            is_extended_id=False,
            data=b"\x01",
        )

        active_notifier = None

        class Clock:
            now = 100.0

            def monotonic(self) -> float:
                return self.now

            def sleep(self, seconds: float) -> None:
                self.now += seconds
                if active_notifier.exception is None:
                    for listener in active_notifier.listeners:
                        listener(message)
                    active_notifier.exception = OSError("receive failed")

        class FailedNotifier:
            def __init__(self, buses, listeners) -> None:
                self.listeners = listeners
                self.exception = None

            def __enter__(self):
                nonlocal active_notifier
                active_notifier = self
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                return None

        clock = Clock()
        with (
            tempfile.TemporaryDirectory(prefix="capture_error_") as directory,
            mock.patch.object(live_capture.can, "Notifier", FailedNotifier),
            mock.patch.object(live_capture.time, "monotonic", clock.monotonic),
            mock.patch.object(live_capture.time, "sleep", clock.sleep),
        ):
            with self.assertRaisesRegex(
                live_capture.CaptureError, "CAN receive thread failed: receive failed"
            ) as raised:
                live_capture.capture_passively(
                    object(), Path(directory) / "trace.csv", 2.0, bus_channel=0
                )

        self.assertEqual(raised.exception.stats.frames, 1)
        self.assertEqual(raised.exception.stats.rows_written, 1)

    def test_writer_failure_after_counter_increment_surfaces_partial_stats(self) -> None:
        message = can.Message(
            timestamp=1_700_000_031.0,
            channel="vcan0",
            arbitration_id=0x123,
            is_extended_id=False,
            data=b"\x01",
        )

        class WriterFailedNotifier:
            def __init__(self, buses, listeners) -> None:
                self.listeners = listeners
                self.exception = None

            def __enter__(self):
                try:
                    for listener in self.listeners:
                        listener(message)
                except OSError as exc:
                    self.exception = exc
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                return None

        with (
            tempfile.TemporaryDirectory(prefix="capture_writer_error_") as directory,
            mock.patch.object(live_capture.can, "Notifier", WriterFailedNotifier),
            mock.patch.object(
                live_capture.NormalizedCSVWriter,
                "on_message_received",
                side_effect=OSError("CSV write failed"),
            ),
        ):
            with self.assertRaisesRegex(
                live_capture.CaptureError, "CAN receive thread failed: CSV write failed"
            ) as raised:
                live_capture.capture_passively(
                    object(), Path(directory) / "trace.csv", 0.0, bus_channel=0
                )

        self.assertEqual(raised.exception.stats.frames, 1)
        self.assertEqual(raised.exception.stats.rows_written, 0)

    def test_socketcan_open_is_explicit_and_configuration_free(self) -> None:
        filters = [{"can_id": 0x123, "can_mask": 0x1FFFFFFF}]
        sentinel_bus = object()
        with mock.patch.object(
            live_capture, "SocketcanBusWithOverflow", return_value=sentinel_bus
        ) as bus:
            opened = live_capture.open_socketcan_bus("can0", can_filters=filters)
        self.assertIs(opened, sentinel_bus)
        bus.assert_called_once_with(
            channel="can0",
            fd=True,
            receive_own_messages=False,
            can_filters=filters,
        )
        kwargs = bus.call_args.kwargs
        self.assertNotIn("bitrate", kwargs)
        self.assertNotIn("data_bitrate", kwargs)
        self.assertNotIn("timing", kwargs)
        self.assertNotIn("listen_only", kwargs)

    def test_socketcan_open_failure_is_clear_and_has_no_fallback(self) -> None:
        with mock.patch.object(
            live_capture,
            "SocketcanBusWithOverflow",
            side_effect=can.CanInitializationError("missing interface"),
        ) as bus:
            with self.assertRaisesRegex(RuntimeError, "could not open.*missing0"):
                live_capture.open_socketcan_bus("missing0")
        self.assertEqual(bus.call_count, 1)

    def test_cansub_open_preserves_established_bus_arguments(self) -> None:
        config = {
            "interface": "cansub",
            "channel": "aabbccdd-usb.local@1",
            "nominal": 500_000,
            "data": 2_000_000,
            "sample_point": 80.0,
        }
        timing = object()
        filters = [{"can_id": 0x123, "can_mask": 0x1FFFFFFF}]
        sentinel_bus = object()
        with (
            mock.patch.object(live_capture.common, "make_timing", return_value=timing),
            mock.patch.object(live_capture.can, "Bus", return_value=sentinel_bus) as bus,
        ):
            opened = live_capture.open_cansub_bus(
                config, listen_only=True, can_filters=filters
            )
        self.assertIs(opened, sentinel_bus)
        bus.assert_called_once_with(
            interface="cansub",
            channel="aabbccdd-usb.local@1",
            timing=timing,
            listen_only=True,
            error_frames=True,
            can_filters=filters,
        )

    def test_cli_default_remains_cansub_with_recorded_silent_profile(self) -> None:
        config = {
            "interface": "cansub",
            "channel": "aabbccdd-usb.local@1",
            "nominal": 500_000,
            "data": 1_000_000,
            "sample_point": 80.0,
            "listen_only": True,
            "profile": "vehicle",
        }
        bus_context = mock.MagicMock()
        opened_bus = object()
        bus_context.__enter__.return_value = opened_bus
        stats = live_capture.CaptureStats(frames=2, ids={0x123})
        with tempfile.TemporaryDirectory(prefix="cansub_cli_regression_") as directory:
            output = str(Path(directory) / "trace.csv")
            stdout = io.StringIO()
            with (
                mock.patch.object(capture, "resolve_config", return_value=config),
                mock.patch.object(
                    capture.live_capture, "open_cansub_bus", return_value=bus_context
                ) as opener,
                mock.patch.object(
                    capture.live_capture, "capture_passively", return_value=stats
                ) as passive,
                mock.patch.object(
                    capture.live_capture, "socketcan_receive_buffer"
                ) as receive_buffer,
                mock.patch.object(
                    capture.live_capture, "snapshot_netdevice_rx_counters"
                ) as rx_counters,
                mock.patch("sys.stdout", new=stdout),
                mock.patch("sys.stderr", new=io.StringIO()),
            ):
                result = capture.main(["--duration", "0", "--out", output])

        self.assertEqual(result, 0)
        opener.assert_called_once_with(
            config, listen_only=True, can_filters=None
        )
        passive.assert_called_once_with(opened_bus, Path(output), 0.0)
        receive_buffer.assert_not_called()
        rx_counters.assert_not_called()
        self.assertIn("Captured 2 data frames, 1 unique IDs", stdout.getvalue())

    def test_cansub_cli_reports_shared_capture_failure_without_traceback(self) -> None:
        config = {
            "interface": "cansub",
            "channel": "aabbccdd-usb.local@1",
            "nominal": 500_000,
            "data": 1_000_000,
            "sample_point": 80.0,
            "listen_only": True,
            "profile": "vehicle",
        }
        bus_context = mock.MagicMock()
        bus_context.__enter__.return_value = object()
        stats = live_capture.CaptureStats(frames=1, rows_written=0, ids={0x123})
        failure = live_capture.CaptureError("CAN receive thread failed: boom", stats)
        with tempfile.TemporaryDirectory(prefix="cansub_cli_failure_") as directory:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(capture, "resolve_config", return_value=config),
                mock.patch.object(
                    capture.live_capture,
                    "open_cansub_bus",
                    return_value=bus_context,
                ),
                mock.patch.object(
                    capture.live_capture,
                    "capture_passively",
                    side_effect=failure,
                ),
                mock.patch("sys.stdout", new=stdout),
                mock.patch("sys.stderr", new=stderr),
            ):
                result = capture.main(
                    [
                        "--duration",
                        "1",
                        "--out",
                        str(Path(directory) / "trace.csv"),
                    ]
                )

        self.assertEqual(result, 1)
        self.assertIn("Captured 1 data frames", stdout.getvalue())
        self.assertIn("ERROR: CAN receive thread failed: boom", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_socketcan_cli_normalizes_captured_frames_as_rx(self) -> None:
        bus_context = mock.MagicMock()
        opened_bus = object()
        bus_context.__enter__.return_value = opened_bus
        stats = live_capture.CaptureStats(
            frames=1,
            rows_written=1,
            ids={0x123},
            elapsed_seconds=2.0,
            average_fps=0.5,
            socket_rx_queue_overflow_drops=4,
        )
        before = {name: 10 for name in live_capture.NETDEVICE_RX_COUNTERS}
        after = {name: 10 for name in live_capture.NETDEVICE_RX_COUNTERS}
        with tempfile.TemporaryDirectory(prefix="socketcan_cli_direction_") as directory:
            output = str(Path(directory) / "trace.csv")
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    capture.live_capture,
                    "open_socketcan_bus",
                    return_value=bus_context,
                ) as opener,
                mock.patch.object(
                    capture.live_capture,
                    "capture_passively",
                    return_value=stats,
                ) as passive,
                mock.patch.object(
                    capture.live_capture,
                    "socketcan_receive_buffer",
                    return_value=212_992,
                ),
                mock.patch.object(
                    capture.live_capture,
                    "snapshot_netdevice_rx_counters",
                    side_effect=[before, after],
                ),
                mock.patch("sys.stdout", new=stdout),
                mock.patch("sys.stderr", new=io.StringIO()),
            ):
                result = capture.main(
                    [
                        "--backend", "socketcan",
                        "--channel", "vcan0",
                        "--duration", "0",
                        "--out", output,
                    ]
                )

        self.assertEqual(result, 0)
        opener.assert_called_once_with("vcan0", can_filters=None)
        passive.assert_called_once_with(
            opened_bus,
            Path(output),
            0.0,
            bus_channel=0,
            normalize_as_rx=True,
        )
        self.assertIn("Captured 1 data frames, wrote 1 CSV rows", stdout.getvalue())
        self.assertIn("in 2.000s", stdout.getvalue())
        self.assertIn("average=0.5 data frames/s", stdout.getvalue())
        self.assertNotIn("peak", stdout.getvalue())
        self.assertIn("SocketCAN SO_RCVBUF: 212992 bytes", stdout.getvalue())
        self.assertIn(
            "SocketCAN RX queue overflow drops: 4", stdout.getvalue()
        )
        self.assertIn("rx_packets=0", stdout.getvalue())
        self.assertIn("rx_missed_errors=0", stdout.getvalue())

    def test_socketcan_cli_surfaces_receive_thread_failure(self) -> None:
        bus_context = mock.MagicMock()
        bus_context.__enter__.return_value = object()
        stats = live_capture.CaptureStats(
            frames=2,
            rows_written=1,
            ids={0x123},
            elapsed_seconds=1.0,
            average_fps=2.0,
        )
        failure = live_capture.CaptureError("CAN receive thread failed: boom", stats)
        counters = {name: 0 for name in live_capture.NETDEVICE_RX_COUNTERS}

        with tempfile.TemporaryDirectory(prefix="socketcan_cli_failure_") as directory:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    capture.live_capture,
                    "open_socketcan_bus",
                    return_value=bus_context,
                ),
                mock.patch.object(
                    capture.live_capture,
                    "capture_passively",
                    side_effect=failure,
                ),
                mock.patch.object(
                    capture.live_capture,
                    "socketcan_receive_buffer",
                    return_value=212_992,
                ),
                mock.patch.object(
                    capture.live_capture,
                    "snapshot_netdevice_rx_counters",
                    side_effect=[counters, counters],
                ),
                mock.patch("sys.stdout", new=stdout),
                mock.patch("sys.stderr", new=stderr),
            ):
                result = capture.main(
                    [
                        "--backend",
                        "socketcan",
                        "--channel",
                        "vcan0",
                        "--duration",
                        "1",
                        "--out",
                        str(Path(directory) / "trace.csv"),
                    ]
                )

        self.assertEqual(result, 1)
        self.assertIn("Captured 2 data frames, wrote 1 CSV rows", stdout.getvalue())
        self.assertIn("received/written mismatch", stderr.getvalue())
        self.assertIn("CAN receive thread failed: boom", stderr.getvalue())

    def test_socketcan_rejects_cansub_only_cli_options_before_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="socketcan_cli_reject_") as directory:
            output = str(Path(directory) / "trace.csv")
            stderr = io.StringIO()
            with (
                mock.patch.object(capture.live_capture, "open_socketcan_bus") as opener,
                mock.patch("sys.stderr", new=stderr),
            ):
                result = capture.main(
                    [
                        "--backend", "socketcan",
                        "--channel", "can0",
                        "--duration", "0",
                        "--bitrate", "500000",
                        "--out", output,
                    ]
                )
        self.assertEqual(result, 1)
        self.assertIn("--bitrate cannot be applied", stderr.getvalue())
        opener.assert_not_called()

    def test_production_layer_has_no_transmit_or_interface_configuration_calls(self) -> None:
        source_paths = [SCRIPT_DIR / "live_capture.py", SCRIPT_DIR / "capture.py"]
        trees = [ast.parse(path.read_text(encoding="utf-8")) for path in source_paths]
        forbidden_calls = {
            "send",
            "send_periodic",
            "replay",
            "inject",
        }
        calls = {
            node.func.attr
            for tree in trees
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden_calls.isdisjoint(calls))
        production_source = "\n".join(
            path.read_text(encoding="utf-8") for path in source_paths
        )
        self.assertEqual(production_source.count(".setsockopt("), 1)
        self.assertIn("SO_RXQ_OVFL", production_source)
        for source_path in source_paths:
            self.assertNotIn(
                "ip link set", source_path.read_text(encoding="utf-8").lower()
            )


if __name__ == "__main__":
    unittest.main()
