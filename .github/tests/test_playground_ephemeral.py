"""Preview lifecycle switches preserve the existing staging render by default."""

import hashlib
import json
from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART_ROOT = ROOT / "utility-apps/playground-staging"
CHARTS = ("namespace", "kafka", "postgres", "mysql", "rabbitmq", "redis")
PREVIEW_NAMESPACE = "playground-preview-feature-a-01234567"

# Normalized Helm output captured before the additive preview parameterization.
# These hashes intentionally protect every staging field, including storage and
# namespace retention, from accidental changes while adding ephemeral behavior.
STAGING_RENDER_HASHES = {
    "namespace": "23d6bc69bcf16ee31e484f462d0d331bdc0a3a25aadfec8d5a11fcbc9874568c",
    "kafka": "0fb2c04ae2cd5946d7e11bfb3ae71a53832e7cbe967c6a2315c9ad4c5b8d835a",
    "postgres": "687fe0482a92cb8241fb124a76f17cf05ca0af2f9679119962f7d6872e598579",
    "mysql": "f6b8b899d16b14d82852323f83557e5268db9361165d0606f799a14225551a77",
    "rabbitmq": "4a82f28a0d02606952ba48ff74f93b73fd385dec4824c42d928daf802b02b08d",
    "redis": "3174f3bd4e66d2e3124ac305568f9d24205df3729dafde1040edc4ea0392c1f8",
}


def render(chart, namespace="playground-staging", values=None):
    command = ["helm", "template", chart, str(CHART_ROOT / chart),
               "--namespace", namespace]
    if values is not None:
        command.extend(["--values", "-"])
    output = subprocess.run(
        command, input=None if values is None else yaml.safe_dump(values),
        text=True, capture_output=True, check=True,
    ).stdout
    return [document for document in yaml.safe_load_all(output) if document]


def preview_values():
    return {
        "ephemeral": True,
        "vaultPath": f"apps/{PREVIEW_NAMESPACE}",
        "vault": {
            "role": PREVIEW_NAMESPACE,
            "envPath": f"apps/{PREVIEW_NAMESPACE}",
            "registryPath": f"apps/{PREVIEW_NAMESPACE}/registry",
        },
    }


class PlaygroundEphemeralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.staging = {chart: render(chart) for chart in CHARTS}
        cls.preview = {chart: render(chart, PREVIEW_NAMESPACE, preview_values())
                       for chart in CHARTS}

    def document(self, chart, kind, name, *, preview=True):
        documents = self.preview if preview else self.staging
        return next(document for document in documents[chart]
                    if document["kind"] == kind and document["metadata"]["name"] == name)

    def test_default_staging_render_is_unchanged_for_all_six_charts(self):
        for chart in CHARTS:
            with self.subTest(chart=chart):
                encoded = json.dumps(self.staging[chart], sort_keys=True,
                                     separators=(",", ":")).encode()
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), STAGING_RENDER_HASHES[chart])
                defaults = yaml.safe_load((CHART_ROOT / chart / "values.yaml").read_text())
                self.assertIs(defaults["ephemeral"], False)
                self.assertEqual(self.staging[chart], render(chart, values={"ephemeral": False}))

    def test_ephemeral_render_contains_no_retention_annotation_or_keep_flag(self):
        for chart, documents in self.preview.items():
            with self.subTest(chart=chart):
                text = yaml.safe_dump(documents)
                self.assertNotIn("Prune=false", text)
                self.assertNotIn("Delete=false", text)
                self.assertNotIn("Retain", text)
                self.assertNotIn("keepAfterDelete: true", text)
                self.assertNotIn("deleteClaim: false", text)
                self.assertEqual(len(documents), len(self.staging[chart]))

    def test_namespace_remains_internal_isolated_and_is_deleted_last(self):
        namespace = self.document("namespace", "Namespace", PREVIEW_NAMESPACE)
        self.assertEqual(namespace["metadata"]["labels"]["gateway.api-api-api.com/internal"], "true")
        self.assertNotIn("gateway.api-api-api.com/public", namespace["metadata"]["labels"])
        self.assertEqual(namespace["metadata"]["annotations"], {"argocd.argoproj.io/sync-wave": "-3"})
        self.assertEqual(self.document("namespace", "ServiceAccount", "playground-runtime")
                         ["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "-2")
        peers = self.document("namespace", "NetworkPolicy", "playground-isolation")
        self.assertEqual(peers, self.document("namespace", "NetworkPolicy", "playground-isolation",
                                              preview=False))

    def test_every_secret_uses_preview_paths_and_namespace_bound_auth(self):
        auth = self.document("namespace", "VaultAuth", "playground-runtime")["spec"]
        self.assertEqual(auth["kubernetes"]["role"], PREVIEW_NAMESPACE)
        self.assertEqual(auth["kubernetes"]["serviceAccount"], "playground-runtime")
        secrets = [document for documents in self.preview.values() for document in documents
                   if document["kind"] == "VaultStaticSecret"]
        self.assertEqual(len(secrets), 7)
        for secret in secrets:
            self.assertIn(secret["spec"]["path"], {
                f"apps/{PREVIEW_NAMESPACE}", f"apps/{PREVIEW_NAMESPACE}/registry",
            })
            self.assertTrue(secret["spec"]["destination"]["transformation"]["excludeRaw"])
        self.assertNotIn("apps/playground-staging", yaml.safe_dump(self.preview))
        self.assertNotIn("apps/playground-prod", yaml.safe_dump(self.preview))
        rabbit = self.document("rabbitmq", "VaultStaticSecret", "rabbitmq-default-user")
        host = rabbit["spec"]["destination"]["transformation"]["templates"]["host"]["text"]
        self.assertEqual(host, f"rabbitmq.{PREVIEW_NAMESPACE}.svc.cluster.local")

    def test_kafka_claims_are_deleted_and_topics_are_deleted_before_the_broker(self):
        pool = self.document("kafka", "KafkaNodePool", "combined")["spec"]
        self.assertTrue(pool["storage"]["deleteClaim"])
        self.assertNotIn("persistentVolumeClaim", pool["template"])
        topics = [document for document in self.preview["kafka"] if document["kind"] == "KafkaTopic"]
        self.assertEqual(len(topics), 3)
        for topic in topics:
            self.assertEqual(topic["metadata"]["annotations"], {"argocd.argoproj.io/sync-wave": "1"})
        kafka = self.document("kafka", "Kafka", "playground-kafka")
        self.assertNotIn("annotations", kafka["metadata"])
        peers = kafka["spec"]["kafka"]["listeners"][0]["networkPolicyPeers"]
        self.assertEqual(peers, [{"namespaceSelector": {"matchLabels": {
            "kubernetes.io/metadata.name": PREVIEW_NAMESPACE,
        }}}])

    def test_rabbit_and_redis_claim_retention_is_delete_for_ephemeral_only(self):
        rabbit = self.document("rabbitmq", "RabbitmqCluster", "rabbitmq")["spec"]
        statefulset = rabbit["override"]["statefulSet"]["spec"]
        self.assertEqual(statefulset["persistentVolumeClaimRetentionPolicy"], {
            "whenDeleted": "Delete", "whenScaled": "Delete",
        })
        self.assertNotIn("annotations", statefulset["volumeClaimTemplates"][0]["metadata"])
        redis = self.document("redis", "Redis", "playground-redis")["spec"]
        self.assertFalse(redis["storage"]["keepAfterDelete"])
        self.assertEqual(redis["kubernetesConfig"]["persistentVolumeClaimRetentionPolicy"], {
            "whenDeleted": "Delete", "whenScaled": "Delete",
        })
        # The installed Redis CRD forbids nested PVC metadata.
        self.assertNotIn("metadata", redis["storage"]["volumeClaimTemplate"])
        self.assertNotIn("inheritedMetadata", self.document("postgres", "Cluster", "playground-postgres")["spec"])
        self.assertNotIn("annotations", self.document("mysql", "PersistentVolumeClaim", "playground-mysql")["metadata"])

    def test_overriding_vault_path_does_not_enable_ephemeral_deletion(self):
        for chart in ("postgres", "mysql", "rabbitmq", "redis"):
            with self.subTest(chart=chart):
                documents = render(chart, values={"vaultPath": "apps/another-retained-environment"})
                self.assertIn("Prune=false,Delete=false", yaml.safe_dump(documents))
                for document in documents:
                    if document["kind"] == "VaultStaticSecret":
                        self.assertEqual(document["spec"]["path"], "apps/another-retained-environment")


if __name__ == "__main__":
    unittest.main()
