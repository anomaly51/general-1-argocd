"""Publish a fixed verified MinIO manifest; registry credentials are mounted."""
import hashlib
import os
from pathlib import Path
import subprocess
import urllib.request

REGISTRY = "harbor.internal.api-api-api.com"
IMAGE = REGISTRY + "/applications/minio:RELEASE.2025-04-22T22-12-26Z"
DIGEST = "sha256:3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb"
CLIENT_SHA = "8e0e62a497fcdb8048d18aa927a139613176ba0531f412bc541044e28f9856bd"


def main():
    url = "https://github.com/regclient/regclient/releases/download/v0.11.6/regctl-linux-amd64"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(64 * 1024**2)
    assert hashlib.sha256(data).hexdigest() == CLIENT_SHA
    binary = Path("/work/regctl")
    binary.write_bytes(data)
    binary.chmod(0o700)
    os.environ["DOCKER_CONFIG"] = "/registry"
    os.environ["REGCTL_CONFIG"] = "/work/regctl.json"
    os.environ["TMPDIR"] = "/work"
    def run(*args):
        result = subprocess.run([str(binary), "--verbosity", "error", *args],
                                capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError("Registry operation failed; raw response omitted")
        return result.stdout.strip()
    run("registry", "set", REGISTRY, "--tls", "enabled", "--blob-chunk", "16777216",
        "--blob-max", "16777216", "--req-concurrent", "1", "--skip-check")
    run("image", "import", IMAGE, "/work/minio.tar")
    assert run("image", "digest", IMAGE) == DIGEST
    Path("/work/minio.tar").unlink()
    print("Verified MinIO mirror:", IMAGE + "@" + DIGEST)


if __name__ == "__main__":
    main()
