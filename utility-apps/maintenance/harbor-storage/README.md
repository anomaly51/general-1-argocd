# Harbor storage capacity

The Harbor registry uses a local-path volume on general-1-worker-3. CI image uploads exhausted its 18 GiB root filesystem and triggered DiskPressure evictions.

This one-time Argo CD Job adds a new 64 GiB disk (Proxmox machine-2 / VM 114 / scsi2, serial harbor-storage-v1) to the existing root LVM volume and grows ext4 online. Existing storage and the separate encrypted swap disk remain intact; no reboot is required.

The script checks the node hostname, machine ID, disk serial and size, root LV/VG identity, and absence of signatures, partitions, mounts or nonzero header/trailer data before initializing the new disk. It backs up LVM metadata and records operation state in /var/lib/harbor-storage. Retries consume only remaining extents on that disk.

Both original and added storage disks must remain attached and included in VM backups. The expanded root filesystem spans both disks; removing the new disk is not a rollback. No automatic shrinking or destructive rollback is provided.
