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
    return {"metadata": {"namespace": "apps", "name": name, "uid": uid, "labels": {"app": "example"}, "ownerReferences": [{"kind": "DaemonSet" if daemon else "ReplicaSet", "name": "owner", "uid": "owner-uid", "controller": True}]}, "spec": {"nodeName": m.NODE, "containers": [{"name": "app", "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}], "volumes": [{"name": "tmp", "emptyDir": {}}]}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": [{"name": "app", "ready": True}]}}


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
        self.create_replacement = True
        self.keep_terminated = False
        self.after_eviction = None
        self.on_get = None

    def call(self, method, path, body=None, content_type=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and self.on_get:
            self.on_get(self)
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
        if method == "GET" and ("/pods?" in path or path.endswith("/pods")):
            namespace = path.split("/")[4]
            return {"items": copy.deepcopy([p for p in self.live if p["metadata"]["namespace"] == namespace])}
        if method == "GET" and "/pods/" in path:
            namespace, name = path.split("/")[4], path.split("/")[6]
            found = [p for p in self.live if p["metadata"]["namespace"] == namespace and p["metadata"]["name"] == name]
            if not found:
                raise m.APIError(404)
            return copy.deepcopy(found[0])
        if path.endswith("/poddisruptionbudgets"):
            return {"items": self.pdbs}
        if method == "POST" and path.endswith("/eviction"):
            if self.eviction_status:
                raise m.APIError(self.eviction_status)
            uid = body["deleteOptions"]["preconditions"]["uid"]
            old = next(p for p in self.live if p["metadata"]["uid"] == uid)
            if self.keep_terminated:
                old["status"]["phase"] = "Succeeded"
                old["metadata"]["deletionTimestamp"] = "2026-09-09T00:00:00Z"
            else:
                self.live.remove(old)
            replacement = None
            if self.create_replacement:
                replacement = copy.deepcopy(old)
                replacement["metadata"]["uid"] = uid + "-replacement"
                if m.controller(old)["kind"] != "StatefulSet":
                    replacement["metadata"]["name"] += "-replacement"
                replacement["spec"]["nodeName"] = "general-1-worker-1"
                self.live.append(replacement)
            if self.after_eviction:
                self.after_eviction(self, old, replacement)
            return {}
        raise AssertionError((method, path))


class SafetyTests(unittest.TestCase):
    def setup_run(self, pods=None, exceptions=None, priority=None, timeout=90):
        pods = pods if pods is not None else [pod(), pod("daemon", "daemon-uid", True)]
        api = FakeAPI(pods)
        inventory = {"node": {"name": m.NODE, "uid": "node-uid"}, "namespaces": api.namespaces[:], "pods": [m.identity(p) for p in pods]}
        inventory["readinessExceptions"] = exceptions or []
        inventory["drainPriority"] = priority or []
        now = [0]
        def sleep(seconds):
            now[0] += max(1, seconds)
        runner = m.Maintenance(api, inventory, "approved-operation", timeout=timeout, clock=lambda: now[0], sleep=sleep)
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
        self.assertEqual([p["metadata"]["name"] for p in api.live if p["spec"]["nodeName"] == m.NODE], ["daemon"])
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

    def test_each_eviction_waits_for_a_new_stable_ready_replacement(self):
        api, runner = self.setup_run([pod("first", "first-uid"), pod("second", "second-uid")])
        accepted_at = []
        api.after_eviction = lambda *_: accepted_at.append(runner.clock())
        runner.drain()
        self.assertEqual(len(accepted_at), 2)
        self.assertGreaterEqual(accepted_at[1] - accepted_at[0], 15)
        self.assertGreaterEqual(runner.clock() - accepted_at[1], 15)

    def test_preexisting_ready_sibling_cannot_satisfy_replacement_gate(self):
        api, runner = self.setup_run()
        sibling = pod("sibling", "sibling-uid")
        sibling["spec"]["nodeName"] = "general-1-worker-2"
        api.live.append(sibling)
        api.create_replacement = False
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_new_wrong_owner_uid_cannot_satisfy_replacement_gate(self):
        api, runner = self.setup_run()
        def change_owner(api, old, replacement):
            replacement["metadata"]["ownerReferences"][0]["uid"] = "different-controller"
        api.after_eviction = change_owner
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_replacement_on_worker3_is_inventory_drift(self):
        api, runner = self.setup_run()
        api.after_eviction = lambda api, old, replacement: replacement["spec"].update(nodeName=m.NODE)
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_replacement_oom_stops_before_the_next_eviction(self):
        api, runner = self.setup_run([pod("first", "first-uid"), pod("second", "second-uid")])
        def oom(api, old, replacement):
            replacement["status"]["containerStatuses"][0]["lastState"] = {"terminated": {"reason": "OOMKilled", "exitCode": 137}}
        api.after_eviction = oom
        with self.assertRaises(m.Abort): runner.drain()
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 1)
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_ready_count_cannot_drop_below_pre_eviction_baseline(self):
        api, runner = self.setup_run()
        sibling = pod("sibling", "sibling-uid")
        sibling["spec"]["nodeName"] = "general-1-worker-2"
        api.live.append(sibling)
        api.after_eviction = lambda api, *_: api.live.remove(sibling)
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_unprocessed_pod_disappearance_aborts(self):
        api, runner = self.setup_run([pod("first", "first-uid"), pod("second", "second-uid")])
        api.after_eviction = lambda api, *_: setattr(api, "live", [p for p in api.live if p["metadata"]["uid"] != "second-uid"])
        with self.assertRaises(m.Abort): runner.drain()
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 1)
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_pinned_exceptions_follow_all_movable_pods_in_audited_order(self):
        pods = [pod("harbor", "harbor-uid"), pod("operator", "operator-uid"), pod("movable", "movable-uid")]
        exceptions = [{"namespace": "apps", "name": name, "uid": name + "-uid", "controllerUID": "owner-uid"} for name in ("operator", "harbor")]
        api, runner = self.setup_run(pods, exceptions=exceptions)
        def remove_pinned_replacement(api, old, replacement):
            if m.key(old) in runner.exceptions:
                api.live.remove(replacement)
        api.after_eviction = remove_pinned_replacement
        runner.drain()
        self.assertEqual([c[2]["metadata"]["name"] for c in api.calls if c[0] == "POST"], ["movable", "operator", "harbor"])
        self.assertEqual(runner.clock(), 15)

    def test_pinned_exception_does_not_skip_pdb(self):
        exception = {"namespace": "apps", "name": "app", "uid": "pod-uid", "controllerUID": "owner-uid"}
        api, runner = self.setup_run([pod()], exceptions=[exception])
        api.pdbs = [{"spec": {"selector": {}}, "status": {"disruptionsAllowed": 0}}]
        with self.assertRaises(m.Abort): runner.drain()
        self.assert_untouched(api)

    def test_pinned_exception_waits_until_old_uid_really_disappears(self):
        exception = {"namespace": "apps", "name": "app", "uid": "pod-uid", "controllerUID": "owner-uid"}
        api, runner = self.setup_run([pod()], exceptions=[exception])
        api.keep_terminated = True
        api.create_replacement = False
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_pinned_exception_requires_exact_pod_and_controller_uid(self):
        for field in ("uid", "controllerUID"):
            exception = {"namespace": "apps", "name": "app", "uid": "pod-uid", "controllerUID": "owner-uid"}
            exception[field] = "wrong"
            with self.assertRaises(m.Abort): self.setup_run([pod()], exceptions=[exception])

    def test_statefulset_requires_replacement_of_same_ordinal(self):
        target = pod("database-0", "database-uid")
        target["metadata"]["ownerReferences"][0]["kind"] = "StatefulSet"
        api, runner = self.setup_run([target])
        api.after_eviction = lambda api, old, replacement: replacement["metadata"].update(name="database-1")
        with self.assertRaises(m.Abort): runner.drain()
        self.assertFalse(api.node["spec"]["unschedulable"])

    def test_checked_in_target_and_no_duplicate_pods(self):
        inventory = json.loads((ROOT / "files/inventory.json").read_text())
        self.assertEqual(inventory["proxmox"]["vmId"], 114)
        self.assertEqual(inventory["proxmox"]["desiredMemoryMiB"], 4096)
        self.assertEqual(len({p["uid"] for p in inventory["pods"]}), len(inventory["pods"]))
        self.assertEqual(len({(p["namespace"], p["name"]) for p in inventory["pods"]}), len(inventory["pods"]))
        self.assertEqual(len(inventory["readinessExceptions"]), 6)
        self.assertEqual(inventory["readinessExceptions"][-1]["namespace"], "harbor")
        self.assertTrue(inventory["readinessExceptions"][-1]["name"].startswith("harbor-registry-"))
        m.Maintenance(FakeAPI([]), inventory, "inventory-validation")


if __name__ == "__main__":
    unittest.main()
