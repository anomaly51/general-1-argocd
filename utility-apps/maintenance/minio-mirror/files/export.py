"""Read only verified image blobs, never container files or application data."""
import hashlib
import io
import json
import re
import socket
import subprocess
import sys
import tarfile

INDEX = "sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e"
MANIFEST = "sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb"


def blob(digest):
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    data = subprocess.check_output([
        "/usr/local/bin/k3s", "ctr", "-n", "k8s.io", "content", "get", digest
    ], timeout=120)
    assert hashlib.sha256(data).hexdigest() == digest.split(":")[1]
    return data


def main():
    assert socket.gethostname() == "general-1-worker-1"
    index = json.loads(blob(INDEX))
    descriptor = next(x for x in index["manifests"]
                      if x.get("platform") == {"architecture": "amd64", "os": "linux"})
    assert descriptor["digest"] == MANIFEST
    manifest = json.loads(blob(MANIFEST))
    descriptors = [descriptor, manifest["config"], *manifest["layers"]]
    assert sum(x["size"] for x in descriptors) < 200 * 1024**2
    if "--check" in sys.argv:
        for item in descriptors:
            assert len(blob(item["digest"])) == item["size"]
        print("Verified cached MinIO manifest and every image blob; no files written")
        return
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        def add(name, data):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            item.mode = 0o644
            archive.addfile(item, io.BytesIO(data))
        add("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add("index.json", json.dumps({"schemaVersion": 2, "manifests": [descriptor]}).encode())
        for item in descriptors:
            data = blob(item["digest"])
            assert len(data) == item["size"]
            add("blobs/sha256/" + item["digest"].split(":")[1], data)


if __name__ == "__main__":
    main()
