"""A deliberately narrow restic -> PostgreSQL -> browser recovery runner."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time
import uuid

PG = "postgres:16.10-alpine"
RESTIC = "restic/restic:0.18.1"
APP = "recovery-lab-notes:0.1.0"
BROWSER = "recovery-lab-browser:0.1.0"
LABEL = "io.recovery-lab.run"
TITLE = "The lighthouse log"
BODY = "The spare key is with the harbour keeper. Fixture record RL-001."


class Failure(Exception):
    pass


def command(*args, input=None, output=None, timeout=180):
    # Never export subprocess stderr: database errors can contain restored data.
    try:
        return subprocess.run(args, input=input, stdout=output or subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise Failure(f"{args[0]} command failed ({type(exc).__name__})") from None


def docker(*args, **kwargs):
    return command("docker", *args, **kwargs)


def now():
    return datetime.now(timezone.utc).isoformat()


def data_age(timestamp, at=None):
    captured = datetime.fromisoformat(timestamp)
    if captured.tzinfo is None:
        raise Failure("capture timestamp must include timezone")
    age = ((at or datetime.now(timezone.utc)) - captured).total_seconds()
    if age < 0:
        raise Failure("capture timestamp is in the future")
    return age


def manifest(path):
    value = json.loads(path.read_text())
    required = {"version", "adapter", "repository", "password_file", "snapshot"}
    if set(value) != required or value["version"] != 1 or value["adapter"] != "notes-pg16":
        raise Failure("expected version 1 notes-pg16 manifest with exactly the documented fields")
    if not isinstance(value["snapshot"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["snapshot"]):
        raise Failure("snapshot must be an exact 64-character restic snapshot ID")
    for key in ("repository", "password_file"):
        value[key] = (path.parent / value[key]).resolve()
        if not value[key].exists() or "," in str(value[key]):
            raise Failure(f"invalid {key} path")
    if not value["repository"].is_dir() or not value["password_file"].is_file():
        raise Failure("repository must be a directory and password_file a file")
    if value["password_file"].stat().st_mode & 0o077:
        raise Failure("password_file must be accessible only to its owner (chmod 600)")
    return value


class Lab:
    def __init__(self):
        self.id = "rl-" + uuid.uuid4().hex
        self.network = self.id + "-net"
        self.db = self.id + "-db"
        self.app = self.id + "-app"
        self.volume = self.id + "-data"
        self.label = f"{LABEL}={self.id}"

    def prepare(self):
        docker("network", "create", "--internal", "--label", self.label, self.network)
        docker("volume", "create", "--label", self.label, self.volume)
        docker("run", "-d", "--name", self.db, "--label", self.label,
               "--network", self.network, "--network-alias", "db", "--memory", "512m",
               "--pids-limit", "256", "--security-opt", "no-new-privileges",
               "--mount", f"type=volume,src={self.volume},dst=/var/lib/postgresql/data",
               "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=notes", PG)
        for _ in range(60):
            try:
                # The image's temporary initialization server accepts Unix sockets.
                # Wait for TCP so seed/restore cannot race database initialization.
                docker("exec", self.db, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "notes", timeout=5)
                return
            except Failure:
                time.sleep(1)
        raise Failure("database readiness deadline exceeded")

    def sql(self, sql):
        return docker("exec", "-i", self.db, "psql", "-U", "postgres", "-d", "notes",
                      "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", input=sql.encode())

    def start_app(self):
        docker("run", "-d", "--name", self.app, "--label", self.label,
               "--network", self.network, "--network-alias", "app", "--memory", "128m", "--pids-limit", "64",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "-e", "DATABASE_URL=postgresql://postgres@db/notes", APP)

    def cleanup(self):
        results = {}
        # Query labels, including resources whose create command timed out.
        for kind in ("container", "volume", "network"):
            try:
                flags = "-aq" if kind == "container" else "-q"
                ids = docker(kind, "ls", flags, "--filter", f"label={self.label}").decode().split()
                for resource in ids:
                    args = ("rm", "-f", resource) if kind == "container" else ("rm", resource)
                    docker(kind, *args)
                remaining = docker(kind, "ls", flags, "--filter", f"label={self.label}").strip()
                results[kind] = not remaining
            except Failure:
                results[kind] = False
        return results


def restic(config, scratch, *args, output=None, readonly=True):
    # The v1 backend is a local encrypted restic repository, mounted read-only on restore.
    return docker("run", "--rm", "--label", f"{LABEL}={config['run_id']}", "--network", "none", "--memory", "512m",
                  "--mount", f"type=bind,src={config['repository']},dst=/repo" + (",readonly" if readonly else ""),
                  "--mount", f"type=bind,src={config['password_file']},dst=/key,readonly",
                  "--mount", f"type=bind,src={scratch},dst=/work,readonly",
                  RESTIC, "--repo", "/repo", "--password-file", "/key", "--no-lock",
                  *args, output=output, timeout=300)


def browser_check(lab, broken=False):
    result = docker("run", "--rm", "--label", lab.label, "--network", lab.network,
                    "--memory", "512m", "--pids-limit", "256", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--read-only",
                    "--tmpfs", "/tmp:rw,nosuid,size=256m", BROWSER,
                    "broken" if broken else "restored")
    return json.loads(result)


@contextmanager
def phase(receipt, name):
    start = time.monotonic()
    item = {"name": name, "passed": False}
    receipt["phases"].append(item)
    print(name, flush=True)
    try:
        yield
        item["passed"] = True
    finally:
        item["seconds"] = round(time.monotonic() - start, 3)


def run(path, receipt_path):
    lab = Lab()
    started = time.monotonic()
    receipt = {"version": 1, "run_id": lab.id, "started_at": now(), "status": "failed", "phases": []}
    scratch = tempfile.TemporaryDirectory(prefix="recovery-lab-")
    try:
        with phase(receipt, "validate"):
            config = manifest(path)
            config["run_id"] = lab.id
            receipt["snapshot"] = config["snapshot"]
        with phase(receipt, "retrieve_and_decrypt"):
            metadata = json.loads(restic(config, scratch.name, "dump", config["snapshot"], "/work/metadata.json"))
            if metadata["adapter"] != "notes-pg16" or metadata["postgres_major"] != 16:
                raise Failure("backup adapter or PostgreSQL version mismatch")
            receipt["capture_started_at"] = metadata["capture_started_at"]
            receipt["recoverable_data_age_seconds"] = round(data_age(metadata["capture_started_at"]), 3)
            dump = Path(scratch.name) / "database.dump"
            with dump.open("wb") as stream:
                restic(config, scratch.name, "dump", config["snapshot"], "/work/database.dump", output=stream)
            with dump.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != metadata["dump_sha256"]:
                raise Failure("dump checksum mismatch")
            receipt["dump_sha256"] = digest
        with phase(receipt, "isolate"):
            lab.prepare()
            receipt["images"] = {name: json.loads(docker("image", "inspect", name))[0]["Id"] for name in (PG, RESTIC, APP, BROWSER)}
        with phase(receipt, "restore"):
            with dump.open("rb") as stream:
                # stdin streams the dump; no database credentials or row data reach the receipt.
                subprocess.run(["docker", "exec", "-i", lab.db, "pg_restore", "-U", "postgres",
                                "-d", "notes", "--exit-on-error", "--single-transaction", "--no-owner", "--no-acl"],
                               stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=True, timeout=300)
        with phase(receipt, "database_acceptance"):
            if lab.sql("SELECT count(*) FROM notes;").strip() != b"1":
                raise Failure("expected exactly one synthetic note")
        with phase(receipt, "browser_acceptance"):
            lab.start_app()
            receipt["browser"] = browser_check(lab)
        receipt["recovery_seconds"] = round(time.monotonic() - started, 3)
        receipt["status"] = "passed"
    except (Exception, KeyboardInterrupt) as exc:
        receipt["failure"] = {"type": type(exc).__name__, "phase": receipt["phases"][-1]["name"] if receipt["phases"] else "initialization"}
    finally:
        with phase(receipt, "cleanup"):
            receipt["cleanup"] = lab.cleanup()
            try:
                scratch.cleanup()
                receipt["cleanup"]["plaintext_directory"] = not Path(scratch.name).exists()
            except OSError:
                receipt["cleanup"]["plaintext_directory"] = False
            if not all(receipt["cleanup"].values()):
                receipt["status"] = "failed"
        receipt["phases"][-1]["passed"] = all(receipt["cleanup"].values())
        receipt["finished_at"] = now()
        receipt["total_seconds"] = round(time.monotonic() - started, 3)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"{receipt['status']}: {receipt_path}")
    return 0 if receipt["status"] == "passed" else 1


def build():
    docker("pull", PG, timeout=600)
    docker("pull", RESTIC, timeout=600)
    context = Path(__file__).parent / "app"
    docker("build", "-t", APP, str(context), timeout=600)
    docker("build", "-t", BROWSER, "-f", str(context / "browser.Dockerfile"), str(context), timeout=900)


def demo(directory, receipt_path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    (directory / "repo").mkdir()
    key = directory / "password"
    key.write_text(secrets.token_urlsafe(32) + "\n")
    key.chmod(0o600)
    source = Lab()
    config = {"repository": directory / "repo", "password_file": key, "run_id": source.id}
    source_started = time.monotonic()
    source_failure = None
    try:
        source.prepare()
        source.sql(f"CREATE TABLE notes(id integer PRIMARY KEY, title text NOT NULL, body text NOT NULL); INSERT INTO notes VALUES(1, '{TITLE}', '{BODY}');")
        source.start_app()
        browser_check(source)
        with tempfile.TemporaryDirectory(prefix="recovery-lab-backup-") as temp:
            captured = now()
            dump = Path(temp) / "database.dump"
            with dump.open("wb") as stream:
                docker("exec", source.db, "pg_dump", "-U", "postgres", "-d", "notes", "-Fc", output=stream)
            with dump.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            (Path(temp) / "metadata.json").write_text(json.dumps({"adapter": "notes-pg16", "postgres_major": 16, "capture_started_at": captured, "dump_sha256": digest}))
            restic(config, temp, "init", readonly=False)
            restic(config, temp, "backup", "/work/database.dump", "/work/metadata.json", readonly=False)
            snapshots = json.loads(restic(config, temp, "snapshots", "--json"))
            snapshot = snapshots[0]["id"]
        print("fault injection: dropping synthetic source table", flush=True)
        source.sql("DROP TABLE notes;")
        if source.sql("SELECT count(*) FROM pg_tables WHERE tablename = 'notes';").strip() != b"0":
            raise Failure("fault injection did not destroy the source data")
        browser_check(source, broken=True)
    except (Exception, KeyboardInterrupt) as exc:
        source_failure = type(exc).__name__
        raise
    finally:
        cleanup = source.cleanup()
        (directory / "source-cleanup.json").write_text(json.dumps(cleanup, indent=2) + "\n")
        if source_failure or not all(cleanup.values()):
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps({"version": 1, "run_id": source.id,
                "status": "failed", "failure": {"phase": "demo_source", "type": source_failure or "CleanupFailure"},
                "cleanup": cleanup, "total_seconds": round(time.monotonic() - source_started, 3)}, indent=2) + "\n")
    if not all(cleanup.values()):
        raise Failure("source cleanup failed")
    path = directory / "manifest.json"
    path.write_text(json.dumps({"version": 1, "adapter": "notes-pg16", "repository": "repo", "password_file": "password", "snapshot": snapshot}, indent=2) + "\n")
    result = run(path, receipt_path)
    receipt = json.loads(receipt_path.read_text())
    receipt["demo"] = {"source_browser_passed": True, "fault": "drop source notes table",
                       "broken_source_http_status": 503, "source_cleanup": cleanup}
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return result


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("build", help="pull dependencies and build the synthetic application")
    d = sub.add_parser("demo", help="back up, destroy, restore and verify synthetic data")
    d.add_argument("--directory", type=Path, default=Path(".demo"))
    r = sub.add_parser("run", help="restore an exact snapshot from a manifest")
    r.add_argument("manifest", type=Path)
    for p in (d, r):
        p.add_argument("--receipt", type=Path, default=Path("receipts/latest.json"))
    args = parser.parse_args()
    try:
        if args.action == "build":
            build()
            return 0
        if args.action == "demo":
            return demo(args.directory.resolve(), args.receipt.resolve())
        return run(args.manifest.resolve(), args.receipt.resolve())
    except (Exception, KeyboardInterrupt) as exc:
        print(f"failed: {type(exc).__name__}; no successful recovery claimed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
