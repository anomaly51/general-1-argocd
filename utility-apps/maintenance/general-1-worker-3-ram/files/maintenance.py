"""One-shot Kubernetes maintenance; no Proxmox API or credential access."""
import argparse
import copy
import datetime
import json
import os
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NODE = "general-1-worker-3"
ANNOTATION = "maintenance.cutline.studio/operation"


class Abort(RuntimeError):
    pass


class APIError(Abort):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Kubernetes HTTP {status}; response omitted")


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Abort("API redirect rejected")


class API:
    def __init__(self):
        self.deadline = None
        directory = "/var/run/secrets/kubernetes.io/serviceaccount/"
        with open(directory + "token", encoding="utf-8") as stream:
            self.token = stream.read().strip()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.origin = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=directory + "ca.crt")))

    def call(self, method, path, body=None, content_type="application/json"):
        # The only write surfaces are the one node and pod eviction subresources.
        if method not in ("GET", "PATCH", "POST") or (method == "PATCH" and path != f"/api/v1/nodes/{NODE}") or (method == "POST" and not path.endswith("/eviction")):
            raise Abort("Forbidden API operation")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.origin + path, data=data, method=method, headers={"Authorization": "Bearer " + self.token, "Content-Type": content_type})
        try:
            remaining = 10 if self.deadline is None else min(10, self.deadline - time.monotonic())
            if remaining <= 0:
                raise Abort("Drain deadline exceeded")
            with self.opener.open(request, timeout=remaining) as response:
                payload = response.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise Abort("API response exceeds metadata limit")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Abort("Kubernetes connection failed; details omitted") from None


def identity(pod):
    metadata, spec = pod["metadata"], pod["spec"]
    return {
        "namespace": metadata["namespace"], "name": metadata["name"], "uid": metadata["uid"],
        "owners": [{"kind": o["kind"], "name": o["name"], "uid": o["uid"], "controller": o.get("controller", False)} for o in metadata.get("ownerReferences", [])],
        "mirror": bool(metadata.get("annotations", {}).get("kubernetes.io/config.mirror")),
        "volumes": sorted([{"name": v["name"], "kind": next(k for k in v if k != "name"), "claimName": v.get("persistentVolumeClaim", {}).get("claimName")} for v in spec.get("volumes", [])], key=lambda v: v["name"]),
        "mounts": sorted([{"container": c["name"], "name": m["name"], "path": m["mountPath"], "subPath": m.get("subPath"), "readOnly": m.get("readOnly", False)} for c in spec.get("initContainers", []) + spec["containers"] for m in c.get("volumeMounts", [])], key=lambda m: m["container"] + "/" + m["name"] + "/" + m["path"]),
    }


def key(pod):
    return pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"]


def is_daemon(pod):
    return any(o["kind"] == "DaemonSet" and o.get("controller") for o in pod["metadata"].get("ownerReferences", []))


def selector_matches(labels, selector):
    if not all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items()):
        return False
    for term in selector.get("matchExpressions", []):
        present, value = term["key"] in labels, labels.get(term["key"])
        operator = term["operator"]
        if operator == "In" and value not in term["values"]:
            return False
        if operator == "NotIn" and value in term["values"]:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator not in ("In", "NotIn", "Exists", "DoesNotExist"):
            raise Abort("Unknown PDB selector operator")
    return True


