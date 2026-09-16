from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import can

import socketcan_stress


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBus:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sent: list[tuple[float, bytes]] = []

    def send(self, frame: bytes) -> int:
        self.sent.append((self.clock.now, frame))
        return len(frame)


class FailingBus(FakeBus):
    def __init__(self, clock: FakeClock, failed_calls: set[int]) -> None:
        super().__init__(clock)
        self.failed_calls = failed_calls
        self.calls = 0

    def send(self, frame: bytes) -> int:
        call = self.calls
        self.calls += 1
        if call in self.failed_calls:
            raise OSError("synthetic send failure")
        return super().send(frame)


class FakeReceiverBus:
    channel_info = "fake vcan0"

    def __init__(self, messages: list[can.Message] | None = None) -> None:
        self.messages = list(messages or [])
        self.shutdown_called = False

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self.messages.pop(0) if self.messages else None

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakeSenderSocket:
    def __init__(self) -> None:
        self.close_called = False

    def close(self) -> None:
        self.close_called = True


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.close_called = False

    def send(self, value: object) -> None:
        self.sent.append(value)

    def close(self) -> None:
        self.close_called = True


class SenderTests(unittest.TestCase):
    def test_paced_sender_cycles_ids_and_encodes_global_u64_sequence(self) -> None:
        clock = FakeClock()
        bus = FakeBus(clock)
        scenario = socketcan_stress.StressScenario("unit", 0.5, 100.0)

        stats = socketcan_stress.TestOnlySender(
            bus, clock=clock.monotonic, sleep=clock.sleep
        ).run(scenario)

        self.assertEqual(stats.attempted, 50)
        self.assertEqual(stats.successful, 50)
        self.assertEqual(stats.failures, 0)
        self.assertAlmostEqual(stats.achieved_fps, 100.0)
        self.assertEqual(stats.successful_sequences, tuple(range(50)))
        self.assertEqual(stats.first_sequence, 0)
        self.assertEqual(stats.last_sequence, 49)
        unpacked = [socketcan_stress.CAN_FRAME.unpack(frame) for _, frame in bus.sent]
        self.assertEqual(
            {can_id for can_id, _, _ in unpacked},
            {
                socketcan_stress.BASE_CAN_ID + index
                for index in range(socketcan_stress.CAN_ID_COUNT)
            },
        )
        self.assertEqual(
            [int.from_bytes(payload, "big") for _, _, payload in unpacked],
            list(range(50)),
        )
        self.assertTrue(
            all(dlc == 8 and len(payload) == 8 for _, dlc, payload in unpacked)
        )

    def test_burst_sender_emits_groups_back_to_back_at_target_average(self) -> None:
        clock = FakeClock()
        bus = FakeBus(clock)
        scenario = socketcan_stress.StressScenario(
            "burst-unit", 0.3, 100.0, burst_size=10
        )

        stats = socketcan_stress.TestOnlySender(
            bus, clock=clock.monotonic, sleep=clock.sleep
        ).run(scenario)

        self.assertEqual(stats.attempted, 30)
        self.assertAlmostEqual(stats.achieved_fps, 100.0)
        self.assertEqual([sent_at for sent_at, _ in bus.sent[:10]], [0.0] * 10)
        self.assertEqual([sent_at for sent_at, _ in bus.sent[10:20]], [0.1] * 10)

    def test_sender_tracks_exact_successful_sequences_across_failure(self) -> None:
        clock = FakeClock()
        bus = FailingBus(clock, failed_calls={1})
        scenario = socketcan_stress.StressScenario("unit-failure", 0.03, 100.0)

        stats = socketcan_stress.TestOnlySender(
            bus, clock=clock.monotonic, sleep=clock.sleep
        ).run(scenario)

        self.assertEqual(stats.attempted, 3)
        self.assertEqual(stats.successful, 2)
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.successful_sequences, (0, 2))
        self.assertEqual(stats.first_sequence, 0)
        self.assertEqual(stats.last_sequence, 2)

    def test_late_pacing_caps_back_to_back_catch_up(self) -> None:
        class LateClock(FakeClock):
            def __init__(self) -> None:
                super().__init__()
                self.yields = 0

            def sleep(self, seconds: float) -> None:
                if seconds == 0:
                    self.yields += 1
                    return
                self.now += seconds + 0.75

        clock = LateClock()
        bus = FakeBus(clock)
        scenario = socketcan_stress.StressScenario("late", 2.0, 1_000.0)

        stats = socketcan_stress.TestOnlySender(
            bus, clock=clock.monotonic, sleep=clock.sleep
        ).run(scenario)

        self.assertGreater(clock.yields, 0)
        self.assertGreater(stats.pacing_slots_skipped, 0)
        self.assertGreater(stats.max_pacing_lateness_seconds, 0.0)

    def test_failed_send_is_excluded_from_successful_sequence_identities(self) -> None:
        clock = FakeClock()
        bus = FailingBus(clock, {1})
        scenario = socketcan_stress.StressScenario("send-failure", 0.03, 100.0)

        stats = socketcan_stress.TestOnlySender(
            bus, clock=clock.monotonic, sleep=clock.sleep
        ).run(scenario, start_sequence=10)

        self.assertEqual(stats.attempted, 3)
        self.assertEqual(stats.successful, 2)
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.successful_sequences, (10, 12))


