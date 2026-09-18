from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from benchmark.model import BenchmarkConfig, build_matrix, campaign_seed, matrix_summary, parse_sizes
from benchmark.report import describe, generate_report
from benchmark.runner import Cluster, CommandError, parse_prometheus


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


class ClusterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = Cluster(BenchmarkConfig(dry_run=True), Path("."), lambda _: None)

    @patch("benchmark.runner.time.sleep")
    @patch("benchmark.runner.subprocess.run")
    def test_wait_for_pvc_retries_not_found(self, run, sleep) -> None:
        run.side_effect = [
            CompletedProcess([], 1, "", 'Error from server (NotFound): PVC "p0" not found'),
            CompletedProcess([], 0, '{"status":{"phase":"Bound"}}', ""),
        ]
        pvc = self.cluster.wait_for(
            "persistentvolumeclaim", "p0",
            lambda value: value.get("status", {}).get("phase") == "Bound",
        )
        self.assertEqual(pvc["status"]["phase"], "Bound")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()

    @patch("benchmark.runner.subprocess.run")
    def test_get_keeps_other_kubectl_errors(self, run) -> None:
        run.return_value = CompletedProcess([], 1, "", "Error from server (Forbidden): denied")
        with self.assertRaisesRegex(CommandError, "Forbidden"):
            self.cluster.get("persistentvolumeclaim", "p0", check=False)

    @patch("benchmark.runner.subprocess.run")
    def test_json_ignores_stderr_warning(self, run) -> None:
        run.return_value = CompletedProcess([], 0, '{"kind":"PersistentVolumeClaim"}', "warning")
        self.assertEqual(
            self.cluster.get("persistentvolumeclaim", "p0")["kind"],
            "PersistentVolumeClaim",
        )


class CleanupTests(unittest.TestCase):
    def test_cleanup_all_selects_and_verifies_benchmark_backups(self) -> None:
        script = runpy.run_path(str(Path(__file__).parents[1] / "scripts/harvester-vmbackup-benchmark"))
        arguments = script["parser"]().parse_args(["--cleanup-all", "--namespace", "default"])
        calls = []
        deleted = False

        def kubectl(command, **kwargs):
            nonlocal deleted
            calls.append(command)
            if command[1] == "delete":
                deleted = True
                return CompletedProcess(command, 0, "", "")
            if command[2] == "virtualmachinebackups.harvesterhci.io" and not deleted:
                return CompletedProcess(command, 0, "virtualmachinebackup.harvesterhci.io/b1\n", "")
            return CompletedProcess(command, 0, "", "")

        with patch("subprocess.run", side_effect=kubectl):
            self.assertEqual(script["cleanup_resources"](arguments), 0)
        deletes = [command for command in calls if command[1] == "delete"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][deletes[0].index("-l") + 1], "benchmark.harvesterhci.io/run")

    def test_cleanup_reports_resources_that_remain(self) -> None:
        script = runpy.run_path(str(Path(__file__).parents[1] / "scripts/harvester-vmbackup-benchmark"))
        arguments = script["parser"]().parse_args(["--cleanup-all"])

        def kubectl(command, **kwargs):
            if command[2] == "virtualmachinebackups.harvesterhci.io":
                return CompletedProcess(command, 0, "virtualmachinebackup.harvesterhci.io/b1\n", "")
            return CompletedProcess(command, 0, "", "")

        with patch("subprocess.run", side_effect=kubectl):
            self.assertEqual(script["cleanup_resources"](arguments), 1)


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
