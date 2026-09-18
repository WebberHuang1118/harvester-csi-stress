# Harvester CSI Stress Test

This repository tests Harvester VM backup and restore workflows against a CSI
storage driver. Backups can use local CSI snapshots or a configured remote backup
target. The test can be operated from the command line or from a local web GUI.

The test imports one Harvester `VirtualMachineImage` for the run, starts the
requested number of workers in parallel, and performs the configured number of
iterations sequentially inside each worker. For example, `--parallel 3
--iterations 10` runs three concurrent workers and 30 total iterations.

During each iteration, the test:

1. Generates and creates a minimal KubeVirt VM with boot and data PVCs.
2. Waits for the VM, both PVCs, and guest SSH access. Guest readiness uses a
   dedicated timeout and can optionally retry the entire iteration with fresh
   VMs.
3. Writes random payloads to the root filesystem and data disk and records their
   SHA-256 checksums.
4. Creates a Harvester `VirtualMachineBackup` with filesystem freeze enabled,
   using the configured `snapshot`, `backup`, `restic`, or `kopia` type. Two
   configured types alternate between iterations. Transient errors remain visible
   in the log, but the test allows a bounded recovery window for controller retries.
5. Stops the source VM and restores the backup as a new VM.
6. Recalculates both checksums on the restored VM and verifies data integrity.
   Checksum retrieval retries transient SSH/`virtctl` transport failures
   without retrying or hiding a valid checksum mismatch.
7. Removes the iteration resources unless resource retention is enabled.

If one worker fails, the test stops the other active workers, prints cluster
diagnostics, and attempts to clean all resources belonging to that run.

## Prerequisites

Run the CLI or GUI service from a host that has:

- Bash 5.1 or newer
- Python 3.10 or newer for the GUI; no third-party Python packages are required
- `kubectl`
- `virtctl`
- `jq`
- `base64`
- `ssh-keygen`
- Access to a Harvester cluster with the Harvester, KubeVirt, CDI, Multus, and
  VolumeSnapshot APIs
- A CSI-backed image storage class that supports the selected boot PVC settings
- A CSI-backed data storage class that supports the selected data PVC settings
  and snapshots
- For `backup`, `restic`, or `kopia`, a working Harvester backup target with the
  corresponding backup engine configured
- A Multus `NetworkAttachmentDefinition` reachable by the test VMs

The cloud image must support cloud-init, the selected SSH user, passwordless
`sudo`, DHCP on the selected guest interface, and installation of
`qemu-guest-agent`. The default settings target an Ubuntu cloud image.

An exported VM or reference manifest is not required. The script generates a
KubeVirt VM with `amd64` architecture, a `q35` machine, `virtio` disks and
networking, `RerunOnFailure`, ACPI, inline cloud-init, two PVC templates, and
optional node affinity.

## Browser GUI

### Start the service

From the repository root:

```bash
export KUBECONFIG="/absolute/path/to/harvester-kubeconfig.yaml"
./scripts/harvester-csi-stress-service
```

