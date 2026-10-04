"""Reconcile one Harbor policy; never print API bodies or credentials."""
import base64
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request


API = "https://harbor.internal.api-api-api.com/api/v2.0"
POLICIES = "/projects/playground/webhook/policies"
NAME = "playground-image-updater"
ENDPOINT = "http://playground-image-updater.argocd.svc.cluster.local:8080/webhook?type=harbor"
FIELDS = ("name", "description", "enabled", "event_types", "targets")


def desired_policy(config, token):
    if set(config) != {"enabled", "endpoint"} or type(config["enabled"]) is not bool:
        raise ValueError("Invalid policy configuration")
    if config["endpoint"] != ENDPOINT:
        raise ValueError("Only the private playground receiver is allowed")
    if (len(token) < 32 or not token.isascii() or not token.isprintable()
            or any(c.isspace() for c in token)):
        raise ValueError("Webhook token must be at least 32 non-whitespace ASCII characters")
    return {
        "name": NAME,
        "description": "Managed by general-1-argocd/utility-apps/harbor/playground-webhook",
        "enabled": config["enabled"],
        "event_types": ["PUSH_ARTIFACT"],
        "targets": [{"type": "http", "address": ENDPOINT, "auth_header": token,
                     "skip_cert_verify": False, "payload_format": "Default"}],
    }


def find_policy(request):
    matches = []
    # Bound pagination and fail closed if Harbor returns an unexpected shape.
    for page in range(1, 101):
        policies = request("GET", f"{POLICIES}?page={page}&page_size=100")
        if not isinstance(policies, list) or any(not isinstance(p, dict) for p in policies):
            raise ValueError("Unexpected policy list response")
        matches.extend(p for p in policies if p.get("name") == NAME)
        if len(matches) > 1:
            raise ValueError("Duplicate managed policy names; refusing ambiguous update")
        if len(policies) < 100:
            break
    else:
        raise ValueError("Policy pagination limit reached")
    if not matches:
        return None
    policy = matches[0]
    if type(policy.get("id")) is not int or policy["id"] < 1:
        raise ValueError("Invalid managed policy identity")
    return policy


def managed_fields(policy):
    fields = {field: policy.get(field) for field in FIELDS}
    # Harbor omits this bool when false. Only an absent field defaults to false;
    # explicit true (or null) remains drift; a TLS bypass is never requested.
    if isinstance(fields["targets"], list):
        fields["targets"] = [dict(target, skip_cert_verify=target.get("skip_cert_verify", False))
                             if isinstance(target, dict) else target for target in fields["targets"]]
    return fields


def configure(request, config, token):
    desired = desired_policy(config, token)
    current = find_policy(request)
    if current and managed_fields(current) == desired:
        return "unchanged"
    if current:
        request("PUT", f"{POLICIES}/{current['id']}", desired)
        action = "updated"
    else:
        request("POST", POLICIES, desired)
        action = "created"
    after = find_policy(request)
    if after is None or managed_fields(after) != desired:
        raise ValueError("Harbor did not retain the desired policy")
    return action


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Harbor Basic authentication to a redirect destination.
        return None


def api_client(username, password):
    if not username or ":" in username or not password:
        raise ValueError("Missing or invalid Harbor API credentials")
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())

    def request(method, path, payload=None):
        if method not in {"GET", "POST", "PUT"} or not (
                path == POLICIES or path.startswith(POLICIES + "?")
                or (path.startswith(POLICIES + "/") and path[len(POLICIES) + 1:].isdigit())):
            raise ValueError("Out-of-scope Harbor operation")
        req = urllib.request.Request(
            API + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json",
                     "Accept": "application/json", "X-Is-Resource-Name": "true"})
        with opener.open(req, timeout=20) as response:
            body = response.read()
            return json.loads(body) if body else None

    return request


def main():
    try:
        config = json.loads(Path("/config/policy.json").read_text())
        token = Path("/webhook/secret").read_text()
        request = api_client(Path("/credentials/username").read_text(),
                             Path("/credentials/password").read_text())
        result = configure(request, config, token)
    except urllib.error.HTTPError as error:
        # Harbor error bodies can echo submitted authentication; never log them.
        raise SystemExit(f"Harbor policy reconciliation failed: HTTP {error.code}") from None
    except Exception as error:
        raise SystemExit(f"Harbor policy reconciliation failed: {type(error).__name__}") from None
    print(f"Playground notification policy {result}", flush=True)


if __name__ == "__main__":
    main()
