from pathlib import Path
import subprocess
import tarfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "utility-apps/argocd/image-updater"
COMPONENTS = {
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
}
REGISTRY = "harbor.internal.api-api-api.com"
GIT_REPOSITORY = "https://github.com/anomaly51/general-1-argocd.git"


class PlaygroundImageUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = CHART / "charts/argocd-image-updater-1.3.1.tgz"
        if not cls.archive.is_file():
            subprocess.run(
                ["helm", "dependency", "build", str(CHART)],
                check=True, capture_output=True, text=True,
            )
        rendered = subprocess.check_output(
            ["helm", "template", "image-updater", str(CHART), "--namespace", "argocd"],
            text=True,
        )
        cls.documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
        cls.updater = next(doc for doc in cls.documents if doc["kind"] == "ImageUpdater")
        cls.references = cls.updater["spec"]["applicationRefs"]

    def document(self, kind, name):
        return next(doc for doc in self.documents
                    if doc["kind"] == kind and doc["metadata"]["name"] == name)

    def test_upstream_chart_and_controller_are_exactly_pinned(self):
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        lock = yaml.safe_load((CHART / "Chart.lock").read_text())
        expected = [{"name": "argocd-image-updater", "version": "1.3.1",
                     "repository": "https://argoproj.github.io/argo-helm"}]
        self.assertEqual(chart["dependencies"], expected)
        self.assertEqual(lock["dependencies"], expected)
        self.assertRegex(lock["digest"], r"^sha256:[0-9a-f]{64}$")
        with tarfile.open(self.archive) as archive:
            upstream = yaml.safe_load(archive.extractfile("argocd-image-updater/Chart.yaml"))
        self.assertEqual(upstream["version"], "1.3.1")
        self.assertEqual(upstream["appVersion"], "v1.3.0")
        deployment = self.document("Deployment", "playground-image-updater-controller")
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "quay.io/argoprojlabs/argocd-image-updater:v1.3.0")

    def test_only_eight_exact_playground_applications_are_selected(self):
        self.assertEqual(len(self.references), 8)
        self.assertEqual({ref["namePattern"] for ref in self.references},
                         {"apps-playground-" + name for name in COMPONENTS})
        for ref in self.references:
            self.assertFalse(ref["useAnnotations"])
            self.assertNotIn("labelSelectors", ref)
            name = ref["namePattern"].removeprefix("apps-playground-")
            self.assertEqual(ref["images"], [{
                "alias": name,
                "imageName": f"{REGISTRY}/playground/{name}:prod",
                "commonUpdateSettings": {
                    "updateStrategy": "digest",
                    "platforms": ["linux/amd64"],
                    "pullSecret": "pullsecret:argocd/playground-image-updater-registry",
                },
                "manifestTargets": {"helm": {"name": "image.repository", "tag": "image.tag"}},
            }])

    def test_every_application_writes_only_its_authoritative_prod_values(self):
        self.assertNotIn("writeBackConfig", self.updater["spec"])
        for ref in self.references:
            name = ref["namePattern"].removeprefix("apps-playground-")
            self.assertEqual(ref["writeBackConfig"], {
                "method": "git:secret:playground-image-updater-git",
                "gitConfig": {
                    "repository": GIT_REPOSITORY,
                    "branch": "main",
                    "writeBackTarget": f"helmvalues:/apps/playground-{name}/values/prod.yaml",
                },
            })
            values_path = ROOT / f"apps/playground-{name}/values/prod.yaml"
            values = yaml.safe_load(values_path.read_text())
            self.assertEqual(values["image"]["repository"], f"{REGISTRY}/playground/{name}")
            self.assertIsInstance(values["image"]["tag"], str)
            self.assertEqual(values["_release"]["policy"], "prod-only")
            self.assertRegex(values["_release"]["revision"], r"^[0-9a-f]{40}$")
            self.assertEqual({path.name for path in values_path.parent.glob("*.yaml")}, {"prod.yaml"})
        self.assertNotIn(".argocd-source", yaml.safe_dump(self.updater))

    def test_appset_keeps_main_values_separate_from_pinned_chart(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/apps.yaml").read_text())
        self.assertEqual(appset["spec"]["generators"][0]["git"]["revision"], "main")
        source = appset["spec"]["template"]["spec"]["source"]
        self.assertEqual(source["targetRevision"], "{{ ._release.revision }}")
        self.assertIn('omit . "_release" "path"', source["helm"]["values"])
        self.assertIn('"prod-only"', appset["spec"]["templatePatch"])

    def test_namespaced_controller_has_bounded_resources_and_no_exposed_listener(self):
        forbidden = {"ClusterRole", "ClusterRoleBinding", "Service", "Ingress", "HTTPRoute"}
        self.assertFalse(forbidden & {doc["kind"] for doc in self.documents})
        for doc in self.documents:
            if doc["kind"] != "CustomResourceDefinition":
                self.assertEqual(doc["metadata"].get("namespace"), "argocd")
        pod = self.document("Deployment", "playground-image-updater-controller")["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "playground-image-updater")
        resources = pod["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["memory"], "128Mi")
        self.assertEqual(resources["limits"]["memory"], "256Mi")
        config = self.document("ConfigMap", "playground-image-updater-config")["data"]
        self.assertEqual(config["argocd.namespace"], "argocd")
        self.assertEqual(config["watch.namespaces"], "argocd")
        self.assertEqual(config["interval"], "2m")
        self.assertEqual(config["webhook.enable"], "false")
        registry = yaml.safe_load(config["registries.conf"])["registries"][0]
        self.assertEqual(registry["credentials"], "pullsecret:argocd/playground-image-updater-registry")
        self.assertFalse(registry["insecure"])

    def test_git_only_rbac_cannot_modify_applications_or_secrets(self):
        application_rules = []
        for doc in self.documents:
            if doc["kind"] != "Role":
                continue
            for rule in doc["rules"]:
                resources = set(rule["resources"])
                self.assertNotIn("*", resources)
                if "applications" in resources:
                    application_rules.append(rule)
                if resources & {"applications", "secrets"}:
                    self.assertLessEqual(set(rule["verbs"]), {"get", "list", "watch"})
        self.assertTrue(application_rules)
        role = self.document("Role", "playground-image-updater-git-only")
        self.assertTrue(any("imageupdaters/status" in rule["resources"] for rule in role["rules"]))
        self.assertTrue(any("imageupdaters/finalizers" in rule["resources"] for rule in role["rules"]))
        leader = self.document("Role", "playground-image-updater-leader-election-role")
        self.assertTrue(any("leases" in rule["resources"] for rule in leader["rules"]))

    def test_vault_auth_uses_its_dedicated_service_account(self):
        auth = self.document("VaultAuth", "playground-image-updater")["spec"]
        self.assertEqual(auth["method"], "kubernetes")
        self.assertEqual(auth["mount"], "kubernetes")
        self.assertEqual(auth["kubernetes"], {
            "role": "playground-image-updater", "serviceAccount": "playground-image-updater",
            "audiences": ["vault"], "tokenExpirationSeconds": 600,
        })

    def test_vault_secrets_use_only_explicit_mapped_fields(self):
        registry = self.document("VaultStaticSecret", "playground-image-updater-registry")["spec"]
        git = self.document("VaultStaticSecret", "playground-image-updater-git")["spec"]
        for secret in [registry, git]:
            self.assertEqual(secret["vaultAuthRef"], "playground-image-updater")
            self.assertEqual(secret["mount"], "kv")
            self.assertEqual(secret["type"], "kv-v2")
            transformation = secret["destination"]["transformation"]
            self.assertTrue(transformation["excludeRaw"])
            self.assertEqual(transformation["excludes"], [".*"])
        self.assertEqual(registry["path"], "apps/playground-prod/registry")
        self.assertEqual(registry["destination"]["type"], "kubernetes.io/dockerconfigjson")
        docker = registry["destination"]["transformation"]["templates"]
        self.assertEqual(set(docker), {".dockerconfigjson"})
        self.assertIn('"auth" (printf "%s:%s" $username $password | b64enc)', docker[".dockerconfigjson"]["text"])
        self.assertIn(REGISTRY, docker[".dockerconfigjson"]["text"])
        self.assertEqual(git["path"], "ci/github-app")
        self.assertEqual(git["destination"]["type"], "Opaque")
        mappings = git["destination"]["transformation"]["templates"]
        expected = {"githubAppID": "app_id", "githubAppInstallationID": "installation_id",
                    "githubAppPrivateKey": "private_key"}
        self.assertEqual(set(mappings), set(expected))
        for destination, source in expected.items():
            self.assertEqual(mappings[destination]["text"], '{{- get .Secrets "' + source + '" -}}')

    def test_existing_argocd_resources_are_not_claimed_or_replaced(self):
        protected = {"argocd-secret", "argocd-cm", "argocd-rbac-cm", "argocd-applicationset-controller"}
        self.assertFalse(protected & {doc["metadata"]["name"] for doc in self.documents})
        self.assertFalse({"Application", "ApplicationSet", "Secret", "Job"}
                         & {doc["kind"] for doc in self.documents})
        destinations = {doc["spec"]["destination"]["name"] for doc in self.documents
                        if doc["kind"] == "VaultStaticSecret"}
        self.assertEqual(destinations, {"playground-image-updater-registry", "playground-image-updater-git"})

    def test_custom_resource_fields_match_the_bundled_upstream_crd(self):
        crd = self.document("CustomResourceDefinition", "imageupdaters.argocd-image-updater.argoproj.io")
        version = next(version for version in crd["spec"]["versions"] if version["name"] == "v1alpha1")

        def check(value, schema, path):
            if isinstance(value, dict):
                self.assertLessEqual(set(schema.get("required", [])), set(value), path)
                properties = schema.get("properties")
                if properties:
                    self.assertLessEqual(set(value), set(properties), path)
                    for key, child in value.items():
                        check(child, properties[key], path + "." + key)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    check(child, schema["items"], path + f"[{index}]")

        check(self.updater, version["schema"]["openAPIV3Schema"], "ImageUpdater")


if __name__ == "__main__":
    unittest.main()
