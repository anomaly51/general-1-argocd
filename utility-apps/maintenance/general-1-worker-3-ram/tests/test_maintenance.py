import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("maintenance", ROOT / "files/maintenance.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def pod(name="app", uid="pod-uid", daemon=False):
    return {"metadata": {"namespace": "apps", "name": name, "uid": uid, "labels": {"app": "example"}, "ownerReferences": [{"kind": "DaemonSet" if daemon else "ReplicaSet", "name": "owner", "uid": "owner-uid", "controller": True}]}, "spec": {"nodeName": m.NODE, "containers": [{"name": "app", "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}], "volumes": [{"name": "tmp", "emptyDir": {}}]}, "status": {"phase": "Running"}}


class FakeAPI:
    def __init__(self, pods):
        self.live = copy.deepcopy(pods)
        self.namespaces = ["apps", "maintenance"]
        self.node = {"metadata": {"name": m.NODE, "uid": "node-uid", "resourceVersion": "1", "annotations": {"existing": "preserve"}}, "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}], "capacity": {"memory": "2978Mi"}}}
        self.pdbs = []
        self.calls = []
        self.eviction_status = None
        self.after_cordon = None
        self.lose_cordon_response = False

    def call(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path.endswith("/nodes/" + m.NODE):
            return copy.deepcopy(self.node)
        if method == "PATCH":
            assert path == "/api/v1/nodes/" + m.NODE
            for op in body:
                if op["path"] == "/metadata/annotations":
                    self.node["metadata"]["annotations"] = copy.deepcopy(op["value"])
                if op["path"] == "/spec/unschedulable":
                    self.node["spec"]["unschedulable"] = op["value"]
            if self.node["spec"]["unschedulable"]:
                if self.after_cordon:
                    self.after_cordon(self)
                if self.lose_cordon_response:
                    raise m.Abort("response lost")
            return copy.deepcopy(self.node)
        if path == "/api/v1/namespaces":
            return {"items": [{"metadata": {"name": n}} for n in self.namespaces]}
        if "/pods?" in path:
            namespace = path.split("/")[4]
            return {"items": copy.deepcopy([p for p in self.live if p["metadata"]["namespace"] == namespace])}
        if path.endswith("/poddisruptionbudgets"):
            return {"items": self.pdbs}
        if method == "POST" and path.endswith("/eviction"):
            if self.eviction_status:
                raise m.APIError(self.eviction_status)
            uid = body["deleteOptions"]["preconditions"]["uid"]
            self.live = [p for p in self.live if p["metadata"]["uid"] != uid]
            return {}
        raise AssertionError((method, path))


class SafetyTests(unittest.TestCase):
    def setup_run(self, pods=None):
        pods = pods if pods is not None else [pod(), pod("daemon", "daemon-uid", True)]
        api = FakeAPI(pods)
        inventory = {"node": {"name": m.NODE, "uid": "node-uid"}, "namespaces": api.namespaces[:], "pods": [m.identity(p) for p in pods]}
        now = [0]
        def sleep(seconds):
            now[0] += max(1, seconds)
        runner = m.Maintenance(api, inventory, "approved-operation", timeout=30, clock=lambda: now[0], sleep=sleep)
        return api, runner

    def assert_untouched(self, api):
        self.assertFalse(any(c[0] != "GET" for c in api.calls))

    def test_success_uses_uid_locked_eviction_preserves_daemon_and_cordon(self):
        api, runner = self.setup_run()
        runner.drain()
        self.assertTrue(api.node["spec"]["unschedulable"])
        self.assertEqual(api.node["metadata"]["annotations"]["existing"], "preserve")
        writes = [c for c in api.calls if c[0] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]["apiVersion"], "policy/v1")
        self.assertEqual(writes[0][2]["deleteOptions"], {"preconditions": {"uid": "pod-uid"}})
        self.assertEqual(len(api.live), 1)
        self.assertFalse(any(c[0] == "DELETE" for c in api.calls))

    def test_changed_uid_aborts_before_cordon(self):
        api, runner = self.setup_run()
        api.live[0]["metadata"]["uid"] = "replacement"
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_changed_mount_aborts_before_cordon(self):
        api, runner = self.setup_run()
        api.live[0]["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] = "/user-data"
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_new_namespace_aborts_before_cordon(self):
        api, runner = self.setup_run()
        api.namespaces.append("new-namespace")
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_new_pod_aborts_before_cordon(self):
        api, runner = self.setup_run()
        api.live.append(pod("unexpected", "new"))
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_zero_pdb_aborts_without_writes(self):
        api, runner = self.setup_run()
        api.pdbs = [{"spec": {"selector": {"matchLabels": {"app": "example"}}}, "status": {"disruptionsAllowed": 0}}]
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_429_waits_then_rolls_back_without_bypassing_pdb(self):
        api, runner = self.setup_run()
        api.eviction_status = 429
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])
        self.assertNotIn(m.ANNOTATION, api.node["metadata"]["annotations"])
        self.assertTrue(all(c[2]["deleteOptions"] == {"preconditions": {"uid": "pod-uid"}} for c in api.calls if c[0] == "POST"))

    def test_post_cordon_drift_rolls_back(self):
        api, runner = self.setup_run()
        api.after_cordon = lambda a: a.live.append(pod("new", "new-uid"))
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_lost_successful_cordon_response_still_rolls_back(self):
        api, runner = self.setup_run()
        api.lose_cordon_response = True
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_preexisting_cordon_is_not_owned_or_removed(self):
        api, runner = self.setup_run()
        api.node["spec"]["unschedulable"] = True
        with self.assertRaises(m.Abort): runner.drain()
        self.assertTrue(api.node["spec"]["unschedulable"])
        self.assert_untouched(api)

    def test_explicit_uncordon_requires_upgrade_and_owned_marker(self):
        api, runner = self.setup_run()
        runner.set_cordon(True)
        with self.assertRaises(m.Abort): runner.set_cordon(False, require_upgraded=True)
        api.node["status"]["capacity"]["memory"] = "4000Mi"
        runner.set_cordon(False, require_upgraded=True)
        self.assertFalse(api.node["spec"]["unschedulable"])
        with self.assertRaises(m.Abort): runner.set_cordon(False, require_upgraded=True)

    def test_node_uid_change_refuses_all_writes(self):
        api, runner = self.setup_run()
        api.node["metadata"]["uid"] = "new-node"
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_explicit_recovery_uncordon_accepts_original_memory_but_requires_ready(self):
        api, runner = self.setup_run()
        runner.set_cordon(True)
        api.node["status"]["conditions"][0]["status"] = "False"
        with self.assertRaises(m.Abort): runner.set_cordon(False, require_recovered=True)
        api.node["status"]["conditions"][0]["status"] = "True"
        runner.set_cordon(False, require_recovered=True)
        self.assertFalse(api.node["spec"]["unschedulable"])
        self.assertEqual(api.node["status"]["capacity"]["memory"], "2978Mi")

    def test_unmanaged_pod_aborts_without_force(self):
        bare = pod()
        bare["metadata"]["ownerReferences"] = []
        api, runner = self.setup_run([bare])
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_checked_in_target_and_no_duplicate_pods(self):
        inventory = json.loads((ROOT / "files/inventory.json").read_text())
        self.assertEqual(inventory["proxmox"]["vmId"], 114)
        self.assertEqual(inventory["proxmox"]["desiredMemoryMiB"], 4096)
        self.assertEqual(len({p["uid"] for p in inventory["pods"]}), len(inventory["pods"]))
        self.assertEqual(len({(p["namespace"], p["name"]) for p in inventory["pods"]}), len(inventory["pods"]))


if __name__ == "__main__":
    unittest.main()
