import base64
import datetime
import json
import pathlib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

context = ssl.create_default_context(cafile="/certs/ca.crt")
password = pathlib.Path("/credentials/password").read_text().strip()
authorization = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()


def request(path, payload=None, method=None, dashboards=False):
    base = "http://opensearch-logs-dashboards:5601" if dashboards else "https://opensearch-logs:9200"
    headers = {"Authorization": authorization, "Content-Type": "application/json"}
    if dashboards:
        headers.update({"osd-xsrf": "true", "securitytenant": "global"})
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, context=context, timeout=20) as response:
        return json.load(response)


for attempt in range(120):
    try:
        health = request("/_cluster/health")
        if health["status"] != "red" and health["number_of_nodes"] == 3:
            break
    except (OSError, ValueError):
        pass
    time.sleep(5)
else:
    raise RuntimeError("OpenSearch did not become ready")

policy = {"policy": {
    "description": "Delete General1 daily log indices after seven days.",
    "default_state": "retain",
    "states": [
        {"name": "retain", "actions": [], "transitions": [
            {"state_name": "delete", "conditions": {"min_index_age": "7d"}}]},
        {"name": "delete", "actions": [{"delete": {}}], "transitions": []},
    ],
    "ism_template": [{"index_patterns": ["logs-*"], "priority": 100}],
}}
policy_path = "/_plugins/_ism/policies/logs-retention"
try:
    current = request(policy_path)
    policy_path += f'?if_seq_no={current["_seq_no"]}&if_primary_term={current["_primary_term"]}'
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
request(policy_path, policy, "PUT")
request("/_index_template/logs", {
    "index_patterns": ["logs-*"], "priority": 100,
    "template": {
        "settings": {"number_of_shards": 1, "number_of_replicas": 1},
        "mappings": {
            "dynamic_templates": [{"strings": {"match_mapping_type": "string",
                "mapping": {"type": "keyword", "ignore_above": 512}}}],
            "properties": {
                "time": {"type": "date"},
                "observedTimestamp": {"type": "date"},
                "body": {"type": "text"},
                "attributes": {"properties": {"message": {"type": "text"}}},
            },
        },
    },
}, "PUT")
index = "logs-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y.%m.%d")
try:
    request("/" + index, {}, "PUT")
except urllib.error.HTTPError as error:
    details = json.load(error)
    if details.get("error", {}).get("type") != "resource_already_exists_exception":
        raise

for attempt in range(120):
    try:
        query = urllib.parse.urlencode({
            "pattern": "logs-*",
            "meta_fields": ["_source", "_id", "_type", "_index", "_score"],
        }, doseq=True)
        fields = request("/api/index_patterns/_fields_for_wildcard?" + query,
                         dashboards=True)["fields"]
        if not any(field["name"] == "time" and field["type"] == "date" for field in fields):
            raise ValueError("Log time field is not mapped as a date")
        request("/api/saved_objects/index-pattern/general1-logs?overwrite=true", {
            "attributes": {"title": "logs-*", "timeFieldName": "time",
                           "fields": json.dumps(fields)},
        }, "POST", dashboards=True)
        request("/api/opensearch-dashboards/settings", {
            "changes": {"defaultIndex": "general1-logs"},
        }, "POST", dashboards=True)
        break
    except (OSError, ValueError):
        time.sleep(5)
else:
    raise RuntimeError("Dashboards data view setup failed")
print("Log retention, index template and Dashboards data view configured.")
