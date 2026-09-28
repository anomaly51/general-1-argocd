"""Extend only the audited Harbor root LV using a newly allocated, serial-pinned disk."""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

HOST = "general-1-worker-3"
MACHINE = "2da91eddca4d4368bd8a0da3b889bc1b"
SERIAL = "harbor-storage-v1"
DISK_BYTES = 64 * 1024**3
VG = "MiWiFi-RD18-srv-vg"
VG_UUID = "C2SD2s-D9aO-GfRp-XqLL-NlkL-7eGF-2v7m1x"
LV_UUID = "hNiCGA-tDBE-ZR4e-s2th-mkJd-YQPV-hRwuVs"
ROOT = f"/dev/{VG}/root"
STATE = Path("/var/lib/harbor-storage")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def table(command, section, fields):
    return json.loads(run(command, "--reportformat", "json", "--units", "b",
                          "--nosuffix", "-o", fields))["report"][0][section]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def require_blank_disk(disk):
    require(not run("lsblk", "-nr", "-o", "MOUNTPOINTS", disk), "Disk is mounted")
    require(len(run("lsblk", "-nr", "-o", "NAME", disk).splitlines()) == 1, "Disk has child devices")
    require(not run("wipefs", "--no-act", "--noheadings", "--output", "TYPE", disk), "Disk has existing signatures")
    with open(disk, "rb", buffering=0) as source:
        require(not any(source.read(1024**2)), "Disk header is not blank")
        source.seek(DISK_BYTES - 1024**2)
        require(not any(source.read(1024**2)), "Disk trailer is not blank")


def main():
    require(socket.gethostname() == HOST, "Wrong host")
    require(Path("/etc/machine-id").read_text().strip() == MACHINE, "Wrong machine identity")
    require(run("findmnt", "-no", "FSTYPE", "/") == "ext4", "Unexpected root filesystem")
    require(Path(run("findmnt", "-no", "SOURCE", "/")).resolve() == Path(ROOT).resolve(), "Wrong root LV")
    volumes = table("lvs", "lv", "lv_name,lv_uuid,vg_name,lv_size")
    root = next(v for v in volumes if v["vg_name"] == VG and v["lv_name"] == "root")
    require(root["lv_uuid"] == LV_UUID, "Wrong root LV identity")
    group = next(v for v in table("vgs", "vg", "vg_name,vg_uuid") if v["vg_name"] == VG)
    require(group["vg_uuid"] == VG_UUID, "Wrong volume group identity")

    devices = json.loads(run("lsblk", "--json", "--nodeps", "-o", "PATH,SERIAL"))["blockdevices"]
    candidates = {Path(device["path"]).resolve() for device in devices if device.get("serial") == SERIAL}
    require(len(candidates) == 1, "New Harbor disk must be uniquely identified")
    disk = str(candidates.pop())
    require(run("lsblk", "-dn", "-o", "TYPE", disk) == "disk", "Expected a whole disk")
    require(run("lsblk", "-dn", "-o", "SERIAL", disk) == SERIAL, "Disk serial mismatch")
    require(int(run("blockdev", "--getsize64", disk)) == DISK_BYTES, "Disk capacity mismatch")

    if "--check" in sys.argv:
        require_blank_disk(disk)
        require(not any(str(Path(p["pv_name"]).resolve()) == disk
                        for p in table("pvs", "pv", "pv_name")), "Disk already belongs to LVM")
        print("Read-only preflight passed: audited host, LV/VG and blank 64 GiB Harbor disk")
        return

    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        intent = STATE / "intent.json"
        identity = {"machine": MACHINE, "serial": SERIAL, "bytes": DISK_BYTES,
                    "vg_uuid": VG_UUID, "lv_uuid": LV_UUID}
        pvs = table("pvs", "pv", "pv_name,pv_uuid,vg_name,pv_pe_count,pv_pe_alloc_count")
        existing = next((p for p in pvs if str(Path(p["pv_name"]).resolve()) == disk), None)
        if intent.exists():
            require(json.loads(intent.read_text()) == identity, "Recorded operation does not match")
        else:
            require(existing is None, "Refusing an existing physical volume without our intent record")
            require(18 * 1024**3 < float(root["lv_size"]) < 20 * 1024**3, "Root has changed since audit")
            require_blank_disk(disk)
            run("vgcfgbackup", "-f", str(STATE / "before.vg"), VG)
            with intent.open("x") as record:
                json.dump(identity, record)
                record.flush()
                os.fsync(record.fileno())

        if existing is None:
            require_blank_disk(disk)
            run("pvcreate", "--yes", disk)
        else:
            require(existing["vg_name"] in {"", VG}, "Disk belongs to a different volume group")
        if existing is None or existing["vg_name"] == "":
            run("vgextend", VG, disk)

        current = next(p for p in table("pvs", "pv", "pv_name,vg_name,pv_pe_count,pv_pe_alloc_count")
                       if str(Path(p["pv_name"]).resolve()) == disk)
        require(current["vg_name"] == VG, "Unexpected PV membership")
        # Only consume free extents on this particular new disk, never other PVs.
        segments = table("pvs", "pv", "pv_name,lv_name")
        require(all(p.get("lv_name", "") in {"", "root"} for p in segments
                    if str(Path(p["pv_name"]).resolve()) == disk), "New disk is used by another LV")
        free = int(current["pv_pe_count"]) - int(current["pv_pe_alloc_count"])
        if free:
            run("lvextend", "--yes", "-l", "+" + str(free), ROOT, disk)
        run("resize2fs", ROOT)
        size = os.statvfs("/").f_blocks * os.statvfs("/").f_frsize
        require(size > 80 * 1024**3, "Root filesystem expansion did not finish")
        (STATE / "complete.json").write_text(json.dumps({**identity, "filesystem_bytes": size}))
        print(f"Verified {HOST}: root filesystem now {size / 1024**3:.1f} GiB; original PV and data retained")


if __name__ == "__main__":
    main()
