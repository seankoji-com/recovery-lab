import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
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


CLEAN = {"container": True, "volume": True, "network": True}


class DemoSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / "demo"
        self.receipt = self.root / "receipts" / "latest.json"

    def test_source_failure_writes_failed_receipt_and_cleans(self):
        with patch.object(cli.Lab, "prepare", side_effect=cli.Failure("secret row")), \
             patch.object(cli.Lab, "cleanup", return_value=dict(CLEAN)) as cleanup, \
             patch.object(cli, "run") as run:
            with self.assertRaises(cli.Failure):
                cli.demo(self.directory, self.receipt)
        cleanup.assert_called_once()
        run.assert_not_called()
        result = json.loads(self.receipt.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"], {"phase": "demo_source", "type": "Failure"})
        self.assertEqual(result["cleanup"], CLEAN)
        self.assertNotIn("secret row", self.receipt.read_text())
        self.assertEqual(json.loads((self.directory / "source-cleanup.json").read_text()), CLEAN)
        self.assertFalse((self.directory / "manifest.json").exists())

    def test_source_cleanup_failure_blocks_restore(self):
        def restic(config, scratch, *args, **kwargs):
            return json.dumps([{"id": "f" * 64}]).encode() if "snapshots" in args else b""
        leaked = {"container": True, "volume": False, "network": True}
        with patch.object(cli.Lab, "prepare"), \
             patch.object(cli.Lab, "sql", return_value=b"0\n"), \
             patch.object(cli.Lab, "start_app"), \
             patch.object(cli.Lab, "cleanup", return_value=leaked), \
             patch.object(cli, "browser_check"), \
             patch.object(cli, "docker", return_value=b""), \
             patch.object(cli, "restic", side_effect=restic), \
             patch.object(cli, "run") as run:
            with self.assertRaises(cli.Failure):
                cli.demo(self.directory, self.receipt)
        run.assert_not_called()
        result = json.loads(self.receipt.read_text())
        self.assertEqual(result["failure"], {"phase": "demo_source", "type": "CleanupFailure"})
        self.assertFalse(result["cleanup"]["volume"])


class SignalTests(unittest.TestCase):
    def setUp(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        umask = os.umask(0o077)
        self.addCleanup(os.umask, umask)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / "repo").mkdir()
        (root / "key").write_text("synthetic-password")
        (root / "key").chmod(0o600)
        self.manifest = root / "manifest.json"
        self.manifest.write_text(json.dumps(dict(version=1, adapter="notes-pg16", repository="repo",
                                                 password_file="key", snapshot="a" * 64)))
        self.receipt = root / "receipt.json"

    def test_sigterm_during_run_cleans_up_and_fails(self):
        def restic(*args, **kwargs):
            signal.raise_signal(signal.SIGTERM)
            return b"{}"
        argv = ["recovery-lab", "run", str(self.manifest), "--receipt", str(self.receipt)]
        with patch("sys.argv", argv), \
             patch.object(cli, "restic", side_effect=restic), \
             patch.object(cli.Lab, "prepare") as prepare, \
             patch.object(cli.Lab, "cleanup", return_value=dict(CLEAN)) as cleanup:
            self.assertEqual(cli.main(), 1)
        cleanup.assert_called_once()
        prepare.assert_not_called()
        result = json.loads(self.receipt.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"], {"type": "KeyboardInterrupt", "phase": "retrieve_and_decrypt"})
        self.assertTrue(result["cleanup"]["plaintext_directory"])
        self.assertEqual(result["phases"][-1]["name"], "cleanup")


SQL = (f"CREATE TABLE notes(id integer PRIMARY KEY, title text NOT NULL, body text NOT NULL);\n"
       f"INSERT INTO notes VALUES(1, '{cli.TITLE}', '{cli.BODY}');\n").encode()


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age is not installed")
class AgeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.identity = self.keygen("identity.txt")
        self.dump = self.root / "notes-2026-09-01.sql.gz.age"
        self.encrypt(gzip.compress(SQL), self.identity, self.dump)
        self.path = self.root / "manifest.json"
        self.value = dict(version=1, adapter="notes-pg16-sql-age", dump=self.dump.name,
                          identity_file="identity.txt", capture_started_at="2026-09-01T02:00:00+00:00")

    def keygen(self, name):
        # A throwaway key that exists only for this test.
        path = self.root / name
        subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
        path.chmod(0o600)
        return path

    def encrypt(self, data, identity, target):
        recipient = subprocess.run(["age-keygen", "-y", str(identity)], check=True,
                                   capture_output=True, text=True).stdout.strip()
        subprocess.run(["age", "-r", recipient, "-o", str(target)], input=data, check=True, capture_output=True)

    def load(self):
        self.path.write_text(json.dumps(self.value))
        return cli.manifest(self.path)

    def test_manifest_validates_age_fields(self):
        self.assertEqual(self.load()["dump"], self.dump)
        for field, bad in (("capture_started_at", "2026-09-01T02:00:00"), ("capture_started_at", 7),
                           ("dump", "identity.txt"), ("identity_file", "missing.txt")):
            with self.subTest(field=field, bad=bad):
                value = dict(self.value)
                value[field] = bad
                self.path.write_text(json.dumps(value))
                with self.assertRaises(cli.Failure):
                    cli.manifest(self.path)

    def test_restic_fields_rejected_for_age_adapter(self):
        self.value["snapshot"] = "a" * 64
        with self.assertRaises(cli.Failure):
            self.load()

    def test_world_readable_identity_rejected(self):
        self.identity.chmod(0o644)
        with self.assertRaises(cli.Failure):
            self.load()

    def simulate(self):
        self.load()
        restored = []
        def restore(lab, dump, adapter):
            restored.append((dump.read_bytes(), adapter))
        receipt = self.root / "receipt.json"
        with patch.object(cli, "restore", side_effect=restore), \
             patch.object(cli, "restic") as restic, \
             patch.object(cli.Lab, "prepare") as prepare, \
             patch.object(cli.Lab, "sql", return_value=b"1\n"), \
             patch.object(cli.Lab, "start_app"), \
             patch.object(cli.Lab, "cleanup", return_value=dict(CLEAN)), \
             patch.object(cli, "docker", return_value=b'[{"Id":"sha256:fixture"}]'), \
             patch.object(cli, "browser_check", return_value={"passed": True}):
            code = cli.run(self.path, receipt)
        restic.assert_not_called()
        return code, json.loads(receipt.read_text()), receipt.read_text(), prepare.called, restored

    def test_decrypts_and_restores_plain_sql(self):
        code, receipt, text, _, restored = self.simulate()
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["adapter"], "notes-pg16-sql-age")
        self.assertEqual(restored, [(SQL, "notes-pg16-sql-age")])
        self.assertEqual(receipt["dump_sha256"], hashlib.sha256(SQL).hexdigest())
        self.assertEqual(receipt["backup_sha256"], hashlib.sha256(self.dump.read_bytes()).hexdigest())
        self.assertTrue(receipt["cleanup"]["plaintext_directory"])
        self.assertNotIn(cli.RESTIC, receipt["images"])
        self.assertNotIn("snapshot", receipt)
        for private in (cli.BODY, self.identity.read_text().split()[-1], str(self.root)):
            self.assertNotIn(private, text)

    def test_wrong_identity_cannot_start_database(self):
        self.encrypt(gzip.compress(SQL), self.keygen("other.txt"), self.dump)
        code, receipt, _, prepared, restored = self.simulate()
        self.assertEqual(code, 1)
        self.assertFalse(prepared)
        self.assertEqual(restored, [])
        self.assertEqual(receipt["failure"]["phase"], "retrieve_and_decrypt")

    def test_non_gzip_payload_cannot_start_database(self):
        self.encrypt(SQL, self.identity, self.dump)
        code, receipt, _, prepared, _ = self.simulate()
        self.assertEqual(code, 1)
        self.assertFalse(prepared)
        self.assertEqual(receipt["failure"]["phase"], "retrieve_and_decrypt")


class RestoreCommandTests(unittest.TestCase):
    def test_adapter_selects_restore_tool(self):
        with tempfile.NamedTemporaryFile() as dump, patch.object(cli.subprocess, "run") as run:
            lab = cli.Lab()
            cli.restore(lab, Path(dump.name), "notes-pg16")
            cli.restore(lab, Path(dump.name), "notes-pg16-sql-age")
        custom, plain = (call.args[0] for call in run.call_args_list)
        self.assertEqual(custom[4], "pg_restore")
        self.assertIn("--no-owner", custom)
        self.assertEqual(plain[4], "psql")
        self.assertIn("ON_ERROR_STOP=1", plain)
        self.assertIn("--single-transaction", plain)


if __name__ == "__main__":
    unittest.main()
