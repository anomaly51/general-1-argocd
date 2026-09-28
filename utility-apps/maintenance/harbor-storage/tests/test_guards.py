import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("expand", Path(__file__).parents[1] / "files/expand.py")
expand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(expand)


class DiskGuards(unittest.TestCase):
    def test_wrong_node_stops_before_any_command(self):
        with patch.object(expand.socket, "gethostname", return_value="production-node"), patch.object(expand, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Wrong host"):
                expand.main()
            run.assert_not_called()

    def test_rejects_mounted_partitioned_or_formatted_disk(self):
        for replies, message in [(["/data"], "mounted"), (["", "sdb\nsdb1"], "child devices"),
                                  (["", "sdb", "ext4"], "signatures")]:
            with self.subTest(message=message), patch.object(expand, "run", side_effect=replies):
                with self.assertRaisesRegex(RuntimeError, message):
                    expand.require_blank_disk("/dev/never-open")

    def test_blank_disk_and_data_at_either_end(self):
        with tempfile.NamedTemporaryFile() as disk, patch.object(expand, "DISK_BYTES", 4 * 1024**2):
            disk.truncate(expand.DISK_BYTES)
            with patch.object(expand, "run", side_effect=["", "disk", ""]):
                expand.require_blank_disk(disk.name)
            for position, label in [(0, "header"), (expand.DISK_BYTES - 1, "trailer")]:
                disk.seek(position)
                disk.write(b"x")
                disk.flush()
                with patch.object(expand, "run", side_effect=["", "disk", ""]):
                    with self.assertRaisesRegex(RuntimeError, label):
                        expand.require_blank_disk(disk.name)
                disk.seek(position)
                disk.write(b"\0")
                disk.flush()


if __name__ == "__main__":
    unittest.main()
