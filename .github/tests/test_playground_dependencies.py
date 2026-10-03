"""Offline contract checks for the seven isolated playground utility charts."""

from pathlib import Path
import re
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
UTILITIES = ROOT / "utility-apps/playground-prod"
NAMESPACE = "playground-prod"
CHARTS = {"namespace", "kafka", "rabbitmq", "postgres", "mysql", "redis", "edge"}
URL_KEYS = {
    "POSTGRES_URL", "FLASHDROP_ANALYTICS_POSTGRES_URL", "MYSQL_URL",
    "REDIS_URL", "RABBITMQ_URL",
}
OPERATORS = {"kafka-system", "rabbitmq-system", "database-system", "redis-system"}


class PlaygroundDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = {}
        cls.documents = []
        for name in sorted(CHARTS):
            output = subprocess.check_output(
                ["helm", "template", name, str(UTILITIES / name),
                 "--namespace", NAMESPACE],
                text=True,
            )
            cls.rendered[name] = [doc for doc in yaml.safe_load_all(output) if doc]
            cls.documents.extend(cls.rendered[name])

    def document(self, chart, kind, name):
        return next(doc for doc in self.rendered[chart]
                    if doc["kind"] == kind and doc["metadata"]["name"] == name)

    def assert_retained(self, metadata):
        options = set(metadata.get("annotations", {}).get(
            "argocd.argoproj.io/sync-options", "").split(","))
        self.assertLessEqual({"Prune=false", "Delete=false"}, options)

    def assert_bounded(self, resources):
        for field in ("requests", "limits"):
            self.assertLessEqual({"cpu", "memory"}, set(resources[field]))
            for value in resources[field].values():
                self.assertNotEqual(str(value), "0")

    def test_seven_independent_charts_have_no_operator_install_dependencies(self):
        actual = {path.parent.name for path in UTILITIES.glob("*/Chart.yaml")}
        self.assertEqual(actual, CHARTS)
        for name in CHARTS:
            chart = yaml.safe_load((UTILITIES / name / "Chart.yaml").read_text())
            self.assertEqual(chart["name"], name)
            self.assertEqual(chart["type"], "application")
            self.assertFalse(chart.get("dependencies"))
            self.assertTrue(self.rendered[name])
        forbidden = {"Application", "ApplicationSet", "CustomResourceDefinition",
                     "ClusterRole", "ClusterRoleBinding", "Secret", "Job"}
        self.assertFalse(forbidden & {doc["kind"] for doc in self.documents})
        identities = [(doc["apiVersion"], doc["kind"], doc["metadata"]["name"])
                      for doc in self.documents]
        self.assertEqual(len(identities), len(set(identities)))

    def test_namespace_is_retained_and_only_internal_gateway_is_allowed(self):
        namespace = self.document("namespace", "Namespace", NAMESPACE)
        self.assert_retained(namespace["metadata"])
        labels = namespace["metadata"]["labels"]
        self.assertEqual(labels["gateway.api-api-api.com/internal"], "true")
        self.assertNotIn("gateway.api-api-api.com/public", labels)
        for doc in self.documents:
            if doc["kind"] != "Namespace":
                self.assertEqual(doc["metadata"].get("namespace", NAMESPACE), NAMESPACE)

    def test_ingress_is_restricted_to_namespace_and_known_operators(self):
        policy = self.document("namespace", "NetworkPolicy", "playground-isolation")["spec"]
        self.assertEqual(policy["podSelector"], {})
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertEqual(len(policy["ingress"]), 1)
        peers = policy["ingress"][0]["from"]
        self.assertEqual(peers[0], {"podSelector": {}})
        self.assertEqual(len(peers), 2)
        self.assertEqual(peers[1]["namespaceSelector"]["matchExpressions"], [{
            "key": "kubernetes.io/metadata.name", "operator": "In",
            "values": ["kafka-system", "rabbitmq-system", "database-system", "redis-system"],
        }])
        self.assertEqual(set(peers[1]["namespaceSelector"]["matchExpressions"][0]["values"]), OPERATORS)

    def test_traefik_can_reach_only_the_edge_http_port(self):
        policy = self.document("namespace", "NetworkPolicy", "playground-edge-from-traefik")["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": {
            "app.kubernetes.io/name": "playground-edge"}})
        self.assertEqual(policy["ingress"], [{
            "from": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"app.kubernetes.io/name": "traefik"}},
            }],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }])

    def test_vault_auth_is_namespace_scoped_and_tokens_are_not_automounted(self):
        account = self.document("namespace", "ServiceAccount", "playground-runtime")
        self.assertFalse(account["automountServiceAccountToken"])
        self.assertEqual(account["imagePullSecrets"], [{"name": "playground-registry"}])
        auth = self.document("namespace", "VaultAuth", "playground-runtime")["spec"]
        self.assertEqual(auth["method"], "kubernetes")
        self.assertEqual(auth["kubernetes"], {
            "role": "playground-prod", "serviceAccount": "playground-runtime",
            "audiences": ["vault"], "tokenExpirationSeconds": 600,
        })

    def test_all_secrets_are_explicit_vault_projections_without_raw_leakage(self):
        secrets = [doc for doc in self.documents if doc["kind"] == "VaultStaticSecret"]
        self.assertEqual(len(secrets), 7)
        for secret in secrets:
            with self.subTest(secret=secret["metadata"]["name"]):
                spec = secret["spec"]
                self.assertEqual(spec["vaultAuthRef"], "playground-runtime")
                self.assertEqual(spec["mount"], "kv")
                self.assertEqual(spec["type"], "kv-v2")
                self.assertIn(spec["path"], {"apps/playground-prod", "apps/playground-prod/registry"})
                transform = spec["destination"]["transformation"]
                self.assertTrue(transform["excludeRaw"])
                self.assertEqual(transform["excludes"], [".*"])
                self.assertTrue(transform["templates"])
        env = self.document("namespace", "VaultStaticSecret", "playground-env")["spec"]
        templates = env["destination"]["transformation"]["templates"]
        self.assertEqual(set(templates), URL_KEYS)
        for key, template in templates.items():
            self.assertEqual(template["text"], '{{ get .Secrets "' + key + '" }}')
        self.assertNotIn("ADMIN", yaml.safe_dump(env))
        self.assertNotIn("ROOT_PASSWORD", yaml.safe_dump(env))
        registry = self.document("namespace", "VaultStaticSecret", "playground-registry")["spec"]
        self.assertEqual(registry["path"], "apps/playground-prod/registry")
        self.assertEqual(registry["destination"]["type"], "kubernetes.io/dockerconfigjson")
        self.assertEqual(set(registry["destination"]["transformation"]["templates"]), {".dockerconfigjson"})

    def test_no_public_gateway_or_broker_admin_service_exposure(self):
        routes = [doc for doc in self.documents if doc["kind"] == "HTTPRoute"]
        self.assertEqual(len(routes), 1)
        route = routes[0]["spec"]
        self.assertEqual(route["parentRefs"], [{
            "group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "internal",
            "namespace": "networking", "sectionName": "https",
        }])
        self.assertEqual(route["hostnames"], ["playground.internal.api-api-api.com"])
        self.assertEqual(route["rules"][0]["backendRefs"], [
            {"group": "", "kind": "Service", "name": "edge", "port": 8080}])
        self.assertFalse({"Ingress", "IngressRoute", "TCPRoute", "Gateway"}
                         & {doc["kind"] for doc in self.documents})
        for doc in self.documents:
            if doc["kind"] == "Service":
                self.assertEqual(doc["spec"].get("type", "ClusterIP"), "ClusterIP")
                self.assertNotIn("externalIPs", doc["spec"])
                self.assertNotIn("externalName", doc["spec"])
                self.assertTrue(all("nodePort" not in port for port in doc["spec"]["ports"]))
            if doc["kind"] in {"Deployment", "StatefulSet"}:
                pod = doc["spec"]["template"]["spec"]
                self.assertFalse(pod.get("hostNetwork", False))
                for container in pod["containers"]:
                    self.assertTrue(all("hostPort" not in port for port in container.get("ports", [])))

    def test_kafka_is_dedicated_internal_plaintext_and_single_node(self):
        kafka = self.document("kafka", "Kafka", "playground-kafka")
        self.assert_retained(kafka["metadata"])
        listeners = kafka["spec"]["kafka"]["listeners"]
        self.assertEqual(listeners, [{
            "name": "plain", "port": 9092, "type": "internal", "tls": False,
            "networkPolicyPeers": [{"namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}}}],
        }])
        config = kafka["spec"]["kafka"]["config"]
        self.assertFalse(config["auto.create.topics.enable"])
        for key in ("default.replication.factor", "min.insync.replicas",
                    "offsets.topic.replication.factor", "transaction.state.log.replication.factor",
                    "transaction.state.log.min.isr"):
            self.assertEqual(config[key], 1)
        pool = self.document("kafka", "KafkaNodePool", "combined")
        self.assertEqual(pool["metadata"]["labels"]["strimzi.io/cluster"], "playground-kafka")
        self.assertEqual(pool["spec"]["replicas"], 1)
        self.assertEqual(set(pool["spec"]["roles"]), {"broker", "controller"})
        self.assert_bounded(pool["spec"]["resources"])
        self.assert_bounded(kafka["spec"]["entityOperator"]["topicOperator"]["resources"])

    def test_kafka_topics_match_the_application_contract_and_retain_data(self):
        topics = [doc for doc in self.rendered["kafka"] if doc["kind"] == "KafkaTopic"]
        self.assertEqual({doc["spec"]["topicName"]: (
            doc["spec"]["partitions"], doc["spec"]["config"]["retention.ms"])
            for doc in topics}, {
                "flashdrop.orders.v1": (6, 86400000),
                "flashdrop.traces.v1": (6, 3600000),
                "flashdrop.analytics.v1": (3, 86400000),
            })
        for topic in topics:
            self.assert_retained(topic["metadata"])
            self.assertEqual(topic["metadata"]["labels"]["strimzi.io/cluster"], "playground-kafka")
            self.assertEqual(topic["spec"]["replicas"], 1)
            self.assertEqual(topic["spec"]["config"]["min.insync.replicas"], 1)

    def test_rabbit_default_queue_type_stays_classic_with_application_owned_topology(self):
        cluster = self.document("rabbitmq", "RabbitmqCluster", "rabbitmq")
        config = cluster["spec"]["rabbitmq"]["additionalConfig"]
        match = re.search(r"^\s*default_queue_type\s*=\s*(\S+)", config, re.MULTILINE)
        self.assertTrue(match is None or match.group(1) == "classic")
        # Gateway/inventory explicitly declare quorum; event-hub declares classic.
        # A global quorum default would also affect third-party temporary queues.
        self.assertFalse({"Queue", "Exchange", "Binding", "Vhost"}
                         & {doc["kind"] for doc in self.rendered["rabbitmq"]})
        self.assertEqual(cluster["spec"]["replicas"], 1)
        self.assert_bounded(cluster["spec"]["resources"])
        self.assertNotIn("topology-allowed-namespaces", str(cluster["metadata"]))

    def test_rabbit_separates_operator_admin_from_scoped_application_user(self):
        cluster = self.document("rabbitmq", "RabbitmqCluster", "rabbitmq")
        self.assertEqual(cluster["spec"]["secretBackend"]["externalSecret"]["name"], "rabbitmq-default-user")
        for secret_name, prefix in (("rabbitmq-default-user", "RABBITMQ_ADMIN"),
                                    ("rabbitmq-app-user", "RABBITMQ")):
            secret = self.document("rabbitmq", "VaultStaticSecret", secret_name)["spec"]
            templates = secret["destination"]["transformation"]["templates"]
            for field in ("username", "password"):
                self.assertEqual(templates[field]["text"],
                                 '{{ get .Secrets "' + prefix + '_' + field.upper() + '" }}')
        user = self.document("rabbitmq", "User", "playground")["spec"]
        self.assertEqual(user["tags"], ["monitoring"])
        self.assertEqual(user["importCredentialsSecret"], {"name": "rabbitmq-app-user"})
        permission = self.document("rabbitmq", "Permission", "playground")["spec"]
        self.assertEqual(permission["vhost"], "/")
        self.assertEqual(permission["userReference"], {"name": "playground"})
        for resource in (user, permission):
            self.assertEqual(resource["rabbitmqClusterReference"], {"name": "rabbitmq"})
        for pattern in permission["permissions"].values():
            self.assertIsNotNone(re.fullmatch(pattern, "flashdrop.inventory.commands.v1"))
            self.assertIsNone(re.fullmatch(pattern, "unrelated.application.queue"))

    def test_database_owners_and_storage_do_not_reuse_other_applications(self):
        postgres = self.document("postgres", "Cluster", "playground-postgres")["spec"]
        self.assertEqual(postgres["instances"], 1)
        self.assertFalse(postgres["enableSuperuserAccess"])
        bootstrap = postgres["bootstrap"]["initdb"]
        self.assertEqual((bootstrap["database"], bootstrap["owner"]), ("playground", "playground"))
        self.assertEqual(bootstrap["secret"]["name"], "playground-postgres-app")
        self.assert_bounded(postgres["resources"])
        mysql = self.document("mysql", "StatefulSet", "mysql")["spec"]
        self.assertEqual(mysql["replicas"], 1)
        pod = mysql["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["volumes"], [{"name": "data", "persistentVolumeClaim": {
            "claimName": "playground-mysql"}}])
        container = pod["containers"][0]
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": "playground-mysql-env"}}])
        self.assertTrue(all(probe in container for probe in ("startupProbe", "readinessProbe", "livenessProbe")))
        self.assert_bounded(container["resources"])

    def test_every_stateful_dependency_retains_its_pvc_and_controller(self):
        kafka = self.document("kafka", "KafkaNodePool", "combined")
        self.assert_retained(kafka["metadata"])
        self.assertFalse(kafka["spec"]["storage"]["deleteClaim"])
        self.assert_retained(kafka["spec"]["template"]["persistentVolumeClaim"]["metadata"])
        rabbit = self.document("rabbitmq", "RabbitmqCluster", "rabbitmq")
        self.assert_retained(rabbit["metadata"])
        statefulset = rabbit["spec"]["override"]["statefulSet"]["spec"]
        self.assertEqual(statefulset["persistentVolumeClaimRetentionPolicy"], {
            "whenDeleted": "Retain", "whenScaled": "Retain"})
        self.assert_retained(statefulset["volumeClaimTemplates"][0]["metadata"])
        postgres = self.document("postgres", "Cluster", "playground-postgres")
        self.assert_retained(postgres["metadata"])
        self.assert_retained(postgres["spec"]["inheritedMetadata"])
        self.assert_retained(self.document("mysql", "PersistentVolumeClaim", "playground-mysql")["metadata"])
        redis = self.document("redis", "Redis", "playground-redis")
        self.assert_retained(redis["metadata"])
        self.assertTrue(redis["spec"]["storage"]["keepAfterDelete"])
        self.assert_retained(redis["spec"]["storage"]["volumeClaimTemplate"]["metadata"])
        self.assertEqual(redis["spec"]["kubernetesConfig"]["persistentVolumeClaimRetentionPolicy"], {
            "whenDeleted": "Retain", "whenScaled": "Retain"})

    def test_redis_is_password_protected_and_preserves_idempotency_records(self):
        redis = self.document("redis", "Redis", "playground-redis")["spec"]
        self.assertEqual(redis["kubernetesConfig"]["redisSecret"], {
            "name": "playground-redis-password", "key": "password"})
        self.assertEqual(redis["redisConfig"]["additionalRedisConfig"], "playground-redis-config")
        self.assert_bounded(redis["kubernetesConfig"]["resources"])
        config = self.document("redis", "ConfigMap", "playground-redis-config")["data"]["redis-additional.conf"]
        for directive in ("appendonly yes", "appendfsync everysec", "maxmemory-policy noeviction"):
            self.assertIn(directive, config)
        self.assertRegex(config, r"(?m)^maxmemory\s+[1-9][0-9]*(?:mb|gb)$")

    def test_edge_preserves_sse_and_same_origin_routes_without_admin_tools(self):
        edge = self.document("edge", "ConfigMap", "playground-edge")["data"]["nginx.conf"]
        self.assertNotIn("127.0.0.11", edge)
        self.assertIn("resolver 10.43.0.10", edge)
        for service, port in (("order-service", 3000), ("event-hub", 3003),
                              ("pricing-service", 8001), ("analytics-service", 8002),
                              ("inventory-service", 3004), ("shell", 80),
                              ("topology-mfe", 80), ("traffic-mfe", 80)):
            self.assertIn(f"{service}.{NAMESPACE}.svc.cluster.local:{port}", edge)
        for directive in ("proxy_buffering off;", "proxy_cache off;", "proxy_read_timeout 1h;",
                          'proxy_set_header Connection "";', 'add_header X-Accel-Buffering "no" always;'):
            self.assertIn(directive, edge)
        self.assertIn("location /ops/tools/ { return 404; }", edge)
        self.assertNotRegex(edge, r"http://(?:kafka-ui|adminer|redis-insight|airflow|rabbitmq)")
        route = self.document("edge", "HTTPRoute", "playground")["spec"]
        self.assertEqual(route["rules"][0]["timeouts"], {"request": "0s", "backendRequest": "0s"})
        pod = self.document("edge", "Deployment", "edge")["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        self.assert_bounded(pod["containers"][0]["resources"])


if __name__ == "__main__":
    unittest.main()
