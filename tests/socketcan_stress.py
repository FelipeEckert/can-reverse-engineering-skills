#!/usr/bin/env python3
"""Test-only SocketCAN stress harness for the externally prepared ``vcan0``.

This module is deliberately outside the production skill scripts.  Its sender
is hard-coded to vcan0 and opens a separate SocketCAN socket; production capture
remains receive-only and is exercised through ``live_capture.capture_passively``.
The harness never creates, configures, or tunes the interface.

Development suite (about 40 seconds):

    .venv/bin/python -B tests/socketcan_stress.py --scenario dev

Recommended optional endurance validation (10 minutes at about 2,000 fps):

    .venv/bin/python -B tests/socketcan_stress.py --scenario endurance-2k-10m
"""
from __future__ import annotations

import argparse
import math
import multiprocessing
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / ".agents/skills/can-reverse-engineering/scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import live_capture  # noqa: E402
from trace_validation import validate_normalized_trace  # noqa: E402


VCAN_CHANNEL = "vcan0"
BASE_CAN_ID = 0x600
CAN_ID_COUNT = 50
UINT64_MAX = (1 << 64) - 1
CAN_FRAME = struct.Struct("=IB3x8s")
RECEIVER_READY_TIMEOUT_SECONDS = 1.0
RECEIVER_DRAIN_TIMEOUT_SECONDS = 2.0
CAPTURE_SHUTDOWN_GUARD_SECONDS = 0.25
CAPTURE_JOIN_GRACE_SECONDS = 3.0
SENDER_PROCESS_GRACE_SECONDS = 5.0
SENDER_PROCESS_STARTUP_ALLOWANCE_SECONDS = 3.0
MAX_SUSTAINED_CATCH_UP_FRAMES = 16
MAX_SUSTAINED_BACKLOG_SECONDS = 0.5


@dataclass(frozen=True)
class StressScenario:
    name: str
    duration_seconds: float
    target_fps: float | None
    burst_size: int = 1
    overload_characterization: bool = False

    @property
    def pattern(self) -> str:
        if self.target_fps is None:
            return "unpaced/max-rate"
        if self.burst_size > 1:
            interval = self.burst_size / self.target_fps
            return f"{self.burst_size}-frame bursts every {interval:.3f}s"
        return "paced sustained"


SCENARIOS = {
    "sustained-2k": StressScenario("sustained-2k", 1.5, 2_000.0),
    "sustained-5k": StressScenario(
        "sustained-5k", 1.5, 5_000.0, overload_characterization=True
    ),
    "burst-2k": StressScenario("burst-2k", 1.5, 2_000.0, burst_size=200),
    "max-rate": StressScenario(
        "max-rate", 0.5, None, overload_characterization=True
    ),
    "endurance-2k-10m": StressScenario(
        "endurance-2k-10m", 10 * 60.0, 2_000.0
    ),
}
DEV_SCENARIOS = (
    "sustained-2k",
    "sustained-5k",
    "burst-2k",
    "max-rate",
)


@dataclass
class SenderStats:
    attempted: int
    failures: int
    elapsed_seconds: float
    achieved_fps: float
    successful_sequences: tuple[int, ...]
    pacing_slots_skipped: int
    max_pacing_lateness_seconds: float

    @property
    def successful(self) -> int:
        return len(self.successful_sequences)

    @property
    def first_sequence(self) -> int | None:
        return self.successful_sequences[0] if self.successful_sequences else None

    @property
    def last_sequence(self) -> int | None:
        return self.successful_sequences[-1] if self.successful_sequences else None


@dataclass
class SequenceValidation:
    csv_rows: int
    missing_sequences: int
    duplicate_sequences: int
    out_of_order_sequences: int
    invalid_payloads: int
    unexpected_sequences: int
    can_id_sequence_mismatches: int
    sender_receiver_count_agreement: bool
    capture_csv_count_agreement: bool
    first_received_sequence: int | None
    last_received_sequence: int | None
    missing_samples: tuple[int, ...]
    can_id_mismatch_samples: tuple[tuple[int, int, int], ...]

    @property
    def ok(self) -> bool:
        return (
            self.missing_sequences == 0
            and self.duplicate_sequences == 0
            and self.out_of_order_sequences == 0
            and self.invalid_payloads == 0
            and self.unexpected_sequences == 0
            and self.can_id_sequence_mismatches == 0
            and self.sender_receiver_count_agreement
            and self.capture_csv_count_agreement
        )


