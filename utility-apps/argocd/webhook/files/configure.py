"""Merge only the webhook key, then reload the ApplicationSet webhook listener."""
import base64
import hashlib
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request

KEY = "webhook.github.secret"
SECRET = "/api/v1/namespaces/argocd/secrets/argocd-secret"
DEPLOYMENT = "/apis/apps/v1/namespaces/argocd/deployments/argocd-applicationset-controller"
ANNOTATION = "gitops.api-api-api.com/webhook-secret-sha256"


def merge_payload(current, value):
    # A merge patch preserves admin.password, server.secretkey and future keys.
    # resourceVersion prevents silently racing an administrator's change.
    return {"metadata": {"resourceVersion": current["metadata"]["resourceVersion"]},
            "data": {KEY: base64.b64encode(value).decode()}}


def configure(request, value, sleep=time.sleep, monotonic=time.monotonic):
    if len(value) < 32:
        raise ValueError("Webhook secret must contain at least 32 bytes")
    for attempt in range(5):
        before = request(SECRET)
        if before.get("data", {}).get(KEY) == base64.b64encode(value).decode():
            break
        try:
            after = request(SECRET, merge_payload(before, value))
        except urllib.error.HTTPError as error:
            if error.code == 409 and attempt < 4:
                error.close()
                sleep(1)
                continue
            raise
        assert all(after["data"].get(key) == data for key, data in before.get("data", {}).items()
                   if key != KEY), "Existing Argo credentials changed unexpectedly"
        print("Webhook key merged; existing Argo credentials preserved", flush=True)
        break

    digest = hashlib.sha256(value).hexdigest()
    deployment = request(DEPLOYMENT)
    annotations = deployment["spec"]["template"].get("metadata", {}).get("annotations", {})
    if annotations.get(ANNOTATION) != digest:
        request(DEPLOYMENT, {"spec": {"template": {"metadata": {"annotations": {ANNOTATION: digest}}}}})
        print("Requested ApplicationSet controller reload", flush=True)
    deadline = monotonic() + 300
    while monotonic() < deadline:
        deployment = request(DEPLOYMENT)
        status = deployment.get("status", {})
        replicas = deployment["spec"].get("replicas", 1)
        if (replicas > 0 and status.get("observedGeneration", 0) >= deployment["metadata"]["generation"]
                and all(status.get(key, 0) == replicas for key in
                        ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
            print("ApplicationSet webhook listener reloaded", flush=True)
            return
        sleep(5)
    raise TimeoutError("ApplicationSet controller did not finish reloading")


def main():
    account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    token = (account / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(account / "ca.crt"))

    def request(path, patch=None):
        req = urllib.request.Request("https://kubernetes.default.svc" + path,
            data=None if patch is None else json.dumps(patch).encode(),
            method="GET" if patch is None else "PATCH",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/merge-patch+json"})
        with urllib.request.urlopen(req, context=context, timeout=20) as response:
            return json.load(response)

    try:
        configure(request, Path("/webhook/secret").read_bytes().strip())
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Kubernetes API rejected webhook configuration: HTTP {error.code}") from None


if __name__ == "__main__":
    main()
