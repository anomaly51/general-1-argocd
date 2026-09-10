# Studio encrypted swap maintenance

This chart was initially reviewed **while suspended**; its Git values record
the explicitly approved operational phase. It manages only General1 worker 1
(`general-1-worker-1`, machine-id `b233eaca510b4f35a4a3715cf11fa89d`) and the
new dedicated **8 GiB** disk with serial `studio-swap-v1`. The disk was hot-added
to VM109; this chart has no Proxmox access and never changes RAM or restarts a VM.
The utility ApplicationSet discovers it as `maintenance-studio-swap`.

## Two reviewed phases

1. `phase: prepareDependencies`, `suspended: false`: validate host identity,
   active `k3s-agent` with `KillMode=process` and `Delegate=yes`, root filesystem
   free space at least 1 GiB, and the audited kubelet config hashes. Discover
   the exact disk by serial (never by a hard-coded `/dev/sdb` or stale by-id
   link), reject partitions/signatures/mounts/foreign holders, and read all
   8 GiB to prove a new unowned disk is blank. Trigger udev change only for that
   verified block device, then settle. If missing, install `cryptsetup-bin` and
   its required new Debian dependencies through existing signed APT sources.
   Simulation rejects removals, upgrades of installed packages, and packages
   outside the reviewed dependency closure. Actual versions are pinned to the
   simulation; no recommends/upgrades/removals are requested. Index/install
   transient systemd units have 240/360-second runtime bounds, 60-second stop
   bounds and a 60-second package-lock wait. Each new APT unit is limited to
   256 MiB RAM, 256 MiB swap and 50% of one CPU. No apt verification bypass,
   repository change, forced lock removal or automatic repair is used. This
   phase does not restart k3s or enable swap.
2. Review the completed dependency report. Change to `phase: activate`, keeping
   `suspended: true` until the separate activation review; then explicitly set
   it false. This creates a different Job. The previous phase cannot activate
   swap on its own. Active Jobs are never automatically retried (`backoffLimit: 0`).

The job is privileged with host PID access and one host-root mount because it
must manage host block devices/systemd. It has **no service account token,
RBAC, Secret references, network credentials or Kubernetes client operations**.
Host commands use `chroot` + `nsenter` and Python on stdin. Only fixed operations
exist; the script accepts no arbitrary shell commands or target paths.

## Activation and reboot behavior

The initial full-zero check precedes the root-only ownership record. The record
binds serial, size, hostname, machine-id and exact hashes of every managed file.
Existing unowned files, changed ownership/configuration, formatted raw disks,
partitions, mounted filesystems and foreign device-mapper holders fail closed.
The owned re-run path accepts an existing active `[SWAP]` child only after
checking its exact PLAIN mapping and underlying serial-verified device.

The `studio-swap.service` oneshot opens only this disk as a PLAIN dm-crypt mapping
with a new 512-bit random key from `/dev/urandom`, stored only in kernel memory.
It creates the swap signature **inside** that mapping, then enables it with
priority 100. The key is not persisted. After reboot the old ciphertext is
unreadable; the service rekeys/reinitializes only the recorded disk. No raw
partition, existing swap device, data filesystem or `/etc/fstab` is modified.

Managed host files:

- `/var/lib/studio-swap/ownership.json`
- `/etc/studio-swap/plan.json`
- `/usr/local/libexec/studio-swap.py`
- `/etc/systemd/system/studio-swap.service`
- `/etc/systemd/system/k3s-agent.service.d/90-studio-swap.conf` (ordering only)
- `/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/90-studio-swap.conf`

The kubelet drop-in sets `failSwapOn=false`, `memorySwap.swapBehavior=LimitedSwap`
and `evictionHard.memory.available=200Mi`. It repeats the audited
`imagefs.available=5%` and `nodefs.available=5%` entries because a drop-in replaces
the map. Other kubelet files and disk reclaim settings are unchanged. Their
SHA256 map is checked before and after the restart. Explicit `kubelet-arg`
process arguments fail the preflight; the reviewed systemd/environment/config
audit must also remain valid. K3s v1.35.4 supports this native drop-in directory.
[K3s configuration documentation](https://docs.k3s.io/installation/configuration#kubelet-configuration-files)

Swap becomes active **before** restarting only `k3s-agent.service`, so kubelet
observes the new capacity at startup. Its process-only KillMode is mandatory.
The job checks local kubelet health, active agent state and that every previously
running container ID remains present. It does not stop containerd, delete pods,
drain nodes, run a killall script or change service-wide swap policies. In
particular, the already-swapped k3s daemon keeps its previous swap policy.

If activation/restart/health checks fail, the job restores only its previous
kubelet and agent drop-ins, reloads systemd, and restarts the same agent to
recover its earlier configuration. The audited original `failSwapOn=false` is
required for this rollback. **Encrypted swap stays active**: there is no
`swapoff`, including in `ExecStop`, since reclaiming live swap could OOM other
services. Review failures before another explicit Job operation. Chart deletion
does not undo host files or swap; decommissioning requires a separate plan.

## Required external verification

Do not enable Studio API merely because this Job completed. With the explicit
General1 kubeconfig, verify W1 Ready, existing application readiness/restart
counts, `configz` effective LimitedSwap and all three eviction thresholds,
node swap capacity, and the new API container's finite nonzero
`memory.swap.max`. Confirm that VM109 is still 3072 MiB and its other disks are
unchanged. No forced refresh, sync or node mutation is required for these reads.

LimitedSwap needs cgroup v2 and a noncritical Burstable container whose memory
request is below its memory limit. Its swap allowance is proportional to the
request divided by physical node RAM, not to `limit - request`. Swap does not
increase scheduler allocatable RAM. `/dev/shm` memory-backed volumes remain
resident on this kernel. Keep the API single-replica and run only one heavy
operation during the canary. Global serialization is not yet unified: the
generation semaphore does not cover direct renders, imports and pose work
together. Production memory is not validated yet; measure the migrated-project
export's combined API/Chrome cgroup peak and swap, not Node process RSS alone.
[Kubernetes 1.35 swap behavior](https://v1-35.docs.kubernetes.io/docs/concepts/cluster-administration/swap-memory-management/)

Local verification (no host execution):

```sh
rtk python3 -m unittest discover -s utility-apps/maintenance/studio-swap/tests -v
rtk proxy helm lint utility-apps/maintenance/studio-swap
rtk proxy helm template studio-swap utility-apps/maintenance/studio-swap --namespace maintenance
```

The Python tests fake host commands and use temporary files. Never invoke
`--boot` or `--enter-host` on a workstation as a test.
