from __future__ import annotations

import ast
import importlib
import io
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
    def test_socketcan_open_is_explicit_and_configuration_free(self) -> None:
        filters = [{"can_id": 0x123, "can_mask": 0x1FFFFFFF}]
        sentinel_bus = object()
        with mock.patch.object(live_capture.can, "Bus", return_value=sentinel_bus) as bus:
            opened = live_capture.open_socketcan_bus("can0", can_filters=filters)
        self.assertIs(opened, sentinel_bus)
        bus.assert_called_once_with(
            interface="socketcan",
            channel="can0",
            fd=True,
            receive_own_messages=False,
            ignore_config=True,
            can_filters=filters,
        )
        kwargs = bus.call_args.kwargs
        self.assertNotIn("bitrate", kwargs)
        self.assertNotIn("data_bitrate", kwargs)
        self.assertNotIn("timing", kwargs)
        self.assertNotIn("listen_only", kwargs)

    def test_socketcan_open_failure_is_clear_and_has_no_fallback(self) -> None:
        with mock.patch.object(
            live_capture.can,
            "Bus",
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
            with (
                mock.patch.object(capture, "resolve_config", return_value=config),
                mock.patch.object(
                    capture.live_capture, "open_cansub_bus", return_value=bus_context
                ) as opener,
                mock.patch.object(
                    capture.live_capture, "capture_passively", return_value=stats
                ) as passive,
                mock.patch("sys.stdout", new=io.StringIO()),
                mock.patch("sys.stderr", new=io.StringIO()),
            ):
                result = capture.main(["--duration", "0", "--out", output])

        self.assertEqual(result, 0)
        opener.assert_called_once_with(
            config, listen_only=True, can_filters=None
        )
        passive.assert_called_once_with(opened_bus, Path(output), 0.0)

    def test_socketcan_cli_normalizes_captured_frames_as_rx(self) -> None:
        bus_context = mock.MagicMock()
        opened_bus = object()
        bus_context.__enter__.return_value = opened_bus
        stats = live_capture.CaptureStats(frames=1, ids={0x123})
        with tempfile.TemporaryDirectory(prefix="socketcan_cli_direction_") as directory:
            output = str(Path(directory) / "trace.csv")
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
                mock.patch("sys.stdout", new=io.StringIO()),
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
        source_path = SCRIPT_DIR / "live_capture.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden_calls = {"send", "send_periodic", "replay", "inject"}
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden_calls.isdisjoint(calls))
        self.assertNotIn("ip link set", source_path.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
