# Recovery lab

Prove that an encrypted backup restores to a usable application. Recovery lab retrieves an exact restic snapshot, restores PostgreSQL in disposable Docker containers, opens the recovered application in Chromium, and writes a JSON receipt with timings and verified cleanup.

Version 0.1 supports **Recovery Notes**, a small read-only application with one synthetic PostgreSQL 16 record. Its acceptance check opens the note from the list and matches its full body. This is a runnable reference adapter, not a Wiki.js, Immich or Authentik recovery claim.

See a [receipt from a completed local demo](examples/demo-receipt.json). CI repeats the destructive demo, replays its snapshot, and checks wrong-key and missing-snapshot failures.

## Run the destructive demo

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running local Linux-container Docker engine. Tested on OrbStack; Linux CI uses Docker Engine. Image downloads and browser installation need internet access.

```sh
uv sync --locked
uv run recovery-lab build
uv run recovery-lab demo
```

The build installs Chromium and its libraries inside a dedicated checker image; no host browser installation is needed.

The demo checks the source application, captures a custom-format `pg_dump`, encrypts it with restic, drops the source table, and proves the broken application returns HTTP 503 in a browser. It then removes the source containers and volume before restoring into a new environment. The recovered browser journey must pass. Only uniquely labelled demo resources are destroyed.

Outputs:

| File | Purpose |
| --- | --- |
| `.demo/repo/` | Encrypted local restic repository |
| `.demo/password` | Generated demo key, mode 600; never commit it |
| `.demo/manifest.json` | Exact snapshot and local restore inputs |
| `.demo/source-cleanup.json` | Source-environment cleanup results |
| `receipts/latest.json` | Recovery result, phase timings, image IDs, checksum and cleanup |

The demo refuses to overwrite an existing directory. Use `--directory .demo/another-run` for another independent run. Replay an existing backup with:

```sh
uv run recovery-lab run .demo/manifest.json --receipt receipts/replay.json
uv run python -m unittest discover -s tests -v
```

## Manifest and backup contract

```json
{
  "version": 1,
  "adapter": "notes-pg16",
  "repository": "repo",
  "password_file": "password",
  "snapshot": "<exact 64-character snapshot ID>"
}
```

Paths resolve relative to the manifest. The repository must be a local directory and the password file must be owner-only. Snapshot prefixes and `latest` are rejected. Version 1 accepts exactly these fields; it does not execute manifest commands or accept arbitrary mounts/images.

The restic snapshot contains `/work/database.dump` (PostgreSQL 16 custom-format dump) and `/work/metadata.json` with `adapter`, `postgres_major`, `capture_started_at` (timezone-aware ISO 8601), and `dump_sha256`. `demo` produces the complete contract. The adapter expects the schema and synthetic record defined in the demo. Existing age-encrypted SQL files require a future adapter.

## What the receipt proves

- `status: passed` requires decryption, checksum validation, restore, row-count acceptance, the real Chromium journey, and cleanup to succeed. Any failure exits nonzero.
- `recovery_seconds` measures validation through browser acceptance, excluding image builds/downloads and cleanup. `total_seconds` includes cleanup. These are drill measurements, not a production RTO guarantee.
- `recoverable_data_age_seconds` measures backup capture start to retrieval. It is conservative for this single transaction dump; it is not replication lag or a promised RPO.
- Each run records actual local image IDs. Release tags are pinned by version but can move; record digests before using a new adapter for operational evidence.
- Error receipts export failure categories, not SQL stderr, keys, repository paths or restored row contents. Receipts are local evidence, not signed attestations.

## Isolation and failure handling

No container publishes a host port. The database, application and Chromium checker share a new internal Docker network with no outbound routing. Fresh labelled volumes, memory/PID limits and an unprivileged read-only application keep the drill separate from other services. PostgreSQL uses trust authentication only within this disposable network. Anyone with host/Docker access remains trusted.

Restore helpers have no network, and mount the encrypted repository and key read-only. Decrypted dumps live briefly in a mode-700 temporary directory and are deleted after the attempt. Deletion is not secure erasure; use an encrypted host disk. Only trusted backups should be restored: PostgreSQL dumps can contain executable database code. Docker is not a hostile-input sandbox.

Normal failures, Ctrl-C and SIGTERM trigger cleanup. Cleanup checks containers (including stopped ones), volumes and networks by the exact run label and records any leftovers. SIGKILL, host failure or a dead Docker daemon can prevent cleanup. Inspect `docker ps -a --filter label=io.recovery-lab.run=<run_id>` and the equivalent `docker volume ls` / `docker network ls` filters; remove only those exact resources. Never use a global prune.

The demo deliberately keeps its encrypted repository and key for replay. In real recovery, keep an independent key copy outside the failed site and rehearse retrieval separately. This local demo does not prove off-site key recovery.

## Next adapters

Add operational applications only with their own acceptance contracts: Wiki.js browser login/page access; Immich database extensions plus photo bytes and thumbnail/original retrieval; Authentik keys, version compatibility and a login journey. Remote restic/S3 copies need explicit endpoint, credentials, transport and retention handling plus a restore from the remote copy. None is implemented in 0.1.

MIT licensed. Contributions should include a synthetic fixture, a broken-source check, a restored browser journey and failure-path cleanup tests.