class ReceiverProbeTests(unittest.TestCase):
    @staticmethod
    def _message(sequence: int) -> can.Message:
        return can.Message(
            arbitration_id=(
                socketcan_stress.BASE_CAN_ID
                + sequence % socketcan_stress.CAN_ID_COUNT
            ),
            data=sequence.to_bytes(8, "big"),
            is_extended_id=False,
        )

    def test_readiness_requires_notifier_receive_activity(self) -> None:
        probe = socketcan_stress.ReceiverProbe(FakeReceiverBus())

        self.assertFalse(probe.ready.is_set())
        probe.recv(0.0)
        self.assertTrue(probe.ready.is_set())

    def test_drain_completes_only_after_listener_dispatch_cycle(self) -> None:
        probe = socketcan_stress.ReceiverProbe(FakeReceiverBus([self._message(7)]))

        probe.recv(0.0)
        self.assertFalse(probe.wait_until_dispatched(7, 0.0))
        probe.recv(0.0)
        self.assertTrue(probe.wait_until_dispatched(7, 0.0))

    def test_drain_timeout_is_distinct_from_observation(self) -> None:
        probe = socketcan_stress.ReceiverProbe(FakeReceiverBus([self._message(7)]))

        probe.recv(0.0)

        self.assertFalse(probe.wait_until_dispatched(7, 0.001))


