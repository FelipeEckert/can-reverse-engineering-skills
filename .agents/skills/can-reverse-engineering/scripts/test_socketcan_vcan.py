from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import can


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
import live_capture  # noqa: E402
from trace_validation import validate_normalized_trace  # noqa: E402


class VcanIntegrationTest(unittest.TestCase):
    def test_vcan0_capture_reaches_existing_offline_pipeline(self) -> None:
        try:
            receiver = live_capture.open_socketcan_bus("vcan0")
        except RuntimeError as exc:
            self.skipTest(f"vcan0 is unavailable: {exc}")

        try:
            transmitter = can.Bus(
                interface="socketcan",
                channel="vcan0",
                fd=True,
                receive_own_messages=False,
                ignore_config=True,
            )
        except (can.CanError, OSError) as exc:
            receiver.shutdown()
            self.skipTest(f"vcan0 test transmitter is unavailable: {exc}")

        with tempfile.TemporaryDirectory(prefix="socketcan_vcan_") as directory:
            root = Path(directory)
            trace = root / "trace_vcan.csv"
            captured: list[live_capture.CaptureStats] = []
            failures: list[BaseException] = []

            def receive() -> None:
                try:
                    captured.append(
                        live_capture.capture_passively(
                            receiver,
                            trace,
                            1.5,
                            bus_channel=0,
                            normalize_as_rx=True,
                        )
                    )
                except BaseException as exc:  # surfaced after joining the test thread
                    failures.append(exc)

            thread = threading.Thread(target=receive, daemon=True)
            thread.start()
            time.sleep(0.15)
            send_failure: BaseException | None = None
            try:
                for value in range(4):
                    transmitter.send(
                        can.Message(
                            arbitration_id=0x123,
                            is_extended_id=False,
                            data=bytes((value, value + 1)),
                        )
                    )
                    transmitter.send(
                        can.Message(
                            arbitration_id=0x18FF50E5,
                            is_extended_id=True,
                            data=bytes((0xA0 + value,)),
                        )
                    )
                    transmitter.send(
                        can.Message(
                            arbitration_id=0x456,
                            is_extended_id=False,
                            is_remote_frame=True,
                            dlc=8,
                        )
                    )
                    transmitter.send(
                        can.Message(
                            arbitration_id=0x321,
                            is_extended_id=False,
                            is_fd=True,
                            bitrate_switch=True,
                            data=bytes(range(12)),
                        )
                    )
                    time.sleep(0.04)
            except (can.CanError, OSError) as exc:
                send_failure = exc
            thread.join(timeout=3.0)
            transmitter.shutdown()
            receiver.shutdown()

            if send_failure is not None:
                self.skipTest(f"vcan0 test transmission failed: {send_failure}")
            self.assertFalse(thread.is_alive(), "passive capture did not finish")
            self.assertFalse(failures, failures)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].frames, 16)
            self.assertEqual(captured[0].rows_written, 16)
            self.assertGreater(captured[0].elapsed_seconds, 0.0)
            self.assertGreater(captured[0].average_fps, 0.0)

            validated = validate_normalized_trace(trace)
            self.assertEqual(len(validated), 16)
            self.assertTrue(all(frame.bus_channel == 0 for frame in validated))
            self.assertTrue(all(frame.direction == 0 for frame in validated))
            self.assertEqual(
                {frame.can_id for frame in validated},
                {0x123, 0x321, 0x456, 0x18FF50E5},
            )
            classical = [frame for frame in validated if frame.can_id == 0x123]
            extended = [frame for frame in validated if frame.can_id == 0x18FF50E5]
            remote = [frame for frame in validated if frame.can_id == 0x456]
            can_fd = [frame for frame in validated if frame.can_id == 0x321]
            self.assertEqual(
                [frame.data for frame in classical],
                [bytes((value, value + 1)) for value in range(4)],
            )
            self.assertTrue(all(not frame.ide and not frame.rtr for frame in classical))
            self.assertEqual(
                [frame.data for frame in extended],
                [bytes((0xA0 + value,)) for value in range(4)],
            )
            self.assertTrue(all(frame.ide for frame in extended))
            self.assertTrue(
                all(frame.rtr and frame.dlc == 8 and frame.data_length == 0 for frame in remote)
            )
            self.assertTrue(
                all(
                    frame.edl
                    and frame.brs
                    and frame.dlc == 9
                    and frame.data_length == 12
                    and frame.data == bytes(range(12))
                    for frame in can_fd
                )
            )

            loaded = common.load_trace(str(trace))
            self.assertEqual(len(loaded), 16)
            self.assertEqual(
                set(loaded["id"]), {0x123, 0x321, 0x456, 0x18FF50E5}
            )

            survey_json = root / "survey.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "survey.py"),
                    "--trace", str(trace),
                    "--json", str(survey_json),
                    "--no-plots",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(survey_json.exists())
            self.assertIn("4 unique IDs surveyed", result.stdout)


if __name__ == "__main__":
    unittest.main()
