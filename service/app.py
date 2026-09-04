#!/usr/bin/env python3
"""Local web service for the Harvester CSI stress-test script."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "harvester-csi-stress-test"
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BYTES = 64 * 1024
MAX_LOG_ENTRIES = 20_000

MODES = [
    {
        "value": "stress",
        "label": "Stress test",
        "description": "Create VMs, back up and restore them, then verify disk data.",
    },
    {
        "value": "cleanup_run",
        "label": "Clean one run",
        "description": "Delete resources associated with one stress-test run ID.",
    },
    {
        "value": "cleanup_all",
        "label": "Clean all runs",
        "description": "Delete all detected stress-test resources in the namespace.",
    },
]

PARAMETERS = [
    {
        "key": "kubeconfig",
        "label": "Kubeconfig",
        "type": "text",
        "default": os.environ.get("KUBECONFIG", ""),
        "group": "Cluster connection",
        "modes": ["stress", "cleanup_run", "cleanup_all"],
        "placeholder": "/absolute/path/to/kubeconfig.yaml",
        "help": "Defaults to the KUBECONFIG inherited by this service. Leave empty for Kubernetes' standard lookup.",
    },
    {
        "key": "namespace",
        "label": "Namespace",
        "type": "text",
        "default": "default",
        "required": True,
        "group": "Cluster connection",
        "modes": ["stress", "cleanup_run", "cleanup_all"],
        "help": "Namespace in which resources are created or cleaned.",
    },
    {
        "key": "timeout",
        "label": "Operation timeout",
        "type": "number",
        "default": 3600,
        "min": 1,
        "suffix": "seconds",
        "required": True,
        "group": "Cluster connection",
        "modes": ["stress", "cleanup_run", "cleanup_all"],
        "help": (
            "Maximum wait for each asynchronous cluster operation and cleanup action; "
            "guest SSH uses its own timeout."
        ),
    },
    {
        "key": "image_url",
        "label": "Image URL",
        "type": "url",
        "default": "",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
        "placeholder": "https://example.com/cloud-image.qcow2",
        "help": "Source URL for the Harvester VirtualMachineImage.",
    },
    {
        "key": "image_storage_class",
        "label": "Image storage class",
        "type": "text",
        "default": "",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
        "placeholder": "image-storage-class",
        "help": "StorageClass used for the Harvester VM image and generated boot PVCs.",
    },
    {
        "key": "data_storage_class",
        "label": "Data storage class",
        "type": "text",
        "default": "",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
        "placeholder": "data-storage-class",
        "help": "StorageClass for generated data PVCs.",
    },
    {
        "key": "cpu",
        "label": "vCPU count",
        "type": "number",
        "default": 2,
        "min": 1,
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "memory",
        "label": "Memory",
        "type": "text",
        "default": "4Gi",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
        "placeholder": "4Gi",
        "help": "Kubernetes memory quantity assigned to each VM.",
    },
    {
        "key": "boot_size_gib",
        "label": "Boot PVC capacity",
        "type": "number",
        "default": 50,
        "min": 1,
        "suffix": "GiB",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "data_volume_size_gib",
        "label": "Data PVC capacity",
        "type": "number",
        "default": 100,
        "min": 1,
        "suffix": "GiB",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "boot_volume_mode",
        "label": "Boot volume mode",
        "type": "select",
        "options": ["Block", "Filesystem"],
        "default": "Block",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "data_volume_mode",
        "label": "Data volume mode",
        "type": "select",
        "options": ["Filesystem", "Block"],
        "default": "Filesystem",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "boot_access_mode",
        "label": "Boot access mode",
        "type": "select",
        "options": ["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany", "ReadWriteOncePod"],
        "default": "ReadWriteOnce",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "data_access_mode",
        "label": "Data access mode",
        "type": "select",
        "options": ["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany", "ReadWriteOncePod"],
        "default": "ReadWriteOnce",
        "required": True,
        "group": "VM and storage",
        "modes": ["stress"],
    },
    {
        "key": "network_name",
        "label": "Multus network",
        "type": "text",
        "default": "default/mgmt-vlan",
        "required": True,
        "group": "VM network",
        "modes": ["stress"],
        "placeholder": "namespace/network-name",
        "help": "NetworkAttachmentDefinition used by each VM.",
    },
    {
        "key": "guest_interface_name",
        "label": "Guest interface",
        "type": "text",
        "default": "enp1s0",
        "required": True,
        "group": "VM network",
        "modes": ["stress"],
        "help": "Linux network interface configured for DHCP by cloud-init.",
    },
    {
        "key": "node_selector",
        "label": "Required node selector",
        "type": "text",
        "default": "network.harvesterhci.io/mgmt=true",
        "group": "VM network",
        "modes": ["stress"],
        "placeholder": "KEY=VALUE (empty disables affinity)",
        "help": "Restricts VM scheduling to matching nodes. Clear this field to disable the selector.",
    },
    {
        "key": "iterations",
        "label": "Iterations per worker",
        "type": "number",
        "default": 1,
        "min": 1,
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
    },
    {
        "key": "parallel",
        "label": "Parallel workers",
        "type": "number",
        "default": 1,
        "min": 1,
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
        "help": "Workers run concurrently; iterations within one worker run sequentially.",
    },
    {
        "key": "vm_backup_types",
        "label": "VM backup type(s)",
        "type": "select",
        "options": [
            "snapshot",
            "backup",
            "restic",
            "kopia",
            "snapshot,backup",
            "snapshot,restic",
            "snapshot,kopia",
            "backup,restic",
            "backup,kopia",
            "restic,kopia",
        ],
        "default": "snapshot",
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
        "help": (
            "Use one type for every iteration, or a comma-separated pair to alternate "
            "between them. Remote types require a working Harvester backup target."
        ),
    },
    {
        "key": "data_size_gib",
        "label": "Data-disk payload",
        "type": "number",
        "default": 10,
        "min": 1,
        "suffix": "GiB",
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
    },
    {
        "key": "root_data_size_gib",
        "label": "Root-disk payload",
        "type": "number",
        "default": 1,
        "min": 1,
        "suffix": "GiB",
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
    },
    {
        "key": "poll_interval",
        "label": "Polling interval",
        "type": "number",
        "default": 5,
        "min": 1,
        "suffix": "seconds",
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
    },
    {
        "key": "snapshot_error_grace",
        "label": "VM backup error grace",
        "type": "number",
        "default": 60,
        "min": 0,
        "suffix": "seconds",
        "required": True,
        "group": "Workload",
        "modes": ["stress"],
        "help": (
            "Continue waiting after a VM backup reports an error so controller "
            "retries can recover. Set to 0 for fail-fast behavior."
        ),
    },
    {
        "key": "ssh_user",
        "label": "SSH user",
        "type": "text",
        "default": "ubuntu",
        "required": True,
        "group": "Guest access",
        "modes": ["stress"],
    },
    {
        "key": "ssh_timeout",
        "label": "SSH timeout",
        "type": "number",
        "default": 900,
        "min": 1,
        "suffix": "seconds",
        "required": True,
        "group": "Guest access",
        "modes": ["stress"],
        "help": (
            "Total time allowed for guest SSH readiness and for retrying transient "
            "SSH or virtctl failures during guest payload writes and restored checksums."
        ),
    },
    {
        "key": "ssh_connect_timeout",
        "label": "SSH connect timeout",
        "type": "number",
        "default": 30,
        "min": 1,
        "suffix": "seconds",
        "required": True,
        "group": "Guest access",
        "modes": ["stress"],
        "help": "Time allowed for each SSH connection and banner exchange.",
    },
    {
        "key": "ssh_vm_retries",
        "label": "Fresh VM retries",
        "type": "number",
        "default": 0,
        "min": 0,
        "required": True,
        "group": "Guest access",
        "modes": ["stress"],
        "help": (
            "Additional full iteration attempts with newly created VMs after source or "
            "restored guest SSH readiness times out."
        ),
    },
    {
        "key": "ssh_identity_file",
        "label": "SSH identity file",
        "type": "text",
        "default": "",
        "group": "Guest access",
        "modes": ["stress"],
        "placeholder": "Generated automatically when empty",
        "help": "Optional local path to an existing unencrypted private key.",
    },
    {
        "key": "guest_device",
        "label": "Guest data device",
        "type": "text",
        "default": "/dev/vdb",
        "required": True,
        "group": "Guest access",
        "modes": ["stress"],
    },
    {
        "key": "guest_mount_path",
        "label": "Guest mount path",
        "type": "text",
        "default": "/mnt/csi-stress",
        "required": True,
        "group": "Guest access",
        "modes": ["stress"],
    },
    {
        "key": "keep_resources",
        "label": "Keep resources after the run",
        "type": "checkbox",
        "default": False,
        "group": "Guest access",
        "modes": ["stress"],
        "help": "Retain generated resources for debugging instead of deleting them.",
    },
    {
        "key": "keep_failed_resources",
        "label": "Keep only failed iteration resources",
        "type": "checkbox",
        "default": False,
        "group": "Guest access",
        "modes": ["stress"],
        "help": "Retain a failed iteration and the shared image for diagnosis while still cleaning successful iterations.",
    },
    {
        "key": "cleanup_run_id",
        "label": "Run ID",
        "type": "text",
        "default": "",
        "required": True,
        "group": "Cleanup target",
        "modes": ["cleanup_run"],
        "placeholder": "csi-stress-YYYYMMDDHHMMSS-NNNNN",
        "help": "The run ID printed near the beginning of a stress-test log.",
    },
]

PARAMETER_BY_KEY = {parameter["key"]: parameter for parameter in PARAMETERS}
MODE_VALUES = {mode["value"] for mode in MODES}
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SCRIPT_LOG = re.compile(
    r"^\[(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\] "
    r"(?:\[(?P<context>[^]]+)\] )?(?P<message>.*)$"
)
RUN_ID_LOG = re.compile(r"^Run ID: (?P<run_id>[a-z0-9.-]+)$")
TOTAL_LOG = re.compile(r"\((?P<total>\d+) total\)")
RUN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")


class ConfigError(ValueError):
    """Raised when a submitted job configuration is invalid."""


class JobConflict(RuntimeError):
    """Raised when a job is already running."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def classify_log(message: str, stream: str) -> str:
    lowered = message.lower()
    if stream == "system":
        return "system"
    if "error" in lowered or "failure" in lowered or "failed" in lowered or "timed out" in lowered:
        return "error"
    if "warning" in lowered:
        return "warning"
    if "checksum" in lowered:
        return "checksum"
    if (
        "passed" in lowered
        or "ready:" in lowered
        or "successfully" in lowered
        or lowered.startswith("completed")
    ):
        return "success"
    activity_prefixes = (
        "waiting",
        "still waiting",
        "creating",
        "starting",
        "writing",
        "reading",
        "restoring",
        "retrying",
        "stopping",
        "cleaning",
    )
    if lowered.startswith(activity_prefixes):
        return "activity"
    return "info"


