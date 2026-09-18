from __future__ import annotations

import unittest

from service.app import ConfigError, build_command, normalize_config


def benchmark_payload() -> dict[str, object]:
    return {
        "mode": "benchmark",
        "kubeconfig": "",
        "namespace": "default",
        "timeout": 3600,
        "image_url": "https://example.test/image.qcow2",
        "image_storage_class": "lvm-95-thin",
        "data_storage_class": "lvm-95-thin",
        "cpu": 2,
        "memory": "4Gi",
        "boot_size_gib": 50,
        "network_name": "default/mgmt-vlan",
        "guest_interface_name": "enp1s0",
        "node_selector": "network.harvesterhci.io/mgmt=true",
        "ssh_user": "ubuntu",
        "ssh_timeout": 900,
        "ssh_identity_file": "",
        "guest_device": "/dev/vdb",
        "benchmark_profile": "standard",
        "benchmark_engines": "restic,kopia",
        "benchmark_sizes_gib": "10,20,30",
        "benchmark_paired_runs": 5,
        "benchmark_suites": "capacity,sensitivity,incremental,cache",
        "benchmark_seed": "seed",
        "benchmark_snapshot_class": "lvm-snapshot",
        "benchmark_include_fault_recovery": False,
        "benchmark_fault_runs": 3,
        "benchmark_fault_progress_percent": 30,
        "benchmark_s3_pause_seconds": 60,
        "benchmark_cooldown": 60,
        "benchmark_poll_interval": 1,
        "benchmark_output_dir": "results",
        "benchmark_keep_backups": True,
        "benchmark_dry_run": False,
    }


class BenchmarkServiceTests(unittest.TestCase):
    def test_cleanup_all_includes_both_run_types(self) -> None:
        config = normalize_config({
            "mode": "cleanup_all",
            "confirm_cleanup": True,
            "namespace": "default",
        })
        self.assertIn("harvester-csi-stress-test", build_command(config)[0])
        self.assertIn("--cleanup", build_command(config))

    def test_cleanup_run_uses_benchmark_command(self) -> None:
        config = normalize_config({
            "mode": "cleanup_run",
            "cleanup_run_id": "vmbr-20260917104123-924d0ccc",
            "kubeconfig": "",
            "namespace": "default",
        })
        command = build_command(config)
        self.assertIn("harvester-vmbackup-benchmark", command[0])
        self.assertIn("--cleanup-run", command)
        self.assertEqual(command[command.index("--kubeconfig") + 1], "")

    def test_benchmark_command_defaults_to_no_faults_and_retains_backups(self) -> None:
        config = normalize_config(benchmark_payload())
        command = build_command(config)
        self.assertIn("harvester-vmbackup-benchmark", command[0])
        self.assertNotIn("--include-fault-recovery", command)
        self.assertNotIn("--delete-backups", command)
        self.assertEqual(command[command.index("--paired-runs") + 1], "5")

    def test_benchmark_optional_flags(self) -> None:
        payload = benchmark_payload()
        payload["benchmark_include_fault_recovery"] = True
        payload["benchmark_keep_backups"] = False
        payload["benchmark_dry_run"] = True
        command = build_command(normalize_config(payload))
        self.assertIn("--include-fault-recovery", command)
        self.assertIn("--delete-backups", command)
        self.assertIn("--dry-run", command)

    def test_invalid_sizes_are_rejected(self) -> None:
        payload = benchmark_payload()
        payload["benchmark_sizes_gib"] = "10,nope"
        with self.assertRaises(ConfigError):
            normalize_config(payload)

    def test_invalid_fault_progress_is_rejected(self) -> None:
        payload = benchmark_payload()
        payload["benchmark_fault_progress_percent"] = 100
        with self.assertRaises(ConfigError):
            normalize_config(payload)


if __name__ == "__main__":
    unittest.main()