@dataclass
class StressResult:
    scenario: StressScenario
    sender: SenderStats
    capture_stats: live_capture.CaptureStats | None
    validation: SequenceValidation | None
    socket_receive_buffer: int | None
    netdevice_rx_deltas: dict[str, int | None]
    capture_exception: str | None
    receiver_ready: bool
    drain_completed: bool
    drain_target_sequence: int | None
    drain_wait_seconds: float
    lifecycle_failures: tuple[str, ...]
    trace: Path

    @property
    def lifecycle_failed(self) -> bool:
        return (
            bool(self.lifecycle_failures)
            or self.capture_exception is not None
            or self.capture_stats is None
        )

    @property
    def loss_confirmation_available(self) -> bool:
        return (
            not self.lifecycle_failed
            and self.receiver_ready
            and self.drain_completed
            and self.validation is not None
        )

    @property
    def confirmed_capture_loss(self) -> bool:
        return bool(
            self.loss_confirmation_available
            and self.validation is not None
            and self.validation.missing_sequences
        )

    @property
    def overflow_matches_missing(self) -> bool | None:
        if not self.loss_confirmation_available:
            return None
        assert self.capture_stats is not None
        assert self.validation is not None
        overflow = self.capture_stats.socket_rx_queue_overflow_drops
        if overflow is None:
            return None
        return overflow == self.validation.missing_sequences

    @property
    def outcome(self) -> str:
        if self.lifecycle_failed:
            return "HARNESS/LIFECYCLE FAILURE"
        assert self.capture_stats is not None
        assert self.validation is not None
        validation = self.validation
        non_loss_failure = (
            validation.duplicate_sequences
            or validation.out_of_order_sequences
            or validation.invalid_payloads
            or validation.unexpected_sequences
            or validation.can_id_sequence_mismatches
            or not validation.capture_csv_count_agreement
            or (
                not validation.sender_receiver_count_agreement
                and not validation.missing_sequences
            )
            or self.capture_stats.errors
            or self.overflow_matches_missing is not True
        )
        if non_loss_failure:
            return "CAPTURE VALIDATION FAILURE"
        if validation.missing_sequences:
            if self.scenario.overload_characterization:
                return "EXPECTED OVERLOAD CHARACTERIZATION LOSS"
            return "CONFIRMED CAPTURE LOSS"
        if self.sender.failures:
            if self.scenario.overload_characterization:
                return "EXPECTED SENDER-LIMIT CHARACTERIZATION"
            return "SENDER FAILURE"
        return "PASS"

    @property
    def exit_code(self) -> int:
        if self.lifecycle_failed:
            return 2
        if self.outcome.startswith("EXPECTED ") or self.outcome == "PASS":
            return 0
        return 1


class SenderSocket(Protocol):
    def send(self, data: bytes) -> int: ...


class ReceiverProbe:
    """Observe notifier lifecycle without altering production capture behavior.

    A receive call proves the notifier thread is active.  When the notifier calls
    ``recv`` again, its synchronous listeners have finished handling the previous
    message, so that message's sequence is safe to acknowledge as dispatched.
    """

    def __init__(self, bus: object) -> None:
        self._bus = bus
        self.ready = threading.Event()
        self._pending_sequence: int | None = None
        self._last_dispatched_sequence: int | None = None
        self._drain_target_sequence: int | None = None
        self._drain_event = threading.Event()

    def __getattr__(self, name: str) -> object:
        return getattr(self._bus, name)

    @staticmethod
    def _sequence_from_message(message: object) -> int | None:
        if getattr(message, "is_error_frame", False):
            return None
        data = bytes(getattr(message, "data", b""))
        if len(data) != 8:
            return None
        return int.from_bytes(data, byteorder="big", signed=False)

    def recv(self, timeout: float | None = None) -> object:
        if self._pending_sequence is not None:
            self._last_dispatched_sequence = self._pending_sequence
            if self._pending_sequence == self._drain_target_sequence:
                self._drain_event.set()
            self._pending_sequence = None
        if not self.ready.is_set():
            self.ready.set()

        message = self._bus.recv(timeout)
        if message is not None:
            sequence = self._sequence_from_message(message)
            if sequence is not None:
                self._pending_sequence = sequence
        return message

    def wait_until_dispatched(self, sequence: int, timeout: float) -> bool:
        self._drain_event.clear()
        self._drain_target_sequence = sequence
        if self._last_dispatched_sequence == sequence:
            return True
        return self._drain_event.wait(timeout=max(0.0, timeout))