class LifecycleTests(unittest.TestCase):
    @staticmethod
    def _capture_after_receive(
        bus: object, output: Path, duration: float, **kwargs: object
    ) -> socketcan_stress.live_capture.CaptureStats:
        bus.recv(0.0)
        return socketcan_stress.live_capture.CaptureStats()

    @staticmethod
    def _capture_without_receive(
        bus: object, output: Path, duration: float, **kwargs: object
    ) -> socketcan_stress.live_capture.CaptureStats:
        return socketcan_stress.live_capture.CaptureStats()

    def _patch_runtime(
        self,
        receiver: FakeReceiverBus,
        capture: object,
        *,
        sender_stats: socketcan_stress.SenderStats | None = None,
        sender_failure: BaseException | None = None,
    ) -> tuple[object, ...]:
        counters = {
            name: 0 for name in socketcan_stress.live_capture.NETDEVICE_RX_COUNTERS
        }
        return (
            mock.patch.object(
                socketcan_stress.live_capture,
                "open_socketcan_bus",
                return_value=receiver,
            ),
            mock.patch.object(
                socketcan_stress,
                "_run_test_sender",
                return_value=sender_stats or socketcan_stress._empty_sender_stats(),
                side_effect=sender_failure,
            ),
            mock.patch.object(
                socketcan_stress.live_capture,
                "socketcan_receive_buffer",
                return_value=212_992,
            ),
            mock.patch.object(
                socketcan_stress.live_capture,
                "snapshot_netdevice_rx_counters",
                return_value=counters,
            ),
            mock.patch.object(
                socketcan_stress.live_capture, "capture_passively", side_effect=capture
            ),
        )

    def test_readiness_timeout_is_lifecycle_failure_and_cleans_up(self) -> None:
        receiver = FakeReceiverBus()
        patches = self._patch_runtime(receiver, self._capture_without_receive)
        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_ready_timeout_"
        ) as directory:
            with (
                patches[0],
                patches[1] as sender_runner,
                patches[2],
                patches[3],
                patches[4],
            ):
                result = socketcan_stress.run_scenario(
                    socketcan_stress.StressScenario("ready-timeout", 0.01, 100.0),
                    Path(directory),
                    readiness_timeout=0.001,
                    drain_timeout=0.001,
                )

        self.assertTrue(result.lifecycle_failed)
        self.assertFalse(result.receiver_ready)
        self.assertEqual(result.outcome, "HARNESS/LIFECYCLE FAILURE")
        self.assertTrue(
            any("readiness timed out" in item for item in result.lifecycle_failures)
        )
        sender_runner.assert_not_called()
        self.assertTrue(receiver.shutdown_called)

    def test_sender_exception_still_closes_receiver(self) -> None:
        receiver = FakeReceiverBus()
        patches = self._patch_runtime(
            receiver,
            self._capture_after_receive,
            sender_failure=RuntimeError("synthetic sender failure"),
        )
        scenario = socketcan_stress.StressScenario("cleanup", 0.01, 100.0)

        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_cleanup_"
        ) as directory:
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic sender failure"):
                    socketcan_stress.run_scenario(
                        scenario,
                        Path(directory),
                        readiness_timeout=0.1,
                        drain_timeout=0.001,
                    )

        self.assertTrue(receiver.shutdown_called)

    def test_sender_process_closes_socket_when_sender_raises(self) -> None:
        sender = FakeSenderSocket()
        connection = FakeConnection()
        scenario = socketcan_stress.StressScenario("cleanup", 0.01, 100.0)

        with (
            mock.patch.object(
                socketcan_stress, "_open_test_sender", return_value=sender
            ),
            mock.patch.object(
                socketcan_stress.TestOnlySender,
                "run",
                side_effect=RuntimeError("synthetic sender failure"),
            ),
        ):
            socketcan_stress._sender_process_main(scenario, connection)

        self.assertTrue(sender.close_called)
        self.assertTrue(connection.close_called)
        stats, failure = connection.sent[0]
        self.assertIsNone(stats)
        self.assertIn("synthetic sender failure", failure)

    def test_drain_timeout_is_reported_as_lifecycle_failure(self) -> None:
        receiver = FakeReceiverBus()
        sender_stats = socketcan_stress.SenderStats(
            attempted=1,
            failures=0,
            elapsed_seconds=0.01,
            achieved_fps=100.0,
            successful_sequences=(7,),
            pacing_slots_skipped=0,
            max_pacing_lateness_seconds=0.0,
        )
        patches = self._patch_runtime(
            receiver, self._capture_after_receive, sender_stats=sender_stats
        )

        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_drain_timeout_"
        ) as directory:
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
            ):
                result = socketcan_stress.run_scenario(
                    socketcan_stress.StressScenario("drain-timeout", 0.01, 100.0),
                    Path(directory),
                    readiness_timeout=0.1,
                    drain_timeout=0.001,
                )

        self.assertTrue(result.receiver_ready)
        self.assertFalse(result.drain_completed)
        self.assertFalse(result.loss_confirmation_available)
        self.assertEqual(result.outcome, "HARNESS/LIFECYCLE FAILURE")
        self.assertTrue(
            any("drain timed out" in item for item in result.lifecycle_failures)
        )
        self.assertTrue(receiver.shutdown_called)


