from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmark.model import BenchmarkConfig, build_matrix, campaign_seed, matrix_summary, parse_sizes
from benchmark.report import describe, generate_report
from benchmark.runner import parse_prometheus


class MatrixTests(unittest.TestCase):
    def test_standard_matrix_default_counts(self) -> None:
        config = BenchmarkConfig(dry_run=True)
        campaigns = build_matrix(config)
        self.assertEqual(
            matrix_summary(campaigns),
            {
                "campaigns": 37,
                "operations": 234,
                "fault_cases": 0,
                "campaigns_by_suite": {
                    "warmup": 2,
                    "capacity": 15,
                    "sensitivity": 10,
                    "incremental": 5,
                    "cache": 5,
                },
            },
        )

    def test_fault_matrix_uses_independent_count(self) -> None:
        config = BenchmarkConfig(dry_run=True, paired_runs=2, fault_runs=3, include_fault_recovery=True)
        campaigns = build_matrix(config)
        summary = matrix_summary(campaigns)
        self.assertEqual(summary["fault_cases"], 24)
        self.assertEqual(summary["campaigns_by_suite"]["failure"], 24)

    def test_smoke_ignores_standard_repetition_and_fault_settings(self) -> None:
        campaigns = build_matrix(
            BenchmarkConfig(profile="smoke", dry_run=True, paired_runs=20, include_fault_recovery=True)
        )
        self.assertEqual(len(campaigns), 1)
        self.assertEqual(matrix_summary(campaigns)["operations"], 4)

    def test_engine_order_alternates(self) -> None:
        campaigns = [item for item in build_matrix(BenchmarkConfig(dry_run=True, paired_runs=2)) if item.suite == "capacity"]
        self.assertEqual(campaigns[0].engine_order(), ("restic", "kopia"))
        self.assertEqual(campaigns[1].engine_order(), ("kopia", "restic"))

    def test_seed_is_stable_and_generation_specific(self) -> None:
        campaign = build_matrix(BenchmarkConfig(profile="smoke", dry_run=True))[0]
        first = campaign_seed(BenchmarkConfig(profile="smoke", dry_run=True), campaign, campaign.generations[0])
        second = campaign_seed(BenchmarkConfig(profile="smoke", dry_run=True), campaign, campaign.generations[0])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_sizes_reject_duplicates_and_invalid_values(self) -> None:
        self.assertEqual(parse_sizes("10,20,30"), (10, 20, 30))
        with self.assertRaises(ValueError):
            parse_sizes("10,10")
        with self.assertRaises(ValueError):
            parse_sizes("10,nope")


class MetricTests(unittest.TestCase):
    def test_prometheus_parser(self) -> None:
        values = parse_prometheus(
            'container_cpu_usage_seconds_total{container="backup",cpu="total",namespace="default",pod="p"} 12.5 1\n'
        )
        self.assertEqual(values[0][0], "container_cpu_usage_seconds_total")
        self.assertEqual(values[0][1]["container"], "backup")
        self.assertEqual(values[0][2], 12.5)

    def test_describe(self) -> None:
        result = describe([1, 2, 3, 4, 5])
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["median"], 3)
        self.assertEqual(result["min"], 1)
        self.assertEqual(result["max"], 5)

    def test_report_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary = generate_report(
                output,
                {"run_id": "vmbr-test", "include_fault_recovery": False},
                {"captured_at": "2026-09-09T00:00:00Z"},
                [],
                [],
            )
            self.assertEqual(summary["operation_count"], 0)
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "report.md").is_file())
            self.assertTrue((output / "charts" / "duration.svg").is_file())


if __name__ == "__main__":
    unittest.main()