class TestOnlySender:
    """Emit sequenced classical CAN frames on a caller-owned test socket."""

    def __init__(
        self,
        bus: SenderSocket,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.sleep = sleep

    def run(self, scenario: StressScenario, *, start_sequence: int = 0) -> SenderStats:
        if scenario.duration_seconds <= 0:
            raise ValueError("duration must be positive")
        if scenario.target_fps is not None and scenario.target_fps <= 0:
            raise ValueError("target frame rate must be positive or omitted")
        if scenario.burst_size <= 0:
            raise ValueError("burst size must be positive")
        if not 0 <= start_sequence <= UINT64_MAX:
            raise ValueError("start sequence must fit unsigned 64 bits")

        attempted = failures = 0
        successful_sequences: list[int] = []
        pacing_slots_skipped = 0
        max_pacing_lateness = 0.0
        started = self.clock()
        deadline = started + scenario.duration_seconds
        next_burst = started
        burst_interval = (
            scenario.burst_size / scenario.target_fps
            if scenario.target_fps is not None
            else None
        )
        catch_up_frames = 0

        while self.clock() < deadline:
            if scenario.target_fps is not None:
                delay = next_burst - self.clock()
                if delay > 0:
                    self.sleep(delay)
                    if self.clock() >= deadline:
                        break
                now = self.clock()
                lateness = max(0.0, now - next_burst)
                max_pacing_lateness = max(max_pacing_lateness, lateness)
                assert burst_interval is not None
                if lateness >= burst_interval:
                    elapsed_slots = max(1, int(lateness / burst_interval))
                    backlog_limit = (
                        max(1, int(MAX_SUSTAINED_BACKLOG_SECONDS / burst_interval))
                        if scenario.burst_size == 1
                        else 0
                    )
                    skipped_slots = max(0, elapsed_slots - backlog_limit)
                    pacing_slots_skipped += skipped_slots
                    next_burst += skipped_slots * burst_interval

            frames_this_burst = (
                scenario.burst_size if scenario.target_fps is not None else 1
            )
            for _ in range(frames_this_burst):
                if self.clock() >= deadline:
                    break
                sequence = start_sequence + attempted
                if sequence > UINT64_MAX:
                    raise ValueError("sequence exceeded unsigned 64-bit range")
                attempted += 1
                frame = CAN_FRAME.pack(
                    BASE_CAN_ID + (sequence % CAN_ID_COUNT),
                    8,
                    sequence.to_bytes(8, byteorder="big", signed=False),
                )
                try:
                    bytes_sent = self.bus.send(frame)
                except OSError:
                    failures += 1
                else:
                    if bytes_sent == len(frame):
                        successful_sequences.append(sequence)
                    else:
                        failures += 1

            if scenario.target_fps is not None:
                assert burst_interval is not None
                next_burst += burst_interval
                if scenario.burst_size == 1 and next_burst <= self.clock():
                    catch_up_frames += 1
                    if catch_up_frames >= MAX_SUSTAINED_CATCH_UP_FRAMES:
                        # Bound uninterrupted catch-up and let the receiver run.
                        self.sleep(0)
                        catch_up_frames = 0
                else:
                    catch_up_frames = 0

        elapsed = max(0.0, self.clock() - started)
        if scenario.target_fps is not None:
            scheduled_frames = math.ceil(
                scenario.duration_seconds * scenario.target_fps
            )
            pacing_slots_skipped = max(
                pacing_slots_skipped, scheduled_frames - attempted
            )
        successful = len(successful_sequences)
        return SenderStats(
            attempted=attempted,
            failures=failures,
            elapsed_seconds=elapsed,
            achieved_fps=successful / elapsed if elapsed > 0 else 0.0,
            successful_sequences=tuple(successful_sequences),
            pacing_slots_skipped=pacing_slots_skipped,
            max_pacing_lateness_seconds=max_pacing_lateness,
        )


def analyze_trace(
    trace: Path,
    sender: SenderStats,
    captured_data_frames: int,
) -> SequenceValidation:
    frames = validate_normalized_trace(trace)
    sequences: list[int] = []
    invalid_payloads = 0
    can_id_mismatches = 0
    can_id_mismatch_samples: list[tuple[int, int, int]] = []
    for frame in frames:
        if len(frame.data) != 8:
            invalid_payloads += 1
            continue
        sequence = int.from_bytes(frame.data, byteorder="big", signed=False)
        sequences.append(sequence)
        expected_can_id = BASE_CAN_ID + (sequence % CAN_ID_COUNT)
        if frame.can_id != expected_can_id:
            can_id_mismatches += 1
            if len(can_id_mismatch_samples) < 10:
                can_id_mismatch_samples.append(
                    (sequence, frame.can_id, expected_can_id)
                )

    seen = set(sequences)
    duplicates = len(sequences) - len(seen)
    out_of_order = sum(
        current < previous for previous, current in zip(sequences, sequences[1:])
    )

    successful = set(sender.successful_sequences)
    missing_sequences = successful - seen
    unexpected = sum(sequence not in successful for sequence in sequences)
    missing_samples = tuple(
        sequence
        for sequence in sender.successful_sequences
        if sequence in missing_sequences
    )[:10]

    return SequenceValidation(
        csv_rows=len(frames),
        missing_sequences=len(missing_sequences),
        duplicate_sequences=duplicates,
        out_of_order_sequences=out_of_order,
        invalid_payloads=invalid_payloads,
        unexpected_sequences=unexpected,
        can_id_sequence_mismatches=can_id_mismatches,
        sender_receiver_count_agreement=sender.successful == len(frames),
        capture_csv_count_agreement=captured_data_frames == len(frames),
        first_received_sequence=sequences[0] if sequences else None,
        last_received_sequence=sequences[-1] if sequences else None,
        missing_samples=missing_samples,
        can_id_mismatch_samples=tuple(can_id_mismatch_samples),
    )


def _open_test_sender() -> socket.socket:
    """Open a dedicated sender socket on vcan0 without changing interface state."""

    sender = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sender.bind((VCAN_CHANNEL,))
        sender.setblocking(False)
    except BaseException:
        sender.close()
        raise
    return sender


def _sender_process_main(scenario: StressScenario, connection: object) -> None:
    """Run the sender in an isolated test process and return serializable stats."""

    sender: socket.socket | None = None
    stats: SenderStats | None = None
    failure: str | None = None
    try:
        sender = _open_test_sender()
        stats = TestOnlySender(sender).run(scenario)
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if sender is not None:
            sender.close()

    try:
        connection.send((stats, failure))
    finally:
        connection.close()


def _run_test_sender(scenario: StressScenario) -> SenderStats:
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_sender_process_main,
        args=(scenario, send_connection),
        name=f"socketcan-stress-sender-{scenario.name}",
    )
    process.start()
    send_connection.close()
    timeout = scenario.duration_seconds + SENDER_PROCESS_GRACE_SECONDS
    stats: SenderStats | None = None
    failure: str | None = None
    timed_out = False
    forced_termination = False
    try:
        if not receive_connection.poll(timeout):
            timed_out = True
        else:
            stats, failure = receive_connection.recv()
    finally:
        receive_connection.close()
        process.join(timeout=SENDER_PROCESS_GRACE_SECONDS)
        if process.is_alive():
            forced_termination = True
            process.terminate()
            process.join(timeout=SENDER_PROCESS_GRACE_SECONDS)

    if timed_out:
        raise RuntimeError(f"test sender did not finish within {timeout:.3f}s")
    if forced_termination:
        raise RuntimeError("test sender process did not exit after reporting stats")
    if failure is not None:
        raise RuntimeError(f"test sender failed: {failure}")
    if process.exitcode != 0 or stats is None:
        raise RuntimeError(
            f"test sender process exited with status {process.exitcode}"
        )
    return stats