class SequenceValidationTests(unittest.TestCase):
    def _write_frames(
        self, path: Path, frames: list[tuple[int, bytes]]
    ) -> None:
        writer = socketcan_stress.live_capture.NormalizedCSVWriter(
            path, bus_channel=0, normalize_as_rx=True
        )
        for index, (can_id, payload) in enumerate(frames):
            writer.on_message_received(
                can.Message(
                    timestamp=1_700_000_000.0 + index / 1_000,
                    arbitration_id=can_id,
                    is_extended_id=False,
                    data=payload,
                )
            )
        writer.stop()

    def _write_sequences(self, path: Path, sequences: list[int]) -> None:
        self._write_frames(
            path,
            [
                (
                    socketcan_stress.BASE_CAN_ID
                    + sequence % socketcan_stress.CAN_ID_COUNT,
                    sequence.to_bytes(8, "big"),
                )
                for sequence in sequences
            ],
        )

    @staticmethod
    def _sender(
        successful_sequences: tuple[int, ...], attempted: int | None = None
    ) -> socketcan_stress.SenderStats:
        attempts = attempted if attempted is not None else len(successful_sequences)
        return socketcan_stress.SenderStats(
            attempted=attempts,
            failures=attempts - len(successful_sequences),
            elapsed_seconds=1.0,
            achieved_fps=float(len(successful_sequences)),
            successful_sequences=successful_sequences,
            pacing_slots_skipped=0,
            max_pacing_lateness_seconds=0.0,
        )

    def test_clean_sequence_and_counts_pass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="socketcan_stress_valid_") as directory:
            trace = Path(directory) / "trace.csv"
            self._write_sequences(trace, [10, 11, 12, 13, 14])
            validation = socketcan_stress.analyze_trace(
                trace, self._sender((10, 11, 12, 13, 14)), captured_data_frames=5
            )

        self.assertTrue(validation.ok)
        self.assertEqual(validation.csv_rows, 5)
        self.assertEqual(validation.missing_sequences, 0)

    def test_loss_duplicate_and_reordering_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_invalid_"
        ) as directory:
            trace = Path(directory) / "trace.csv"
            self._write_sequences(trace, [10, 11, 13, 13, 12, 15])
            validation = socketcan_stress.analyze_trace(
                trace,
                self._sender((10, 11, 12, 13, 14, 15)),
                captured_data_frames=5,
            )

        self.assertFalse(validation.ok)
        self.assertEqual(validation.missing_sequences, 1)
        self.assertEqual(validation.missing_samples, (14,))
        self.assertEqual(validation.duplicate_sequences, 1)
        self.assertEqual(validation.out_of_order_sequences, 1)
        self.assertTrue(validation.sender_receiver_count_agreement)
        self.assertFalse(validation.capture_csv_count_agreement)

    def test_failed_send_is_not_reported_as_receiver_loss(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_send_failure_"
        ) as directory:
            trace = Path(directory) / "trace.csv"
            self._write_sequences(trace, [10, 12])
            validation = socketcan_stress.analyze_trace(
                trace,
                self._sender((10, 12), attempted=3),
                captured_data_frames=2,
            )

        self.assertEqual(validation.missing_sequences, 0)
        self.assertEqual(validation.unexpected_sequences, 0)
        self.assertTrue(validation.sender_receiver_count_agreement)

    def test_can_id_sequence_mismatch_fails_explicitly(self) -> None:
        sequence = 10
        expected_can_id = (
            socketcan_stress.BASE_CAN_ID
            + sequence % socketcan_stress.CAN_ID_COUNT
        )
        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_id_mismatch_"
        ) as directory:
            trace = Path(directory) / "trace.csv"
            self._write_frames(
                trace,
                [(expected_can_id + 1, sequence.to_bytes(8, "big"))],
            )
            validation = socketcan_stress.analyze_trace(
                trace, self._sender((sequence,)), captured_data_frames=1
            )

        self.assertFalse(validation.ok)
        self.assertEqual(validation.can_id_sequence_mismatches, 1)
        self.assertEqual(
            validation.can_id_mismatch_samples,
            ((sequence, expected_can_id + 1, expected_can_id),),
        )

    def test_malformed_and_unexpected_payloads_fail_explicitly(self) -> None:
        expected_can_id = socketcan_stress.BASE_CAN_ID + 10
        unexpected_sequence = 99
        with tempfile.TemporaryDirectory(
            prefix="socketcan_stress_payloads_"
        ) as directory:
            trace = Path(directory) / "trace.csv"
            self._write_frames(
                trace,
                [
                    (expected_can_id, b"1234567"),
                    (
                        socketcan_stress.BASE_CAN_ID
                        + unexpected_sequence % socketcan_stress.CAN_ID_COUNT,
                        unexpected_sequence.to_bytes(8, "big"),
                    ),
                ],
            )
            validation = socketcan_stress.analyze_trace(
                trace, self._sender((10,)), captured_data_frames=2
            )

        self.assertFalse(validation.ok)
        self.assertEqual(validation.invalid_payloads, 1)
        self.assertEqual(validation.unexpected_sequences, 1)
        self.assertEqual(validation.missing_sequences, 1)

    def test_required_development_and_endurance_scenarios_are_defined(self) -> None:
        self.assertEqual(socketcan_stress.SCENARIOS["sustained-2k"].target_fps, 2_000)
        self.assertEqual(socketcan_stress.SCENARIOS["sustained-5k"].target_fps, 5_000)
        self.assertTrue(
            socketcan_stress.SCENARIOS["sustained-5k"].overload_characterization
        )
        self.assertGreater(socketcan_stress.SCENARIOS["burst-2k"].burst_size, 1)
        self.assertIsNone(socketcan_stress.SCENARIOS["max-rate"].target_fps)
        self.assertTrue(
            socketcan_stress.SCENARIOS["max-rate"].overload_characterization
        )
        endurance = socketcan_stress.SCENARIOS["endurance-2k-10m"]
        self.assertEqual(endurance.duration_seconds, 600.0)
        self.assertEqual(endurance.target_fps, 2_000)