Open [http://localhost:7000](http://localhost:7000). Setting `KUBECONFIG` before
starting the service pre-populates the Kubeconfig field. You can also enter or
change that path in the GUI before starting an operation.

Use another port or open the default browser automatically with:

```bash
./scripts/harvester-csi-stress-service --port 7100
./scripts/harvester-csi-stress-service --open-browser
```

The service accepts one managed operation at a time and runs the Bash script as
a child process. Press `Ctrl+C` in the service terminal to stop the service. If
an operation is active, the service requests a clean stop and waits for the
script's cleanup handling.

### Open a GUI running on a remote host

The service intentionally binds to `127.0.0.1` and only accepts requests using a
localhost host name. It cannot be opened directly with
`http://REMOTE_HOST_IP:7000`.

Keep the service running on the remote host, then create an SSH tunnel from your
local workstation:

```bash
ssh -N -L 7000:127.0.0.1:7000 USER@REMOTE_HOST
```

Open [http://localhost:7000](http://localhost:7000) in your local browser. If
local port 7000 is already in use, map another local port:

```bash
ssh -N -L 7100:127.0.0.1:7000 USER@REMOTE_HOST
```

Then open [http://localhost:7100](http://localhost:7100). File paths entered in
the GUI, including Kubeconfig and SSH identity paths, are resolved on the host
running the service—not on the computer running the browser.

### Configure and start a stress test

Select **Stress test**, review the fields, and choose **Start stress test**. The
GUI exposes every supported stress-test parameter in these groups:

- **Cluster connection:** Kubeconfig, namespace, and per-operation cluster timeout
- **VM and storage:** image URL, image/data storage classes, CPU, memory, PVC
  capacities, volume modes, and access modes
- **VM network:** Multus network, guest interface name, and required node selector
- **Workload:** iterations per worker, parallel workers, VM backup type(s),
  root/data payload sizes, polling interval, and backup error grace
- **Guest access:** SSH user/key, SSH retry/connect timeouts, fresh-VM retries, guest
  data device, mount path, and resource retention

The image storage class is used for both the Harvester VM image and every
boot/root PVC. The data storage class is used for the additional data PVC.
There is no separate boot storage-class setting.

The image URL, image storage class, and data storage class are required and have
no environment-independent defaults.

Important GUI and CLI defaults are:

| Setting | Default |
| --- | --- |
| Namespace | `default` |
| Operation timeout | `3600` seconds |
| SSH retry timeout / connect timeout / fresh VM retries | `900` seconds / `30` seconds / `0` |
| vCPU / memory | `2` / `4Gi` |
| Boot PVC | `50Gi`, `Block`, `ReadWriteOnce` |
| Data PVC | `100Gi`, `Filesystem`, `ReadWriteOnce` |
| Multus network | `default/mgmt-vlan` |
| Guest interface | `enp1s0` |
| Required node selector | `network.harvesterhci.io/mgmt=true` |
| Iterations / parallel workers | `1` / `1` |
| VM backup type(s) | `snapshot` |
| Root / data payload | `1Gi` / `10Gi` |
| Polling interval | `5` seconds |
| VM backup error grace | `60` seconds |
| SSH user | `ubuntu` |
| Guest data device / mount | `/dev/vdb` / `/mnt/csi-stress` |
| Keep all resources / keep failed resources | Disabled / disabled |

The GUI validates values before launching the process. In particular:

- Numeric settings must be positive integers, except fresh VM retries, which may
  be zero.
- The root payload must be smaller than the boot PVC capacity.
- The data payload must be smaller than the data PVC capacity.
- Kubeconfig and optional SSH identity files must exist on the service host.
- The node selector must use `KEY=VALUE` syntax; clear it to disable node
  affinity.
- Only one stress or cleanup operation can run at a time.

### Behavior while an operation is running

After an operation starts, all operation choices and configuration fields are
grayed out and locked. **Reset defaults** and **Start** are also disabled, while
**Stop** remains available.

Reloading the browser does not lose the active view. The GUI selects the active
job, repopulates the exact parameters used to launch it, keeps the fields locked,
and continues polling its output. The controls become editable again when the
operation succeeds, fails, or stops.

This state is kept by the running service process. A browser refresh is safe,
but stopping the service requests a stop for any active job and clears all
in-memory job history. A newly started service cannot recover that history.

### Read and copy output

The execution panel provides:

- Job status, iteration progress, run ID, start time, exit code, and current
  activity
- The exact generated command, including `KUBECONFIG` when one is selected, with
  a copy button
- Timestamped logs with main, service, worker, and iteration context
- **All**, **Activity**, **Warnings**, and **Errors** filters
- Text search and optional automatic output following
- **Copy output** to copy the complete unfiltered output, including lines not
  currently visible because of filters
- **Clear view** to remove rendered lines from the current browser view without
  deleting the service's retained output
- A recent-run selector for the latest service-managed operations

The service retains up to 20 recent jobs in the selector and up to 20,000 output
entries per job. All GUI history is memory-only and is cleared when the service
exits.

### Stop or clean resources from the GUI

**Stop** asks for confirmation, sends a termination signal to the full process
group, and allows the Bash cleanup traps to run.

For intermittent failures, enable **Keep only failed iteration resources**.
Successful iterations and workers stopped because another worker failed are
still cleaned, while the actual failed iteration and shared image remain for
inspection. The output prints the run and iteration label selectors. This option
cannot be combined with **Keep resources after the run**. The local temporary
directory is also retained so the generated SSH identity and VM manifests remain
available; its path is printed and must be removed manually after investigation.
When fresh-VM retries are enabled, failed intermediate attempts are diagnosed
and cleaned before retrying; only the final failed attempt is retained.

The GUI also provides two cleanup modes:

- **Clean one run:** enter a stress or benchmark run ID to remove resources
  from that run.
- **Clean all runs:** list and remove detected stress and benchmark resources
  in the selected namespace. This includes retained benchmark backups and may
  delete their remote repository data. This mode requires confirmation.

Prefer single-run cleanup when the run ID is known.

## Restic and Kopia benchmark

The benchmark is separate from the stress test. It executes matched Restic and
Kopia VMBackup/VMRestore campaigns, collects performance and repository metrics,
checks restored data, and writes a durable report for each run.

Use a dedicated, non-production namespace and backup repository. In addition to
the general prerequisites above, the benchmark currently requires:

- An S3 Harvester backup target that the machine running the benchmark can
  reach directly
- Restic and Kopia enabled and supported by the cluster's
  `VirtualMachineBackup` CRD
- A valid `VolumeSnapshotClass`
- No other active Restic or Kopia backup jobs during the benchmark
- Kopia garbage collection disabled for the duration of measured campaigns
- For the optional fault suite, a dedicated namespace with no existing
  `NetworkPolicy` resources

### Run from the GUI

Start the service as described under **Browser GUI**, then:

1. Select **Restic/Kopia benchmark**.
2. Enter the image URL, storage classes, snapshot class, network, and guest
   settings for the test cluster.
3. Keep **Dry run** enabled for the first launch. Review the expanded matrix in
   the GUI and the generated report without changing the cluster.
4. Disable **Dry run**, select the **smoke** profile, and start one small
   end-to-end validation. Smoke runs one size with both selected engines: one
   backup and one restore per engine.
5. Check the live output and open **Download report.md** after completion.
6. After the smoke result is valid, select **standard** or **custom**, configure
   the suites and paired-run count, and start the measured run.
7. Enable **Include fault-recovery suite** only when fault injection is wanted.
   Restored-data correctness checks remain enabled even when this suite is off.

The standard defaults are substantial: 37 campaigns and 234 backup/restore
operations, including excluded warm-ups but not the optional fault suite. They
also include a 60-second cooldown after measured campaigns. Check the matrix
preview and available VM, storage, repository, and maintenance-window capacity
before starting.

### How campaigns become operations

An operation is one `VirtualMachineBackup` or one `VirtualMachineRestore`.
For example, selecting one 20 GiB data-device size, one paired run, both
engines, and all normal suites expands to seven campaigns and 42 operations:

| Suite | Campaigns | Operations |
|---|---:|---:|
| Warm-up | 2 | 4 |
| Capacity | 1 | 4 |
| Sensitivity | 2 | 8 |
| Incremental, 5 generations | 1 | 20 |
| Cache, 2 restores per engine | 1 | 6 |
| **Total** | **7** | **42** |

The operation counts expand as follows:

- **Warm-up:** one excluded, unmeasured campaign per engine. Each engine runs
  one backup and one restore: `2 engines * 2 operations = 4`.
- **Capacity:** both engines each run one backup and one restore:
  `1 campaign * 2 engines * 2 operations = 4`.
- **Sensitivity:** separate compressible and random campaigns each exercise
  both engines: `2 campaigns * 2 engines * 2 operations = 8`.
- **Incremental:** the source passes through G0 initial data, G1 0% changed,
  G2 1% changed, G3 10% changed, and G4 100% changed. Each generation runs one
  backup and one restore with both engines:
  `5 generations * 2 engines * 2 operations = 20`.
- **Cache:** each engine creates one backup followed by two sequential restores:
  `2 engines * (1 backup + 2 restores) = 6`.

Campaigns, engines, and restores execute sequentially. Therefore, the 42
operations increase total runtime and remote-repository consumption, but do not
multiply the peak live LVM PVC requirement. With a 10 GiB root disk and 20 GiB
data disk, the peak live VM PVC capacity is approximately 60 GiB plus the shared
imported-image volume: 30 GiB for the stopped source VM and 30 GiB for the one
restored VM. At most two VM objects coexist, and only one VM runs at a time.

This matrix creates about 20 `VirtualMachineBackup` objects. They and their
remote repository data are retained by default, while source VMs, restored VMs,
and their PVCs are removed as the run advances. The completed smoke validation
also showed that ready remote backups did not retain local `VolumeSnapshot`
objects, so retained backups primarily consume backup-repository rather than
LVM capacity.

### Run from the command line

First expand the default matrix without contacting or modifying the cluster:

```bash
./scripts/harvester-vmbackup-benchmark \
  --profile standard \
  --dry-run \
  --output-dir ./results
```

The command prints the complete matrix and creates a report directory under
`./results/`. A dry run does not require an image URL or kubeconfig.

Next run the smoke profile with environment-specific values:

```bash
export KUBECONFIG="/absolute/path/to/harvester-kubeconfig.yaml"
IMAGE_URL="https://example.com/path/to/cloud-image.qcow2"
IMAGE_STORAGE_CLASS="your-image-storage-class"
DATA_STORAGE_CLASS="your-data-storage-class"
SNAPSHOT_CLASS="your-volume-snapshot-class"
NAMESPACE="vmbackup-benchmark"

./scripts/harvester-vmbackup-benchmark \
  --profile smoke \
  --kubeconfig "$KUBECONFIG" \
  --namespace "$NAMESPACE" \
  --image-url "$IMAGE_URL" \
  --image-storage-class "$IMAGE_STORAGE_CLASS" \
  --data-storage-class "$DATA_STORAGE_CLASS" \
  --snapshot-class "$SNAPSHOT_CLASS" \
  --sizes-gib 10 \
  --engines restic,kopia \
  --network-name default/mgmt-vlan \
  --guest-interface-name enp1s0 \
  --node-selector network.harvesterhci.io/mgmt=true \
  --output-dir ./results
```

When the smoke run succeeds and its restored hashes are correct, run the normal
benchmark. The following uses every normal suite and five paired repetitions:

```bash
./scripts/harvester-vmbackup-benchmark \
  --profile standard \
  --kubeconfig "$KUBECONFIG" \
  --namespace "$NAMESPACE" \
  --image-url "$IMAGE_URL" \
  --image-storage-class "$IMAGE_STORAGE_CLASS" \
  --data-storage-class "$DATA_STORAGE_CLASS" \
  --snapshot-class "$SNAPSHOT_CLASS" \
  --sizes-gib 10,20,30 \
  --engines restic,kopia \
  --suites capacity,sensitivity,incremental,cache \
  --paired-runs 5 \
  --seed harvester-vmbackup-benchmark-v1 \
  --network-name default/mgmt-vlan \
  --guest-interface-name enp1s0 \
  --node-selector network.harvesterhci.io/mgmt=true \
  --cooldown 60 \
  --output-dir ./results
```

Add the fault-recovery campaigns only when needed:

```bash
--include-fault-recovery \
--fault-runs 3 \
--fault-progress-percent 30 \
--s3-pause-seconds 60
```

Run `./scripts/harvester-vmbackup-benchmark --help` for all VM sizing, SSH,
timeout, collection, retention, and suite-selection options.

### Results and benchmark cleanup

At startup the benchmark prints its run ID and artifact directory. Each run is
stored below `--output-dir` as `vmbr-<timestamp>-<token>/` and includes:

- `report.md`, `summary.json`, and `charts/duration.svg`
- `config.json`, `matrix.json`, `environment.json`, and `events.ndjson`
- `results/operations.csv` with per-operation timings, throughput, repository
  deltas, resource usage, failures, and correctness results
- `metrics/pod-samples.csv` with raw pod CPU, memory, and network samples
- Supporting Kubernetes and repository captures when collected

Source VMs, restored VMs, temporary PVCs, policies, and the imported image are
cleaned at the end. Benchmark `VirtualMachineBackup` objects are retained by
default so results can be inspected without repository deletion changing later
measurements. To delete them automatically after reporting, add
`--delete-backups`.

To explicitly clean Kubernetes resources retained by one benchmark run:

```bash
./scripts/harvester-vmbackup-benchmark \
  --kubeconfig "$KUBECONFIG" \
  --namespace "$NAMESPACE" \
  --cleanup-run vmbr-YYYYMMDDHHMMSS-TOKEN
```

Cleanup deletes resources carrying that exact benchmark run label. Deleting
retained `VirtualMachineBackup` objects may also remove their remote repository
data through the Harvester backup controller, so review the run ID first.
The GUI's **Clean all runs** action also selects benchmark resources, and
**Clean one run** accepts a `vmbr-` run ID.

## Command-line usage

Copy this example, update the environment-specific values, and run it from the
repository root:

```bash
export KUBECONFIG="/absolute/path/to/harvester-kubeconfig.yaml"
IMAGE_URL="https://example.com/path/to/cloud-image.qcow2"
IMAGE_STORAGE_CLASS="your-image-storage-class"
DATA_STORAGE_CLASS="your-data-storage-class"
NAMESPACE="default"

./scripts/harvester-csi-stress-test \
  --image-url "$IMAGE_URL" \
  --image-storage-class "$IMAGE_STORAGE_CLASS" \
  --data-storage-class "$DATA_STORAGE_CLASS" \
  --cpu 2 \
  --memory 4Gi \
  --boot-size-gib 50 \
  --data-volume-size-gib 100 \
  --boot-volume-mode Block \
  --data-volume-mode Filesystem \
  --boot-access-mode ReadWriteOnce \
  --data-access-mode ReadWriteOnce \
  --network-name default/mgmt-vlan \
  --guest-interface-name enp1s0 \
  --node-selector network.harvesterhci.io/mgmt=true \
  --iterations 3 \
  --parallel 2 \
  --vm-backup-types restic,kopia \
  --root-data-size-gib 1 \
  --data-size-gib 10 \
  --poll-interval 5 \
  --snapshot-error-grace 60 \
  --ssh-user ubuntu \
  --ssh-timeout 900 \
  --ssh-connect-timeout 30 \
  --ssh-vm-retries 1 \
  --guest-device /dev/vdb \
  --guest-mount-path /mnt/csi-stress \
  --namespace "$NAMESPACE" \
  --timeout 3600
```

This example runs two workers concurrently, with three iterations per worker,
for six backup/restore and integrity-check cycles in total. Each worker alternates
between `restic` and `kopia`; workers are offset so concurrent iterations exercise
both types. Iterations within one worker are sequential. If source or restored VM
guest readiness exceeds 15 minutes, that logical iteration is retried once with
newly created VMs and the same backup type. Set `--ssh-vm-retries 0` to disable
retries.

By default, a VM backup error may recover for up to 60 seconds before the
iteration fails. The first error and any recovery are logged. Set
`--snapshot-error-grace 0` to restore strict fail-fast behavior.

Source payload writes and restored checksum reads retry transient SSH and
`virtctl` transport failures up to the `--ssh-timeout` limit. Guest writes are
serialized so a retry cannot overlap a remote operation that survived a lost
connection. Remote validation failures, malformed checksum output, and valid
checksum mismatches are not retried. Use `--ssh-connect-timeout` to control the
timeout for each individual connection and SSH banner exchange.

`KUBECONFIG` selection uses this precedence:

1. `--kubeconfig PATH`
2. The inherited `KUBECONFIG` environment variable
3. Kubernetes' standard kubeconfig lookup

For example, to override the environment for one invocation:

```bash
./scripts/harvester-csi-stress-test \
  --kubeconfig /absolute/path/to/another-kubeconfig.yaml \
  --image-url "$IMAGE_URL" \
  --image-storage-class "$IMAGE_STORAGE_CLASS" \
  --data-storage-class "$DATA_STORAGE_CLASS" \
  --cpu 2 \
  --memory 4Gi \
  --iterations 1
```

Run `./scripts/harvester-csi-stress-test --help` for the complete option list.

To retain only the resources from an iteration that actually fails, add:

```bash
--keep-failed-resources
```

Before cleanup, every failed iteration attempt prints its failure category, the
final guest-readiness probe error when applicable, VM/VMI and storage YAML,
relevant events, launcher-pod details, compute and guest serial-console logs,
and PV/VolumeAttachment state.
Workers terminated only because another worker failed are cleaned without
producing additional failure bundles.

## Command-line cleanup

The script prints a run ID near the beginning of each stress test. To list and
remove resources belonging to one interrupted or retained run:

```bash
export KUBECONFIG="/absolute/path/to/harvester-kubeconfig.yaml"
RUN_ID="csi-stress-YYYYMMDDHHMMSS-NNNNN"

./scripts/harvester-csi-stress-test \
  --cleanup-run "$RUN_ID" \
  --namespace default
```

To list and remove resources from every detected stress and benchmark run in a namespace:

```bash
./scripts/harvester-csi-stress-test \
  --cleanup \
  --namespace default
```

Global cleanup selects stress resources using the stress-run label and generated
name prefixes, then benchmark resources using the benchmark-run label. It also
deletes retained benchmark backups, which may remove remote repository data.
Review the printed resource list carefully and prefer `--cleanup-run` when
possible.

## Operational considerations

- The image URL must be reachable by the Harvester image importer. Some image
  servers do not return a `Content-Length` header; Harvester may reject those
  downloads with `Content-Length is not found in the header`. Use a URL that
  provides the header or re-host the image on a compatible HTTP/object server.
- PVC capacity and payload size are different settings. The payload must be
  smaller than the PVC, and the root disk must also have room for the guest OS,
  packages, logs, and filesystem metadata.
- Capacity usage scales with `--parallel` because every worker can have a source
  VM, backup artifacts, and restored storage at the same time. Remote backups also
  consume capacity on the configured backup target.
- The data disk is formatted as ext4 inside the guest when it has no existing
  filesystem signature. The script refuses to format the root device or a disk
  with an unexpected signature.
- The default guest data device is `/dev/vdb`; verify the device name for custom
  images before running large payloads.
- Cloud-init enables DHCP on the selected guest interface, installs
  `qemu-guest-agent`, injects the selected SSH public key, enables password SSH,
  and sets the temporary password `password1234`. Use an isolated test network,
  especially with **Keep resources** enabled.
- The default node selector requires nodes labeled
  `network.harvesterhci.io/mgmt=true`. Use an empty node selector when that label
  is not present or another selector appropriate for the cluster.
- `ReadWriteOnce` is the recommended access mode. Select only modes supported by
  the CSI driver and suitable for writable VM disks.
- Generated resources are removed by default. Enable **Keep resources** or pass
  `--keep-resources` only when retained VMs, PVCs, backups, and snapshots are
  needed for investigation.
- Prefer **Keep only failed iteration resources** or
  `--keep-failed-resources` for long parallel runs. It preserves the failed
  iteration without retaining every successful iteration.
- Use a dedicated namespace and non-production storage classes when possible;
  the test creates, backs up, restores, formats, and deletes storage resources.

## Troubleshooting

- **The GUI cannot be opened through a remote IP:** use the SSH tunnel described
  above and browse to `localhost`.
- **Fields are locked after refreshing:** an operation is still active. The GUI
  intentionally restores its parameters and prevents changes until it finishes
  or is stopped.
- **Insufficient root or data space:** increase the corresponding PVC capacity,
  reduce its payload, and allow extra root-disk space for the cloud image.
- **VM scheduling does not start:** verify the Multus network, node selector,
  requested CPU/memory, storage classes, PVC modes, and access modes.
- **SSH never becomes ready:** verify the image's default user, cloud-init
  support, guest interface name, DHCP, package access, and passwordless `sudo`.
  The failure output identifies whether connectivity/authentication, required
  guest commands, or passwordless sudo was the last incomplete stage. Use
  `--ssh-timeout` to change the shared readiness and operation-retry limit,
  `--ssh-connect-timeout` to change each connection/banner timeout,
  `--ssh-vm-retries` to retry with fresh VMs, and
  `--keep-failed-resources` to retain the final failed attempt.
- **A guest operation reports an SSH transport failure:** increase
  `--ssh-timeout` if the API or `virtctl` tunnel is temporarily unstable. A
  valid checksum mismatch always fails immediately regardless of this setting.
- **Resources remain after interruption:** use **Clean one run** in the GUI or
  `--cleanup-run RUN_ID` from the CLI.
