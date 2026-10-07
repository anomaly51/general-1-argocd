"""Offline contract checks; live parser samples are tested with _simulate before rollout."""

import importlib.util
import json
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[2] / "utility-apps/monitoring/opensearch/downloader.py"
spec = importlib.util.spec_from_file_location("downloader_dashboard", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DownloaderDashboardTests(unittest.TestCase):
    def test_stable_ids_and_resolved_references(self):
        objects = module.saved_objects()
        self.assertEqual(objects, module.saved_objects())
        ids = {(item["type"], item["id"]) for item in objects}
        self.assertEqual(len(ids), len(objects))
        for item in objects:
            for ref in item["references"]:
                self.assertTrue((ref["type"], ref["id"]) in ids or
                                (ref["type"], ref["id"]) == ("index-pattern", "general1-logs"))

    def test_panels_are_bot_scoped_and_do_not_show_personal_data(self):
        for item in module.saved_objects():
            if item["type"] == "dashboard":
                continue
            source = json.loads(item["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
            self.assertIn('k8s.namespace.name:"apps"', source["query"]["query"])
            self.assertIn('k8s.deployment.name:"downloader-sloniara-bot"', source["query"]["query"])
            if item["type"] == "search":
                self.assertNotIn("body", item["attributes"]["columns"])
                self.assertTrue(all(col.startswith("downloader.") for col in item["attributes"]["columns"]))

    def test_layout_and_aggregation_fields(self):
        objects = module.saved_objects()
        dashboard = next(item for item in objects if item["type"] == "dashboard")
        rectangles = [p["gridData"] for p in json.loads(dashboard["attributes"]["panelsJSON"])]
        for i, rect in enumerate(rectangles):
            self.assertLessEqual(rect["x"] + rect["w"], 48)
            for other in rectangles[i + 1:]:
                overlap = (rect["x"] < other["x"] + other["w"] and other["x"] < rect["x"] + rect["w"] and
                           rect["y"] < other["y"] + other["h"] and other["y"] < rect["y"] + rect["h"])
                self.assertFalse(overlap)
        fields = module.MAPPING["properties"]["downloader"]["properties"]
        for item in objects:
            if item["type"] == "visualization":
                state = json.loads(item["attributes"]["visState"])
                for agg in state["aggs"]:
                    if state["type"] == "table" and agg["type"] == "terms":
                        self.assertEqual(agg["schema"], "bucket")
                    field = agg["params"].get("field", "")
                    if field.startswith("downloader."):
                        self.assertIn(field.removeprefix("downloader."), fields)

    def test_error_and_success_events_remain_distinct(self):
        self.assertEqual(module.PROXY_EVENTS["response"][2], "progress")
        self.assertEqual(module.PROXY_EVENTS["response_timeout"][2], "error")
        self.assertEqual(module.PROXY_EVENTS["telegram_media_ok"][2], "success")
        self.assertEqual(module.PROXY_EVENTS["start_response_timeout"][2], "warning")
        self.assertTrue(module.PIPELINE["on_failure"])


if __name__ == "__main__":
    unittest.main()