def validate_kubeconfig(value: str) -> str:
    if not value:
        return ""
    paths = []
    for raw_path in value.split(os.pathsep):
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"Kubeconfig file not found: {path}")
        paths.append(str(path.resolve()))
    if not paths:
        raise ConfigError("Kubeconfig must contain at least one file path")
    return os.pathsep.join(paths)


def normalize_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigError("Request body must be a JSON object")

    mode = payload.get("mode", "stress")
    if mode not in MODE_VALUES:
        raise ConfigError(f"Unsupported operation mode: {mode!r}")

    allowed_keys = set(PARAMETER_BY_KEY) | {"mode", "confirm_cleanup"}
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise ConfigError(f"Unknown parameter(s): {', '.join(unknown_keys)}")

    config: dict[str, Any] = {"mode": mode}
    for parameter in PARAMETERS:
        key = parameter["key"]
        value = payload.get(key, parameter["default"])
        active = mode in parameter["modes"]

        if parameter["type"] == "checkbox":
            if not isinstance(value, bool):
                raise ConfigError(f"{parameter['label']} must be true or false")
            config[key] = value
            continue

        if parameter["type"] == "number":
            if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value).strip()):
                integer_kind = "non-negative" if parameter.get("min", 1) == 0 else "positive"
                raise ConfigError(f"{parameter['label']} must be a {integer_kind} integer")
            number = int(value)
            if number < parameter.get("min", 1):
                raise ConfigError(f"{parameter['label']} must be at least {parameter.get('min', 1)}")
            config[key] = number
            continue

        if not isinstance(value, str):
            raise ConfigError(f"{parameter['label']} must be text")
        value = value.strip()
        if active and parameter.get("required") and not value:
            raise ConfigError(f"{parameter['label']} is required")
        config[key] = value

    config["kubeconfig"] = validate_kubeconfig(config["kubeconfig"])

    if mode == "stress":
        if config["keep_resources"] and config["keep_failed_resources"]:
            raise ConfigError("Keep resources and keep only failed iteration resources cannot both be enabled")
        for key in (
            "boot_volume_mode",
            "data_volume_mode",
            "boot_access_mode",
            "data_access_mode",
            "vm_backup_types",
        ):
            options = PARAMETER_BY_KEY[key]["options"]
            if config[key] not in options:
                raise ConfigError(f"{PARAMETER_BY_KEY[key]['label']} must be one of: {', '.join(options)}")
        if config["node_selector"]:
            selector_key, separator, selector_value = config["node_selector"].partition("=")
            if not separator or not selector_key or not selector_value:
                raise ConfigError("Required node selector must use KEY=VALUE syntax")
        if config["root_data_size_gib"] >= config["boot_size_gib"]:
            raise ConfigError("Root-disk payload must be smaller than boot PVC capacity")
        if config["data_size_gib"] >= config["data_volume_size_gib"]:
            raise ConfigError("Data-disk payload must be smaller than data PVC capacity")
        if config["ssh_identity_file"]:
            identity = Path(config["ssh_identity_file"]).expanduser()
            if not identity.is_file():
                raise ConfigError(f"SSH identity file not found: {identity}")
            config["ssh_identity_file"] = str(identity.resolve())
    elif mode == "cleanup_run":
        run_id = config["cleanup_run_id"]
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ConfigError("Run ID is not a valid Kubernetes label value")
    elif payload.get("confirm_cleanup") is not True:
        raise ConfigError("Cleaning all runs requires explicit confirmation")

    return config


