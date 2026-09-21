from __future__ import annotations

import contextlib
import builtins
import concurrent.futures
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import flask_sync  # noqa: E402


LEGACY_KEYS = {"n", "note", "frames", "trace", "sidecar"}
SIDECAR_HEADER = "epoch;kind;label;value"


class _Closable:
    def stop(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class _Logger(_Closable):
    def __init__(self, path: str):
        Path(path).write_text("trace fixture\n", encoding="utf-8")


class _Notifier(_Closable):
    def __init__(self, buses, listeners):
        self.buses = buses
        self.listeners = listeners


@contextlib.contextmanager
def fake_capture(notifier=_Notifier, *, bus=None, logger=_Logger):
    config = {
        "interface": "fake",
        "channel": "test",
        "nominal": 500_000,
        "data": 500_000,
        "sample_point": 0.8,
        "listen_only": True,
    }
    bus_patch = (mock.patch.object(flask_sync.can, "Bus", side_effect=bus)
                 if callable(bus)
                 else mock.patch.object(flask_sync.can, "Bus", return_value=bus or _Closable()))
    with (
        mock.patch.object(flask_sync.capture, "resolve_config", return_value=config),
        mock.patch.object(flask_sync.common, "make_timing", return_value=object()),
        bus_patch,
        mock.patch.object(flask_sync.can, "Logger", side_effect=logger),
        mock.patch.object(flask_sync.can, "Notifier", side_effect=notifier),
    ):
        yield


class CaptureControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="flask_sync_test_")
        self.addCleanup(self.temporary.cleanup)
        self.outdir = Path(self.temporary.name)

    def controller(self, **kwargs) -> flask_sync.CaptureController:
        return flask_sync.CaptureController("run", self.outdir, **kwargs)

    def read_index(self) -> list[dict]:
        return json.loads((self.outdir / "runs_run.json").read_text(encoding="utf-8"))

    @staticmethod
    def batch(batch_id: str, *rows: dict) -> dict:
        return {"id": batch_id, "run": int(batch_id.split(":", 1)[0]),
                "rows": list(rows)}

    @staticmethod
    def event(event_id: str, epoch: float, phase: str, note: str = "", **extra) -> dict:
        event = {"id": event_id, "run": int(event_id.split(":", 1)[0]),
                 "epoch": epoch, "phase": phase, "note": note}
        event.update(extra)
        return event

    @staticmethod
    def plan() -> dict:
        return {
            "version": 1,
            "objective": "Separate angle from rate",
            "context": "Stationary controlled setup",
            "steps": [
                {"phase": "baseline", "instruction": "Hold centered"},
                {"phase": "stimulus", "instruction": "Turn slowly left",
                 "expected_observation": "Rate peaks only during motion",
                 "reason": "Separates position from rate"},
                {"phase": "return", "instruction": "Return to center"},
            ],
        }

    def test_loads_legacy_index_without_changing_old_record(self) -> None:
        legacy = {
            "n": 4,
            "note": "holds",
            "frames": 123,
            "trace": "temp-output/trace_run_4.csv",
            "sidecar": "temp-output/sidecar_run_4.csv",
        }
        (self.outdir / "runs_run.json").write_text(
            json.dumps([legacy]), encoding="utf-8")

        controller = self.controller()

        self.assertEqual(controller.runs, [legacy])
        self.assertEqual(controller.run_n, 4)

    def test_experiment_plan_loader_normalizes_valid_schema(self) -> None:
        path = self.outdir / "plan.json"
        path.write_text(json.dumps(self.plan()), encoding="utf-8")

        plan = flask_sync.load_experiment_plan(path)

        self.assertEqual(plan, self.plan())
        self.assertEqual(plan["steps"][1]["phase"], "stimulus")

    def test_experiment_plan_loader_rejects_invalid_schema(self) -> None:
        invalid = [
            [],
            {"version": 2, "objective": "x", "steps": [{}]},
            {"version": 1, "objective": "", "steps": [{}]},
            {"version": 1, "objective": "x", "steps": []},
            {"version": 1, "objective": "x", "steps": [
                {"phase": "unexpected", "instruction": "not planned"}]},
            {"version": 1, "objective": "x", "steps": [
                {"phase": "baseline", "instruction": "x", "timer": 5}]},
        ]
        path = self.outdir / "plan.json"
        for document in invalid:
            with self.subTest(document=document):
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ValueError):
                    flask_sync.load_experiment_plan(path)
        path.write_text("{truncated", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid experiment plan JSON"):
            flask_sync.load_experiment_plan(path)

    def test_guided_run_persists_static_plan_snapshot(self) -> None:
        source = self.plan()
        controller = self.controller(experiment_plan=source)
        source["objective"] = "mutated after construction"
        with fake_capture():
            controller.start("sweep", mode="sweep")
            record = self.read_index()[0]
            controller.stop()

        self.assertEqual(record["experiment_plan"], self.plan())
        self.assertEqual(record["events"], [])

    def test_guided_progression_is_server_authoritative(self) -> None:
        controller = self.controller(experiment_plan=self.plan())
        with fake_capture():
            controller.start("sweep", mode="sweep")
            self.assertEqual(controller.plan_progress(1),
                             {"step": 1, "total": 3, "state": "before"})
            with self.assertRaises(ValueError):
                controller.add_event(1, self.event("1:guide:9", 9.0, "baseline"))
            finish_first = self.event(
                "1:guide:1", 10.0, "baseline", step=1, boundary="finish")
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, finish_first)
            wrong_phase = self.event(
                "1:guide:2", 10.0, "stimulus", step=1, boundary="start")
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, wrong_phase)

            start = self.event(
                "1:guide:3", 10.0, "baseline", step=1, boundary="start")
            self.assertEqual(controller.add_event(1, start), "1:guide:3")
            self.assertEqual(controller.add_event(1, start), "1:guide:3")
            duplicate_start = self.event(
                "1:other:1", 10.1, "baseline", step=1, boundary="start")
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, duplicate_start)
            early_next = self.event(
                "1:guide:4", 10.2, "stimulus", step=2, boundary="start")
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, early_next)

            finish = self.event(
                "1:guide:5", 11.0, "baseline", step=1, boundary="finish")
            controller.add_event(1, finish)
            self.assertEqual(controller.plan_progress(1),
                             {"step": 2, "total": 3, "state": "before"})
            for number, phase in ((2, "stimulus"), (3, "return")):
                controller.add_event(1, self.event(
                    f"1:guide:{number * 10}", number * 10.0, phase,
                    step=number, boundary="start"))
                controller.add_event(1, self.event(
                    f"1:guide:{number * 10 + 1}", number * 10.0 + 1, phase,
                    step=number, boundary="finish"))
            self.assertEqual(controller.plan_progress(1),
                             {"step": 3, "total": 3, "state": "complete"})
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, self.event(
                    "1:guide:99", 99.0, "return", step=3, boundary="finish"))
            controller.stop()

        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "baseline", "step": 1, "boundary": "start"},
            {"epoch": 11.0, "phase": "baseline", "step": 1, "boundary": "finish"},
            {"epoch": 20.0, "phase": "stimulus", "step": 2, "boundary": "start"},
            {"epoch": 21.0, "phase": "stimulus", "step": 2, "boundary": "finish"},
            {"epoch": 30.0, "phase": "return", "step": 3, "boundary": "start"},
            {"epoch": 31.0, "phase": "return", "step": 3, "boundary": "finish"},
        ])

    def test_guided_same_id_different_boundary_is_rejected(self) -> None:
        controller = self.controller(experiment_plan=self.plan())
        start = self.event("1:guide:1", 10.0, "baseline", step=1, boundary="start")
        changed = self.event("1:guide:1", 10.0, "baseline", step=1, boundary="finish")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.add_event(1, start)
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, changed)
            controller.stop()

    def test_unexpected_marker_does_not_advance_guided_plan(self) -> None:
        controller = self.controller(experiment_plan=self.plan())
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.add_event(1, self.event(
                "1:guide:1", 10.0, "unexpected", "operator paused"))
            self.assertEqual(controller.plan_progress(1),
                             {"step": 1, "total": 3, "state": "before"})
            controller.stop()

        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "unexpected", "note": "operator paused"}])

    def test_pending_guided_boundary_in_stop_is_idempotent(self) -> None:
        controller = self.controller(experiment_plan=self.plan())
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        event = self.event(
            "1:guide:1", 10.0, "baseline", step=1, boundary="start")
        payload = {"run": 1, "batches": [], "events": [event]}
        with fake_capture():
            controller.start("sweep", mode="sweep")
            first = app.test_client().post("/api/capture/stop", json=payload)
            retry = app.test_client().post("/api/capture/stop", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_json()["accepted_event_ids"], ["1:guide:1"])
        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "baseline", "step": 1, "boundary": "start"}])

    def test_next_run_uses_highest_index_or_artifact_number(self) -> None:
        legacy = {"n": 3, "note": "holds", "frames": 1,
                  "trace": "trace_run_3.csv", "sidecar": "sidecar_run_3.csv"}
        (self.outdir / "runs_run.json").write_text(json.dumps([legacy]), encoding="utf-8")
        preserved = self.outdir / "trace_run_7.csv"
        preserved.write_text("do not overwrite\n", encoding="utf-8")
        controller = self.controller()

        with fake_capture():
            status = controller.start("sweep", mode="sweep")

        self.assertTrue(status["running"])
        self.assertEqual(status["run"], 8)
        self.assertEqual(preserved.read_text(encoding="utf-8"), "do not overwrite\n")

    def test_restart_never_overwrites_previous_run_pair(self) -> None:
        with fake_capture():
            first = self.controller()
            first.start("holds", mode="holds")
            first.log_batch(1, self.batch(
                "1:test:1", {"t": 10.0, "kind": "value", "value": 1}))
            first.stop()
        first_sidecar = self.outdir / "sidecar_run_1.csv"
        original = first_sidecar.read_text(encoding="utf-8")

        with fake_capture():
            second = self.controller()
            status = second.start("sweep", mode="sweep")
            second.stop()

        self.assertEqual(status["run"], 2)
        self.assertEqual(first_sidecar.read_text(encoding="utf-8"), original)
        self.assertTrue((self.outdir / "trace_run_2.csv").exists())
        self.assertTrue((self.outdir / "sidecar_run_2.csv").exists())

    def test_start_and_stop_persist_lifecycle_settings_and_legacy_keys(self) -> None:
        settings = {"unit": "deg", "min": -90, "max": 90,
                    "state_values": [-90, 0, 90], "hold_window_s": 2.0,
                    "hold_guard_s": 0.1}
        controller = self.controller(reference_settings=settings)
        with fake_capture(), mock.patch.object(
                flask_sync.time, "time", side_effect=[100.0, 100.25, 105.5]):
            controller.start(
                "sweep", mode="sweep", experiment_note="angle versus rate")
            running = self.read_index()[0]
            controller.counter.stats.frames = 17
            controller.counter.stats.errors = 2
            controller.stop()

        self.assertEqual(running["status"], "running")
        self.assertEqual(running["started_at"], 100.0)
        self.assertIsNone(running["ended_at"])
        self.assertIsNone(running["duration_s"])
        self.assertEqual(running["events"], [])

        complete = self.read_index()[0]
        self.assertTrue(LEGACY_KEYS.issubset(complete))
        self.assertEqual(complete["note"], "sweep")
        self.assertEqual(complete["mode"], "sweep")
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(complete["started_at"], 100.0)
        self.assertEqual(complete["ended_at"], 105.5)
        self.assertEqual(complete["duration_s"], 5.5)
        self.assertEqual(complete["frames"], 17)
        self.assertEqual(complete["errors"], 2)
        self.assertEqual(complete["reference_settings"], settings)
        self.assertEqual(complete["experiment_note"], "angle versus rate")

    def test_restart_marks_interrupted_running_record_incomplete(self) -> None:
        first = self.controller()
        with fake_capture():
            first.start("holds", mode="holds")
        self.assertEqual(self.read_index()[0]["status"], "running")

        restarted = self.controller()

        self.assertEqual(restarted.runs[0]["status"], "incomplete")
        self.assertIsNone(restarted.runs[0]["ended_at"])
        self.assertEqual(self.read_index()[0]["status"], "incomplete")
        first._teardown()

    def test_empty_repeated_and_chronologically_ordered_events(self) -> None:
        controller = self.controller()
        with fake_capture():
            controller.start("sweep", mode="sweep")
            self.assertEqual(self.read_index()[0]["events"], [])
            controller.add_event(1, self.event("1:test:1", 30.0, "baseline"))
            controller.add_event(1, self.event(
                "1:test:2", 10.0, "stimulus", "first input"))
            controller.add_event(1, self.event("1:test:3", 20.0, "stimulus"))
            controller.stop()

        events = self.read_index()[0]["events"]
        self.assertEqual([event["epoch"] for event in events], [10.0, 20.0, 30.0])
        self.assertEqual([event["phase"] for event in events],
                         ["stimulus", "stimulus", "baseline"])
        self.assertEqual(events[0]["note"], "first input")
        self.assertNotIn("note", events[1])

    def test_event_outside_active_run_is_rejected_cleanly(self) -> None:
        controller = self.controller()
        with self.assertRaises(flask_sync.RunConflictError):
            controller.add_event(1, self.event("1:test:1", 10.0, "baseline"))

        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        response = app.test_client().post(
            "/api/event", json={"run": 1, "event": self.event(
                "1:test:1", 10.0, "baseline")})
        self.assertEqual(response.status_code, 409)
        self.assertIn("not active", response.get_json()["error"])

        with fake_capture():
            controller.start("holds", mode="holds")
            invalid = app.test_client().post(
                "/api/event", json={"run": 1, "event": self.event(
                    "1:test:2", 11.0, "signal-family-specific")})
            controller.stop()
        self.assertEqual(invalid.status_code, 400)

    def test_stop_endpoint_drains_pending_rows_before_teardown(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            batch = self.batch(
                "1:browser:1",
                {"t": 1000.25, "kind": "value", "value": 42.5},
                {"t": 1000.50, "kind": "anchor", "value": 42.5})
            response = app.test_client().post(
                "/api/capture/stop", json={"run": 1, "batches": [batch], "events": []})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["running"])
        lines = (self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(lines[0], SIDECAR_HEADER)
        self.assertEqual(lines[1:], [
            "1000.250000;value;run;42.5",
            "1000.500000;anchor;run;42.5",
        ])
        self.assertTrue(all(len(line.split(";")) == 4 for line in lines))
        self.assertEqual(response.get_json()["accepted_batch_ids"], ["1:browser:1"])

    def test_lost_log_response_retry_does_not_duplicate_rows(self) -> None:
        controller = self.controller()
        row = {"t": 1000.25, "kind": "value", "value": 42.5}
        batch = self.batch("1:browser:1", row)
        with fake_capture():
            controller.start("sweep", mode="sweep")
            first = controller.log_batch(1, batch)  # response could be lost here
            retry = controller.log_batch(1, batch)
            controller.stop()

        self.assertEqual(first["accepted_batch_ids"], ["1:browser:1"])
        self.assertEqual(retry["accepted_batch_ids"], ["1:browser:1"])
        lines = (self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(lines[1:], ["1000.250000;value;run;42.5"])

    def test_same_batch_id_with_different_rows_is_rejected(self) -> None:
        controller = self.controller()
        first = self.batch("1:client:1", {"t": 10.0, "kind": "value", "value": 1})
        changed = self.batch("1:client:1", {"t": 10.0, "kind": "value", "value": 2})
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.log_batch(1, first)
            with self.assertRaises(flask_sync.RunConflictError):
                controller.log_batch(1, changed)
            controller.stop()

        self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()[1:], ["10.000000;value;run;1"])

    def test_same_event_id_with_different_content_is_rejected(self) -> None:
        controller = self.controller()
        first = self.event("1:client:1", 10.0, "baseline")
        changed = self.event("1:client:1", 11.0, "stimulus")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            self.assertEqual(controller.add_event(1, first), "1:client:1")
            self.assertEqual(controller.add_event(1, first), "1:client:1")
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(1, changed)
            controller.stop()

        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "baseline"}])

    def test_two_component_batch_and_event_ids_are_rejected(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            malformed_batch = app.test_client().post(
                "/api/log", json={"run": 1, "batch": self.batch(
                    "1:1", {"t": 10.0, "kind": "value", "value": 1})})
            malformed_event = app.test_client().post(
                "/api/event", json={"run": 1, "event": self.event(
                    "1:event", 10.0, "stimulus")})
            controller.stop()

        self.assertEqual(malformed_batch.status_code, 400)
        self.assertEqual(malformed_event.status_code, 400)
        self.assertIn("run:page-namespace:sequence", malformed_batch.get_json()["error"])
        self.assertIn("run:page-namespace:sequence", malformed_event.get_json()["error"])
        self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines(), [SIDECAR_HEADER])
        self.assertEqual(self.read_index()[0]["events"], [])

    def test_ambiguous_stop_then_new_run_confirms_without_leaking_rows(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        stale = self.batch("1:browser:1", {"t": 10.0, "kind": "value", "value": 1})
        with fake_capture():
            controller.start("sweep", mode="sweep")
            first_stop = app.test_client().post(
                "/api/capture/stop", json={"run": 1, "batches": [stale], "events": []})
            controller.start("holds", mode="holds")
            late_stop = app.test_client().post(
                "/api/capture/stop", json={"run": 1, "batches": [stale], "events": []})
            controller.stop()

        self.assertEqual(first_stop.status_code, 200)
        self.assertEqual(late_stop.status_code, 200)
        self.assertEqual(late_stop.get_json()["run"], 1)
        self.assertEqual(late_stop.get_json()["accepted_batch_ids"], ["1:browser:1"])
        second = (self.outdir / "sidecar_run_2.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(second, [SIDECAR_HEADER])

    def test_pending_prior_run_items_cannot_be_relabelled_to_new_run(self) -> None:
        controller = self.controller()
        stale_batch = self.batch(
            "1:client:1", {"t": 10.0, "kind": "value", "value": 1})
        stale_event = self.event("1:client:1", 10.0, "stimulus")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.stop()
            controller.start("holds", mode="holds")
            with self.assertRaises(flask_sync.RunConflictError):
                controller.log_batch(2, stale_batch)
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(2, stale_event)
            forged_batch = dict(stale_batch, run=2)
            forged_event = dict(stale_event, run=2)
            with self.assertRaises(flask_sync.RunConflictError):
                controller.log_batch(2, forged_batch)
            with self.assertRaises(flask_sync.RunConflictError):
                controller.add_event(2, forged_event)
            controller.stop()

        self.assertEqual((self.outdir / "sidecar_run_2.csv").read_text(
            encoding="utf-8").splitlines(), [SIDECAR_HEADER])
        self.assertEqual(self.read_index()[1]["events"], [])

    def test_two_client_namespaces_do_not_collide_with_same_sequence(self) -> None:
        controller = self.controller()
        first = self.batch("1:client-a:1", {"t": 10.0, "kind": "value", "value": 1})
        second = self.batch("1:client-b:1", {"t": 11.0, "kind": "value", "value": 2})
        first_event = self.event("1:client-a:1", 10.0, "baseline")
        second_event = self.event("1:client-b:1", 11.0, "stimulus")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.log_batch(1, first)
            controller.log_batch(1, second)
            controller.add_event(1, first_event)
            controller.add_event(1, second_event)
            controller.stop()

        self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()[1:], [
                "10.000000;value;run;1", "11.000000;value;run;2"])
        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "baseline"},
            {"epoch": 11.0, "phase": "stimulus"},
        ])

    def test_repeated_stop_with_same_pending_batch_is_idempotent(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        batch = self.batch("1:browser:1", {"t": 10.0, "kind": "value", "value": 1})
        payload = {"run": 1, "batches": [batch], "events": []}
        with fake_capture():
            controller.start("sweep", mode="sweep")
            first = app.test_client().post("/api/capture/stop", json=payload)
            retry = app.test_client().post("/api/capture/stop", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_json()["accepted_batch_ids"], ["1:browser:1"])
        lines = (self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(lines[1:], ["10.000000;value;run;1"])

    def test_late_log_after_stop_is_rejected(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        batch = self.batch("1:browser:1", {"t": 10.0, "kind": "value", "value": 1})
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.stop()
            response = app.test_client().post(
                "/api/log", json={"run": 1, "batch": batch})

        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines(), [SIDECAR_HEADER])

    def test_late_event_from_prior_run_is_rejected(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        late = self.event("1:browser:1", 10.0, "stimulus")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.stop()
            controller.start("holds", mode="holds")
            response = app.test_client().post(
                "/api/event", json={"run": 1, "event": late})
            controller.stop()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.read_index()[1]["events"], [])

    def test_marker_immediately_before_stop_is_persisted_once(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        event = self.event("1:browser:1", 10.0, "stimulus", "turn left")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            marker = app.test_client().post(
                "/api/event", json={"run": 1, "event": event})
            stopped = app.test_client().post(
                "/api/capture/stop",
                json={"run": 1, "batches": [], "events": [event]})

        self.assertEqual(marker.status_code, 200)
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.get_json()["accepted_event_ids"], ["1:browser:1"])
        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "stimulus", "note": "turn left"}])

    def test_retry_stop_repersists_completion_after_index_write_failure(self) -> None:
        controller = self.controller()
        batch = self.batch("1:client:1", {"t": 10.0, "kind": "value", "value": 1})
        with fake_capture():
            controller.start("sweep", mode="sweep")
            with mock.patch.object(controller, "_write_index", side_effect=OSError("disk busy")):
                with self.assertRaises(OSError):
                    controller.stop(1, batches=[batch], events=[])

            self.assertFalse(controller.running)
            self.assertEqual(self.read_index()[0]["status"], "running")
            retry = controller.stop(1, batches=[batch], events=[])

        self.assertFalse(retry["running"])
        self.assertEqual(retry["status"], "complete")
        self.assertEqual(self.read_index()[0]["status"], "complete")
        self.assertTrue(controller._final_persisted[1])

    def test_retry_finalized_stop_does_not_affect_new_active_run(self) -> None:
        controller = self.controller()
        batch = self.batch("1:client:1", {"t": 10.0, "kind": "value", "value": 1})
        with fake_capture():
            controller.start("sweep", mode="sweep")
            controller.stop(1, batches=[batch], events=[])
            controller.start("holds", mode="holds")

            retry = controller.stop(1, batches=[batch], events=[])

            self.assertTrue(controller.running)
            self.assertEqual(controller.run_n, 2)
            self.assertEqual(retry["run"], 1)
            self.assertEqual(retry["status"], "complete")
            self.assertEqual((self.outdir / "sidecar_run_2.csv").read_text(
                encoding="utf-8").splitlines(), [SIDECAR_HEADER])
            controller.stop(2)

    def test_each_shutdown_failure_marks_run_incomplete(self) -> None:
        class FailingNotifier(_Notifier):
            def stop(self) -> None:
                raise OSError("notifier failed")

        class FailingLogger(_Logger):
            def stop(self) -> None:
                raise OSError("logger failed")

        class FailingBus(_Closable):
            def shutdown(self) -> None:
                raise OSError("bus failed")

        cases = [
            {"notifier": FailingNotifier},
            {"logger": FailingLogger},
            {"bus": FailingBus()},
        ]
        for number, capture_args in enumerate(cases, start=1):
            with self.subTest(capture_args=capture_args), tempfile.TemporaryDirectory() as path:
                controller = flask_sync.CaptureController("run", Path(path))
                with fake_capture(**capture_args):
                    controller.start("sweep", mode="sweep")
                    status = controller.stop()
                record = json.loads((Path(path) / "runs_run.json").read_text(
                    encoding="utf-8"))[0]
                self.assertEqual(record["status"], "incomplete", number)
                self.assertIn("failed", record["error"])
                self.assertEqual(status["status"], "incomplete")
                self.assertIn("failed", status["error"])

    def test_failed_notifier_startup_persists_incomplete(self) -> None:
        class FailingStartupNotifier:
            def __init__(self, buses, listeners):
                raise OSError("notifier startup failed")

        controller = self.controller()
        with fake_capture(FailingStartupNotifier):
            status = controller.start("sweep", mode="sweep")

        record = self.read_index()[0]
        self.assertFalse(status["running"])
        self.assertEqual(record["status"], "incomplete")
        self.assertIn("notifier startup failed", record["error"])

    def test_final_counters_are_sampled_after_notifier_shutdown(self) -> None:
        class UpdatingNotifier(_Notifier):
            def stop(self) -> None:
                counter = self.listeners[1]
                counter.stats.frames += 3
                counter.stats.errors += 2

        controller = self.controller()
        with fake_capture(UpdatingNotifier):
            controller.start("sweep", mode="sweep")
            controller.counter.stats.frames = 7
            controller.counter.stats.errors = 1
            controller.stop()

        record = self.read_index()[0]
        self.assertEqual(record["frames"], 10)
        self.assertEqual(record["errors"], 3)

    def test_running_record_exists_before_bus_can_receive(self) -> None:
        index = self.outdir / "runs_run.json"

        class InspectingBus(_Closable):
            def __init__(self, **kwargs):
                record = json.loads(index.read_text(encoding="utf-8"))[0]
                if record["status"] != "running":
                    raise AssertionError("run record was not persisted before bus")

        with fake_capture(bus=InspectingBus):
            status = self.controller().start("sweep", mode="sweep")
        self.assertTrue(status["running"])

    def test_concurrent_log_event_and_stop_are_serialized_safely(self) -> None:
        controller = self.controller()
        batch = self.batch("1:client:1", {"t": 10.0, "kind": "value", "value": 1})
        event = self.event("1:client:1", 10.0, "stimulus")
        with fake_capture():
            controller.start("sweep", mode="sweep")

            def ignore_late(call):
                try:
                    return call()
                except flask_sync.RunConflictError:
                    return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(ignore_late, lambda: controller.log_batch(1, batch)),
                    executor.submit(ignore_late, lambda: controller.add_event(1, event)),
                    executor.submit(controller.stop, 1, batches=[batch], events=[event]),
                ]
                for future in futures:
                    future.result()

        self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()[1:], ["10.000000;value;run;1"])
        self.assertEqual(self.read_index()[0]["events"], [
            {"epoch": 10.0, "phase": "stimulus"}])
        self.assertEqual(self.read_index()[0]["status"], "complete")

    def test_partial_batch_write_rolls_back_and_can_retry(self) -> None:
        controller = self.controller()
        batch = self.batch(
            "1:test:1",
            {"t": 10.0, "kind": "value", "value": 1},
            {"t": 11.0, "kind": "value", "value": 2})

        class PartialWrite:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def write(self, text):
                self.wrapped.write(text[:len(text) // 2])
                raise OSError("simulated partial write")

        with fake_capture():
            controller.start("sweep", mode="sweep")
            real_open = builtins.open

            def failing_open(path, mode="r", *args, **kwargs):
                opened = real_open(path, mode, *args, **kwargs)
                return PartialWrite(opened) if mode == "a+" else opened

            with mock.patch("builtins.open", side_effect=failing_open):
                with self.assertRaises(OSError):
                    controller.log_batch(1, batch)
            self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
                encoding="utf-8").splitlines(), [SIDECAR_HEADER])
            controller.log_batch(1, batch)
            controller.stop()

        self.assertEqual((self.outdir / "sidecar_run_1.csv").read_text(
            encoding="utf-8").splitlines()[1:], [
                "10.000000;value;run;1", "11.000000;value;run;2"])

    def test_corrupt_index_fails_closed_without_touching_artifacts(self) -> None:
        index = self.outdir / "runs_run.json"
        trace = self.outdir / "trace_run_9.csv"
        sidecar = self.outdir / "sidecar_run_9.csv"
        index.write_text('[{"n": 9}', encoding="utf-8")
        trace.write_text("preserve trace\n", encoding="utf-8")
        sidecar.write_text(SIDECAR_HEADER + "\n", encoding="utf-8")

        with self.assertRaises(json.JSONDecodeError):
            self.controller()

        self.assertEqual(index.read_text(encoding="utf-8"), '[{"n": 9}')
        self.assertEqual(trace.read_text(encoding="utf-8"), "preserve trace\n")
        self.assertEqual(sidecar.read_text(encoding="utf-8"), SIDECAR_HEADER + "\n")

    def test_log_and_stop_requests_share_bounded_retry_transport(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        page = app.test_client().get("/").get_data(as_text=True)

        self.assertIn("const REQUEST_TIMEOUT_MS = 3000", page)
        self.assertIn("const abort = new AbortController()", page)
        self.assertIn("signal: abort.signal", page)
        self.assertIn("requestJson('/api/log', {run: batch.run, batch})", page)
        self.assertIn("requestJson('/api/capture/stop', {run: stopRun, batches, events})", page)
        self.assertIn("pendingBatches.clear()", page)
        self.assertIn("if (stopping) stopPending = true", page)
        self.assertIn("capturing = stopPending ? false : serverRunning", page)
        self.assertIn("if (!stopPending && serverRunning && s.run) activeRun = s.run", page)
        self.assertIn("if (!stopPending) stopRun = activeRun", page)
        self.assertIn("CLIENT_NAMESPACE", page)
        self.assertIn("pendingBatches.set(id, {id, run, rows})", page)
        self.assertIn("const event = {id, run, epoch", page)

    def test_browser_rows_keep_creation_run_when_status_changes(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        page = app.test_client().get("/").get_data(as_text=True)

        # A row created under run N carries N even if polling later sets activeRun=N+1.
        self.assertIn("buffer.push({run: activeRun, t: now(), kind, value})", page)
        self.assertIn("const run = row.run", page)
        self.assertIn(
            "rowsByRun.get(run).push({t: row.t, kind: row.kind, value: row.value})",
            page,
        )
        self.assertNotIn("const run = stopPending ? stopRun : activeRun", page)

    def test_page_exposes_only_lightweight_generic_chronology_controls(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        page = app.test_client().get("/").get_data(as_text=True)

        self.assertIn('id="experiment-note"', page)
        self.assertIn('id="event-note"', page)
        for phase in flask_sync.CaptureController.EVENT_PHASES:
            self.assertIn(f'data-phase="{phase}"', page)
        self.assertIn("REQUEST_TIMEOUT_MS", page)
        self.assertIn("AbortController", page)
        self.assertIn("request timed out; data retained for retry", page)
        self.assertIn("settleEventRequests", page)
        self.assertIn("retry stop", page)

    def test_manual_page_is_unchanged_without_experiment_plan(self) -> None:
        controller = self.controller()
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        page = app.test_client().get("/").get_data(as_text=True)

        self.assertNotIn('id="guide"', page)
        self.assertNotIn('id="maneuver-btn"', page)
        for phase in ("baseline", "stimulus", "return", "unexpected"):
            self.assertIn(f'data-phase="{phase}"', page)

    def test_guided_page_exposes_plan_and_reuses_retry_protocol(self) -> None:
        controller = self.controller(experiment_plan=self.plan())
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        page = app.test_client().get("/").get_data(as_text=True)

        self.assertIn('id="guide"', page)
        self.assertIn('id="maneuver-btn"', page)
        self.assertIn("CAPTURING CAN DATA", page)
        self.assertIn("pendingGuideEvent", page)
        self.assertIn("const event = pendingGuideEvent", page)
        self.assertIn("applyPlanProgress(result.plan_progress, event.run)", page)
        self.assertIn("progressRank(progress) < progressRank(planProgress)", page)
        self.assertNotIn('data-phase="baseline"', page)
        self.assertNotIn('data-phase="stimulus"', page)
        self.assertNotIn('data-phase="return"', page)
        self.assertIn('data-phase="unexpected"', page)
        self.assertIn("mode: \"both\"", page)

    def test_status_reconstructs_guided_progress_after_page_reload(self) -> None:
        controller = self.controller(experiment_plan=self.plan())
        app = flask_sync.make_app(
            controller, "run", "%", 0, 100, 2, [0, 100], "test", 2.0, 0.0, "both")
        with fake_capture():
            controller.start("sweep", mode="sweep")
            response = app.test_client().post("/api/event", json={
                "run": 1,
                "event": self.event(
                    "1:guide:1", 10.0, "baseline", step=1, boundary="start"),
            })
            reloaded = app.test_client().get("/api/capture/status")
            controller.stop()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["plan_progress"],
                         {"step": 1, "total": 3, "state": "active"})
        self.assertEqual(reloaded.get_json()["plan_progress"],
                         {"step": 1, "total": 3, "state": "active"})


if __name__ == "__main__":
    unittest.main()
