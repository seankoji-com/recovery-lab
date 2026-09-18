import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recovery_lab import cli


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "repo").mkdir()
        key = self.root / "key"
        key.write_text("synthetic-password")
        key.chmod(0o600)
        self.path = self.root / "manifest.json"
        self.value = dict(version=1, adapter="notes-pg16", repository="repo", password_file="key", snapshot="a" * 64)

    def load(self):
        self.path.write_text(json.dumps(self.value))
        return cli.manifest(self.path)

    def test_relative_paths_resolve_from_manifest(self):
        self.assertEqual(self.load()["repository"], self.root / "repo")

    def test_latest_and_prefixes_rejected(self):
        for snapshot in ("latest", "abcd", "-abc", 7):
            self.value["snapshot"] = snapshot
            with self.assertRaises(cli.Failure):
                self.load()

    def test_unknown_fields_rejected(self):
        self.value["shell"] = "anything"
        with self.assertRaises(cli.Failure):
            self.load()

    def test_incompatible_adapter_rejected(self):
        self.value["adapter"] = "immich"
        with self.assertRaises(cli.Failure):
            self.load()

    def test_world_readable_key_rejected(self):
        (self.root / "key").chmod(0o644)
        with self.assertRaises(cli.Failure):
            self.load()

    def test_future_or_naive_timestamp_rejected(self):
        for timestamp in ("2999-01-01T00:00:00+00:00", "2020-01-01T00:00:00"):
            with self.assertRaises(cli.Failure):
                cli.data_age(timestamp)

    def test_failure_still_writes_receipt_and_cleans(self):
        self.value["snapshot"] = "latest"
        self.path.write_text(json.dumps(self.value))
        receipt = self.root / "receipt.json"
        with patch.object(cli.Lab, "cleanup", return_value={"container": True, "volume": True, "network": True}) as cleanup:
            self.assertEqual(cli.run(self.path, receipt), 1)
        cleanup.assert_called_once()
        result = json.loads(receipt.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["plaintext_directory"])
        self.assertNotIn("synthetic-password", receipt.read_text())

    def test_decryption_failure_cannot_start_database(self):
        self.load()
        with patch.object(cli, "restic", side_effect=cli.Failure("secret row")), \
             patch.object(cli.Lab, "prepare") as prepare, \
             patch.object(cli.Lab, "cleanup", return_value={"container": True}):
            receipt = self.root / "receipt.json"
            self.assertEqual(cli.run(self.path, receipt), 1)
        prepare.assert_not_called()
        self.assertNotIn("secret row", receipt.read_text())

    def simulate_restore(self, cleanup, browser=None, checksum=None):
        self.load()
        metadata = {"adapter": "notes-pg16", "postgres_major": 16,
                    "capture_started_at": cli.now(),
                    "dump_sha256": checksum or hashlib.sha256(b"dump").hexdigest()}
        def restic(*args, output=None, **kwargs):
            if output is not None:
                output.write(b"dump")
                return b""
            return json.dumps(metadata).encode()
        receipt = self.root / "receipt.json"
        with patch.object(cli, "restic", side_effect=restic), \
             patch.object(cli.Lab, "prepare") as prepare, \
             patch.object(cli.Lab, "sql", return_value=b"1\n"), \
             patch.object(cli.Lab, "start_app", return_value="http://127.0.0.1:1234"), \
             patch.object(cli.Lab, "cleanup", return_value=cleanup), \
             patch.object(cli, "docker", return_value=b'[{"Id":"sha256:fixture"}]'), \
             patch.object(cli.subprocess, "run"), \
             patch.object(cli, "browser_check", side_effect=browser, return_value={"passed": True}):
            code = cli.run(self.path, receipt)
        return code, json.loads(receipt.read_text()), prepare.called

    def test_cleanup_failure_overrides_successful_browser(self):
        code, receipt, _ = self.simulate_restore({"volume": False})
        self.assertEqual(code, 1)
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["phases"][-1]["passed"])

    def test_browser_failure_still_cleans(self):
        code, receipt, _ = self.simulate_restore({"volume": True}, browser=RuntimeError("private row"))
        self.assertEqual(code, 1)
        self.assertEqual(receipt["failure"]["phase"], "browser_acceptance")
        self.assertTrue(receipt["cleanup"]["volume"])

    def test_checksum_failure_prevents_database_start(self):
        code, receipt, prepared = self.simulate_restore({"volume": True}, checksum="bad")
        self.assertEqual(code, 1)
        self.assertFalse(prepared)
        self.assertEqual(receipt["failure"]["phase"], "retrieve_and_decrypt")


class CleanupTests(unittest.TestCase):
    def test_stopped_containers_are_included_and_removal_verified(self):
        calls = []
        def fake(*args, **kwargs):
            calls.append(args)
            if args[:3] == ("container", "ls", "-aq") and len(calls) == 1:
                return b"stopped-container\n"
            return b""
        with patch.object(cli, "docker", side_effect=fake):
            self.assertTrue(all(cli.Lab().cleanup().values()))
        self.assertIn(("container", "rm", "-f", "stopped-container"), calls)

    def test_cleanup_continues_after_one_resource_fails(self):
        def fake(*args, **kwargs):
            if args[0] == "container":
                raise cli.Failure("unavailable")
            return b""
        with patch.object(cli, "docker", side_effect=fake):
            result = cli.Lab().cleanup()
        self.assertEqual(result, {"container": False, "volume": True, "network": True})


if __name__ == "__main__":
    unittest.main()
