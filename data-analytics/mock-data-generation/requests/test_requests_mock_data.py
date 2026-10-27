"""Check issue #587's edge cases independently of PostgreSQL availability."""

import csv
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "requests_mock", HERE / "generate_requests_mock_data.py"
)
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)


class RequestsMockTests(unittest.TestCase):
    """Verify contracts that would otherwise hide archival pipeline bugs."""

    @classmethod
    def setUpClass(cls):
        """Build an in-memory dataset and schema for independent assertions."""
        cls.tables, cls.cases, cls.pii, cls.broken = GEN.build_dataset()
        cls.schema = GEN.read_schema()
        cls.rows = cls.tables["requests"]

    def test_all_primary_and_foreign_keys_match_schema(self):
        """Check every generated table, including nullable category parents."""
        self.assertEqual(len(self.rows), 500)
        self.assertEqual(set(self.tables), set(GEN.TABLES))
        self.assertEqual(list(self.tables["users"][0]), ["user_id"])
        for table, definitions in self.schema.items():
            key = next(
                name for name, column in definitions.items() if column["primary_key"]
            )
            values = [row[key] for row in self.tables[table]]
            self.assertEqual(len(values), len(set(values)))
            self.assertNotIn(None, values)
            for name, column in definitions.items():
                if column["reference"]:
                    target, target_key = column["reference"]
                    allowed = {row[target_key] for row in self.tables[target]}
                    self.assertTrue(
                        all(
                            row[name] is None or row[name] in allowed
                            for row in self.tables[table]
                        )
                    )
        GEN.validate_dataset(self.tables, self.schema)

    def test_skewed_nulls_and_contradictory_lead_flags(self):
        """Cases 1 and 10 must survive left joins and contradict the lead flag."""
        self.assertEqual(
            sum(row["lead_volunteer_id"] is None for row in self.rows), 493
        )
        self.assertEqual(sum(row["beneficiary_id"] is None for row in self.rows), 18)
        self.assertEqual(self.rows[10]["req_islead_id"], 1)
        self.assertIsNone(self.rows[10]["lead_volunteer_id"])
        self.assertGreater(sum(row["req_islead_id"] for row in self.rows), 7)

    def test_timestamp_boundaries_and_reversed_dates(self):
        """Cases 2 and 3 exercise microsecond half-open boundaries."""
        boundary = "2026-10-24 12:00:00.123456"
        at_boundary = [row for row in self.rows if row["last_updated_at"] == boundary]
        self.assertGreaterEqual(len(at_boundary), 3)
        self.assertEqual(self.rows[3]["last_updated_at"], "2026-10-24 12:00:00.123455")
        self.assertEqual(self.rows[4]["last_updated_at"], "2026-10-24 12:00:00.123457")
        first = [
            row
            for row in self.rows
            if boundary <= row["last_updated_at"] < "2026-10-24 12:00:00.123457"
        ]
        second = [
            row
            for row in self.rows
            if "2026-10-24 12:00:00.123457"
            <= row["last_updated_at"]
            < "2026-10-24 12:00:00.123458"
        ]
        self.assertEqual(len(first), len(at_boundary))
        self.assertEqual(len(second), 1)
        self.assertFalse({r["req_id"] for r in first} & {r["req_id"] for r in second})
        self.assertLess(
            datetime.fromisoformat(self.rows[5]["last_updated_at"]),
            datetime.fromisoformat(self.rows[5]["submission_date"]),
        )

    def test_csv_round_trip_preserves_newlines_unicode_null_and_empty(self):
        """Cases 4, 5 and 7 must remain distinct after CSV encoding/decoding."""
        with tempfile.TemporaryDirectory() as directory:
            GEN.generate(directory)
            for table in GEN.TABLES:
                with (Path(directory) / f"{table}.csv").open(
                    encoding="utf-8", newline=""
                ) as stream:
                    actual = list(csv.DictReader(stream))
                self.assertEqual(
                    actual,
                    [
                        {k: str(GEN.encode(v)) for k, v in row.items()}
                        for row in self.tables[table]
                    ],
                )
            with (Path(directory) / "requests.csv").open(
                encoding="utf-8", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertIn("\n", rows[6]["req_desc"])
            self.assertIn('"', rows[6]["req_desc"])
            self.assertIn(",", rows[6]["req_desc"])
            self.assertIn("mañana", rows[7]["req_desc"])
            nullable_text = [
                name
                for name, col in self.schema["requests"].items()
                if col["nullable"] and col["type"].startswith(("varchar", "text"))
            ]
            self.assertEqual(
                set(nullable_text),
                {
                    "beneficiary_id",
                    "lead_volunteer_id",
                    "req_doc_link",
                    "audio_req_desc",
                    "deletion_reason",
                },
            )
            for name in nullable_text:
                self.assertEqual(rows[0][name], r"\N")
                self.assertEqual(rows[8][name], "")

    def test_synthetic_personal_details_have_complete_labels(self):
        """Case 6 has six marked rows and several phone representations."""
        self.assertEqual(
            {row["req_id"] for row in self.pii},
            {row["req_id"] for row in self.rows[16:22]},
        )
        for row in self.rows[16:22]:
            self.assertIn("Synthetic person:", row["req_desc"])
            for label in ("Phone:", "Email:", "Address:"):
                self.assertIn(label, row["req_desc"])
            self.assertRegex(row["req_desc"], r"@example\.(org|com|net)")
            self.assertIn("Fictional City", row["req_desc"])
        self.assertIn("four eight zero", self.rows[21]["req_desc"])
        self.assertIn("李示例", self.rows[20]["req_desc"])

    def test_maximum_lengths_and_every_lookup_value(self):
        """Cases 8 and 9 cover every requests varchar and every enum value."""
        for name, column in self.schema["requests"].items():
            if column["limit"]:
                lengths = [len(row[name]) for row in self.rows if row[name] is not None]
                self.assertEqual(max(lengths), column["limit"], name)
        for field in (
            "req_cat_id",
            "req_for_id",
            "req_islead_id",
            "req_type_id",
            "req_priority_id",
            "req_status_id",
        ):
            target, key = self.schema["requests"][field]["reference"]
            self.assertEqual(
                {row[field] for row in self.rows},
                {row[key] for row in self.tables[target]},
            )
        self.assertEqual(
            [r["req_status"] for r in self.tables["request_statuses"]],
            [
                "CREATED",
                "MATCHING_VOLUNTEER",
                "IN_PROGRESS",
                "RESOLVED",
                "CANCELLED",
                "DELETED",
                "RATED_BY_REQUESTER",
                "RATED_BY_VOLUNTEER",
                "VOLUNTEER_NOT_FOUND",
            ],
        )

    def test_broken_reference_is_isolated_and_only_one_fk_is_broken(self):
        """Case 11 must not contaminate the loadable main fixtures."""
        self.assertNotIn(self.broken["req_id"], {row["req_id"] for row in self.rows})
        broken_columns = []
        for field, column in self.schema["requests"].items():
            if column["reference"] and self.broken[field] is not None:
                target, key = column["reference"]
                if self.broken[field] not in {row[key] for row in self.tables[target]}:
                    broken_columns.append(field)
        self.assertEqual(broken_columns, ["creator_id"])
        self.assertEqual({row["case_number"] for row in self.cases}, set(range(1, 12)))
        valid_ids = {row["req_id"] for row in self.rows}
        self.assertTrue(
            all(
                row["req_id"] in valid_ids
                for row in self.cases
                if row["case_number"] != 11
            )
        )

    def test_reproducibility_across_processes_and_seed_changes(self):
        """Check actual file bytes under distinct Python hash seeds."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, hash_seed in (("first", "1"), ("second", "99")):
                env = dict(os.environ, PYTHONHASHSEED=hash_seed)
                subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(HERE / "generate_requests_mock_data.py"),
                        "--output-dir",
                        str(root / name),
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    check=True,
                )
            first = {
                str(p.relative_to(root / "first")): p.read_bytes()
                for p in (root / "first").rglob("*")
                if p.is_file()
            }
            second = {
                str(p.relative_to(root / "second")): p.read_bytes()
                for p in (root / "second").rglob("*")
                if p.is_file()
            }
            self.assertEqual(first, second)
            for relative, content in first.items():
                self.assertEqual(
                    (GEN.DEFAULT_OUTPUT / relative).read_bytes(), content, relative
                )
            GEN.generate(root / "other_seed", seed=17)
            self.assertNotEqual(
                first["requests.csv"], (root / "other_seed/requests.csv").read_bytes()
            )

    def test_custom_sizes_keep_cases_and_do_not_remove_unrelated_files(self):
        """Small supported datasets retain coverage; output cleanup is explicit."""
        for size in (50, 101):
            tables, _, _, _ = GEN.build_dataset(seed=7, row_count=size)
            GEN.validate_dataset(tables, self.schema)
            self.assertEqual(len(tables["requests"]), size)
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "unrelated.csv"
            sentinel.write_text("keep me", encoding="utf-8")
            GEN.generate(directory)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
            manifest = json.loads((Path(directory) / "manifest.json").read_text())
            self.assertEqual(
                manifest["null_counts"],
                {"lead_volunteer_id": 493, "beneficiary_id": 18},
            )
        with self.assertRaises(ValueError):
            GEN.build_dataset(row_count=49)


if __name__ == "__main__":
    unittest.main()
