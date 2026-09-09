"""Rendered security-contract checks. Requires Helm and PyYAML in the test environment."""
import json
from pathlib import Path
import subprocess
import unittest

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(yaml is None, "Install PyYAML in the test environment for rendered chart checks")
class ChartTests(unittest.TestCase):
    def render(self, *settings):
        command = ["rtk", "proxy", "helm", "template", "general-1-worker-3-ram", str(ROOT), "--namespace", "maintenance"]
        for setting in settings:
            command += ["--set", setting]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_explicitly_disabled_fixture_renders_no_resources(self):
        self.assertEqual(self.render("enabled=false"), [])

    def test_enabled_remains_suspended_and_temporary_audit_not_approved(self):
        docs = self.render()
        job = next(d for d in docs if d["kind"] == "Job")
        self.assertTrue(job["spec"]["suspend"])
        self.assertNotIn("--temporary-data-audit-approved", job["spec"]["template"]["spec"]["containers"][0]["args"])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 960)

    def test_node_permissions_are_exact(self):
        docs = self.render("enabled=true")
        role = next(d for d in docs if d["kind"] == "ClusterRole")
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["nodes"], "resourceNames": ["general-1-worker-3"], "verbs": ["get", "patch"]},
            {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["list"]},
        ])

    def test_eviction_roles_match_exact_audited_non_daemon_pods(self):
        docs = self.render("enabled=true")
        inventory = json.loads((ROOT / "files/inventory.json").read_text())
        expected = {}
        for p in inventory["pods"]:
            if not p["mirror"] and not any(o["kind"] == "DaemonSet" and o["controller"] for o in p["owners"]):
                expected.setdefault(p["namespace"], set()).add(p["name"])
        actual = {}
        for role in (d for d in docs if d["kind"] == "Role"):
            for rule in role["rules"]:
                if rule["resources"] == ["pods/eviction"]:
                    self.assertEqual(rule["verbs"], ["create"])
                    actual[role["metadata"]["namespace"]] = set(rule["resourceNames"])
        self.assertEqual(actual, expected)
        self.assertEqual({d["metadata"]["namespace"] for d in docs if d["kind"] == "Role"}, set(inventory["namespaces"]))

    def test_no_secret_delete_exec_storage_or_wildcard_permissions(self):
        for d in self.render("enabled=true"):
            self.assertNotEqual(d["kind"], "Secret")
            if d["kind"] in ("Role", "ClusterRole"):
                for rule in d["rules"]:
                    self.assertFalse(set(rule["verbs"]) & {"delete", "deletecollection", "update", "*"})
                    self.assertFalse(set(rule["resources"]) & {"secrets", "pods/exec", "pods/log", "nodes/proxy", "persistentvolumes", "persistentvolumeclaims", "*"})

    def test_job_is_nonroot_readonly_pinned_away_from_worker3(self):
        job = next(d for d in self.render("enabled=true") if d["kind"] == "Job")
        spec = job["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertTrue(spec["securityContext"]["runAsNonRoot"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["resources"]["limits"]["memory"], "128Mi")
        self.assertEqual(container["resources"]["requests"]["memory"], "64Mi")
        self.assertRegex(container["image"], r"^docker.io/library/python:3\.12-slim-bookworm@sha256:[a-f0-9]{64}$")
        expressions = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
        self.assertEqual(next(x["values"] for x in expressions if x["key"] == "kubernetes.io/hostname"), ["general-1-worker-1", "general-1-worker-2"])
        self.assertNotIn("envFrom", container)
        self.assertFalse(any("secret" in v for v in spec["volumes"]))

    def test_uncordon_phase_has_no_pod_or_namespace_permissions(self):
        docs = self.render("enabled=true", "phase=uncordon")
        self.assertFalse(any(d["kind"] in ("Role", "RoleBinding") for d in docs))
        role = next(d for d in docs if d["kind"] == "ClusterRole")
        self.assertEqual(len(role["rules"]), 1)
        self.assertEqual(role["rules"][0]["resources"], ["nodes"])
        job = next(d for d in docs if d["kind"] == "Job")
        self.assertIn("-uncordon-", job["metadata"]["name"])

    def test_recovery_phase_is_explicit_and_node_only(self):
        docs = self.render("enabled=true", "phase=recover-uncordon")
        self.assertFalse(any(d["kind"] in ("Role", "RoleBinding") for d in docs))
        job = next(d for d in docs if d["kind"] == "Job")
        self.assertLessEqual(len(job["metadata"]["name"]), 63)
        self.assertNotIn("--recovery-approved", job["spec"]["template"]["spec"]["containers"][0]["args"])
        approved = next(d for d in self.render("enabled=true", "phase=recover-uncordon", "recoveryApproved=true") if d["kind"] == "Job")
        self.assertIn("--recovery-approved", approved["spec"]["template"]["spec"]["containers"][0]["args"])

    def test_invalid_phase_timeout_or_unpinned_image_fail_render(self):
        for setting in ("phase=delete", "drainTimeoutSeconds=900", "image=python:latest"):
            with self.assertRaises(subprocess.CalledProcessError):
                self.render("enabled=true", setting)


if __name__ == "__main__":
    unittest.main()