def build_command(config: dict[str, Any]) -> list[str]:
    command = [str(SCRIPT_PATH)]
    mode = config["mode"]

    if mode == "cleanup_all":
        command.append("--cleanup")
    elif mode == "cleanup_run":
        command.extend(["--cleanup-run", config["cleanup_run_id"]])
    else:
        argument_map = [
            ("--image-url", "image_url"),
            ("--image-storage-class", "image_storage_class"),
            ("--data-storage-class", "data_storage_class"),
            ("--cpu", "cpu"),
            ("--memory", "memory"),
            ("--boot-size-gib", "boot_size_gib"),
            ("--data-volume-size-gib", "data_volume_size_gib"),
            ("--boot-volume-mode", "boot_volume_mode"),
            ("--data-volume-mode", "data_volume_mode"),
            ("--boot-access-mode", "boot_access_mode"),
            ("--data-access-mode", "data_access_mode"),
            ("--network-name", "network_name"),
            ("--guest-interface-name", "guest_interface_name"),
            ("--node-selector", "node_selector"),
            ("--iterations", "iterations"),
            ("--parallel", "parallel"),
            ("--vm-backup-types", "vm_backup_types"),
            ("--data-size-gib", "data_size_gib"),
            ("--root-data-size-gib", "root_data_size_gib"),
            ("--poll-interval", "poll_interval"),
            ("--snapshot-error-grace", "snapshot_error_grace"),
            ("--ssh-user", "ssh_user"),
            ("--ssh-timeout", "ssh_timeout"),
            ("--ssh-connect-timeout", "ssh_connect_timeout"),
            ("--ssh-vm-retries", "ssh_vm_retries"),
            ("--guest-device", "guest_device"),
            ("--guest-mount-path", "guest_mount_path"),
        ]
        for flag, key in argument_map:
            command.extend([flag, str(config[key])])
        if config["ssh_identity_file"]:
            command.extend(["--ssh-identity-file", config["ssh_identity_file"]])
        if config["keep_resources"]:
            command.append("--keep-resources")
        if config["keep_failed_resources"]:
            command.append("--keep-failed-resources")

    command.extend(["--namespace", config["namespace"], "--timeout", str(config["timeout"])])
    return command