class OutcomeTests(unittest.TestCase):
    @staticmethod
    def _loss_result(overload_characterization: bool) -> socketcan_stress.StressResult:
        sender = socketcan_stress.SenderStats(
            attempted=2,
            failures=0,
            elapsed_seconds=1.0,
            achieved_fps=2.0,
            successful_sequences=(0, 1),
            pacing_slots_skipped=0,
            max_pacing_lateness_seconds=0.0,
        )
        capture_stats = socketcan_stress.live_capture.CaptureStats(
            frames=1,
            rows_written=1,
            elapsed_seconds=1.0,
            average_fps=1.0,
            socket_rx_queue_overflow_drops=1,
        )
        validation = socketcan_stress.SequenceValidation(
            csv_rows=1,
            missing_sequences=1,
            duplicate_sequences=0,
            out_of_order_sequences=0,
            invalid_payloads=0,
            unexpected_sequences=0,
            can_id_sequence_mismatches=0,
            sender_receiver_count_agreement=False,
            capture_csv_count_agreement=True,
            first_received_sequence=0,
            last_received_sequence=0,
            missing_samples=(1,),
            can_id_mismatch_samples=(),
        )
        return socketcan_stress.StressResult(
            scenario=socketcan_stress.StressScenario(
                "loss",
                1.0,
                2.0,
                overload_characterization=overload_characterization,
            ),
            sender=sender,
            capture_stats=capture_stats,
            validation=validation,
            socket_receive_buffer=212_992,
            netdevice_rx_deltas={},
            capture_exception=None,
            receiver_ready=True,
            drain_completed=True,
            drain_target_sequence=1,
            drain_wait_seconds=0.1,
            lifecycle_failures=(),
            trace=Path("trace.csv"),
        )

    def test_expected_overload_loss_is_characterization_not_harness_failure(
        self,
    ) -> None:
        result = self._loss_result(overload_characterization=True)

        self.assertFalse(result.lifecycle_failed)
        self.assertTrue(result.confirmed_capture_loss)
        self.assertTrue(result.overflow_matches_missing)
        self.assertEqual(result.outcome, "EXPECTED OVERLOAD CHARACTERIZATION LOSS")
        self.assertEqual(result.exit_code, 0)

    def test_unexpected_loss_remains_a_failing_result(self) -> None:
        result = self._loss_result(overload_characterization=False)

        self.assertEqual(result.outcome, "CONFIRMED CAPTURE LOSS")
        self.assertEqual(result.exit_code, 1)

    def test_overflow_mismatch_is_a_capture_validation_failure(self) -> None:
        result = self._loss_result(overload_characterization=True)
        assert result.capture_stats is not None
        result.capture_stats.socket_rx_queue_overflow_drops = 0

        self.assertFalse(result.overflow_matches_missing)
        self.assertEqual(result.outcome, "CAPTURE VALIDATION FAILURE")
        self.assertEqual(result.exit_code, 1)

    def test_unavailable_overflow_metric_is_a_capture_validation_failure(self) -> None:
        result = self._loss_result(overload_characterization=True)
        assert result.capture_stats is not None
        result.capture_stats.socket_rx_queue_overflow_drops = None

        self.assertIsNone(result.overflow_matches_missing)
        self.assertEqual(result.outcome, "CAPTURE VALIDATION FAILURE")
        self.assertEqual(result.exit_code, 1)

    def test_report_labels_overflow_as_observed_through_last_packet(self) -> None:
        stdout = io.StringIO()

        with mock.patch("sys.stdout", new=stdout):
            socketcan_stress.print_result(
                self._loss_result(overload_characterization=True)
            )

        self.assertIn(
            "socket_rx_queue_overflow_drops=1 "
            "(observed SO_RXQ_OVFL delta through last delivered packet)",
            stdout.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
