"""Fixed, guarded encrypted-swap install. No Kubernetes API or VM API.

The host-resident --boot mode rekeys ONLY the recorded dedicated disk. No code
path issues swapoff, formats an existing partition, or stops a VM/container.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request

OWNER = "cutline-studio-encrypted-swap-v1"
SERIAL = "studio-swap-v1"
SIZE = 8 * 1024**3
MAPPING = Path("/dev/mapper/studio-swap-v1")
STATE = Path("/var/lib/studio-swap/ownership.json")
PLAN = Path("/etc/studio-swap/plan.json")
HELPER = Path("/usr/local/libexec/studio-swap.py")
UNIT = Path("/etc/systemd/system/studio-swap.service")
AGENT_DROPIN = Path("/etc/systemd/system/k3s-agent.service.d/90-studio-swap.conf")
KUBELET_DIR = Path("/var/lib/rancher/k3s/agent/etc/kubelet.conf.d")
KUBELET_DROPIN = KUBELET_DIR / "90-studio-swap.conf"


class GuardError(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise GuardError(message)


def log(phase: str, **safe) -> None:
    print(json.dumps({"phase": phase, **safe}), flush=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_plan(plan: dict, activation=True) -> None:
    require(re.fullmatch(r"general-1-worker-[123]", plan.get("hostname", "")) is not None, "invalid target hostname")
    require(re.fullmatch(r"[a-f0-9]{32}", plan.get("machine_id", "")) is not None, "invalid machine identity")
    require(plan.get("serial") == SERIAL and plan.get("size_bytes") == SIZE, "dedicated exact 8GiB disk required")
    require(plan.get("root_reserve_bytes", 0) >= 1024**3, "root reserve must be at least 1GiB")
    if activation:
        require(plan.get("no_kubelet_overrides_verified") is True, "kubelet argument audit missing")
    require(plan.get("baseline_fail_swap_on") is False, "rollback requires audited original failSwapOn=false")
    hashes = plan.get("expected_kubelet_hashes", {})
    require(isinstance(hashes, dict) and bool(hashes), "kubelet baseline hashes missing")
    require(all(re.fullmatch(r"[a-zA-Z0-9_.-]+\.conf", name) and re.fullmatch(r"[a-f0-9]{64}", value)
                for name, value in hashes.items()), "invalid kubelet baseline hash map")
    require(KUBELET_DROPIN.name not in hashes, "owned drop-in must not be an original baseline file")
    require(plan.get("disk_eviction_hard") == {"imagefs.available": "5%", "nodefs.available": "5%"},
            "disk eviction thresholds differ from reviewed baseline")


def validate_identity(plan: dict, hostname: str, machine_id: str) -> None:
    require(hostname == plan["hostname"], "host name mismatch")
    require(machine_id.strip() == plan["machine_id"], "host machine-id mismatch")


def validate_service(properties: dict) -> None:
    require(properties.get("ActiveState") == "active", "k3s-agent must already be active")
    require(properties.get("KillMode") == "process", "k3s-agent KillMode must be process")
    require(properties.get("Delegate") == "yes", "k3s-agent must delegate container cgroups")
    require(int(properties.get("MainPID", "0")) > 1, "k3s-agent has no live main process")


def select_disk(devices: list[dict], plan: dict) -> dict:
    found = [item for item in devices if str(item.get("serial") or "").strip() == plan["serial"]]
    require(len(found) == 1, "expected exactly one disk with the dedicated serial")
    disk = found[0]
    require(disk.get("type") == "disk" and int(disk.get("size", 0)) == SIZE, "wrong disk type or size")
    require(not disk.get("ro"), "dedicated disk is read-only")
    require(not any(disk.get("mountpoints") or []), "dedicated disk is mounted")
    require(re.fullmatch(r"/dev/[a-zA-Z0-9_-]+", disk.get("path", "")) is not None, "unexpected disk path")
    require(not disk.get("fstype"), "dedicated raw disk has a filesystem signature")
    # A dm-crypt child is allowed only after ownership/mapping validation below.
    require(all(child.get("type") == "crypt" and child.get("name") == SERIAL
                and all(point in (None, "", "[SWAP]") for point in child.get("mountpoints") or [])
                for child in disk.get("children", [])), "partition or unrelated holder on dedicated disk")
    return disk


def validate_owner(owner: dict, plan: dict) -> None:
    expected = {"owner": OWNER, "hostname": plan["hostname"], "machine_id": plan["machine_id"],
                "serial": SERIAL, "size_bytes": SIZE}
    require(all(owner.get(key) == value for key, value in expected.items()), "disk ownership record mismatch")


def kubelet_config(plan: dict) -> bytes:
    # A map is replaced as a whole by kubelet drop-ins: retain both disk guards.
    return (json.dumps({"apiVersion": "kubelet.config.k8s.io/v1beta1", "kind": "KubeletConfiguration",
                        "failSwapOn": False, "memorySwap": {"swapBehavior": "LimitedSwap"},
                        "evictionHard": {**plan["disk_eviction_hard"], "memory.available": "200Mi"}},
                       indent=2) + "\n").encode()


def unit_text() -> bytes:
    return b"""[Unit]
