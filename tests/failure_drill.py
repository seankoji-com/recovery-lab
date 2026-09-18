"""Run after demo: real restic failures must not reach database startup."""
import json
from pathlib import Path
import tempfile
import sys

from recovery_lab.cli import run

source = Path(sys.argv[1] if len(sys.argv) > 1 else ".demo/manifest.json").resolve()
original = json.loads(source.read_text())
original["repository"] = str(source.parent / original["repository"])
original["password_file"] = str(source.parent / original["password_file"])
with tempfile.TemporaryDirectory(prefix="recovery-lab-negative-") as temp:
    root = Path(temp)
    key = root / "wrong-key"
    key.write_text("deliberately incorrect synthetic key")
    key.chmod(0o600)
    for name, override in (("wrong-key", {"password_file": str(key)}),
                           ("missing-snapshot", {"snapshot": "0" * 64})):
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps(original | override))
        receipt = Path("receipts") / f"{name}.json"
        assert run(manifest, receipt) == 1
        result = json.loads(receipt.read_text())
        assert result["failure"]["phase"] == "retrieve_and_decrypt"
        assert all(result["cleanup"].values())
        assert "isolate" not in [p["name"] for p in result["phases"]]
        print(f"{name}: correctly rejected and cleaned")
