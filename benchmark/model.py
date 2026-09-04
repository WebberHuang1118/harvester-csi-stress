"""Configuration and deterministic matrix expansion for the benchmark."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ENGINES = ("restic", "kopia")
NORMAL_SUITES = ("capacity", "sensitivity", "incremental", "cache")
FAULTS = ("pod-kill", "s3-pause")
OPERATIONS = ("backup", "restore")
GENERATION_CHANGES = (0, 0, 1, 10, 100)


def parse_csv(value: str, *, name: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError(f"{name} must contain at least one value")
    if len(set(items)) != len(items):
        raise ValueError(f"{name} must not contain duplicates")
    return items


def parse_sizes(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for item in parse_csv(value, name="sizes"):
        if not re.fullmatch(r"[1-9][0-9]*", item):
            raise ValueError(f"sizes must be positive integers, got {item!r}")
        result.append(int(item))
    return tuple(result)


@dataclass(frozen=True)
class Generation:
    name: str
    change_percent: int


@dataclass(frozen=True)
class Campaign:
    suite: str
    repetition: int
    size_gib: int
    pattern: str
    engines: tuple[str, ...]
    generations: tuple[Generation, ...] = (Generation("G0", 0),)
    restore_count: int = 1
    measured: bool = True
    fault: str = ""
    fault_operation: str = ""

    @property
    def key(self) -> str:
        parts = [self.suite, f"s{self.size_gib}", self.pattern, f"r{self.repetition}"]
        if self.fault:
            parts.extend((self.engines[0], self.fault_operation, self.fault))
        return "-".join(parts)

    def engine_order(self, generation_index: int = 0) -> tuple[str, ...]:
        if len(self.engines) < 2:
            return self.engines
        if (self.repetition + generation_index) % 2:
            return self.engines
        return tuple(reversed(self.engines))


@dataclass
class BenchmarkConfig:
    profile: str = "standard"
    kubeconfig: str = ""
    namespace: str = "default"
    image_url: str = ""
    image_storage_class: str = "lvm-95-thin"
    data_storage_class: str = "lvm-95-thin"
    snapshot_class: str = "lvm-snapshot"
    sizes_gib: tuple[int, ...] = (10, 20, 30)
    paired_runs: int = 5
    fault_runs: int = 3
    include_fault_recovery: bool = False
    suites: tuple[str, ...] = NORMAL_SUITES
    engines: tuple[str, ...] = ENGINES
    seed: str = "harvester-vmbackup-benchmark-v1"
    output_dir: str = "results"
    cpu: int = 2
    memory: str = "4Gi"
    boot_size_gib: int = 50
    network_name: str = "default/mgmt-vlan"
    guest_interface_name: str = "enp1s0"
    node_selector: str = "network.harvesterhci.io/mgmt=true"
    ssh_user: str = "ubuntu"
    ssh_identity_file: str = ""
    guest_device: str = "/dev/vdb"
    operation_timeout: int = 3600
    ssh_timeout: int = 900
    poll_interval: int = 1
    cooldown: int = 60
    s3_pause_seconds: int = 60
    fault_progress_percent: int = 30
    keep_backups: bool = True
    dry_run: bool = False

    def validate(self) -> None:
        if self.profile not in {"smoke", "standard", "custom"}:
            raise ValueError("profile must be smoke, standard, or custom")
        if not self.namespace or len(self.namespace) > 63:
            raise ValueError("namespace must be a non-empty Kubernetes name")
        if not self.image_url and not self.dry_run:
            raise ValueError("image URL is required unless --dry-run is used")
        for name in ("image_storage_class", "data_storage_class", "snapshot_class"):
            if not getattr(self, name):
                raise ValueError(f"{name.replace('_', ' ')} is required")
        for name in (
            "paired_runs",
            "fault_runs",
            "cpu",
            "boot_size_gib",
            "operation_timeout",
            "ssh_timeout",
            "poll_interval",
            "s3_pause_seconds",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name.replace('_', ' ')} must be positive")
        if self.cooldown < 0:
            raise ValueError("cooldown must be non-negative")
        if not 1 <= self.fault_progress_percent <= 99:
            raise ValueError("fault progress percent must be between 1 and 99")
        unknown_engines = sorted(set(self.engines) - set(ENGINES))
        if unknown_engines:
            raise ValueError(f"unsupported engines: {', '.join(unknown_engines)}")
        unknown_suites = sorted(set(self.suites) - set(NORMAL_SUITES))
        if unknown_suites:
            raise ValueError(f"unsupported suites: {', '.join(unknown_suites)}")
        if not self.engines:
            raise ValueError("at least one engine is required")
        if not self.sizes_gib or any(size < 1 for size in self.sizes_gib):
            raise ValueError("sizes must contain positive integers")
        if self.kubeconfig:
            for raw_path in self.kubeconfig.split(":"):
                if raw_path and not Path(raw_path).expanduser().is_file():
                    raise ValueError(f"kubeconfig file not found: {raw_path}")
        if self.ssh_identity_file and not Path(self.ssh_identity_file).expanduser().is_file():
            raise ValueError(f"SSH identity file not found: {self.ssh_identity_file}")

    def safe_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _normal_repetitions(config: BenchmarkConfig) -> int:
    return 1 if config.profile == "smoke" else config.paired_runs


def build_matrix(config: BenchmarkConfig) -> list[Campaign]:
    """Build a stable, fully expanded campaign matrix."""
    config.validate()
    campaigns: list[Campaign] = []

    if config.profile == "smoke":
        size = config.sizes_gib[0]
        campaigns.append(
            Campaign("smoke", 1, size, "mixed", config.engines, measured=True)
        )
        return campaigns

    # One excluded readiness/image-pull warm-up per engine.
    warmup_size = config.sizes_gib[0]
    for index, engine in enumerate(config.engines, start=1):
        campaigns.append(
            Campaign("warmup", index, warmup_size, "mixed", (engine,), measured=False)
        )

    repetitions = _normal_repetitions(config)
    if "capacity" in config.suites:
        for size in config.sizes_gib:
            for repetition in range(1, repetitions + 1):
                campaigns.append(Campaign("capacity", repetition, size, "mixed", config.engines))

    if "sensitivity" in config.suites:
        sensitivity_size = 20 if 20 in config.sizes_gib else config.sizes_gib[len(config.sizes_gib) // 2]
        for pattern in ("compressible", "random"):
            for repetition in range(1, repetitions + 1):
                campaigns.append(
                    Campaign("sensitivity", repetition, sensitivity_size, pattern, config.engines)
                )

    if "incremental" in config.suites:
        incremental_size = 30 if 30 in config.sizes_gib else max(config.sizes_gib)
        generations = tuple(
            Generation(f"G{index}", change)
            for index, change in enumerate(GENERATION_CHANGES)
        )
        for repetition in range(1, repetitions + 1):
            campaigns.append(
                Campaign(
                    "incremental",
                    repetition,
                    incremental_size,
                    "mixed",
                    config.engines,
                    generations=generations,
                )
            )

    if "cache" in config.suites:
        cache_size = 20 if 20 in config.sizes_gib else config.sizes_gib[len(config.sizes_gib) // 2]
        for repetition in range(1, repetitions + 1):
            campaigns.append(
                Campaign(
                    "cache",
                    repetition,
                    cache_size,
                    "mixed",
                    config.engines,
                    restore_count=2,
                )
            )

    if config.include_fault_recovery:
        fault_size = 20 if 20 in config.sizes_gib else config.sizes_gib[0]
        for engine in config.engines:
            for operation in OPERATIONS:
                for fault in FAULTS:
                    for repetition in range(1, config.fault_runs + 1):
                        campaigns.append(
                            Campaign(
                                "failure",
                                repetition,
                                fault_size,
                                "mixed",
                                (engine,),
                                fault=fault,
                                fault_operation=operation,
                            )
                        )
    return campaigns


def campaign_seed(config: BenchmarkConfig, campaign: Campaign, generation: Generation) -> str:
    source = ":".join(
        (
            config.seed,
            campaign.suite,
            str(campaign.size_gib),
            campaign.pattern,
            str(campaign.repetition),
            generation.name,
        )
    )
    return hashlib.sha256(source.encode()).hexdigest()


def matrix_summary(campaigns: Iterable[Campaign]) -> dict[str, Any]:
    campaigns = list(campaigns)
    operations = 0
    fault_cases = 0
    for campaign in campaigns:
        generations = len(campaign.generations)
        # One backup and N restores for every engine and generation.
        operations += generations * len(campaign.engines) * (1 + campaign.restore_count)
        if campaign.fault:
            fault_cases += 1
    suites: dict[str, int] = {}
    for campaign in campaigns:
        suites[campaign.suite] = suites.get(campaign.suite, 0) + 1
    return {
        "campaigns": len(campaigns),
        "operations": operations,
        "fault_cases": fault_cases,
        "campaigns_by_suite": suites,
    }


def matrix_as_json(config: BenchmarkConfig, campaigns: list[Campaign]) -> str:
    payload = {
        "config": config.safe_dict(),
        "summary": matrix_summary(campaigns),
        "campaigns": [dataclasses.asdict(campaign) | {"key": campaign.key} for campaign in campaigns],
    }
    return json.dumps(payload, indent=2, sort_keys=True)
