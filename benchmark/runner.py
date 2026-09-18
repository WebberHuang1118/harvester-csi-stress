"""Live Harvester VMBackup/VMRestore benchmark orchestration."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .model import BenchmarkConfig, Campaign, Generation, build_matrix, campaign_seed, matrix_as_json, matrix_summary
from .report import OPERATION_FIELDS, SAMPLE_FIELDS, generate_report, write_csv, write_json


RUN_LABEL = "benchmark.harvesterhci.io/run"
CAMPAIGN_LABEL = "benchmark.harvesterhci.io/campaign"
ENGINE_LABELS = {
    "restic": "harvesterhci.io/restic-job",
    "kopia": "harvesterhci.io/kopia-job",
}
OWNER_LABELS = {
    "backup": "harvesterhci.io/vm-backup-name",
    "restore": "harvesterhci.io/vm-restore-name",
}
BACKUP_TARGET_SECRET = "harvester-backup-target-secret"
BACKUP_TARGET_NAMESPACE = "longhorn-system"
UTC = dt.timezone.utc
METRIC_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{(?P<labels>.*)\} (?P<value>[-+0-9.eE]+)(?: [0-9]+)?$")
LABEL_VALUE = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')


class CommandError(RuntimeError):
    def __init__(self, command: list[str], returncode: int, output: str) -> None:
        super().__init__(f"command failed ({returncode}): {shlex.join(command)}\n{output[-2000:]}")
        self.command = command
        self.returncode = returncode
        self.output = output


class OperationFailed(RuntimeError):
    pass


class FaultNotInjected(OperationFailed):
    pass


def utc_now() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def resource_name(value: str, limit: int = 50) -> str:
    value = re.sub(r"[^a-z0-9.-]+", "-", value.lower().replace("_", "-"))
    value = value.strip("-.")[:limit].rstrip("-.")
    if not value:
        raise ValueError("resource name is empty after normalization")
    return value


class Cluster:
    def __init__(self, config: BenchmarkConfig, output_dir: Path, log: Callable[[str], None]) -> None:
        self.config = config
        self.output_dir = output_dir
        self.log = log
        self.environment = os.environ.copy()
        if config.kubeconfig:
            self.environment["KUBECONFIG"] = config.kubeconfig
        else:
            self.environment.pop("KUBECONFIG", None)

    def run(
        self,
        command: list[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        timeout: int | None = None,
    ) -> str:
        process = subprocess.run(
            command,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment,
            timeout=timeout,
            check=False,
        )
        if check and process.returncode != 0:
            raise CommandError(command, process.returncode, process.stdout)
        return process.stdout

    def kubectl(self, arguments: list[str], **kwargs: Any) -> str:
        return self.run(["kubectl", *arguments], **kwargs)

    def json(self, arguments: list[str], *, check: bool = True) -> dict[str, Any]:
        command = ["kubectl", *arguments, "-o", "json"]
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.environment,
            check=False,
        )
        if process.returncode != 0:
            if not check and "(NotFound)" in process.stderr:
                return {}
            raise CommandError(command, process.returncode, process.stderr or process.stdout)
        output = process.stdout
        if not output and not check:
            return {}
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"kubectl returned invalid JSON for {arguments!r}: {output[:500]!r}"
            ) from error
        if not isinstance(value, dict):
            raise RuntimeError(f"kubectl returned non-object JSON for {arguments!r}")
        return value

    def apply(self, manifest: dict[str, Any]) -> None:
        self.kubectl(["apply", "-f", "-"], input_text=json.dumps(manifest))

    def delete(self, resource: str, name: str, namespace: str | None = None, *, wait: bool = True) -> None:
        arguments = ["delete", resource, name, "--ignore-not-found", f"--wait={'true' if wait else 'false'}"]
        if namespace:
            arguments.extend(("-n", namespace))
        self.kubectl(arguments, check=False, timeout=self.config.operation_timeout)

    def get(self, resource: str, name: str, namespace: str | None = None, *, check: bool = True) -> dict[str, Any]:
        arguments = ["get", resource, name]
        if namespace:
            arguments.extend(("-n", namespace))
        return self.json(arguments, check=check)

    def list(self, resource: str, namespace: str | None = None, selector: str = "") -> list[dict[str, Any]]:
        arguments = ["get", resource]
        if namespace:
            arguments.extend(("-n", namespace))
        if selector:
            arguments.extend(("-l", selector))
        return self.json(arguments).get("items", [])

    def wait_for(
        self,
        resource: str,
        name: str,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        namespace: str | None = None,
        failure: Callable[[dict[str, Any]], str] | None = None,
        timeout: int | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or self.config.operation_timeout)
        last_error = ""
        while time.monotonic() < deadline:
            value = self.get(resource, name, namespace, check=False)
            if value:
                if progress:
                    progress(value)
                if predicate(value):
                    return value
                if failure and (message := failure(value)):
                    last_error = message
                    raise OperationFailed(message)
            time.sleep(self.config.poll_interval)
        raise OperationFailed(f"timed out waiting for {resource}/{name}: {last_error}")

    def ssh(self, vm: str, command: str, *, check: bool = True, timeout: int | None = None) -> str:
        arguments = [
            "virtctl",
            "ssh",
            "--namespace",
            self.config.namespace,
            "--username",
            self.config.ssh_user,
            "--identity-file",
            self.config.ssh_identity_file,
            "--known-hosts",
            "/dev/null",
            "--local-ssh-opts=-o StrictHostKeyChecking=no",
            "--local-ssh-opts=-o BatchMode=yes",
            "--local-ssh-opts=-o ConnectTimeout=30",
            "--command",
            command,
            f"vm/{vm}",
        ]
        return self.run(arguments, check=check, timeout=timeout or self.config.ssh_timeout)

    def guest_script(self, vm: str, script: str, *arguments: str, timeout: int | None = None) -> str:
        encoded = base64.b64encode(script.encode()).decode()
        command = f"printf %s {shlex.quote(encoded)} | base64 -d | sudo -n bash -s --"
        command += "".join(f" {shlex.quote(argument)}" for argument in arguments)
        return self.ssh(vm, command, timeout=timeout)


class S3Inventory:
    """Minimal SigV4 ListObjectsV2 client; secrets are never serialized."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        virtual_hosted: bool = False,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region or "us-east-1"
        self.access_key = access_key
        self.secret_key = secret_key
        self.virtual_hosted = virtual_hosted

    @classmethod
    def from_cluster(cls, cluster: Cluster) -> tuple["S3Inventory", dict[str, Any]]:
        setting = cluster.get("settings.harvesterhci.io", "backup-target")
        target = json.loads(setting.get("value") or setting.get("default") or "{}")
        if target.get("type") != "s3":
            raise RuntimeError("benchmark repository metrics require an S3 backup target")
        secret = cluster.get("secret", BACKUP_TARGET_SECRET, BACKUP_TARGET_NAMESPACE)
        data = secret.get("data", {})
        access = base64.b64decode(data["AWS_ACCESS_KEY_ID"]).decode()
        secret_key = base64.b64decode(data["AWS_SECRET_ACCESS_KEY"]).decode()
        client = cls(
            target["endpoint"],
            target["bucketName"],
            target.get("bucketRegion", ""),
            access,
            secret_key,
            bool(target.get("virtualHostedStyle", False)),
        )
        sanitized = {
            "type": "s3",
            "endpoint": target["endpoint"],
            "bucket": target["bucketName"],
            "region": target.get("bucketRegion", ""),
            "virtual_hosted_style": bool(target.get("virtualHostedStyle", False)),
            "kopia_gc_enabled": bool(target.get("kopiaGCEnabled", False)),
        }
        return client, sanitized

    def _signing_key(self, date: str) -> bytes:
        date_key = hmac.new(("AWS4" + self.secret_key).encode(), date.encode(), hashlib.sha256).digest()
        region_key = hmac.new(date_key, self.region.encode(), hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    def _request(self, query: list[tuple[str, str]]) -> bytes:
        parsed = urllib.parse.urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"invalid S3 endpoint: {self.endpoint!r}")
        if self.virtual_hosted:
            host = f"{self.bucket}.{parsed.netloc}"
            canonical_uri = parsed.path.rstrip("/") + "/"
        else:
            host = parsed.netloc
            canonical_uri = parsed.path.rstrip("/") + "/" + urllib.parse.quote(self.bucket, safe="")
        query_string = urllib.parse.urlencode(sorted(query), quote_via=urllib.parse.quote)
        now = dt.datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(("GET", canonical_uri or "/", query_string, canonical_headers, signed_headers, payload_hash))
        scope = f"{date}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(("AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()))
        signature = hmac.new(self._signing_key(date), string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        url = urllib.parse.urlunsplit((parsed.scheme, host, canonical_uri, query_string, ""))
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": authorization,
                "Host": host,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": amz_date,
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def inventory(self, prefix: str) -> dict[str, Any]:
        continuation = ""
        objects = 0
        total_bytes = 0
        while True:
            query = [("list-type", "2"), ("prefix", prefix)]
            if continuation:
                query.append(("continuation-token", continuation))
            root = ET.fromstring(self._request(query))
            namespace = ""
            if root.tag.startswith("{"):
                namespace = root.tag.split("}", 1)[0] + "}"
            for content in root.findall(f"{namespace}Contents"):
                size = content.findtext(f"{namespace}Size", "0")
                objects += 1
                total_bytes += int(size)
            truncated = root.findtext(f"{namespace}IsTruncated", "false").lower() == "true"
            if not truncated:
                break
            continuation = root.findtext(f"{namespace}NextContinuationToken", "")
            if not continuation:
                raise RuntimeError("S3 listing is truncated without a continuation token")
        return {"captured_at": utc_now(), "prefix": prefix, "objects": objects, "bytes": total_bytes}


def parse_prometheus(text: str) -> list[tuple[str, dict[str, str], float]]:
    result: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        match = METRIC_LINE.match(line)
        if not match:
            continue
        labels = {
            item.group("key"): bytes(item.group("value"), "utf-8").decode("unicode_escape")
            for item in LABEL_VALUE.finditer(match.group("labels"))
        }
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        result.append((match.group("name"), labels, value))
    return result


class OperationMonitor:
    def __init__(
        self,
        cluster: Cluster,
        output_dir: Path,
        context: dict[str, Any],
        owner_name: str,
    ) -> None:
        self.cluster = cluster
        self.output_dir = output_dir
        self.context = context
        self.owner_name = owner_name
        self.samples: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.pods: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._collect, name=f"metrics-{owner_name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(timeout=max(10, self.cluster.config.poll_interval * 3))
        self._collect_once()
        self._archive()
        return self.summary()

    def _relevant_job(self, job: dict[str, Any]) -> bool:
        name = job.get("metadata", {}).get("name", "")
        engine = self.context["engine"]
        operation = self.context["operation"]
        return name.startswith(f"{engine}-{operation}-") and "-check-" not in name and "-forget-" not in name

    def _collect(self) -> None:
        while not self._stop.is_set():
            self._collect_once()
            if self._stop.wait(self.cluster.config.poll_interval):
                break

    def _collect_once(self) -> None:
        selector = f"{OWNER_LABELS[self.context['operation']]}={self.owner_name}"
        try:
            jobs = self.cluster.list("jobs.batch", self.cluster.config.namespace, selector)
        except Exception as error:  # metrics failure must not stop the operation
            self.warnings.append(f"job metrics collection failed: {error}")
            return
        for job in jobs:
            if not self._relevant_job(job):
                continue
            name = job["metadata"]["name"]
            self.jobs[name] = job
            try:
                pods = self.cluster.list("pods", self.cluster.config.namespace, f"job-name={name}")
            except Exception as error:
                self.warnings.append(f"pod metrics collection failed for {name}: {error}")
                continue
            for pod in pods:
                pod_name = pod["metadata"]["name"]
                self.pods[pod_name] = pod
                node = pod.get("spec", {}).get("nodeName")
                if not node:
                    continue
                try:
                    raw = self.cluster.kubectl(["get", "--raw", f"/api/v1/nodes/{node}/proxy/metrics/cadvisor"])
                    metrics = parse_prometheus(raw)
                    self.samples.append(self._pod_sample(metrics, job, pod, node))
                except Exception as error:
                    self.warnings.append(f"cAdvisor collection failed for {pod_name}: {error}")

    def _pod_sample(
        self,
        metrics: list[tuple[str, dict[str, str], float]],
        job: dict[str, Any],
        pod: dict[str, Any],
        node: str,
    ) -> dict[str, Any]:
        pod_name = pod["metadata"]["name"]
        pod_uid = pod["metadata"].get("uid", "")
        cpu = 0.0
        memory = 0.0
        receive = 0.0
        transmit = 0.0
        for name, labels, value in metrics:
            if labels.get("namespace") != self.cluster.config.namespace or labels.get("pod") != pod_name:
                continue
            container = labels.get("container", "")
            image = labels.get("image", "")
            workload_container = bool(image) and "pause" not in image
            if name == "container_cpu_usage_seconds_total" and labels.get("cpu") == "total":
                if workload_container:
                    cpu += value
            elif name == "container_memory_working_set_bytes":
                if workload_container:
                    memory += value
            elif name == "container_network_receive_bytes_total" and "pause" in image and labels.get("interface") != "lo":
                receive += value
            elif name == "container_network_transmit_bytes_total" and "pause" in image and labels.get("interface") != "lo":
                transmit += value
        return {
            "timestamp": utc_now(),
            "monotonic_seconds": time.monotonic(),
            "campaign": self.context["campaign"],
            "engine": self.context["engine"],
            "operation": self.context["operation"],
            "job": job["metadata"]["name"],
            "pod": pod_name,
            "pod_uid": pod_uid,
            "node": node,
            "cpu_seconds": cpu,
            "memory_working_set_bytes": memory,
            "network_receive_bytes": receive,
            "network_transmit_bytes": transmit,
        }

    def _archive(self) -> None:
        directory = self.output_dir / "kubernetes" / self.context["campaign"] / self.context["engine"] / self.context["operation"]
        directory.mkdir(parents=True, exist_ok=True)
        for name, job in self.jobs.items():
            write_json(directory / f"job-{name}.json", job)
            output = self.cluster.kubectl(
                ["logs", f"job/{name}", "-n", self.cluster.config.namespace, "--timestamps"],
                check=False,
            )
            (directory / f"job-{name}.log").write_text(output, encoding="utf-8")
        for name, pod in self.pods.items():
            write_json(directory / f"pod-{name}.json", pod)

    def summary(self) -> dict[str, Any]:
        starts: list[dt.datetime] = []
        completions: list[dt.datetime] = []
        for job in self.jobs.values():
            status = job.get("status", {})
            if start := parse_time(status.get("startTime")):
                starts.append(start)
            if completion := parse_time(status.get("completionTime")):
                completions.append(completion)
        transfer_seconds: float | None = None
        if starts and completions:
            transfer_seconds = (max(completions) - min(starts)).total_seconds()

        by_pod: dict[str, list[dict[str, Any]]] = {}
        for sample in self.samples:
            by_pod.setdefault(sample["pod_uid"], []).append(sample)
        cpu_seconds = 0.0
        receive = 0.0
        transmit = 0.0
        memories: list[float] = []
        peak_cpu = 0.0
        for pod_samples in by_pod.values():
            pod_samples.sort(key=lambda item: item["monotonic_seconds"])
            if len(pod_samples) >= 2:
                cpu_seconds += max(0.0, pod_samples[-1]["cpu_seconds"] - pod_samples[0]["cpu_seconds"])
                receive += max(0.0, pod_samples[-1]["network_receive_bytes"] - pod_samples[0]["network_receive_bytes"])
                transmit += max(0.0, pod_samples[-1]["network_transmit_bytes"] - pod_samples[0]["network_transmit_bytes"])
                for previous, current in zip(pod_samples, pod_samples[1:]):
                    elapsed = current["monotonic_seconds"] - previous["monotonic_seconds"]
                    if elapsed > 0:
                        peak_cpu = max(peak_cpu, (current["cpu_seconds"] - previous["cpu_seconds"]) / elapsed)
            memories.extend(sample["memory_working_set_bytes"] for sample in pod_samples)
        return {
            "transfer_seconds": transfer_seconds,
            "cpu_seconds": cpu_seconds if self.samples else None,
            "peak_cpu_cores": peak_cpu if self.samples else None,
            "mean_memory_bytes": statistics.fmean(memories) if memories else None,
            "peak_memory_bytes": max(memories) if memories else None,
            "network_receive_bytes": int(receive) if self.samples else None,
            "network_transmit_bytes": int(transmit) if self.samples else None,
            "warnings": sorted(set(self.warnings)),
            "samples": self.samples,
        }


class BenchmarkRunner:
    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        timestamp = dt.datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        token = hashlib.sha256(f"{timestamp}:{os.getpid()}:{config.seed}".encode()).hexdigest()[:8]
        self.run_id = resource_name(f"vmbr-{timestamp}-{token}", 40)
        self.output_dir = Path(config.output_dir).expanduser().resolve() / self.run_id
        self.output_dir.mkdir(parents=True, exist_ok=False)
        for directory in ("metrics", "results", "kubernetes", "repository", "charts"):
            (self.output_dir / directory).mkdir()
        self.events_path = self.output_dir / "events.ndjson"
        self.operations: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.cluster = Cluster(config, self.output_dir, self.log)
        self.s3: S3Inventory | None = None
        self.s3_target: dict[str, Any] = {}
        self.image_name = resource_name(f"{self.run_id}-img")
        self.created_vms: set[str] = set()
        self.created_pvcs: set[str] = set()
        self.created_restores: set[str] = set()
        self.created_policies: set[str] = set()
        self.created_backups: set[str] = set()
        self._stopping = False
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._old_signal_handlers: dict[int, Any] = {}
        safe_config = config.safe_dict() | {"run_id": self.run_id}
        write_json(self.output_dir / "config.json", safe_config)

    def log(self, message: str, *, event: str = "info", **fields: Any) -> None:
        record = {"timestamp": utc_now(), "event": event, "message": message, **fields}
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
        print(f"[{record['timestamp']}] {message}", flush=True)

    def install_signal_handlers(self) -> None:
        def stop(signum: int, _frame: Any) -> None:
            self._stopping = True
            self.log(f"Stop requested by signal {signum}", event="stop-requested")

        for signum in (signal.SIGINT, signal.SIGTERM):
            self._old_signal_handlers[signum] = signal.signal(signum, stop)

    def restore_signal_handlers(self) -> None:
        for signum, handler in self._old_signal_handlers.items():
            signal.signal(signum, handler)

    def preflight(self) -> dict[str, Any]:
        self.log("Running benchmark preflight checks", event="preflight-start")
        for command in ("kubectl", "virtctl", "ssh-keygen"):
            if not shutil_which(command):
                raise RuntimeError(f"required command not found: {command}")
        self.cluster.kubectl(["get", "namespace", self.config.namespace])
        self.cluster.kubectl(["get", "storageclass", self.config.image_storage_class])
        self.cluster.kubectl(["get", "storageclass", self.config.data_storage_class])
        self.cluster.kubectl(["get", "volumesnapshotclass", self.config.snapshot_class])
        network_namespace, _, network_name = self.config.network_name.rpartition("/")
        if not network_namespace:
            network_namespace = self.config.namespace
            network_name = self.config.network_name
        self.cluster.kubectl(
            ["get", "network-attachment-definitions.k8s.cni.cncf.io", network_name, "-n", network_namespace]
        )
        active_jobs: list[str] = []
        for engine, label in ENGINE_LABELS.items():
            jobs = self.cluster.json(["get", "jobs.batch", "-A", "-l", f"{label}=true"]).get("items", [])
            for job in jobs:
                status = job.get("status", {})
                if status.get("active", 0):
                    active_jobs.append(f"{job['metadata'].get('namespace')}/{job['metadata']['name']} ({engine})")
        if active_jobs:
            raise RuntimeError("active engine jobs prevent a controlled run: " + ", ".join(active_jobs))

        self.s3, self.s3_target = S3Inventory.from_cluster(self.cluster)
        if self.s3_target.get("kopia_gc_enabled"):
            raise RuntimeError("Kopia GC must be disabled during measured campaigns")
        if self.config.include_fault_recovery:
            policies = self.cluster.list("networkpolicies.networking.k8s.io", self.config.namespace)
            if policies:
                names = ", ".join(policy["metadata"]["name"] for policy in policies)
                raise RuntimeError(
                    "S3-pause injection requires a benchmark namespace without existing NetworkPolicies; "
                    f"found: {names}"
                )

        crd = self.cluster.get("crd", "virtualmachinebackups.harvesterhci.io")
        enum_values: list[str] = []
        for version in crd.get("spec", {}).get("versions", []):
            if version.get("storage"):
                enum_values = (
                    version.get("schema", {})
                    .get("openAPIV3Schema", {})
                    .get("properties", {})
                    .get("spec", {})
                    .get("properties", {})
                    .get("type", {})
                    .get("enum", [])
                )
        missing = sorted(set(self.config.engines) - set(enum_values))
        if missing:
            raise RuntimeError(f"cluster VMBackup CRD does not support: {', '.join(missing)}")

        settings = {}
        all_settings = self.cluster.list("settings.harvesterhci.io")
        for setting in all_settings:
            name = setting["metadata"]["name"]
            if name.startswith(("restic-", "kopia-")):
                settings[name] = setting.get("value") or setting.get("default")
        environment = {
            "captured_at": utc_now(),
            "kubernetes": self.cluster.json(["version"]),
            "nodes": self.cluster.list("nodes"),
            "storage_classes": [self.config.image_storage_class, self.config.data_storage_class],
            "snapshot_class": self.config.snapshot_class,
            "backup_target": self.s3_target,
            "engine_settings": settings,
            "vmbackup_types": enum_values,
        }
        # Environment files may be shared; reduce large managedFields before writing.
        for node in environment["nodes"]:
            node.get("metadata", {}).pop("managedFields", None)
        write_json(self.output_dir / "environment.json", environment)
        self.log("Preflight checks passed", event="preflight-complete")
        return environment

    def prepare_identity(self) -> None:
        if self.config.ssh_identity_file:
            self.config.ssh_identity_file = str(Path(self.config.ssh_identity_file).expanduser().resolve())
            return
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="harvester-vmbackup-benchmark-")
        identity = Path(self._temporary_directory.name) / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", self.run_id, "-f", str(identity)],
            check=True,
        )
        self.config.ssh_identity_file = str(identity)

    def public_key(self) -> str:
        return self.cluster.run(["ssh-keygen", "-y", "-P", "", "-f", self.config.ssh_identity_file]).strip()

    def create_image(self) -> None:
        self.log(f"Creating shared VM image {self.config.namespace}/{self.image_name}", event="image-create")
        manifest = {
            "apiVersion": "harvesterhci.io/v1beta1",
            "kind": "VirtualMachineImage",
            "metadata": {
                "name": self.image_name,
                "namespace": self.config.namespace,
                "annotations": {"harvesterhci.io/storageClassName": self.config.image_storage_class},
                "labels": {
                    RUN_LABEL: self.run_id,
                    "harvesterhci.io/image-type": "raw_qcow2",
                    "harvesterhci.io/imageDisplayName": f"{self.image_name}.img",
                },
            },
            "spec": {
                "backend": "cdi",
                "checksum": "",
                "displayName": f"{self.image_name}.img",
                "retry": 3,
                "sourceType": "download",
                "targetStorageClassName": self.config.image_storage_class,
                "url": self.config.image_url,
            },
        }
        self.cluster.apply(manifest)

        def failure(value: dict[str, Any]) -> str:
            for condition in value.get("status", {}).get("conditions", []):
                if condition.get("type") == "RetryLimitExceeded" and condition.get("status") == "True":
                    return condition.get("message") or "VM image import reached its retry limit"
            return ""

        image = self.cluster.wait_for(
            "virtualmachineimages.harvesterhci.io",
            self.image_name,
            lambda value: any(
                condition.get("type") == "Imported" and condition.get("status") == "True"
                for condition in value.get("status", {}).get("conditions", [])
            ),
            namespace=self.config.namespace,
            failure=failure,
        )
        write_json(self.output_dir / "kubernetes" / "virtualmachineimage.json", image)

    def _cloud_init(self) -> tuple[str, str]:
        public_key = self.public_key()
        user_data = "\n".join(
            (
                "#cloud-config",
                "password: password1234",
                "chpasswd: { expire: False }",
                "ssh_pwauth: true",
                "ssh_authorized_keys:",
                f"- {public_key}",
                "package_update: true",
                "packages:",
                "- qemu-guest-agent",
                "runcmd:",
                "- [systemctl, enable, --now, qemu-guest-agent.service]",
            )
        )
        network_data = "\n".join(
            (
                "network:",
                "  ethernets:",
                f"    {self.config.guest_interface_name}:",
                "      dhcp4: true",
                "      dhcp6: true",
                "      dhcp-identifier: mac",
                "  version: 2",
            )
        )
        return user_data, network_data

    def create_vm(self, campaign: Campaign) -> str:
        token = hashlib.sha256(f"{self.run_id}:{campaign.key}".encode()).hexdigest()[:10]
        vm = resource_name(f"vb{token}", 20)
        boot_pvc = f"{vm}-p0"
        data_pvc = f"{vm}-p1"
        labels = {RUN_LABEL: self.run_id, CAMPAIGN_LABEL: resource_name(campaign.key, 63)}
        user_data, network_data = self._cloud_init()
        templates = [
            {
                "metadata": {
                    "name": boot_pvc,
                    "annotations": {"harvesterhci.io/imageId": f"{self.config.namespace}/{self.image_name}"},
                    "labels": labels,
                },
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": f"{self.config.boot_size_gib}Gi"}},
                    "volumeMode": "Block",
                    "storageClassName": self.config.image_storage_class,
                },
            },
            {
                "metadata": {"name": data_pvc, "labels": labels},
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": f"{campaign.size_gib}Gi"}},
                    "volumeMode": "Block",
                    "storageClassName": self.config.data_storage_class,
                },
            },
        ]
        vm_spec: dict[str, Any] = {
            "architecture": "amd64",
            "domain": {
                "cpu": {"cores": self.config.cpu, "sockets": 1, "threads": 1, "maxSockets": 1},
                "devices": {
                    "disks": [
                        {"name": "boot-disk", "bootOrder": 1, "disk": {"bus": "virtio"}},
                        {"name": "data-disk", "bootOrder": 2, "disk": {"bus": "virtio"}},
                        {"name": "cloudinitdisk", "disk": {"bus": "virtio"}},
                    ],
                    "interfaces": [{"name": "default", "model": "virtio", "bridge": {}}],
                },
                "features": {"acpi": {"enabled": True}},
                "machine": {"type": "q35"},
                "memory": {"guest": self.config.memory},
                "resources": {
                    "limits": {"cpu": str(self.config.cpu), "memory": self.config.memory},
                    "requests": {"cpu": str(self.config.cpu), "memory": self.config.memory},
                },
            },
            "hostname": vm,
            "networks": [{"name": "default", "multus": {"networkName": self.config.network_name}}],
            "terminationGracePeriodSeconds": 20,
            "volumes": [
                {"name": "boot-disk", "persistentVolumeClaim": {"claimName": boot_pvc}},
                {"name": "data-disk", "persistentVolumeClaim": {"claimName": data_pvc}},
                {"name": "cloudinitdisk", "cloudInitNoCloud": {"userData": user_data, "networkData": network_data}},
            ],
        }
        if self.config.node_selector:
            key, separator, value = self.config.node_selector.partition("=")
            if not separator or not key or not value:
                raise ValueError("node selector must use KEY=VALUE syntax")
            vm_spec["affinity"] = {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {"matchExpressions": [{"key": key, "operator": "In", "values": [value]}]}
                        ]
                    }
                }
            }
        manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": vm,
                "namespace": self.config.namespace,
                "annotations": {
                    "harvesterhci.io/volumeClaimTemplates": json.dumps(templates, separators=(",", ":")),
                    "harvesterhci.io/vmRunStrategy": "RerunOnFailure",
                },
                "labels": labels | {"harvesterhci.io/creator": "harvester", "harvesterhci.io/os": "ubuntu"},
            },
            "spec": {"runStrategy": "RerunOnFailure", "template": {"metadata": {"labels": labels | {"harvesterhci.io/vmName": vm}}, "spec": vm_spec}},
        }
        self.log(f"Creating source VM {vm} for {campaign.key}", event="vm-create", campaign=campaign.key)
        self.cluster.apply(manifest)
        self.created_vms.add(vm)
        self.created_pvcs.update((boot_pvc, data_pvc))
        for pvc in (boot_pvc, data_pvc):
            self.cluster.wait_for(
                "persistentvolumeclaim",
                pvc,
                lambda value: value.get("status", {}).get("phase") == "Bound",
                namespace=self.config.namespace,
            )
        self.cluster.wait_for(
            "virtualmachines.kubevirt.io",
            vm,
            lambda value: value.get("status", {}).get("ready") is True,
            namespace=self.config.namespace,
        )
        self.wait_for_ssh(vm)
        sentinel = f"{self.run_id}:{campaign.key}"
        self.cluster.guest_script(
            vm,
            "set -Eeuo pipefail\nmkdir -p /var/lib/vmbackup-benchmark\nprintf '%s' \"$1\" > /var/lib/vmbackup-benchmark/sentinel\nsync\nsha256sum /var/lib/vmbackup-benchmark/sentinel | cut -d ' ' -f1",
            sentinel,
        )
        return vm

    def wait_for_ssh(self, vm: str) -> None:
        deadline = time.monotonic() + self.config.ssh_timeout
        last_output = ""
        while time.monotonic() < deadline:
            last_output = self.cluster.ssh(vm, "sudo -n true && command -v base64 openssl sha256sum blockdev dd >/dev/null", check=False, timeout=60)
            # virtctl combines output but run(check=False) doesn't expose return code; use a sentinel.
            output = self.cluster.ssh(vm, "sudo -n true >/dev/null 2>&1 && echo BENCHMARK_SSH_READY", check=False, timeout=60)
            if "BENCHMARK_SSH_READY" in output:
                return
            time.sleep(max(2, self.config.poll_interval))
        raise OperationFailed(f"timed out waiting for SSH on {vm}: {last_output[-500:]}")

    def _device_script(self) -> str:
        return r'''set -Eeuo pipefail
device="$1"
pattern="$2"
seed="$3"
mode="$4"
percent="$5"
for command in blockdev dd openssl sha256sum sync readlink findmnt lsblk grep; do
  command -v "$command" >/dev/null || { echo "missing guest command: $command" >&2; exit 1; }
done
test -b "$device"
device_real="$(readlink -f "$device")"
root_source="$(findmnt -n -o SOURCE /)"
if lsblk -sno PATH "$root_source" | grep -Fxq "$device_real"; then
  echo "refusing to overwrite root disk $device_real" >&2
  exit 1
fi
size="$(blockdev --getsize64 "$device")"
bs=$((4 * 1024 * 1024))
blocks=$((size / bs))
key="$(printf '%s' "$seed" | sha256sum | cut -d ' ' -f1)"
iv="$(printf 'iv:%s' "$seed" | sha256sum | cut -c1-32)"
write_random() {
  local count="$1" seek="$2" local_seed="$3"
  local local_key local_iv
  local_key="$(printf '%s' "$local_seed" | sha256sum | cut -d ' ' -f1)"
  local_iv="$(printf 'iv:%s' "$local_seed" | sha256sum | cut -c1-32)"
  set +o pipefail
  openssl enc -aes-256-ctr -K "$local_key" -iv "$local_iv" -nosalt < /dev/zero \
    | dd of="$device" bs="$bs" count="$count" seek="$seek" iflag=fullblock conv=notrunc status=none
  set -o pipefail
}
write_repeated() {
  local count="$1" seek="$2"
  set +o pipefail
  yes "HARVESTER-VM-BACKUP-$seed" \
    | dd of="$device" bs="$bs" count="$count" seek="$seek" iflag=fullblock conv=notrunc status=none
  set -o pipefail
}
if [[ "$mode" == full ]]; then
  case "$pattern" in
    random) write_random "$blocks" 0 "$seed" ;;
    compressible)
      half=$((blocks / 2))
      dd if=/dev/zero of="$device" bs="$bs" count="$half" conv=notrunc status=none
      write_repeated "$((blocks - half))" "$half"
      ;;
    mixed)
      random_blocks=$((blocks / 2))
      repeated_blocks=$((blocks / 4))
      zero_blocks=$((blocks - random_blocks - repeated_blocks))
      write_random "$random_blocks" 0 "$seed"
      write_repeated "$repeated_blocks" "$random_blocks"
      dd if=/dev/zero of="$device" bs="$bs" count="$zero_blocks" seek="$((random_blocks + repeated_blocks))" conv=notrunc status=none
      ;;
    *) echo "unsupported pattern: $pattern" >&2; exit 1 ;;
  esac
elif [[ "$mode" == mutate ]]; then
  if ((percent > 0)); then
    change_blocks=$((blocks * percent / 100))
    ((change_blocks > 0)) || change_blocks=1
    offset_seed=$((16#${key:0:8}))
    max_offset=$((blocks - change_blocks))
    ((max_offset > 0)) && offset=$((offset_seed % max_offset)) || offset=0
    write_random "$change_blocks" "$offset" "change:$seed"
    printf 'CHANGED_BYTES=%s\n' "$((change_blocks * bs))"
  else
    printf 'CHANGED_BYTES=0\n'
  fi
else
  echo "unsupported mode: $mode" >&2
  exit 1
fi
sync
printf 'DEVICE_BYTES=%s\n' "$size"
sha256sum "$device" | awk '{print "SHA256=" $1}'
'''

    def initialize_device(self, vm: str, campaign: Campaign, seed: str) -> tuple[int, str]:
        self.log(f"Initializing {campaign.size_gib} GiB {campaign.pattern} raw device", event="data-initialize", campaign=campaign.key)
        output = self.cluster.guest_script(
            vm,
            self._device_script(),
            self.config.guest_device,
            campaign.pattern,
            seed,
            "full",
            "100",
            timeout=max(self.config.operation_timeout, campaign.size_gib * 120),
        )
        return self._parse_device_output(output)

    def mutate_device(self, vm: str, campaign: Campaign, generation: Generation, seed: str) -> tuple[int, int, str]:
        mode = "full" if generation.change_percent == 100 else "mutate"
        output = self.cluster.guest_script(
            vm,
            self._device_script(),
            self.config.guest_device,
            campaign.pattern,
            seed,
            mode,
            str(generation.change_percent),
            timeout=max(self.config.operation_timeout, campaign.size_gib * 120),
        )
        size, checksum = self._parse_device_output(output)
        changed = size if generation.change_percent == 100 else self._output_integer(output, "CHANGED_BYTES")
        return size, changed, checksum

    @staticmethod
    def _output_integer(output: str, key: str) -> int:
        match = re.search(rf"^{re.escape(key)}=([0-9]+)$", output, re.MULTILINE)
        if not match:
            raise OperationFailed(f"guest output did not contain {key}: {output[-1000:]}")
        return int(match.group(1))

    def _parse_device_output(self, output: str) -> tuple[int, str]:
        size = self._output_integer(output, "DEVICE_BYTES")
        match = re.search(r"^SHA256=([a-f0-9]{64})$", output, re.MULTILINE)
        if not match:
            raise OperationFailed(f"guest output did not contain a valid SHA256: {output[-1000:]}")
        return size, match.group(1)

    def repository_inventory(self, engine: str, label: str, *, stabilize: bool = False) -> dict[str, Any] | None:
        if not self.s3:
            return None
        prefix = "restic/" if engine == "restic" else "kopia/"
        try:
            inventory = self.s3.inventory(prefix)
            if stabilize:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    time.sleep(5)
                    candidate = self.s3.inventory(prefix)
                    if (candidate["bytes"], candidate["objects"]) == (inventory["bytes"], inventory["objects"]):
                        inventory = candidate
                        break
                    inventory = candidate
                else:
                    self.warnings.append(f"repository inventory did not stabilize for {engine}/{label}")
            path = self.output_dir / "repository" / f"{resource_name(label, 120)}.json"
            write_json(path, inventory)
            return inventory
        except (RuntimeError, urllib.error.URLError, ET.ParseError) as error:
            warning = f"repository inventory unavailable for {engine}: {error}"
            self.warnings.append(warning)
            self.log(warning, event="warning")
            return None

    @staticmethod
    def _operation_failure(value: dict[str, Any]) -> str:
        status = value.get("status", {})
        error = status.get("error") or {}
        if error.get("message"):
            return str(error["message"])
        conditions = status.get("conditions", [])
        for condition in conditions:
            reason = str(condition.get("reason", ""))
            message = str(condition.get("message", ""))
            if reason.lower() == "error" or (condition.get("status") == "False" and "error" in reason.lower()):
                return message or reason
        return ""

    @staticmethod
    def _progress(value: dict[str, Any], operation: str) -> int:
        status = value.get("status", {})
        entries = status.get("volumeBackups", []) if operation == "backup" else status.get("restores", [])
        progress_values: list[int] = []
        for entry in entries or []:
            try:
                progress_values.append(int(entry.get("progress", 0)))
            except (TypeError, ValueError):
                pass
        return min(progress_values) if progress_values else 0

    def _fault_callback(
        self,
        engine: str,
        operation: str,
        owner_name: str,
        fault: str,
        state: dict[str, Any],
    ) -> Callable[[dict[str, Any]], None]:
        def callback(value: dict[str, Any]) -> None:
            if state.get("injected") or self._progress(value, operation) < self.config.fault_progress_percent:
                return
            selector = ",".join((f"{ENGINE_LABELS[engine]}=true", f"{OWNER_LABELS[operation]}={owner_name}"))
            pods = [
                pod
                for pod in self.cluster.list("pods", self.config.namespace, selector)
                if pod.get("status", {}).get("phase") == "Running"
            ]
            if not pods:
                return
            pod = sorted(pods, key=lambda item: item["metadata"].get("creationTimestamp", ""))[-1]
            pod_name = pod["metadata"]["name"]
            pod_uid = pod["metadata"].get("uid", "")
            state.update({"injected": True, "at": utc_now(), "pod": pod_name, "pod_uid": pod_uid})
            self.log(
                f"Injecting {fault} into {engine} {operation} pod {pod_name}",
                event="fault-injected",
                owner=owner_name,
                pod_uid=pod_uid,
            )
            if fault == "pod-kill":
                self.cluster.kubectl(
                    [
                        "delete",
                        "pod",
                        pod_name,
                        "-n",
                        self.config.namespace,
                        "--grace-period=0",
                        "--force",
                        "--wait=false",
                    ]
                )
            elif fault == "s3-pause":
                self._pause_s3(engine, operation, owner_name)
            else:
                raise RuntimeError(f"unsupported fault: {fault}")

        return callback

    def _pause_s3(self, engine: str, operation: str, owner_name: str) -> None:
        endpoint = urllib.parse.urlsplit(self.s3_target["endpoint"])
        host = endpoint.hostname
        if not host:
            raise OperationFailed("S3 endpoint has no hostname")
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, endpoint.port or 80, type=socket.SOCK_STREAM)})
        ipv4 = [address for address in addresses if ":" not in address]
        if not ipv4:
            raise OperationFailed("S3 pause currently requires an IPv4 endpoint")
        policy_name = resource_name(f"{self.run_id}-{owner_name}-s3-pause", 63)
        except_blocks = [f"{address}/32" for address in ipv4]
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": policy_name,
                "namespace": self.config.namespace,
                "labels": {RUN_LABEL: self.run_id},
            },
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        ENGINE_LABELS[engine]: "true",
                        OWNER_LABELS[operation]: owner_name,
                    }
                },
                "policyTypes": ["Egress"],
                "egress": [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": except_blocks}}]}],
            },
        }
        self.cluster.apply(policy)
        self.created_policies.add(policy_name)
        try:
            deadline = time.monotonic() + self.config.s3_pause_seconds
            while time.monotonic() < deadline and not self._stopping:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        finally:
            self.cluster.delete("networkpolicy", policy_name, self.config.namespace, wait=False)
            self.created_policies.discard(policy_name)

    def _base_result(
        self,
        campaign: Campaign,
        generation: Generation,
        engine: str,
        operation: str,
        logical_bytes: int,
        source_sha256: str,
        restore_index: int = 0,
        changed_bytes: int = 0,
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "campaign": campaign.key,
            "suite": campaign.suite,
            "repetition": campaign.repetition,
            "generation": generation.name,
            "change_percent": generation.change_percent,
            "pattern": campaign.pattern,
            "size_gib": campaign.size_gib,
            "engine": engine,
            "operation": operation,
            "restore_index": restore_index,
            "measured": campaign.measured,
            "logical_bytes": logical_bytes,
            "changed_bytes": changed_bytes,
            "source_sha256": source_sha256,
            "fault": campaign.fault if campaign.fault_operation == operation else "",
            "fault_injected": False,
            "automatic_recovery": None,
            "valid": True,
            "warning": "",
        }

    def _finish_result(
        self,
        result: dict[str, Any],
        started_wall: str,
        started: float,
        status: str,
        metrics: dict[str, Any],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        warning: str = "",
        elapsed: float | None = None,
        ended_wall: str | None = None,
    ) -> dict[str, Any]:
        elapsed = elapsed if elapsed is not None else time.monotonic() - started
        result.update(
            {
                "status": status,
                "started_at": started_wall,
                "ended_at": ended_wall or utc_now(),
                "e2e_seconds": elapsed,
                "transfer_seconds": metrics.get("transfer_seconds"),
                "cpu_seconds": metrics.get("cpu_seconds"),
                "peak_cpu_cores": metrics.get("peak_cpu_cores"),
                "mean_cpu_cores": (
                    metrics.get("cpu_seconds") / metrics["transfer_seconds"]
                    if metrics.get("cpu_seconds") is not None and metrics.get("transfer_seconds")
                    else None
                ),
                "mean_memory_bytes": metrics.get("mean_memory_bytes"),
                "peak_memory_bytes": metrics.get("peak_memory_bytes"),
                "network_receive_bytes": metrics.get("network_receive_bytes"),
                "network_transmit_bytes": metrics.get("network_transmit_bytes"),
                "repository_bytes_before": before.get("bytes") if before else None,
                "repository_bytes_after": after.get("bytes") if after else None,
                "repository_delta_bytes": (
                    after["bytes"] - before["bytes"] if before is not None and after is not None else None
                ),
                "repository_objects_before": before.get("objects") if before else None,
                "repository_objects_after": after.get("objects") if after else None,
                "logical_mib_per_second": (
                    result["logical_bytes"] / 1024 / 1024 / metrics["transfer_seconds"]
                    if metrics.get("transfer_seconds")
                    else None
                ),
                "warning": "; ".join(filter(None, [warning, *metrics.get("warnings", [])])),
            }
        )
        repository_delta = result.get("repository_delta_bytes")
        if result["operation"] == "backup" and repository_delta is not None and repository_delta > 0:
            result["effective_reduction_ratio"] = result["logical_bytes"] / repository_delta
            result["incremental_amplification"] = (
                repository_delta / result["changed_bytes"] if result.get("changed_bytes", 0) > 0 else None
            )
        else:
            result["effective_reduction_ratio"] = None
            result["incremental_amplification"] = None
        self.samples.extend(metrics.get("samples", []))
        self.operations.append(result)
        self._flush_results()
        return result

    def _flush_results(self) -> None:
        write_csv(self.output_dir / "results" / "operations.csv", self.operations, OPERATION_FIELDS)
        write_json(self.output_dir / "results" / "operations.json", self.operations)
        write_csv(self.output_dir / "metrics" / "pod-samples.csv", self.samples, SAMPLE_FIELDS)

    def backup(
        self,
        vm: str,
        campaign: Campaign,
        generation: Generation,
        engine: str,
        data_bytes: int,
        checksum: str,
        changed_bytes: int,
        *,
        suffix: str = "",
        inject_fault: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        digest = hashlib.sha256(f"{campaign.key}:{generation.name}:{engine}:{suffix}".encode()).hexdigest()[:8]
        name = resource_name(f"b{digest}", 18)
        labels = {RUN_LABEL: self.run_id, CAMPAIGN_LABEL: resource_name(campaign.key, 63)}
        manifest = {
            "apiVersion": "harvesterhci.io/v1beta1",
            "kind": "VirtualMachineBackup",
            "metadata": {
                "name": name,
                "namespace": self.config.namespace,
                "annotations": {"harvesterhci.io/snapshotFreezeFS": "true"},
                "labels": labels,
            },
            "spec": {
                "source": {"apiGroup": "kubevirt.io", "kind": "VirtualMachine", "name": vm},
                "type": engine,
            },
        }
        logical_bytes = data_bytes + self.config.boot_size_gib * 1024**3
        result = self._base_result(
            campaign, generation, engine, "backup", logical_bytes, checksum, changed_bytes=changed_bytes
        )
        before = self.repository_inventory(engine, f"{campaign.key}-{generation.name}-{engine}-backup-before")
        monitor = OperationMonitor(self.cluster, self.output_dir, result, name)
        started_wall = utc_now()
        started = time.monotonic()
        monitor.start()
        fault_state: dict[str, Any] = {}
        progress = None
        if inject_fault:
            progress = self._fault_callback(engine, "backup", name, campaign.fault, fault_state)
        status = "succeeded"
        warning = ""
        try:
            self.cluster.apply(manifest)
            self.created_backups.add(name)
            backup = self.cluster.wait_for(
                "virtualmachinebackups.harvesterhci.io",
                name,
                lambda value: value.get("status", {}).get("readyToUse") is True,
                namespace=self.config.namespace,
                failure=self._operation_failure,
                progress=progress,
            )
            write_json(self.output_dir / "kubernetes" / campaign.key / f"vmb-{name}.json", backup)
        except Exception as error:
            status = "failed"
            warning = str(error)
        finally:
            metrics = monitor.stop()
        operation_elapsed = time.monotonic() - started
        operation_ended_wall = utc_now()
        after = self.repository_inventory(engine, f"{campaign.key}-{generation.name}-{engine}-backup-after", stabilize=True)
        fault_missing = inject_fault and not fault_state.get("injected")
        if fault_missing:
            status = "invalid"
            result["valid"] = False
            warning = "; ".join(filter(None, (warning, "operation completed before the configured fault could be injected")))
        result["fault_injected"] = bool(fault_state.get("injected"))
        result["automatic_recovery"] = status == "succeeded" if inject_fault and fault_state.get("injected") else None
        self._finish_result(
            result, started_wall, started, status, metrics, before, after, warning, operation_elapsed, operation_ended_wall
        )
        if fault_missing:
            raise FaultNotInjected(warning)
        if status != "succeeded":
            raise OperationFailed(warning or f"backup {name} failed")
        return name, result

    def halt_vm(self, vm: str) -> None:
        self.cluster.kubectl(
            ["patch", "virtualmachine.kubevirt.io", vm, "-n", self.config.namespace, "--type=merge", "-p", '{"spec":{"runStrategy":"Halted"}}']
        )
        self.cluster.wait_for(
            "virtualmachine.kubevirt.io",
            vm,
            lambda value: value.get("status", {}).get("ready") is not True,
            namespace=self.config.namespace,
        )
        deadline = time.monotonic() + self.config.operation_timeout
        while time.monotonic() < deadline:
            launchers = [
                pod
                for pod in self.cluster.list("pods", self.config.namespace)
                if pod.get("metadata", {}).get("deletionTimestamp") is None
                and pod.get("metadata", {}).get("labels", {}).get("kubevirt.io") == "virt-launcher"
                and (
                    pod.get("metadata", {}).get("labels", {}).get("vm.kubevirt.io/name") == vm
                    or pod.get("metadata", {}).get("labels", {}).get("kubevirt.io/domain") == vm
                )
            ]
            if not launchers:
                return
            time.sleep(self.config.poll_interval)
        raise OperationFailed(f"timed out waiting for virt-launcher of {vm} to stop")

    def start_vm(self, vm: str) -> None:
        self.cluster.kubectl(
            ["patch", "virtualmachine.kubevirt.io", vm, "-n", self.config.namespace, "--type=merge", "-p", '{"spec":{"runStrategy":"RerunOnFailure"}}']
        )
        self.cluster.wait_for(
            "virtualmachine.kubevirt.io",
            vm,
            lambda value: value.get("status", {}).get("ready") is True,
            namespace=self.config.namespace,
        )
        self.wait_for_ssh(vm)

    def restore(
        self,
        backup_name: str,
        campaign: Campaign,
        generation: Generation,
        engine: str,
        data_bytes: int,
        checksum: str,
        restore_index: int,
        changed_bytes: int = 0,
        *,
        suffix: str = "",
        inject_fault: bool = False,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(
            f"{campaign.key}:{generation.name}:{engine}:{restore_index}:{suffix}".encode()
        ).hexdigest()[:8]
        restore_name = resource_name(f"r{digest}", 18)
        target_vm = resource_name(f"n{digest}", 18)
        labels = {RUN_LABEL: self.run_id, CAMPAIGN_LABEL: resource_name(campaign.key, 63)}
        manifest = {
            "apiVersion": "harvesterhci.io/v1beta1",
            "kind": "VirtualMachineRestore",
            "metadata": {"name": restore_name, "namespace": self.config.namespace, "labels": labels},
            "spec": {
                "newVM": True,
                "target": {"apiGroup": "kubevirt.io", "kind": "VirtualMachine", "name": target_vm},
                "virtualMachineBackupName": backup_name,
                "virtualMachineBackupNamespace": self.config.namespace,
            },
        }
        logical_bytes = data_bytes + self.config.boot_size_gib * 1024**3
        result = self._base_result(
            campaign, generation, engine, "restore", logical_bytes, checksum, restore_index, changed_bytes
        )
        before = self.repository_inventory(engine, f"{campaign.key}-{generation.name}-{engine}-restore{restore_index}-before")
        monitor = OperationMonitor(self.cluster, self.output_dir, result, restore_name)
        started_wall = utc_now()
        started = time.monotonic()
        monitor.start()
        fault_state: dict[str, Any] = {}
        progress = None
        if inject_fault:
            progress = self._fault_callback(engine, "restore", restore_name, campaign.fault, fault_state)
        status = "succeeded"
        warning = ""
        restored_checksum = ""
        correct = False
        restored_pvcs: list[str] = []
        try:
            self.cluster.apply(manifest)
            self.created_restores.add(restore_name)
            self.created_vms.add(target_vm)
            restore = self.cluster.wait_for(
                "virtualmachinerestores.harvesterhci.io",
                restore_name,
                lambda value: value.get("status", {}).get("complete") is True,
                namespace=self.config.namespace,
                failure=self._operation_failure,
                progress=progress,
            )
            for entry in restore.get("status", {}).get("restores", []) or []:
                pvc_name = (
                    entry.get("persistentVolumeClaimSpec", {}).get("metadata", {}).get("name")
                    or entry.get("persistentVolumeClaimName")
                )
                if pvc_name:
                    restored_pvcs.append(pvc_name)
                    self.created_pvcs.add(pvc_name)
            self.cluster.wait_for(
                "virtualmachines.kubevirt.io",
                target_vm,
                lambda value: value.get("status", {}).get("ready") is True,
                namespace=self.config.namespace,
            )
            self.wait_for_ssh(target_vm)
            output = self.cluster.guest_script(
                target_vm,
                "set -Eeuo pipefail\ndevice=\"$1\"\ntest -b \"$device\"\nprintf 'DEVICE_BYTES=%s\\n' \"$(blockdev --getsize64 \"$device\")\"\nsha256sum \"$device\" | awk '{print \"SHA256=\" $1}'\ntest -f /var/lib/vmbackup-benchmark/sentinel\nsha256sum /var/lib/vmbackup-benchmark/sentinel | awk '{print \"SENTINEL_SHA256=\" $1}'",
                self.config.guest_device,
                timeout=max(self.config.operation_timeout, campaign.size_gib * 120),
            )
            restored_size, restored_checksum = self._parse_device_output(output)
            sentinel_match = re.search(r"^SENTINEL_SHA256=([a-f0-9]{64})$", output, re.MULTILINE)
            expected_sentinel = hashlib.sha256(f"{self.run_id}:{campaign.key}".encode()).hexdigest()
            correct = (
                restored_size == data_bytes
                and restored_checksum == checksum
                and sentinel_match is not None
                and sentinel_match.group(1) == expected_sentinel
            )
            if not correct:
                raise OperationFailed(
                    f"restored data mismatch: expected {data_bytes}/{checksum}, got {restored_size}/{restored_checksum}"
                )
            write_json(self.output_dir / "kubernetes" / campaign.key / f"vmr-{restore_name}.json", restore)
        except Exception as error:
            status = "failed"
            warning = str(error)
        finally:
            metrics = monitor.stop()
        operation_elapsed = time.monotonic() - started
        operation_ended_wall = utc_now()
        after = self.repository_inventory(engine, f"{campaign.key}-{generation.name}-{engine}-restore{restore_index}-after", stabilize=True)
        fault_missing = inject_fault and not fault_state.get("injected")
        if fault_missing:
            status = "invalid"
            result["valid"] = False
            warning = "; ".join(filter(None, (warning, "operation completed before the configured fault could be injected")))
        result.update(
            {
                "restored_sha256": restored_checksum,
                "correct": correct,
                "fault_injected": bool(fault_state.get("injected")),
                "automatic_recovery": status == "succeeded" if inject_fault and fault_state.get("injected") else None,
            }
        )
        self._finish_result(
            result, started_wall, started, status, metrics, before, after, warning, operation_elapsed, operation_ended_wall
        )
        self.cluster.delete("virtualmachinerestore.harvesterhci.io", restore_name, self.config.namespace)
        self.created_restores.discard(restore_name)
        self.cluster.delete("virtualmachine.kubevirt.io", target_vm, self.config.namespace)
        self.created_vms.discard(target_vm)
        for pvc_name in restored_pvcs:
            self.cluster.delete("persistentvolumeclaim", pvc_name, self.config.namespace)
            self.created_pvcs.discard(pvc_name)
        if fault_missing:
            raise FaultNotInjected(warning)
        if status != "succeeded":
            raise OperationFailed(warning or f"restore {restore_name} failed")
        return result

    def run_campaign(self, campaign: Campaign) -> None:
        if self._stopping:
            raise KeyboardInterrupt
        self.log(f"Starting campaign {campaign.key}", event="campaign-start", campaign=campaign.key)
        vm = self.create_vm(campaign)
        initial_generation = campaign.generations[0]
        logical_bytes, checksum = self.initialize_device(
            vm, campaign, campaign_seed(self.config, campaign, initial_generation)
        )
        changed_bytes = logical_bytes + self.config.boot_size_gib * 1024**3
        try:
            for generation_index, generation in enumerate(campaign.generations):
                if generation_index:
                    logical_bytes, changed_bytes, checksum = self.mutate_device(
                        vm, campaign, generation, campaign_seed(self.config, campaign, generation)
                    )
                    self.log(
                        f"Applied {generation.name} mutation: {changed_bytes} bytes changed",
                        event="data-mutated",
                        campaign=campaign.key,
                        generation=generation.name,
                        changed_bytes=changed_bytes,
                    )
                backups: dict[str, str] = {}
                for engine in campaign.engine_order(generation_index):
                    inject = campaign.fault_operation == "backup"
                    try:
                        backup_name, _ = self.backup(
                            vm,
                            campaign,
                            generation,
                            engine,
                            logical_bytes,
                            checksum,
                            changed_bytes,
                            inject_fault=inject,
                        )
                    except FaultNotInjected:
                        raise
                    except OperationFailed:
                        if not inject:
                            raise
                        self.log(
                            f"Faulted {engine} backup did not recover automatically; resubmitting",
                            event="manual-recovery",
                            campaign=campaign.key,
                        )
                        backup_name, recovery = self.backup(
                            vm,
                            campaign,
                            generation,
                            engine,
                            logical_bytes,
                            checksum,
                            changed_bytes,
                            suffix="recovery",
                        )
                        recovery["automatic_recovery"] = False
                        self._flush_results()
                    backups[engine] = backup_name

                self.halt_vm(vm)
                try:
                    for engine in campaign.engine_order(generation_index):
                        count = campaign.restore_count
                        for restore_index in range(1, count + 1):
                            inject = campaign.fault_operation == "restore" and restore_index == 1
                            try:
                                self.restore(
                                    backups[engine],
                                    campaign,
                                    generation,
                                    engine,
                                    logical_bytes,
                                    checksum,
                                    restore_index,
                                    changed_bytes,
                                    inject_fault=inject,
                                )
                            except FaultNotInjected:
                                raise
                            except OperationFailed:
                                if not inject:
                                    raise
                                self.log(
                                    f"Faulted {engine} restore did not recover automatically; resubmitting",
                                    event="manual-recovery",
                                    campaign=campaign.key,
                                )
                                recovery = self.restore(
                                    backups[engine],
                                    campaign,
                                    generation,
                                    engine,
                                    logical_bytes,
                                    checksum,
                                    restore_index,
                                    changed_bytes,
                                    suffix="recovery",
                                )
                                recovery["automatic_recovery"] = False
                                self._flush_results()
                finally:
                    if generation_index < len(campaign.generations) - 1:
                        self.start_vm(vm)
            self.log(f"Campaign {campaign.key} completed", event="campaign-complete", campaign=campaign.key)
        finally:
            self.cluster.delete("virtualmachine.kubevirt.io", vm, self.config.namespace)
            self.created_vms.discard(vm)
            for pvc_name in (f"{vm}-p0", f"{vm}-p1"):
                self.cluster.delete("persistentvolumeclaim", pvc_name, self.config.namespace)
                self.created_pvcs.discard(pvc_name)
        if self.config.cooldown and campaign.measured and not self._stopping:
            self.log(f"Cooling down for {self.config.cooldown} seconds", event="cooldown")
            deadline = time.monotonic() + self.config.cooldown
            while time.monotonic() < deadline and not self._stopping:
                time.sleep(min(1, deadline - time.monotonic()))

    def cleanup(self) -> None:
        self.log("Cleaning temporary benchmark resources", event="cleanup-start")
        for policy in list(self.created_policies):
            self.cluster.delete("networkpolicy", policy, self.config.namespace, wait=False)
            self.created_policies.discard(policy)
        for restore in list(self.created_restores):
            self.cluster.delete("virtualmachinerestore.harvesterhci.io", restore, self.config.namespace)
            self.created_restores.discard(restore)
        for vm in list(self.created_vms):
            self.cluster.delete("virtualmachine.kubevirt.io", vm, self.config.namespace)
            self.created_vms.discard(vm)
        for pvc in list(self.created_pvcs):
            self.cluster.delete("persistentvolumeclaim", pvc, self.config.namespace)
            self.created_pvcs.discard(pvc)
        self.cluster.delete("virtualmachineimage.harvesterhci.io", self.image_name, self.config.namespace)
        if not self.config.keep_backups:
            for backup in list(self.created_backups):
                self.cluster.delete("virtualmachinebackup.harvesterhci.io", backup, self.config.namespace)
                self.created_backups.discard(backup)
        self.log("Temporary resource cleanup completed", event="cleanup-complete")

    def run(self) -> int:
        campaigns = build_matrix(self.config)
        print(f"Run ID: {self.run_id}", flush=True)
        print(f"Artifact directory: {self.output_dir}", flush=True)
        write_json(self.output_dir / "matrix.json", json.loads(matrix_as_json(self.config, campaigns)))
        self.log(
            f"Expanded {len(campaigns)} campaigns and {matrix_summary(campaigns)['operations']} operations",
            event="matrix-expanded",
        )
        if self.config.dry_run:
            print(matrix_as_json(self.config, campaigns))
            environment = {"captured_at": utc_now(), "dry_run": True, "finished_at": utc_now()}
            write_json(self.output_dir / "environment.json", environment)
            generate_report(self.output_dir, self.config.safe_dict() | {"run_id": self.run_id}, environment, [], [])
            return 0

        self.install_signal_handlers()
        environment: dict[str, Any] = {}
        failed_campaigns = 0
        try:
            self.prepare_identity()
            environment = self.preflight()
            self.create_image()
            for index, campaign in enumerate(campaigns, start=1):
                if self._stopping:
                    break
                self.log(
                    f"Campaign {index}/{len(campaigns)}: {campaign.key}",
                    event="progress",
                    completed=index - 1,
                    total=len(campaigns),
                )
                try:
                    self.run_campaign(campaign)
                except Exception as error:
                    failed_campaigns += 1
                    self.warnings.append(f"campaign {campaign.key} failed: {error}")
                    self.log(f"Campaign {campaign.key} failed: {error}", event="error", campaign=campaign.key)
            return_code = 130 if self._stopping else (1 if failed_campaigns else 0)
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.warnings.append(f"cleanup failed: {error}")
                self.log(f"Cleanup failed: {error}", event="error")
            if self._temporary_directory:
                self._temporary_directory.cleanup()
            environment = environment or {"captured_at": utc_now()}
            environment["finished_at"] = utc_now()
            write_json(self.output_dir / "environment.json", environment)
            generate_report(
                self.output_dir,
                self.config.safe_dict() | {"run_id": self.run_id},
                environment,
                self.operations,
                sorted(set(self.warnings)),
            )
            self.restore_signal_handlers()
        return return_code


def shutil_which(command: str) -> str | None:
    return shutil.which(command)
