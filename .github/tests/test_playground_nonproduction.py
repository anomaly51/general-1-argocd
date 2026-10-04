"""Render isolated low-load playground environments without touching prod data."""

from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = (
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
)
CHARTS = {"namespace", "kafka", "postgres", "mysql", "rabbitmq", "redis", "edge"}


def render(path, namespace, values=None):
    command = ["helm", "template", path.name, str(path), "--namespace", namespace]
    if values is not None:
        command.extend(["--values", "-"])
    result = subprocess.run(command, input=None if values is None else yaml.safe_dump(values),
                            text=True, capture_output=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def memory_mib(value):
    if value.endswith("Gi"):
        return float(value[:-2]) * 1024
    if value.endswith("Mi"):
        return float(value[:-2])
    raise AssertionError(f"Unexpected memory unit: {value}")


class PlaygroundNonproductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Dev is deployed and verified first; staging is added in a later commit.
        cls.environments = [env for env in ("dev", "staging")
                            if (ROOT / f"utility-apps/playground-{env}").is_dir()]
        cls.documents = {}
        cls.profiles = {}
        for environment in cls.environments:
            namespace = f"playground-{environment}"
            documents = []
            for chart in CHARTS:
                documents.extend(render(ROOT / f"utility-apps/{namespace}/{chart}", namespace))
            for component in COMPONENTS:
                chart = ROOT / f"apps/playground-{component}"
                profile = yaml.safe_load((chart / f"values/{environment}.yaml").read_text())
                cls.profiles[environment, component] = profile
                documents.extend(render(chart, namespace, {key: value for key, value in profile.items()
                                                           if key != "_release"}))
            cls.documents[environment] = documents

    def document(self, environment, kind, name):
        return next(doc for doc in self.documents[environment]
                    if doc["kind"] == kind and doc["metadata"]["name"] == name)

    def test_eight_apps_and_seven_utility_charts_are_independently_discoverable(self):
        self.assertIn("dev", self.environments)
        for environment in self.environments:
            namespace = f"playground-{environment}"
            path = ROOT / "utility-apps" / namespace
            self.assertEqual({chart.parent.name for chart in path.glob("*/Chart.yaml")}, CHARTS)
            identities = [(doc["apiVersion"], doc["kind"], doc["metadata"]["name"])
                          for doc in self.documents[environment]]
            self.assertEqual(len(identities), len(set(identities)))
            for component in COMPONENTS:
                profile = self.profiles[environment, component]
                self.assertEqual(profile["_release"]["namespace"], namespace)
                self.assertEqual(profile["_release"]["sourceRepository"], f"anomaly51/playground-{component}")
                self.assertRegex(profile["_release"]["revision"], r"^[0-9a-f]{40}$")
                self.assertEqual(profile["image"]["repository"],
                                 f"harbor.internal.api-api-api.com/playground/{component}")
                self.assertRegex(profile["image"]["tag"], rf"^{environment}@sha256:[0-9a-f]{{64}}$")

    def test_each_namespace_has_only_internal_routing_and_its_own_secrets(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/apps.yaml").read_text())
        self.assertIn('(not (hasPrefix "playground-" (index .path.segments 1)))',
                      appset["spec"]["templatePatch"])
        for environment in self.environments:
            namespace = f"playground-{environment}"
            labels = self.document(environment, "Namespace", namespace)["metadata"]["labels"]
            self.assertEqual(labels["gateway.api-api-api.com/internal"], "true")
            self.assertNotIn("gateway.api-api-api.com/public", labels)
            route = self.document(environment, "HTTPRoute", "playground")["spec"]
            self.assertEqual(route["hostnames"], [f"playground-{environment}.internal.api-api-api.com"])
            self.assertEqual(route["parentRefs"][0]["name"], "internal")
            auth = self.document(environment, "VaultAuth", "playground-runtime")["spec"]
            self.assertEqual(auth["kubernetes"]["role"], namespace)
            self.assertEqual(auth["kubernetes"]["serviceAccount"], "playground-runtime")
            secret_count = 0
            for doc in self.documents[environment]:
                if doc["kind"] == "VaultStaticSecret":
                    secret_count += 1
                    self.assertIn(doc["spec"]["path"], {f"apps/{namespace}", f"apps/{namespace}/registry"})
                    self.assertTrue(doc["spec"]["destination"]["transformation"]["excludeRaw"])
                if doc["kind"] == "Service":
                    self.assertEqual(doc["spec"].get("type", "ClusterIP"), "ClusterIP")
            self.assertEqual(secret_count, 7)
            self.assertNotIn("playground-prod", yaml.safe_dump(self.documents[environment]))
            nginx = self.document(environment, "ConfigMap", "playground-edge")["data"]["nginx.conf"]
            self.assertIn(f"order-service.{namespace}.svc.cluster.local", nginx)
            self.assertIn("proxy_buffering off;", nginx)
            for component in ("order-service", "event-hub"):
                self.assertEqual(self.profiles[environment, component]["env"]["CORS_ORIGINS"],
                                 f"https://playground-{environment}.internal.api-api-api.com")

    def test_network_isolation_has_no_other_environment_peer(self):
        for environment in self.environments:
            namespace = f"playground-{environment}"
            policy = self.document(environment, "NetworkPolicy", "playground-isolation")["spec"]
            self.assertEqual(policy["policyTypes"], ["Ingress"])
            self.assertEqual(policy["ingress"][0]["from"][0], {"podSelector": {}})
            operators = policy["ingress"][0]["from"][1]["namespaceSelector"]["matchExpressions"][0]
            self.assertEqual(set(operators["values"]),
                             {"kafka-system", "rabbitmq-system", "database-system", "redis-system"})
            kafka = self.document(environment, "Kafka", "playground-kafka")["spec"]
            peers = kafka["kafka"]["listeners"][0]["networkPolicyPeers"]
            self.assertEqual(peers, [{"namespaceSelector": {"matchLabels": {
                "kubernetes.io/metadata.name": namespace}}}])

    def test_explicit_worker_placement_and_low_load_request_budget(self):
        for environment in self.environments:
            memory = 0
            deployments = []
            for doc in self.documents[environment]:
                kind, spec = doc["kind"], doc.get("spec", {})
                resources = None
                if kind in {"Deployment", "StatefulSet"}:
                    pod = spec["template"]["spec"]
                    for container in pod["containers"]:
                        memory += memory_mib(container["resources"]["requests"]["memory"])
                    if kind == "Deployment" and doc["metadata"]["name"] != "edge":
                        deployments.append(doc)
                        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "general-1-worker-2"})
                elif kind in {"KafkaNodePool", "Cluster", "RabbitmqCluster"}:
                    resources = spec["resources"]
                elif kind == "Redis":
                    resources = spec["kubernetesConfig"]["resources"]
                elif kind == "Kafka":
                    resources = spec["entityOperator"]["topicOperator"]["resources"]
                    self.assertEqual(spec["entityOperator"]["topicOperator"]["jvmOptions"],
                                     {"-Xms": "64m", "-Xmx": "128m"})
                    self.assertIn("general-1-worker-2", yaml.safe_dump(spec["entityOperator"]["template"]))
                if resources:
                    memory += memory_mib(resources["requests"]["memory"])
            self.assertEqual(len(deployments), 9, "Eight services plus the order outbox relay")
            self.assertEqual(memory, 2336)
            self.assertLessEqual(memory, 2560, "Each low-load environment must request <=2.5 GiB")
            for chart in ("kafka", "postgres", "mysql", "rabbitmq", "redis"):
                values = yaml.safe_load((ROOT / f"utility-apps/playground-{environment}/{chart}/values.yaml").read_text())
                expected = "general-1-worker-3" if environment == "staging" and chart == "kafka" else "general-1-worker-1"
                self.assertEqual(values["storage"]["nodeName"], expected)
                self.assertEqual(values["storage"]["className"], "local-path")
            storage = [yaml.safe_load((ROOT / f"utility-apps/playground-{environment}/{chart}/values.yaml").read_text())["storage"]["size"]
                       for chart in ("kafka", "postgres", "mysql", "rabbitmq", "redis")]
            self.assertEqual(sum(int(size.removesuffix("Gi")) for size in storage), 11)

    def test_all_new_persistent_resources_retain_data(self):
        for environment in self.environments:
            for kind, name in (("KafkaNodePool", "combined"), ("Kafka", "playground-kafka"),
                               ("Cluster", "playground-postgres"), ("PersistentVolumeClaim", "playground-mysql"),
                               ("RabbitmqCluster", "rabbitmq"), ("Redis", "playground-redis")):
                doc = self.document(environment, kind, name)
                options = doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
                self.assertIn("Prune=false", options)
                self.assertIn("Delete=false", options)
            kafka = self.document(environment, "KafkaNodePool", "combined")["spec"]
            self.assertFalse(kafka["storage"]["deleteClaim"])
            redis = self.document(environment, "Redis", "playground-redis")["spec"]
            self.assertTrue(redis["storage"]["keepAfterDelete"])


if __name__ == "__main__":
    unittest.main()
