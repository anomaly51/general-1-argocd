"""Delete only reviewed terminal pods, plus one stuck pre-fix MinIO pod; never touch data."""
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request

BASE = "https://kubernetes.default.svc"
AUTH = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TLS = ssl.create_default_context(cafile=str(AUTH / "ca.crt"))
MINIO_IMAGE = "harbor.internal.api-api-api.com/applications/minio:RELEASE.2025-04-22T22-12-26Z@sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb"


def request(path, method="GET", body=None):
    headers = {"Authorization": "Bearer " + (AUTH / "token").read_text().strip(),
               "Content-Type": "application/json"}
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, context=TLS, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"Kubernetes {method} returned HTTP {error.code}; body omitted") from None


def owner(pod):
    return next((o for o in pod["metadata"].get("ownerReferences", []) if o.get("controller")), None)


def replacement_ready(pod):
    ref = owner(pod)
    if not ref or ref["kind"] != "ReplicaSet":
        return False
    ns = pod["metadata"]["namespace"]
    rs = request(f"/apis/apps/v1/namespaces/{ns}/replicasets/{ref['name']}")
    if not rs or rs["metadata"]["uid"] != ref["uid"]:
        return False
    parent = owner(rs)
    if not parent or parent["kind"] != "Deployment":
        return False
    deployment = request(f"/apis/apps/v1/namespaces/{ns}/deployments/{parent['name']}")
    if not deployment or deployment["metadata"]["uid"] != parent["uid"]:
        return False
    wanted = deployment["spec"].get("replicas", 1)
    status = deployment.get("status", {})
    return (wanted > 0 and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
            and status.get("availableReplicas", 0) >= wanted)


def minio_safe(pod):
    if pod["metadata"]["namespace"] != "apps" or pod["metadata"]["name"] != "shisha-guid-backend-minio-0":
        return False
    if pod["status"].get("phase") != "Pending":
        return False
    if pod["spec"]["containers"][0]["image"] != "quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z":
        return False
    if not any(c.get("state", {}).get("waiting", {}).get("reason") in {"ImagePullBackOff", "ErrImagePull"}
               for c in pod["status"].get("containerStatuses", [])):
        return False
    ref = owner(pod)
    if not ref or ref["kind"] != "StatefulSet" or ref["name"] != "shisha-guid-backend-minio":
        return False
    sts = request("/apis/apps/v1/namespaces/apps/statefulsets/shisha-guid-backend-minio")
    if not sts or sts["metadata"]["uid"] != ref["uid"]:
        return False
    desired = sts["spec"]["template"]["spec"]
    return (sts["spec"].get("replicas") == 1 and desired["containers"][0]["image"] == MINIO_IMAGE
            and any(v.get("persistentVolumeClaim", {}).get("claimName") == "shisha-guid-backend-minio"
                    for v in desired.get("volumes", []))
            and sts["status"].get("updateRevision") != pod["metadata"].get("labels", {}).get("controller-revision-hash"))


def recover(target):
    path = f"/api/v1/namespaces/{target['namespace']}/pods/{target['name']}"
    pod = request(path)
    if pod is None or pod["metadata"]["uid"] != target["uid"]:
        return True  # Never delete a replacement that happens to reuse the same name.
    if pod["metadata"].get("deletionTimestamp"):
        return True
    if target["mode"] == "evicted":
        if pod["status"].get("phase") != "Failed" or pod["status"].get("reason") != "Evicted":
            raise RuntimeError("Refusing a pod whose terminal status changed")
        if not replacement_ready(pod):
            return False
    elif target["mode"] == "minio":
        if not minio_safe(pod):
            raise RuntimeError("Refusing MinIO deletion: desired release or pod state changed")
    else:
        raise RuntimeError("Unknown recovery mode")
    request(path, "DELETE", {"apiVersion": "v1", "kind": "DeleteOptions",
                             "gracePeriodSeconds": 30,
                             "preconditions": {"uid": target["uid"],
                                               "resourceVersion": pod["metadata"]["resourceVersion"]}})
    print(json.dumps({"deleted": target["namespace"] + "/" + target["name"], "mode": target["mode"]}), flush=True)
    return True


targets = json.loads(Path("/work/targets.json").read_text())
assert len(targets) == 70 and sum(t["mode"] == "evicted" for t in targets) == 69
targets.sort(key=lambda target: target["mode"] != "minio")
deadline = time.monotonic() + 1500
while targets and time.monotonic() < deadline:
    targets = [target for target in targets if not recover(target)]
    if targets:
        print(json.dumps({"waiting_for_healthy_replacements": len(targets)}), flush=True)
        time.sleep(15)
if targets:
    raise RuntimeError("Some replacements are not healthy; their old pod records were preserved")
print("Reviewed pod recovery complete; no PVCs, Secrets or workloads were deleted", flush=True)
