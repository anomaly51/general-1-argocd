"""Pure guards and sandboxed host lifecycle; never run host commands."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock


SPEC = importlib.util.spec_from_file_location("studio_swap", Path(__file__).parents[1] / "files/maintenance.py")
swap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(swap)


def plan():
    return {"hostname": "general-1-worker-1", "machine_id": "b233eaca510b4f35a4a3715cf11fa89d",
            "serial": swap.SERIAL, "size_bytes": swap.SIZE, "root_reserve_bytes": 1024**3,
            "no_kubelet_overrides_verified": True, "baseline_fail_swap_on": False,
            "expected_kubelet_hashes": {"00-k3s-defaults.conf": "a" * 64},
            "disk_eviction_hard": {"imagefs.available": "5%", "nodefs.available": "5%"}}


def disk():
    return {"name": "sdb", "path": "/dev/sdb", "serial": swap.SERIAL, "type": "disk",
            "size": swap.SIZE, "ro": False, "mountpoints": [None], "fstype": None}


SERVICE = {"ActiveState": "active", "KillMode": "process", "Delegate": "yes", "MainPID": "101"}


class GuardTests(unittest.TestCase):
    def setUp(self):
        blocker = patch.object(swap.subprocess, "run", side_effect=AssertionError("host command escaped mock"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def test_plan_identity_and_activation_audits(self):
        swap.validate_plan(plan())
        for key, value in [("hostname", "general-1-master"), ("machine_id", "wrong"),
                           ("serial", "root-disk"), ("size_bytes", swap.SIZE - 1),
                           ("root_reserve_bytes", 1024**3 - 1), ("baseline_fail_swap_on", True),
                           ("baseline_fail_swap_on", 0), ("expected_kubelet_hashes", {}),
                           ("no_kubelet_overrides_verified", False), ("disk_eviction_hard", {})]:
            with self.subTest(key=key, value=value), self.assertRaises(swap.GuardError):
                swap.validate_plan({**plan(), key: value})
        preparation = {**plan(), "no_kubelet_overrides_verified": False}
        swap.validate_plan(preparation, activation=False)
        swap.validate_identity(plan(), plan()["hostname"], plan()["machine_id"] + "\n")
        for hostname, machine in [("general-1-worker-2", plan()["machine_id"]), (plan()["hostname"], "f" * 32)]:
            with self.assertRaises(swap.GuardError):
                swap.validate_identity(plan(), hostname, machine)

    def test_service_must_preserve_existing_container_processes(self):
        swap.validate_service(SERVICE)
        for key, value in [("KillMode", "control-group"), ("Delegate", "no"),
                           ("ActiveState", "inactive"), ("MainPID", "1")]:
            with self.subTest(key=key), self.assertRaises(swap.GuardError):
                swap.validate_service({**SERVICE, key: value})

    def test_only_exact_unmounted_raw_disk_or_owned_crypt_candidate(self):
        self.assertEqual(swap.select_disk([disk()], plan()), disk())
        for inventory in [[], [disk(), disk()]]:
            with self.assertRaises(swap.GuardError):
                swap.select_disk(inventory, plan())
        for key, value in [("size", swap.SIZE + 1), ("type", "part"), ("ro", True),
                           ("mountpoints", ["/"]), ("fstype", "ext4"), ("path", "/dev/../sda"),
                           ("children", [{"type": "part", "name": "sdb1"}])]:
            with self.subTest(key=key), self.assertRaises(swap.GuardError):
                swap.select_disk([{**disk(), key: value}], plan())
        candidate = {**disk(), "children": [{"type": "crypt", "name": swap.SERIAL, "mountpoints": ["[SWAP]"]}]}
        self.assertEqual(swap.select_disk([candidate], plan()), candidate)
        candidate["children"][0]["mountpoints"] = ["/data"]
        with self.assertRaises(swap.GuardError):
            swap.select_disk([candidate], plan())

    def test_owner_and_config_preserve_scope_and_disk_evictions(self):
        owner = {"owner": swap.OWNER, **{key: plan()[key] for key in ("hostname", "machine_id", "serial", "size_bytes")}}
        swap.validate_owner(owner, plan())
        with self.assertRaises(swap.GuardError):
            swap.validate_owner({**owner, "machine_id": "f" * 32}, plan())
        config = json.loads(swap.kubelet_config(plan()))
        self.assertIs(config["failSwapOn"], False)
        self.assertEqual(config["memorySwap"], {"swapBehavior": "LimitedSwap"})
        self.assertEqual(config["evictionHard"], {**plan()["disk_eviction_hard"], "memory.available": "200Mi"})
        self.assertNotIn(b"ExecStop=", swap.unit_text())
        self.assertNotIn(b"MemorySwapMax", swap.agent_dropin())
        self.assertIn(b"Before=k3s-agent.service", swap.unit_text())

    def test_dependency_plan_only_pins_new_bounded_packages(self):
        simulation = "Inst libpopt0 (1.19+dfsg-2 Debian:13/stable [amd64])\nInst cryptsetup-bin (2:2.7.5-2 Debian:13/stable [amd64])\n"
        self.assertEqual(swap.dependency_packages(simulation, {"libc6"}), ["libpopt0=1.19+dfsg-2", "cryptsetup-bin=2:2.7.5-2"])
        for text, installed in [(simulation, {"libpopt0"}), ("Remv libc6\n" + simulation, set()),
                                ("Inst curl (8.0 Debian [amd64])\n" + simulation, set()),
                                ("Inst cryptsetup-bin malformed", set()), ("", set()),
                                ("Inst cryptsetup-bin (evil;command Debian)", set())]:
            with self.subTest(text=text), self.assertRaises(swap.GuardError):
                swap.dependency_packages(text, installed)

    def test_mapping_must_match_plain_owned_device(self):
        for output, valid in [("type: PLAIN\ndevice: /dev/sdb", True),
                              ("type: LUKS2\ndevice: /dev/sdb", False),
                              ("type: PLAIN\ndevice: /dev/sda", False), ("", False)]:
            with patch.object(swap, "run", return_value=output):
                self.assertEqual(swap.mapping_matches("/dev/sdb"), valid)

    def test_ensure_swap_rekeys_only_mapping_and_never_raw_disk(self):
        mapping = Mock()
        mapping.exists.return_value = False
        mapping.__fspath__ = Mock(return_value="/dev/mapper/studio-swap-v1")
        mapping.__str__ = Mock(return_value="/dev/mapper/studio-swap-v1")
        commands = []
        with patch.object(swap, "MAPPING", mapping), patch.object(swap, "mapping_matches", return_value=True), \
                patch.object(swap, "signatures", return_value=[]), \
                patch.object(swap, "active_swaps", side_effect=[set(), {str(mapping)}]), \
                patch.object(swap, "run", side_effect=lambda args, **kwargs: commands.append(args) or ""):
            swap.ensure_swap(disk())
        self.assertEqual(commands[0][:4], ["cryptsetup", "open", "--type", "plain"])
        self.assertIn("/dev/urandom", commands[0])
        self.assertEqual(commands[1], ["mkswap", "--label", swap.SERIAL, str(mapping)])
        self.assertEqual(commands[2], ["swapon", "--priority", "100", str(mapping)])
        self.assertFalse(any(command[0] == "swapoff" for command in commands))
        with patch.object(swap, "MAPPING", mapping), patch.object(swap, "mapping_matches", return_value=False), \
                patch.object(swap, "run") as command:
            mapping.exists.return_value = True
            with self.assertRaises(swap.GuardError):
                swap.ensure_swap(disk())
            command.assert_not_called()


class FakeHostTests(unittest.TestCase):
    """Exercise real ownership/config writes, but only in a temporary directory."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = plan()
        self.commands = []
        self.active = False
        self.installed = True
        self.source = "# audited host helper\n"
        for name in ("STATE", "PLAN", "HELPER", "UNIT", "AGENT_DROPIN", "KUBELET_DIR", "KUBELET_DROPIN", "MAPPING"):
            original = getattr(swap, name)
            self.replace(swap, name, self.root / str(original).lstrip("/"))
        swap.KUBELET_DIR.mkdir(parents=True)
        baseline = b'{"failSwapOn":false}\n'
        (swap.KUBELET_DIR / "00-k3s-defaults.conf").write_bytes(baseline)
        self.plan["expected_kubelet_hashes"] = {"00-k3s-defaults.conf": swap.digest(baseline)}
        for name, data in [("/etc/machine-id", self.plan["machine_id"].encode()),
                           ("/sys/fs/cgroup/cgroup.controllers", b"memory"),
                           ("/proc/101/cmdline", b"k3s\0agent\0")]:
            target = self.root / name.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        paths = {name: self.root / name.lstrip("/") for name in
                 ("/etc/machine-id", "/sys/fs/cgroup/cgroup.controllers", "/proc/101/cmdline")}
        self.replace(swap, "Path", lambda value: paths.get(str(value), Path(value)))
        self.replace(swap, "check_regular_path", lambda path, **kwargs: self.assertTrue(path.is_relative_to(self.root)))
        self.replace(swap.os, "geteuid", lambda: 0)
        self.replace(swap.socket, "gethostname", lambda: self.plan["hostname"])
        self.replace(swap.shutil, "which", lambda name: None if name == "cryptsetup" and not self.installed else "/usr/bin/" + name)
        self.replace(swap.shutil, "disk_usage", lambda path: SimpleNamespace(free=2 * 1024**3))
        self.replace(swap, "service_state", lambda: dict(SERVICE))
        self.replace(swap, "disk_inventory", Mock(return_value=disk()))
        self.replace(swap, "guard_disk", Mock())
        self.replace(swap, "active_swaps", lambda: {swap.os.path.realpath(swap.MAPPING)} if self.active else set())
        self.replace(swap, "running_containers", Mock(return_value={"a" * 16, "b" * 16}))
        self.replace(swap, "wait_healthy", Mock())
        self.replace(swap, "log", Mock())
        self.replace(swap.subprocess, "run", Mock(side_effect=AssertionError("host command escaped mock")))
        self.replace(swap, "run", self.command)

    def replace(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args == ["k3s", "--version"]:
            return "k3s version v1.35.4+k3s1 (test)"
        if args[:2] == ["systemctl", "start"]:
            self.assertTrue(swap.STATE.exists())
            self.assertTrue(swap.KUBELET_DROPIN.exists())
            self.active = True
        elif args[:2] == ["systemctl", "restart"]:
            self.assertTrue(self.active)
        elif args[0] == "dpkg-query":
            return "libc6 installed\n"
        elif args[0] == "apt-get":
            return "Inst cryptsetup-bin (2:2.7.5-2 Debian [amd64])\n"
        elif args[0] == "systemd-run" and "--unit=studio-swap-prepare-install" in args:
            self.installed = True
        elif args[0] not in ("systemctl", "systemd-run", "udevadm"):
            raise AssertionError("unexpected fake host command: " + args[0])
        return ""

    def test_install_and_owned_rerun_preserve_containers(self):
        swap.run_install(self.plan, self.source)
        owner = json.loads(swap.STATE.read_text())
        swap.validate_owner(owner, self.plan)
        self.assertEqual(json.loads(swap.KUBELET_DROPIN.read_text())["memorySwap"], {"swapBehavior": "LimitedSwap"})
        swap.run_install(self.plan, self.source)
        self.assertEqual(json.loads(swap.STATE.read_text()), owner)
        self.assertTrue(self.active)
        self.assertFalse(any(command[0] == "swapoff" for command in self.commands))

    def test_failure_rolls_back_only_owned_dropins_retaining_swap(self):
        swap.wait_healthy.side_effect = [swap.GuardError("test health failure"), None]
        with self.assertRaisesRegex(swap.GuardError, "test health failure"):
            swap.run_install(self.plan, self.source)
        self.assertFalse(swap.AGENT_DROPIN.exists())
        self.assertFalse(swap.KUBELET_DROPIN.exists())
        self.assertTrue(swap.STATE.exists())
        self.assertTrue(swap.PLAN.exists())
        self.assertTrue(self.active)
        self.assertEqual(self.commands.count(["systemctl", "restart", "k3s-agent.service"]), 2)
        self.assertFalse(any(command[0] == "swapoff" for command in self.commands))

    def test_disk_guard_failure_has_no_managed_writes_or_restart(self):
        swap.guard_disk.side_effect = swap.GuardError("not blank")
        with self.assertRaisesRegex(swap.GuardError, "not blank"):
            swap.run_install(self.plan, self.source)
        self.assertFalse(swap.STATE.exists())
        self.assertFalse(swap.PLAN.exists())
        self.assertEqual(self.commands, [["k3s", "--version"]])

    def test_changed_owned_file_and_revision_fail_closed(self):
        swap.run_install(self.plan, self.source)
        self.commands.clear()
        swap.UNIT.write_bytes(b"changed")
        with self.assertRaisesRegex(swap.GuardError, "changed managed file"):
            swap.run_install(self.plan, self.source)
        self.assertEqual(self.commands, [["k3s", "--version"]])
        with self.assertRaisesRegex(swap.GuardError, "revision changed"):
            swap.run_install(self.plan, "new revision")

    def test_prepare_installs_only_pinned_dependencies_without_swap_writes(self):
        self.installed = False
        self.plan["no_kubelet_overrides_verified"] = False
        swap.run_prepare_dependencies(self.plan)
        self.assertTrue(self.installed)
        self.assertFalse(self.active)
        self.assertFalse(swap.STATE.exists())
        self.assertFalse(swap.KUBELET_DROPIN.exists())
        self.assertFalse(any(command[0] in ("systemctl", "mkswap", "swapon", "cryptsetup") for command in self.commands))
        installs = [command for command in self.commands if "--unit=studio-swap-prepare-install" in command]
        self.assertEqual(len(installs), 1)
        for flag in ("--no-upgrade", "--no-remove", "--no-install-recommends", "cryptsetup-bin=2:2.7.5-2",
                     "--property=MemoryMax=256M", "--property=MemorySwapMax=256M", "--property=CPUQuota=50%"):
            self.assertIn(flag, installs[0])
        self.assertEqual(self.commands[0], ["udevadm", "trigger", "--action=change", "/sys/class/block/sdb"])


if __name__ == "__main__":
    unittest.main()
