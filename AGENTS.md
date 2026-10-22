# Working on recovery-lab

Start with [README.md](README.md) for the manifest, isolation, receipt and cleanup
contracts. Use synthetic fixtures; production backups, credentials and remote
repositories need an explicitly scoped task. Keep keys, restored records, SQL
errors and repository paths out of exported receipts and logs.

Preserve exact snapshot selection, owner-only password files, manifest field
validation and the refusal to overwrite an existing demo directory. Restore only
trusted backups: PostgreSQL dumps can execute database code. Keep restore inputs
read-only, containers isolated on an internal network without published ports,
and decrypted temporary files private with verified cleanup on failure as well
as success. Clean resources by their exact run label; never use a global prune.
Do not broaden mounts, images or manifest command execution.

Run `uv run python -m unittest discover -s tests -v` for relevant code changes.
A passing recovery claim requires the application's actual restored browser
journey and cleanup receipt; a synthetic demo does not establish recovery of
another application or a remote backup. Contributions need a synthetic fixture,
broken-source check, restored browser journey and failure-path cleanup tests.

Inspect the working tree first and preserve unrelated adapter work. Keep common
maintainer instructions here; T3 owns agent orchestration.
