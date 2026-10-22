#!/usr/bin/env python3
"""Install pinned official REA locally without changing agent configuration."""
from pathlib import Path
import base64
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
NPM_VERSION = "12.2.1"
REA_VERSION = "6.7.0"


def main():
    node = shutil.which("node")
    if not node:
        raise SystemExit("Install a supported Node.js runtime first; see README.md")
    npm = HERE / "npm/package/bin/npm-cli.js"
    if not npm.exists():
        metadata = json.load(urlopen(f"https://registry.npmjs.org/npm/{NPM_VERSION}", timeout=30))
        payload = urlopen(metadata["dist"]["tarball"], timeout=60).read()
        algorithm, expected = metadata["dist"]["integrity"].split("-", 1)
        actual = base64.b64encode(hashlib.new(algorithm, payload).digest()).decode()
        if actual != expected:
            raise SystemExit("Official npm tarball integrity check failed")
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            archive.extractall(HERE / "npm", filter="data")
    command = [node, str(npm), "ci" if (HERE / "package-lock.json").exists() else "install",
               "--prefix", str(HERE), "--ignore-scripts", "--omit=optional", "--no-audit", "--no-fund"]
    if not (HERE / "package-lock.json").exists():
        command.extend(["--save-exact", f"rea-agents@{REA_VERSION}"])
    subprocess.run(command, check=True)
    subprocess.run([node, str(HERE / "node_modules/rea-agents/scripts/rea.mjs"), "--version"], check=True)


if __name__ == "__main__":
    main()