def memory_bytes(value):
    for suffix, multiplier in (("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3)):
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * multiplier)
    return int(value)


class Maintenance:
    def __init__(self, api, inventory, operation, timeout=840, clock=time.monotonic, sleep=time.sleep):
        self.api, self.inventory, self.operation = api, inventory, operation
        self.clock, self.sleep, self.timeout = clock, sleep, timeout
        self.expected = {p["namespace"] + "/" + p["name"]: p for p in inventory["pods"]}

    def node(self):
        node = self.api.call("GET", f"/api/v1/nodes/{NODE}")
        if node["metadata"]["name"] != NODE or node["metadata"]["uid"] != self.inventory["node"]["uid"]:
            raise Abort("Node identity changed")
        return node

    def set_cordon(self, enabled, require_upgraded=False, require_recovered=False):
        node = self.node()
        annotations = copy.deepcopy(node["metadata"].get("annotations", {}))
        owner = annotations.get(ANNOTATION)
        if enabled:
            if node.get("spec", {}).get("unschedulable") or owner:
                raise Abort("Node was already cordoned or has another maintenance owner")
            annotations[ANNOTATION] = self.operation
        else:
            if owner != self.operation:
                raise Abort("Refusing to uncordon a node not owned by this operation")
            if require_upgraded or require_recovered:
                ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node.get("status", {}).get("conditions", []))
                minimum_mib = 3840 if require_upgraded else 2816
                if not ready or memory_bytes(node["status"]["capacity"]["memory"]) < minimum_mib * 1024**2:
                    raise Abort("Node must be Ready with the explicitly selected memory gate before uncordon")
            del annotations[ANNOTATION]
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": node["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": node["metadata"]["resourceVersion"]},
            {"op": "add", "path": "/metadata/annotations", "value": annotations},
            {"op": "add", "path": "/spec/unschedulable", "value": enabled},
        ]
        self.api.call("PATCH", f"/api/v1/nodes/{NODE}", patch, "application/json-patch+json")

    def pods(self, require_all=False):
        namespaces = self.api.call("GET", "/api/v1/namespaces")["items"]
        if {n["metadata"]["name"] for n in namespaces} != set(self.inventory["namespaces"]):
            raise Abort("Namespace inventory changed")
        target = []
        selector = urllib.parse.urlencode({"fieldSelector": "spec.nodeName=" + NODE})
        for namespace in self.inventory["namespaces"]:
            result = self.api.call("GET", f"/api/v1/namespaces/{namespace}/pods?{selector}")
            if result.get("metadata", {}).get("continue"):
                raise Abort("Unexpected paginated pod inventory")
            target.extend(p for p in result["items"] if p["spec"].get("nodeName") == NODE and p.get("status", {}).get("phase") not in ("Succeeded", "Failed"))
        if require_all and {key(p) for p in target} != set(self.expected):
            raise Abort("Pod set changed since the approved inventory")
        for pod in target:
            if identity(pod) != self.expected.get(key(pod)):
                raise Abort("Pod UID, owner, volume or mount inventory changed")
            if pod["metadata"].get("annotations", {}).get("kubernetes.io/config.mirror"):
                raise Abort("Static mirror pods require a separate review")
            if not any(o.get("controller") for o in pod["metadata"].get("ownerReferences", [])):
                raise Abort("Unmanaged pods require a separate review; force is forbidden")
        return target

    def check_pdbs(self, pods):
        for namespace in sorted({p["metadata"]["namespace"] for p in pods if not is_daemon(p)}):
            budgets = self.api.call("GET", f"/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets")["items"]
            for budget in budgets:
                if any(p["metadata"]["namespace"] == namespace and not is_daemon(p) and selector_matches(p["metadata"].get("labels", {}), budget.get("spec", {}).get("selector", {})) for p in pods):
                    if budget.get("status", {}).get("disruptionsAllowed", 0) < 1:
                        raise Abort("A PodDisruptionBudget currently blocks this drain")

    def drain(self):
        deadline = self.clock() + self.timeout
        self.api.deadline = deadline
        cordon_attempted = False
        try:
            # Validate everything before making the node unschedulable.
            self.node()
            self.check_pdbs(self.pods(require_all=True))
            cordon_attempted = True
            self.set_cordon(True)
            issued = set()
            while self.clock() < deadline:
                remaining = [p for p in self.pods() if not is_daemon(p)]
                if not remaining:
                    log("drain_complete", evictions_accepted=len(issued), node_left_cordoned=True)
                    return
                for pod in remaining:
                    if self.clock() >= deadline:
                        raise Abort("Drain deadline exceeded")
                    name = key(pod)
                    if name in issued or pod["metadata"].get("deletionTimestamp"):
                        continue
                    namespace, pod_name = pod["metadata"]["namespace"], pod["metadata"]["name"]
                    eviction = {"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"namespace": namespace, "name": pod_name}, "deleteOptions": {"preconditions": {"uid": pod["metadata"]["uid"]}}}
                    try:
                        self.api.call("POST", f"/api/v1/namespaces/{namespace}/pods/{pod_name}/eviction", eviction)
                        issued.add(name)
                    except APIError as exc:
                        if exc.status == 404:
                            continue
                        if exc.status == 429:
                            # The server enforces current PDBs, including changes after preflight.
                            continue
                        raise
                log("drain_progress", remaining=len(remaining), evictions_accepted=len(issued))
                self.sleep(min(3, max(0, deadline - self.clock())))
            raise Abort("Drain deadline exceeded")
        except BaseException:
            # A PATCH can succeed even if its response is lost. Read ownership
            # before rollback instead of relying on a local success flag.
            self.api.deadline = None
            if cordon_attempted:
                try:
                    node = self.node()
                    if node["metadata"].get("annotations", {}).get(ANNOTATION) == self.operation:
                        for attempt in range(3):
                            try:
                                self.set_cordon(False)
                                log("drain_aborted_node_uncordoned")
                                break
                            except APIError as exc:
                                if exc.status != 409 or attempt == 2:
                                    raise
                except BaseException:
                    log("rollback_uncordon_failed", operator_action_required=True)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("drain", "uncordon", "recover-uncordon"), required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--timeout", type=int, default=840)
    parser.add_argument("--inventory-max-age", type=int, default=7200)
    parser.add_argument("--temporary-data-audit-approved", action="store_true")
    parser.add_argument("--recovery-approved", action="store_true")
    args = parser.parse_args()
    with open("/plan/inventory.json", encoding="utf-8") as stream:
        inventory = json.load(stream)
    if inventory["node"]["name"] != NODE or inventory["proxmox"] != {"node": "machine-2", "vmId": 114, "expectedName": NODE, "currentMemoryMiB": 3072, "desiredMemoryMiB": 4096}:
        raise Abort("Fixed maintenance target changed")
    if not 30 <= args.timeout <= 840:
        raise Abort("Drain timeout outside the safe range")
    if not 60 <= args.inventory_max_age <= 7200:
        raise Abort("Inventory maximum age outside the safe range")
    if args.phase == "drain":
        observed = datetime.datetime.fromisoformat(inventory["observedAt"].replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - observed).total_seconds()
        if age < -60 or age > args.inventory_max_age or not args.temporary_data_audit_approved:
            raise Abort("Fresh inventory and explicit temporary-data audit approval are required")
    if args.phase == "recover-uncordon" and not args.recovery_approved:
        raise Abort("Recovery uncordon requires separate explicit approval")
    def interrupted(signum, frame):
        raise Abort("Maintenance interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    maintenance = Maintenance(API(), inventory, args.operation_id, args.timeout)
    if args.phase == "drain":
        maintenance.drain()
    elif args.phase == "uncordon":
        maintenance.set_cordon(False, require_upgraded=True)
        log("explicit_uncordon_complete")
    else:
        maintenance.set_cordon(False, require_recovered=True)
        log("explicit_recovery_uncordon_complete")


if __name__ == "__main__":
    try:
        main()
    except Abort as exc:
        log("maintenance_failed", reason=str(exc))
        sys.exit(1)
    except BaseException:
        log("maintenance_failed", reason="Unexpected failure; details omitted")
        sys.exit(1)
