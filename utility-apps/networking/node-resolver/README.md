# Node DNS recovery

The Debian nodes use dhcpcd. On 2026-09-07 the control-plane node had
an empty `/etc/resolv.conf`: NTP could not resolve its servers, the clock
lagged by 32 seconds, and Telethon rejected incoming messages as too new.

This DaemonSet supplies the existing LAN resolver (192.168.1.2) only when
a nameserver is missing. The dhcpcd `resolv.conf.head` file preserves it
across lease renewals; the loop also repairs the current resolver file.
Existing nameservers and other resolver settings are preserved.

Only these two files are mounted writable. No privileged container,
host PID namespace, clock capability, or Kubernetes API access is needed.
The host's existing systemd-timesyncd service remains responsible for NTP.

Validation: all DaemonSet pods should run, node DNS lookups should work,
and `timedatectl timesync-status` should show a selected server and packets.
Removing the chart stops reconciliation; resolver entries remain on disk.
