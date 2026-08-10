# Harvester CSI Stress Test

This repository contains a Bash stress test for Harvester VM snapshot and restore
workflows backed by CSI storage.

It can be run from the command line or through a local browser interface at
`http://localhost:7000`.

For every worker and iteration, the test:

1. Imports a Harvester `VirtualMachineImage`.
2. Creates a minimal KubeVirt VM with configurable boot and data PVCs.
3. Writes random data to both disks and records their SHA-256 checksums.
4. Creates a `VirtualMachineBackup` snapshot.
5. Restores the snapshot as a new VM.
6. Verifies that the restored data matches both original checksums.
7. Deletes the resources created by the iteration unless `--keep-resources` is
   specified.

## Local browser interface

Start the local service from the repository root:

```bash
./scripts/harvester-csi-stress-service
```

Then open [http://localhost:7000](http://localhost:7000). The service binds only
to `127.0.0.1`; it is not exposed to other machines.

The browser interface provides:

- Editable fields for every stress-test parameter and its default value
- Stress-test, single-run cleanup, and all-runs cleanup modes
- Validation before the Bash process starts
- One managed operation at a time
- Live output with timestamps, worker context, colors, filters, and search
- Iteration progress, run ID, exit status, recent-run history, and command or
  full-output copy
- A stop action that signals the script and allows its cleanup trap to run

To open the browser automatically or select another local port:

```bash
./scripts/harvester-csi-stress-service --open-browser
./scripts/harvester-csi-stress-service --port 7100
```

Press `Ctrl+C` in the service terminal to stop it. If an operation is active,
the service requests a clean stop and waits for the script's resource cleanup.
Run history is kept in memory and is cleared when the service exits.

## Prerequisites

Run the script from a workstation that has:

- Bash 5.1 or newer
- Python 3.10 or newer for the browser interface; it has no third-party Python
  dependencies
- `kubectl`
- `virtctl`
- `jq`
- `base64`
- `ssh-keygen`
- Access to a Harvester cluster with the required Harvester, KubeVirt, CDI, and
  VolumeSnapshot APIs
- An image storage class that supports the selected boot PVC mode
- A data storage class that supports the selected data PVC mode and snapshots

An external VM template is not required. The script generates a minimal KubeVirt VM
with `amd64` architecture, a `q35` machine, `virtio` disks and networking,
`RerunOnFailure`, ACPI, and inline cloud-init data. PVC capacities, volume modes,
access modes, Multus networking, guest interface, and node affinity are configured
with command-line parameters.

## Run the stress test

Copy this block, replace the values in the first section, and run it from the
repository root:

```bash
# Update these values for your environment.
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
  --namespace "$NAMESPACE"
```

This example runs two workers concurrently, with three iterations per worker,
for six snapshot and restore cycles in total. Each iteration writes 1 GiB to the
root disk and 10 GiB to the data disk by default. `--boot-size-gib` and
`--data-volume-size-gib` control PVC capacities; `--root-data-size-gib` and
`--data-size-gib` control how much random payload is written. Leave enough space
for the guest operating system and filesystem metadata.

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

Run `./scripts/harvester-csi-stress-test --help` for all available options,
including payload sizes, timeouts, SSH settings, guest device selection, and
resource retention.

## Cleanup

The script prints a run ID when it starts. To delete resources belonging to one
interrupted or retained run:

```bash
export KUBECONFIG="/absolute/path/to/harvester-kubeconfig.yaml"
RUN_ID="csi-stress-YYYYMMDDHHMMSS-NNNNN"

./scripts/harvester-csi-stress-test \
  --cleanup-run "$RUN_ID" \
  --namespace default
```

To list and delete resources from every stress-test run in a namespace:

```bash
./scripts/harvester-csi-stress-test \
  --cleanup \
  --namespace default
```

The global cleanup command selects resources by the stress-test label and by
generated name prefixes. Review its listed resources carefully and prefer
`--cleanup-run` when possible.

## Operational notes

- By default, generated resources are removed after each iteration.
- `--keep-resources` retains resources for investigation.
- The default guest data device is `/dev/vdb` and the mount point is
  `/mnt/csi-stress`.
- The injected cloud-init configuration expects an Ubuntu-style guest with
  passwordless `sudo` and package access to install `qemu-guest-agent`. DHCP is
  configured on the interface selected by `--guest-interface-name`.
- The default node selector is `network.harvesterhci.io/mgmt=true`. Pass
  `--node-selector ''` to disable required node affinity.
- The script creates and deletes Kubernetes resources. Use a dedicated namespace
  for safer testing when possible.