def _empty_sender_stats() -> SenderStats:
    return SenderStats(
        attempted=0,
        failures=0,
        elapsed_seconds=0.0,
        achieved_fps=0.0,
        successful_sequences=(),
        pacing_slots_skipped=0,
        max_pacing_lateness_seconds=0.0,
    )


def run_scenario(
    scenario: StressScenario,
    output_dir: Path,
    *,
    readiness_timeout: float = RECEIVER_READY_TIMEOUT_SECONDS,
    drain_timeout: float = RECEIVER_DRAIN_TIMEOUT_SECONDS,
) -> StressResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    trace = output_dir / f"trace_{scenario.name}.csv"
    filters = [
        {"can_id": BASE_CAN_ID + index, "can_mask": 0x7FF, "extended": False}
        for index in range(CAN_ID_COUNT)
    ]
    receiver = live_capture.open_socketcan_bus(VCAN_CHANNEL, can_filters=filters)
    thread: threading.Thread | None = None
    try:
        socket_receive_buffer = live_capture.socketcan_receive_buffer(receiver)
        rx_before = live_capture.snapshot_netdevice_rx_counters(VCAN_CHANNEL)
        observer = ReceiverProbe(receiver)
        capture_duration = (
            readiness_timeout
            + SENDER_PROCESS_STARTUP_ALLOWANCE_SECONDS
            + scenario.duration_seconds
            + drain_timeout
            + CAPTURE_SHUTDOWN_GUARD_SECONDS
        )
        captured: list[live_capture.CaptureStats] = []
        capture_failures: list[BaseException] = []
        lifecycle_failures: list[str] = []

        def receive() -> None:
            try:
                captured.append(
                    live_capture.capture_passively(
                        observer,
                        trace,
                        capture_duration,
                        bus_channel=0,
                        normalize_as_rx=True,
                    )
                )
            except live_capture.CaptureError as exc:
                captured.append(exc.stats)
                capture_failures.append(exc)
            except BaseException as exc:
                capture_failures.append(exc)

        thread = threading.Thread(
            target=receive,
            name=f"socketcan-stress-receiver-{scenario.name}",
            daemon=True,
        )
        thread.start()
        receiver_ready = observer.ready.wait(timeout=max(0.0, readiness_timeout))
        if receiver_ready:
            sender_stats = _run_test_sender(scenario)
        else:
            sender_stats = _empty_sender_stats()
            lifecycle_failures.append(
                f"receiver readiness timed out after {readiness_timeout:.3f}s"
            )

        drain_target = sender_stats.last_sequence
        drain_started = time.monotonic()
        if not receiver_ready:
            drain_completed = False
        elif drain_target is None:
            drain_completed = True
        else:
            drain_completed = observer.wait_until_dispatched(
                drain_target, drain_timeout
            )
        drain_wait_seconds = max(0.0, time.monotonic() - drain_started)
        if receiver_ready and not drain_completed:
            lifecycle_failures.append(
                "receiver drain timed out before final successful sequence "
                f"{drain_target} was dispatched ({drain_timeout:.3f}s timeout)"
            )

        thread.join(timeout=capture_duration + CAPTURE_JOIN_GRACE_SECONDS)
        if thread.is_alive():
            lifecycle_failures.append(
                "production capture did not stop within the harness timeout"
            )
        rx_after = live_capture.snapshot_netdevice_rx_counters(VCAN_CHANNEL)
    finally:
        receiver.shutdown()
        if thread is not None and thread.is_alive():
            thread.join(timeout=CAPTURE_JOIN_GRACE_SECONDS)

    stats = captured[0] if captured else None
    validation = None
    if stats is not None and trace.exists():
        validation = analyze_trace(trace, sender_stats, stats.frames)
    return StressResult(
        scenario=scenario,
        sender=sender_stats,
        capture_stats=stats,
        validation=validation,
        socket_receive_buffer=socket_receive_buffer,
        netdevice_rx_deltas=live_capture.netdevice_rx_counter_deltas(
            rx_before, rx_after
        ),
        capture_exception=(
            f"{type(capture_failures[0]).__name__}: {capture_failures[0]}"
            if capture_failures
            else None
        ),
        receiver_ready=receiver_ready,
        drain_completed=drain_completed,
        drain_target_sequence=drain_target,
        drain_wait_seconds=drain_wait_seconds,
        lifecycle_failures=tuple(lifecycle_failures),
        trace=trace,
    )


