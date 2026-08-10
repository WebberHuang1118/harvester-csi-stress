# Harvester CSI Stress Test

This repository tests Harvester VM snapshot and restore workflows against a CSI
storage driver. It can be operated from the command line or from a local web GUI.

The test imports one Harvester `VirtualMachineImage` for the run, starts the
requested number of workers in parallel, and performs the configured number of
iterations sequentially inside each worker. For example, `--parallel 3
--iterations 10` runs three concurrent workers and 30 total iterations.

During each iteration, the test:

1. Generates and creates a minimal KubeVirt VM with boot and data PVCs.
2. Waits for the VM, both PVCs, and guest SSH access.
3. Writes random payloads to the root filesystem and data disk and records their
   SHA-256 checksums.
4. Creates a Harvester `VirtualMachineBackup` with filesystem freeze enabled.
5. Stops the source VM and restores the backup as a new VM.
6. Recalculates both checksums on the restored VM and verifies data integrity.
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

- **Cluster connection:** Kubeconfig, namespace, and per-operation timeout
- **VM and storage:** image URL, image/data storage classes, CPU, memory, PVC
  capacities, volume modes, and access modes
- **VM network:** Multus network, guest interface name, and required node selector
- **Workload:** iterations per worker, parallel workers, root/data payload sizes,
  and polling interval
- **Guest access:** SSH user/key, guest data device, mount path, and resource
  retention

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
| vCPU / memory | `2` / `4Gi` |
| Boot PVC | `50Gi`, `Block`, `ReadWriteOnce` |
| Data PVC | `100Gi`, `Filesystem`, `ReadWriteOnce` |
| Multus network | `default/mgmt-vlan` |
| Guest interface | `enp1s0` |
| Required node selector | `network.harvesterhci.io/mgmt=true` |
| Iterations / parallel workers | `1` / `1` |
| Root / data payload | `1Gi` / `10Gi` |
| Polling interval | `5` seconds |
| SSH user | `ubuntu` |
| Guest data device / mount | `/dev/vdb` / `/mnt/csi-stress` |
| Keep resources | Disabled |

The GUI validates values before launching the process. In particular:

- Numeric settings must be positive integers.
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

The GUI also provides two cleanup modes:

- **Clean one run:** enter the run ID shown in the output to remove resources
  from that run.
- **Clean all runs:** list and remove detected stress-test resources in the
  selected namespace. This mode requires an additional confirmation.

Prefer single-run cleanup when the run ID is known.

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
  --root-data-size-gib 1 \
  --data-size-gib 10 \
  --poll-interval 5 \
  --ssh-user ubuntu \
  --guest-device /dev/vdb \
  --guest-mount-path /mnt/csi-stress \
  --namespace "$NAMESPACE" \
  --timeout 3600
```

This example runs two workers concurrently, with three iterations per worker,
for six snapshot/restore and integrity-check cycles in total. Iterations within
one worker are sequential.

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

To list and remove resources from every detected stress-test run in a namespace:

```bash
./scripts/harvester-csi-stress-test \
  --cleanup \
  --namespace default
```

Global cleanup selects resources using the stress-run label and generated name
prefixes. Review the printed resource list carefully and prefer `--cleanup-run`
when possible.

## Operational considerations

- The image URL must be reachable by the Harvester image importer. Some image
  servers do not return a `Content-Length` header; Harvester may reject those
  downloads with `Content-Length is not found in the header`. Use a URL that
  provides the header or re-host the image on a compatible HTTP/object server.
- PVC capacity and payload size are different settings. The payload must be
  smaller than the PVC, and the root disk must also have room for the guest OS,
  packages, logs, and filesystem metadata.
- Capacity usage scales with `--parallel` because every worker can have a source
  VM, snapshots, and restored storage at the same time.
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
- Use a dedicated namespace and non-production storage classes when possible;
  the test creates, snapshots, restores, formats, and deletes storage resources.

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
- **Resources remain after interruption:** use **Clean one run** in the GUI or
  `--cleanup-run RUN_ID` from the CLI.