Description=Cutline dedicated encrypted ephemeral-key swap
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service
Before=k3s-agent.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -B /usr/local/libexec/studio-swap.py --boot
RemainAfterExit=yes
TimeoutStartSec=180
# Deliberately no ExecStop: never swapoff live memory during stop/rollback.

[Install]
WantedBy=multi-user.target
"""


def agent_dropin() -> bytes:
    return b"""[Unit]
Wants=studio-swap.service
After=studio-swap.service
"""


def check_regular_path(path: Path, may_be_missing=True) -> None:
    for item in [path, *path.parents]:
        require(not item.is_symlink(), "managed path traverses a symlink")
    if path.exists():
        require(path.is_file(), "managed path is not a regular file")
        info = path.stat()
        require(info.st_uid == 0 and not (info.st_mode & 0o022), "managed file ownership or permissions unsafe")
    else:
        require(may_be_missing, "required managed file missing")


def atomic_write(path: Path, data: bytes, mode=0o600) -> None:
    check_regular_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = path.with_name(path.name + ".studio-swap-tmp")
    require(not temporary.exists() and not temporary.is_symlink(), "stale owned temporary file requires review")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(args: list[str], timeout=30, accepted=(0,)) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    # Never include command arguments or stderr: host service configuration could contain secrets.
    require(result.returncode in accepted, f"host command {Path(args[0]).name} failed (exit {result.returncode})")
    return result.stdout


def service_state() -> dict:
    text = run(["systemctl", "show", "k3s-agent.service", "--property=ActiveState,KillMode,Delegate,MainPID"])
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def read_owner(plan: dict) -> dict | None:
    check_regular_path(STATE)
    if not STATE.exists():
        return None
    owner = json.loads(STATE.read_text())
    validate_owner(owner, plan)
    return owner


def disk_inventory(plan: dict) -> dict:
    data = json.loads(run(["lsblk", "--json", "--bytes", "--output", "NAME,PATH,TYPE,SIZE,SERIAL,MOUNTPOINTS,FSTYPE,RO"]))
    return select_disk(data["blockdevices"], plan)


def signatures(path: str) -> list:
    return json.loads(run(["wipefs", "--json", "--no-act", path])).get("signatures", [])


def active_swaps() -> set[str]:
    return {os.path.realpath(line.split()[0]) for line in Path("/proc/swaps").read_text().splitlines()[1:] if line.split()}


def mapping_matches(device: str) -> bool:
    output = run(["cryptsetup", "status", SERIAL], accepted=(0, 4))
    fields = dict(line.strip().split(":", 1) for line in output.splitlines() if ":" in line)
    return fields.get("type", "").strip() == "PLAIN" and os.path.realpath(fields.get("device", "").strip()) == os.path.realpath(device)


def guard_disk(disk: dict, owner: dict | None) -> None:
    path = disk["path"]
    require(stat.S_ISBLK(os.stat(path).st_mode), "dedicated path is not a block device")
    require(int(run(["blockdev", "--getsize64", path]).strip()) == SIZE, "disk size changed")
    require(not signatures(path), "dedicated raw disk has signatures")
    require(os.path.realpath(path) not in active_swaps(), "raw dedicated disk is already used as swap")
    holders = list((Path("/sys/class/block") / Path(path).name / "holders").iterdir())
    if MAPPING.exists() or MAPPING.is_symlink():
        require(owner is not None and mapping_matches(path), "unowned or mismatched crypt mapping")
        require(all((holder / "dm/name").read_text().strip() == SERIAL for holder in holders), "unrelated disk holder")
    else:
        require(not holders and not disk.get("children"), "dedicated disk has an unknown holder")
    if owner is None:
        require(not disk.get("children"), "new disk cannot have existing children")
        # Check every byte, not only known signatures: initial ownership requires a genuinely blank new disk.
        log("blank-disk-check-start", diskSerial=SERIAL, diskBytes=SIZE)
        fd = os.open(path, os.O_RDONLY | os.O_EXCL)
        try:
            remaining = SIZE
            while remaining:
                block = os.read(fd, min(8 * 1024**2, remaining))
                require(bool(block), "short read during blank-disk check")
                require(not any(block), "dedicated disk contains data; refusing ownership")
                remaining -= len(block)
        finally:
            os.close(fd)
        log("blank-disk-check-complete", diskSerial=SERIAL, diskBytes=SIZE)


def ensure_swap(disk: dict) -> None:
    if not MAPPING.exists():
        run(["cryptsetup", "open", "--type", "plain", "--cipher", "aes-xts-plain64", "--key-size", "512",
             "--key-file", "/dev/urandom", "--keyfile-size", "64", disk["path"], SERIAL], timeout=60)
    require(mapping_matches(disk["path"]), "crypt mapping no longer matches owned disk")
    if os.path.realpath(MAPPING) in active_swaps():
        return
    found = signatures(str(MAPPING))
    require(all(item.get("type") == "swap" for item in found), "unexpected content inside crypt mapping")
    if not found:
        run(["mkswap", "--label", SERIAL, str(MAPPING)])
    run(["swapon", "--priority", "100", str(MAPPING)])
    require(os.path.realpath(MAPPING) in active_swaps(), "owned encrypted swap is not active")


def check_baseline(plan: dict) -> None:
    current = {}
    for path in KUBELET_DIR.glob("*.conf"):
        if path == KUBELET_DROPIN:
            continue
        check_regular_path(path, may_be_missing=False)
        current[path.name] = digest(path.read_bytes())
    require(current == plan["expected_kubelet_hashes"], "kubelet configuration changed since audit")


def wait_healthy(timeout=150) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_state().get("ActiveState") == "active":
            try:
                with urllib.request.urlopen("http://127.0.0.1:10248/healthz", timeout=3) as response:
                    if response.status == 200 and response.read(32).strip() == b"ok":
                        return
            except OSError:
                pass
        time.sleep(2)
    raise GuardError("k3s-agent or kubelet health failed after restart")


def running_containers() -> set[str]:
    return {line.strip() for line in run(["k3s", "crictl", "ps", "-q"], timeout=45).splitlines() if re.fullmatch(r"[a-f0-9]{8,64}", line.strip())}


def dependency_packages(simulation: str, installed: set[str]) -> list[str]:
    # Debian trixie's cryptsetup-bin dependency closure. Existing packages may
    # satisfy it; a new/unexpected dependency or any installed-package upgrade fails closed.
    allowed = {"cryptsetup-bin", "libcryptsetup12", "libargon2-1", "libdevmapper1.02.1",
               "libjson-c5", "libssl3t64", "libuuid1", "libpopt0", "libc6"}
    result = []
    for line in simulation.splitlines():
        require(not line.startswith("Remv "), "dependency plan would remove a package")
        if not line.startswith("Inst "):
            continue
        match = re.match(r"Inst ([a-z0-9.+-]+)(?::amd64)? (?:\[[^]]+\] )?\(([^ ]+)", line)
        require(match is not None, "unrecognized apt simulation")
        package, version = match.groups()
        require(package in allowed and package not in installed, "dependency plan changes an installed or unrelated package")
        require(re.fullmatch(r"[a-zA-Z0-9.+:~_-]+", version) is not None, "invalid dependency version")
        result.append(package + "=" + version)
    require(bool(result) and any(item.startswith("cryptsetup-bin=") for item in result), "no bounded cryptsetup install plan")
    return result


def run_prepare_dependencies(plan: dict) -> None:
    validate_plan(plan, activation=False)
    require(os.geteuid() == 0, "host root required")
    validate_identity(plan, socket.gethostname(), Path("/etc/machine-id").read_text())
    require(shutil.disk_usage("/").free >= plan["root_reserve_bytes"], "root filesystem reserve below 1GiB")
    validate_service(service_state())
    check_baseline(plan)
    disk = disk_inventory(plan)
    guard_disk(disk, read_owner(plan))
    for command in ("apt-get", "dpkg-query", "systemd-run", "udevadm"):
        require(shutil.which(command) is not None, f"required host prerequisite missing: {command}")
    # Hot-added QEMU disks can carry stale by-id links. Trigger this already-guarded disk only.
    run(["udevadm", "trigger", "--action=change", "/sys/class/block/" + Path(disk["path"]).name])
    run(["udevadm", "settle", "--timeout=30"], timeout=35)
    require(disk_inventory(plan)["path"] == disk["path"], "dedicated disk identity changed after udev settle")
    if shutil.which("cryptsetup"):
        log("dependencies-already-present", hostname=plan["hostname"])
        return
    apt_options = ["-o", "Acquire::Retries=1", "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30",
                   "-o", "DPkg::Lock::Timeout=60"]
    def apt_unit(name: str, args: list[str], seconds: int) -> str:
        log("dependency-step-start", step=name, timeoutSeconds=seconds)
        return run(["systemd-run", "--unit=studio-swap-prepare-" + name, "--wait", "--pipe", "--collect",
                    "--property=RuntimeMaxSec=" + str(seconds), "--property=TimeoutStopSec=60",
                    "--property=MemoryMax=256M", "--property=MemorySwapMax=256M", "--property=CPUQuota=50%",
                    "--setenv=DEBIAN_FRONTEND=noninteractive", "--setenv=LC_ALL=C",
                    "/usr/bin/apt-get", *apt_options, *args], timeout=seconds + 90)
    apt_unit("index", ["update"], 240)
    installed_text = run(["dpkg-query", "--show", "--showformat=${Package} ${db:Status-Status}\n"])
    installed = {line.split()[0] for line in installed_text.splitlines() if line.endswith(" installed")}
    flags = ["--no-install-recommends", "--no-upgrade", "--no-remove"]
    simulation = run(["apt-get", *apt_options, "--simulate", *flags, "install", "cryptsetup-bin"], timeout=90)
    packages = dependency_packages(simulation, installed)
    log("dependency-plan-validated", packages=[item.split("=")[0] for item in packages])
    apt_unit("install", ["--assume-yes", *flags, "install", *packages], 360)
    require(shutil.which("cryptsetup") is not None, "cryptsetup still unavailable after dependency phase")
    require(shutil.disk_usage("/").free >= plan["root_reserve_bytes"], "root filesystem reserve violated by dependencies")
    check_baseline(plan)
    log("dependencies-ready", hostname=plan["hostname"], installedPackages=[item.split("=")[0] for item in packages],
        agentRestarted=False, swapConfigured=False)


def run_install(plan: dict, source: str) -> None:
    validate_plan(plan)
    require(os.geteuid() == 0, "host root required")
    validate_identity(plan, socket.gethostname(), Path("/etc/machine-id").read_text())
    for command in ("cryptsetup", "lsblk", "blockdev", "wipefs", "mkswap", "swapon", "systemctl", "k3s"):
        require(shutil.which(command) is not None, f"required host prerequisite missing: {command}")
    require(Path("/sys/fs/cgroup/cgroup.controllers").is_file(), "cgroup v2 is required")
    require(re.search(r"v1\.35\.4\+k3s1", run(["k3s", "--version"])), "unaudited k3s version")
    require(shutil.disk_usage("/").free >= plan["root_reserve_bytes"], "root filesystem reserve below 1GiB")
    service = service_state()
    validate_service(service)
    commandline = Path(f"/proc/{service['MainPID']}/cmdline").read_bytes().split(b"\0")
    require(not any(b"kubelet-arg" in arg for arg in commandline), "explicit kubelet arguments require separate review")
    check_baseline(plan)
    owner = read_owner(plan)
    disk = disk_inventory(plan)
    guard_disk(disk, owner)
    plan_bytes = (json.dumps(plan, sort_keys=True, indent=2) + "\n").encode()
    managed = {HELPER: source.encode(), PLAN: plan_bytes, UNIT: unit_text(),
               AGENT_DROPIN: agent_dropin(), KUBELET_DROPIN: kubelet_config(plan)}
    hashes = {str(path): digest(data) for path, data in managed.items()}
    if owner:
        require(owner.get("managed_hashes") == hashes, "owned maintenance revision changed; explicit upgrade review required")
    for path, data in managed.items():
        check_regular_path(path)
        require(not path.exists() or (owner is not None and path.read_bytes() == data), "unowned or changed managed file exists")
    original = {path: path.read_bytes() if path.exists() else None for path in (AGENT_DROPIN, KUBELET_DROPIN)}
    before = running_containers()
    require(bool(before), "cannot verify running container preservation")
    if owner is None:
        owner = {"owner": OWNER, "hostname": plan["hostname"], "machine_id": plan["machine_id"],
                 "serial": SERIAL, "size_bytes": SIZE, "managed_hashes": hashes}
        atomic_write(STATE, (json.dumps(owner, sort_keys=True, indent=2) + "\n").encode())
    for path, data in managed.items():
        atomic_write(path, data, mode=0o644 if path in (UNIT, AGENT_DROPIN, KUBELET_DROPIN) else 0o600)
    log("owned-files-installed", hostname=plan["hostname"], diskSerial=SERIAL, diskBytes=SIZE)
    # Swap is enabled before the kubelet restart, so startup observes the new capacity.
    try:
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "studio-swap.service"])
        run(["systemctl", "start", "studio-swap.service"], timeout=200)
        require(os.path.realpath(MAPPING) in active_swaps(), "swap service did not activate the owned mapping")
        validate_service(service_state())
        run(["systemctl", "restart", "k3s-agent.service"], timeout=180)
        wait_healthy()
        validate_service(service_state())
        require(before.issubset(running_containers()), "a previously running container disappeared; operator review required")
        check_baseline(plan)
    except BaseException:
        # Restore only our two configuration drop-ins. Keep encrypted swap online:
        # forcing swapoff under pressure would risk unrelated services and data.
        for path, data in original.items():
            if data is None:
                check_regular_path(path)
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, data, mode=0o644)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "restart", "k3s-agent.service"], timeout=180)
        wait_healthy()
        log("configuration-rolled-back", encryptedSwapLeftActive=os.path.realpath(MAPPING) in active_swaps())
        raise
    log("complete", hostname=plan["hostname"], containerCountPreserved=len(before),
        encryptedSwapActive=True, kubeletHealth=True, externalConfigzVerificationRequired=True)


def run_boot() -> None:
    check_regular_path(PLAN, may_be_missing=False)
    plan = json.loads(PLAN.read_text())
    validate_plan(plan)
    validate_identity(plan, socket.gethostname(), Path("/etc/machine-id").read_text())
    owner = read_owner(plan)
    require(owner is not None, "boot requires existing dedicated disk ownership")
    for path in (HELPER, PLAN, UNIT):
        check_regular_path(path, may_be_missing=False)
        require(digest(path.read_bytes()) == owner["managed_hashes"].get(str(path)), "owned boot file changed")
    disk = disk_inventory(plan)
    guard_disk(disk, owner)
    ensure_swap(disk)
    log("encrypted-swap-active", hostname=plan["hostname"], diskSerial=SERIAL)


def enter_host(phase: str) -> None:
    # Input is the reviewed ConfigMap, never a credential or an arbitrary command.
    source = Path(__file__).read_text()
    plan = json.loads(Path("/plan/plan.json").read_text())
    require(phase in ("prepareDependencies", "activate"), "unknown maintenance phase")
    validate_plan(plan, activation=phase == "activate")
    bootstrap = "import json\nns={'__name__':'studio_swap_host'}\n"
    bootstrap += f"exec({source!r},ns)\n"
    call = f"ns['run_install'](json.loads({json.dumps(plan)!r}),{source!r})" if phase == "activate" else f"ns['run_prepare_dependencies'](json.loads({json.dumps(plan)!r}))"
    bootstrap += "try:\n    " + call + "\nexcept BaseException as error:\n    ns['log']('failed', reason=str(error) if isinstance(error,ns['GuardError']) else type(error).__name__)\n    raise SystemExit(1)\n"
    result = subprocess.run(["chroot", "/host", "/usr/bin/nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid",
                             "--", "/usr/bin/python3", "-B", "-"], input=bootstrap, text=True, timeout=1100)
    require(result.returncode == 0, "host swap maintenance failed; inspect sanitized phase output")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--enter-host":
            enter_host(sys.argv[2])
        elif sys.argv[1:] == ["--boot"]:
            run_boot()
        else:
            raise GuardError("only --enter-host and --boot operations exist")
    except Exception as error:
        # Guard messages are fixed strings. Do not emit command stderr, file contents or traceback.
        log("failed", reason=str(error) if isinstance(error, GuardError) else type(error).__name__)
        raise SystemExit(1)