def print_result(result: StressResult) -> None:
    sender = result.sender
    print(f"\n== {result.scenario.name}: {result.scenario.pattern} ==")
    print(
        "sender: "
        f"attempted={sender.attempted}, successful={sender.successful}, "
        f"failures={sender.failures}, achieved={sender.achieved_fps:.1f} frames/s, "
        f"successful_sequence={sender.first_sequence}..{sender.last_sequence}, "
        f"pacing_slots_skipped={sender.pacing_slots_skipped}, "
        f"max_pacing_lateness={sender.max_pacing_lateness_seconds * 1_000:.3f}ms"
    )
    stats = result.capture_stats
    if stats is None:
        print("receiver: no capture statistics")
    else:
        overflow = stats.socket_rx_queue_overflow_drops
        print(
            "receiver: "
            f"data_frames={stats.frames}, writer_rows={stats.rows_written}, "
            f"error_frames={stats.errors}, elapsed={stats.elapsed_seconds:.3f}s, "
            f"average={stats.average_fps:.1f} data frames/s, "
            f"socket_rx_queue_overflow_drops="
            f"{overflow if overflow is not None else 'unavailable'} "
            "(observed SO_RXQ_OVFL delta through last delivered packet)"
        )
    validation = result.validation
    if validation is None:
        print("sequence: unavailable")
    else:
        missing_label = (
            "confirmed_missing"
            if result.loss_confirmation_available
            else "unconfirmed_missing"
        )
        print(
            "sequence: "
            f"csv_rows={validation.csv_rows}, "
            f"{missing_label}={validation.missing_sequences}, "
            f"duplicates={validation.duplicate_sequences}, "
            f"out_of_order={validation.out_of_order_sequences}, "
            f"invalid_payloads={validation.invalid_payloads}, "
            f"unexpected={validation.unexpected_sequences}, "
            f"can_id_sequence_mismatches="
            f"{validation.can_id_sequence_mismatches}, "
            f"sender_receiver_agree={validation.sender_receiver_count_agreement}, "
            f"capture_csv_agree={validation.capture_csv_count_agreement}, "
            f"received_sequence={validation.first_received_sequence}.."
            f"{validation.last_received_sequence}"
        )
        if validation.missing_samples:
            print(f"missing sequence samples: {validation.missing_samples}")
        if validation.can_id_mismatch_samples:
            print(
                "CAN-ID mismatch samples (sequence, actual, expected): "
                f"{validation.can_id_mismatch_samples}"
            )
        overflow_match = result.overflow_matches_missing
        print(
            "overflow/missing agreement: "
            f"{overflow_match if overflow_match is not None else 'unavailable'}"
        )
    drain_status = "complete" if result.drain_completed else "TIMEOUT"
    print(
        "lifecycle: "
        f"receiver_ready={result.receiver_ready}, drain={drain_status}, "
        f"drain_target={result.drain_target_sequence}, "
        f"drain_wait={result.drain_wait_seconds:.3f}s"
    )
    for failure in result.lifecycle_failures:
        print(f"lifecycle failure: {failure}")
    buffer_text = (
        str(result.socket_receive_buffer)
        if result.socket_receive_buffer is not None
        else "unavailable"
    )
    print(f"SocketCAN SO_RCVBUF: {buffer_text}")
    print(
        "netdevice RX deltas: "
        + ", ".join(
            f"{name}={value if value is not None else 'unavailable'}"
            for name, value in result.netdevice_rx_deltas.items()
        )
    )
    print(f"capture exception: {result.capture_exception or 'none'}")
    print(f"trace: {result.trace}")
    print(f"RESULT: {result.outcome}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=("dev", "custom", *SCENARIOS),
        help="scenario to run; repeatable (default: dev)",
    )
    parser.add_argument("--duration", type=float, help="custom duration in seconds")
    parser.add_argument(
        "--rate",
        type=float,
        help="custom average frames/s; omit for an unpaced max-rate run",
    )
    parser.add_argument(
        "--burst-size", type=int, default=1, help="custom back-to-back burst size"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("temp-output/socketcan-stress"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    requested = args.scenario or ["dev"]
    scenarios: list[StressScenario] = []
    for name in requested:
        if name == "dev":
            scenarios.extend(SCENARIOS[item] for item in DEV_SCENARIOS)
        elif name == "custom":
            if args.duration is None:
                raise SystemExit("--scenario custom requires --duration")
            scenarios.append(
                StressScenario(
                    "custom", args.duration, args.rate, burst_size=args.burst_size
                )
            )
        else:
            scenarios.append(SCENARIOS[name])

    results: list[StressResult] = []
    try:
        for scenario in scenarios:
            result = run_scenario(scenario, args.output_dir)
            print_result(result)
            results.append(result)
    except (RuntimeError, OSError, ValueError) as exc:
        print(
            f"ERROR: cannot run SocketCAN stress harness on externally prepared "
            f"{VCAN_CHANNEL}: {exc}",
            file=sys.stderr,
        )
        return 2
    return max((result.exit_code for result in results), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