class Job:
    def __init__(self, config: dict[str, Any], command: list[str]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.config = config
        self.command = command
        self.command_display = shlex.join(command)
        if config["kubeconfig"]:
            self.command_display = f"KUBECONFIG={shlex.quote(config['kubeconfig'])} {self.command_display}"
        self.status = "starting"
        self.started_at = utc_now()
        self.ended_at: str | None = None
        self.exit_code: int | None = None
        self.stop_requested = False
        self.process: subprocess.Popen[str] | None = None
        self.logs: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_ENTRIES)
        self.next_sequence = 1
        self.run_id = ""
        self.total_iterations = 0
        self.completed_iterations = 0
        self.last_message = "Preparing process"
        self._lock = threading.RLock()

    def append_log(self, raw_message: str, stream: str = "process") -> None:
        clean = ANSI_ESCAPE.sub("", raw_message).rstrip("\r\n")
        if not clean:
            return

        timestamp = utc_now()
        context = "service" if stream == "system" else ""
        message = clean
        match = SCRIPT_LOG.match(clean)
        if match:
            timestamp = match.group("timestamp")
            context = match.group("context") or "main"
            message = match.group("message")

        level = classify_log(message, stream)
        with self._lock:
            entry = {
                "sequence": self.next_sequence,
                "timestamp": timestamp,
                "context": context,
                "level": level,
                "message": message,
            }
            self.next_sequence += 1
            self.logs.append(entry)
            self.last_message = message

            run_match = RUN_ID_LOG.match(message)
            if run_match:
                self.run_id = run_match.group("run_id")
            if message.startswith("Starting "):
                total_match = TOTAL_LOG.search(message)
                if total_match:
                    self.total_iterations = int(total_match.group("total"))
            if message.startswith("Iteration ") and " passed " in message:
                self.completed_iterations += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "mode": self.config["mode"],
                "config": dict(self.config),
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "exit_code": self.exit_code,
                "run_id": self.run_id,
                "total_iterations": self.total_iterations,
                "completed_iterations": self.completed_iterations,
                "last_message": self.last_message,
                "command": self.command_display,
            }

    def log_snapshot(self, after: int) -> list[dict[str, Any]]:
        with self._lock:
            return [entry for entry in self.logs if entry["sequence"] > after]


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.job_order: deque[str] = deque(maxlen=20)
        self._lock = threading.RLock()

    def active_job(self) -> Job | None:
        with self._lock:
            for job_id in reversed(self.job_order):
                job = self.jobs[job_id]
                if job.status in {"starting", "running", "stopping"}:
                    return job
        return None

    def start(self, config: dict[str, Any]) -> Job:
        command = build_command(config)
        with self._lock:
            active = self.active_job()
            if active:
                raise JobConflict(f"Job {active.id} is already {active.status}")

            job = Job(config, command)
            self.jobs[job.id] = job
            self.job_order.append(job.id)
            environment = os.environ.copy()
            if config["kubeconfig"]:
                environment["KUBECONFIG"] = config["kubeconfig"]
            else:
                environment.pop("KUBECONFIG", None)

            job.append_log(f"Launching: {job.command_display}", "system")
            try:
                job.process = subprocess.Popen(
                    command,
                    cwd=ROOT_DIR,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as error:
                job.status = "failed"
                job.ended_at = utc_now()
                job.exit_code = 127
                job.append_log(f"Unable to start process: {error}", "system")
                return job

            job.status = "running"
            job.append_log(f"Process started with PID {job.process.pid}", "system")
            thread = threading.Thread(target=self._consume_output, args=(job,), name=f"job-{job.id}")
            thread.start()
            return job

    def _consume_output(self, job: Job) -> None:
        assert job.process is not None
        assert job.process.stdout is not None
        for line in job.process.stdout:
            job.append_log(line)
        exit_code = job.process.wait()
        with job._lock:
            job.exit_code = exit_code
            job.ended_at = utc_now()
            if job.stop_requested:
                job.status = "stopped"
                summary = f"Run stopped; process exited with code {exit_code}"
            elif exit_code == 0:
                job.status = "succeeded"
                summary = "Run completed successfully"
            else:
                job.status = "failed"
                summary = f"Run failed with exit code {exit_code}"
        job.append_log(summary, "system")

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.jobs[job_id].snapshot() for job_id in reversed(self.job_order)]

    def stop(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with job._lock:
            if job.status not in {"starting", "running"} or job.process is None:
                return job
            job.stop_requested = True
            job.status = "stopping"
            job.append_log("Stop requested; allowing the script to clean up resources", "system")
            try:
                os.killpg(job.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return job

    def stop_active(self) -> Job | None:
        active = self.active_job()
        if active:
            return self.stop(active.id)
        return None

    def wait_for_active(self) -> None:
        active = self.active_job()
        if active and active.process:
            active.process.wait()


class StressHTTPServer(ThreadingHTTPServer):
    manager: JobManager


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "HarvesterStressGUI/1.0"

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _send_error_json(self, message: str, status: HTTPStatus) -> None:
        self._send_json({"error": message}, status)

    def _valid_local_request(self) -> bool:
        host = self.headers.get("Host", "")
        hostname = host.rsplit(":", 1)[0].strip("[]").lower()
        if hostname not in {"127.0.0.1", "localhost"}:
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            origin_port = parsed.port or 80
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or origin_port != self.server.server_port
            ):
                return False
        return True

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ConfigError("Invalid Content-Length") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ConfigError("Request body is empty or too large")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ConfigError("Request body is not valid JSON") from error

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._valid_local_request():
            self._send_error_json("Only local requests are accepted", HTTPStatus.FORBIDDEN)
            return

        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            self._send_json({"modes": MODES, "parameters": PARAMETERS})
            return
        if parsed.path == "/api/jobs":
            self._send_json({"jobs": self.server.manager.list()})
            return

        job_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})(?:/(logs))?", parsed.path)
        if job_match:
            job = self.server.manager.get(job_match.group(1))
            if job is None:
                self._send_error_json("Job not found", HTTPStatus.NOT_FOUND)
                return
            if job_match.group(2) == "logs":
                values = parse_qs(parsed.query)
                try:
                    after = max(0, int(values.get("after", ["0"])[0]))
                except ValueError:
                    self._send_error_json("The after value must be an integer", HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({"job": job.snapshot(), "logs": job.log_snapshot(after)})
            else:
                self._send_json({"job": job.snapshot()})
            return

        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        static_file = static_files.get(parsed.path)
        if static_file:
            path = STATIC_DIR / static_file[0]
            try:
                data = path.read_bytes()
            except OSError:
                self._send_error_json("Static asset not found", HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", static_file[1])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)
            return

        self._send_error_json("Not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._valid_local_request():
            self._send_error_json("Only local requests are accepted", HTTPStatus.FORBIDDEN)
            return

        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            try:
                config = normalize_config(self._read_json())
                job = self.server.manager.start(config)
            except ConfigError as error:
                self._send_error_json(str(error), HTTPStatus.BAD_REQUEST)
                return
            except JobConflict as error:
                self._send_error_json(str(error), HTTPStatus.CONFLICT)
                return
            self._send_json({"job": job.snapshot()}, HTTPStatus.CREATED)
            return

        stop_match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/stop", parsed.path)
        if stop_match:
            try:
                job = self.server.manager.stop(stop_match.group(1))
            except KeyError:
                self._send_error_json("Job not found", HTTPStatus.NOT_FOUND)
                return
            self._send_json({"job": job.snapshot()})
            return

        self._send_error_json("Not found", HTTPStatus.NOT_FOUND)

    def log_message(self, message_format: str, *args: Any) -> None:
        message = message_format % args
        print(f"[{utc_now()}] HTTP {self.client_address[0]} {message}", flush=True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Harvester CSI stress-test web interface.")
    parser.add_argument("--port", type=int, default=7000, help="Local TCP port (default: 7000)")
    parser.add_argument("--open-browser", action="store_true", help="Open the interface in the default browser")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return arguments


def main() -> int:
    arguments = parse_arguments()
    if not SCRIPT_PATH.is_file():
        raise SystemExit(f"Stress-test script not found: {SCRIPT_PATH}")

    manager = JobManager()
    server = StressHTTPServer(("127.0.0.1", arguments.port), RequestHandler)
    server.manager = manager
    url = f"http://localhost:{arguments.port}"
    print(f"Harvester CSI Stress Test GUI is available at {url}", flush=True)
    print("Press Ctrl+C to stop the service.", flush=True)

    if arguments.open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping local service...", flush=True)
    finally:
        server.server_close()
        active = manager.stop_active()
        if active:
            print(f"Stopping active job {active.id} and waiting for cleanup...", flush=True)
            manager.wait_for_active()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
