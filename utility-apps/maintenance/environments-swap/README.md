# Environment swap

Adds one dedicated **16 GiB encrypted swap disk per worker**, using the guarded
lifecycle from `../studio-swap`. Existing swap and all data disks remain intact.
Each disk is identified by serial `environments-swap-v1`, exact size, hostname
and machine ID. Only a fully blank disk may be claimed. The encryption key is
random on each boot and never stored. No service-account token or API credentials
are mounted in the maintenance jobs.

VM109 (worker 1): scsi2; VM113 and VM114 (workers 2 and 3): scsi1.
Proxmox disks are allocated from local-lvm, 16 GiB, backup disabled. RAM is unchanged.
Host config and activation run through Argo CD; phases and node order are recorded
in values.yaml. Dependencies are prepared first. Activate one target at a time,
verify its Ready state, effective LimitedSwap config and container health, then
continue. Each job has zero automatic retries. All original kubelet config hashes
must match the audit. The existing studio-swap files on worker 1 are preserved.

Managed files use the `environments-swap` prefix; the kubelet drop-in is
`95-environments-swap.conf`. Boot enables swap before k3s-agent. Activation
restarts only k3s-agent with verified KillMode=process; running containers must
survive. Failed activation restores the two original drop-ins. Swap remains on
because reclaiming live swap can OOM workloads. Removing this chart does not
remove host swap; decommissioning needs a separate operation.

Swap does not increase scheduler allocatable memory. Burstable containers need
memory requests below limits to receive LimitedSwap capacity. Production limits
are not lowered automatically.
