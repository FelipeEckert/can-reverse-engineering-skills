from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import physical_hypotheses  # noqa: E402


ROW_KEYS = {
    "candidate",
    "label",
    "hypothesis",
    "unit",
    "factor",
    "offset",
    "decoded_physical_q01",
    "decoded_physical_q99",
    "corpus_precedent",
}
CANDIDATE_KEYS = {
    "upstream_candidate_order",
    "id_hex",
    "field",
    "order",
    "start_bit",
    "length_bits",
    "signed",
    "period_ms",
    "observed_behavior",
    "raw_min",
    "raw_q01",
    "raw_q99",
    "raw_max",
    "raw_edge_fraction",
    "samples",
}
CSV_COLUMNS = [
    "upstream_candidate_order",
    "id_hex",
    "field",
    "start_bit",
    "length_bits",
    "order",
    "signed",
    "period_ms",
    "raw_min",
    "raw_max",
    "hypothesis",
    "unit",
    "factor",
    "offset",
    "decoded_physical_q01",
    "decoded_physical_q99",
    "observed_behavior",
    "corpus_precedent",
]


def marker(candidate: int, interpretation: int) -> dict[str, int]:
    return {"candidate": candidate, "interpretation": interpretation}


def candidate(candidate_index: int) -> dict[str, object]:
    can_id = 0x100 + candidate_index
    return {
        "id": can_id,
        "id_hex": f"0x{can_id:X}",
        "field": f"byte {candidate_index}",
        "order": "single-byte",
        "start_bit": candidate_index * 8,
        "length_bits": 8,
        "signed": False,
        "period_ms": 10.0,
        "raw_min": 0.0,
        "raw_q01": 1.0,
        "raw_q99": 99.0,
        "raw_max": 100.0,
        "raw_edge_fraction": 0.0,
        "samples": 100,
        "ac1": 0.9,
        "wrap_rate": 0.0,
        "jump_rate": 0.01,
        "change_rate": 0.5,
        "delta_entropy_norm": 0.4,
        "distinct": 80,
    }


def row_order(rows: list[dict[str, object]]) -> list[tuple[int, str, float, float]]:
    return [
        (
            int(row["candidate"]["upstream_candidate_order"]),
            str(row["hypothesis"]),
            float(row["factor"]),
            float(row["offset"]),
        )
        for row in rows
    ]


class RoundRobinTests(unittest.TestCase):
    def test_exact_round_robin_order(self) -> None:
        buckets = [
            [marker(candidate_index, interpretation) for interpretation in range(3)]
            for candidate_index in range(1, 4)
        ]

        emitted = physical_hypotheses.round_robin_hypotheses(buckets, 7)

        self.assertEqual(
            emitted,
            [
                marker(1, 0), marker(2, 0), marker(3, 0),
                marker(1, 1), marker(2, 1), marker(3, 1),
                marker(1, 2),
            ],
        )

    def test_one_candidate_preserves_interpretation_order(self) -> None:
        bucket = [marker(1, interpretation) for interpretation in range(21)]
        self.assertEqual(
            physical_hypotheses.round_robin_hypotheses([bucket], 20),
            bucket[:20],
        )

    def test_limit_below_candidate_count_is_deterministic(self) -> None:
        buckets = [[marker(candidate_index, 0)] for candidate_index in range(1, 6)]
        self.assertEqual(
            physical_hypotheses.round_robin_hypotheses(buckets, 3),
            [marker(1, 0), marker(2, 0), marker(3, 0)],
        )


class OutputTests(unittest.TestCase):
    def run_main(
        self,
        candidate_count: int,
        *,
        limit: int | None = None,
        precedent: object | None = None,
    ) -> tuple[dict[str, object], list[dict[str, str]], str]:
        with tempfile.TemporaryDirectory(prefix="physical_hypotheses_test_") as directory:
            root = Path(directory)
            candidates_path = root / "candidates.json"
            out_path = root / "hypotheses.json"
            candidates_path.write_text(
                json.dumps({"candidates": [candidate(index) for index in range(candidate_count)]}),
                encoding="utf-8",
            )
            argv = [
                "physical_hypotheses.py",
                "--candidates",
                str(candidates_path),
                "--out",
                str(out_path),
            ]
            if limit is not None:
                argv.extend(["--limit", str(limit)])

            stdout = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    physical_hypotheses,
                    "load_corpus_document",
                    return_value=(None, "test corpus unavailable"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                if precedent is None:
                    physical_hypotheses.main()
                else:
                    with mock.patch.object(
                        physical_hypotheses, "corpus_precedent", side_effect=precedent
                    ):
                        physical_hypotheses.main()

            document = json.loads(out_path.read_text(encoding="utf-8"))
            with out_path.with_suffix(".csv").open(newline="", encoding="utf-8") as handle:
                csv_rows = list(csv.DictReader(handle))
            return document, csv_rows, stdout.getvalue()

    def test_default_multi_candidate_coverage_and_console_preview(self) -> None:
        document, _, stdout = self.run_main(12)
        rows = document["physical_hypotheses"]
        orders = [row["candidate"]["upstream_candidate_order"] for row in rows]

        self.assertEqual(document["candidates_considered"], 12)
        self.assertEqual(document["candidates_represented"], 12)
        self.assertEqual(document["hypotheses_emitted"], 20)
        self.assertEqual(orders, list(range(1, 13)) + list(range(1, 9)))
        self.assertIn("12/12 considered candidates", stdout)
        self.assertIn("HYPOTHESIS candidate 12:", stdout)

    def test_json_csv_order_and_existing_row_schema_match(self) -> None:
        document, csv_rows, _ = self.run_main(3, limit=8)
        json_rows = document["physical_hypotheses"]
        csv_order = [
            (
                int(row["upstream_candidate_order"]),
                row["hypothesis"],
                float(row["factor"]),
                float(row["offset"]),
            )
            for row in csv_rows
        ]

        self.assertEqual(row_order(json_rows), csv_order)
        self.assertEqual(list(csv_rows[0]), CSV_COLUMNS)
        for row in json_rows:
            self.assertEqual(set(row), ROW_KEYS)
            self.assertEqual(set(row["candidate"]), CANDIDATE_KEYS)

    def test_opendbc_annotation_does_not_affect_order(self) -> None:
        baseline, _, _ = self.run_main(3, limit=9)

        def varying_precedent(
            hypothesis: dict[str, object],
            candidate_identity: dict[str, object],
            _document: object,
            _reason: object,
        ) -> dict[str, object]:
            exact = (
                int(candidate_identity["upstream_candidate_order"])
                + int(float(hypothesis["factor"]) * 1000)
            ) % 2 == 0
            return {
                "available": True,
                "exact_match": exact,
                "interpretation": "test annotation only",
                "family_results": [],
            }

        annotated, _, _ = self.run_main(3, limit=9, precedent=varying_precedent)

        self.assertEqual(
            row_order(baseline["physical_hypotheses"]),
            row_order(annotated["physical_hypotheses"]),
        )


if __name__ == "__main__":
    unittest.main()
