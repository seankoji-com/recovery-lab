# One-time BuildKit cache bootstrap

This is a proposal, not evidence of a seeded cache. The workflow builds only
`recovery_lab/app` from commit `9ec179bc4914ba370ebc899b3dd1d5c83a5067aa`.
It exports `notes-linux-amd64` and `browser-linux-amd64` to the Actions cache
on main. It does not check out PR code, use repository secrets, or change the
existing drill. It needs only `contents: read`; BuildKit receives the Actions
runtime cache credentials supplied to the job, not a personal token. The
workflow is manually dispatched and both jobs refuse any ref other than main.

## Before execution

The coordinator must assess and authorise installation and execution, including
any required branch-protection approval. Review the exact installed workflow and
ensure main still trusts the pinned base, its Docker inputs, the pinned actions,
and their dependencies. Compare the PR's app inputs to that base again if the PR
changes. Do not install the workflow by running code from the PR. Once the
reviewed workflow has been integrated on main through the normal protected
process, verify its contents and the main commit via GitHub before dispatch.
There is no need to grant extra token permissions or bypass protection.

## Execute and verify after approval

From an authenticated operator environment, select the intended repository
explicitly. The dispatch command writes caches; do not run it before approval.

```sh
gh workflow run seed-buildkit-cache.yml --repo seankoji-com/recovery-lab --ref main
gh run list --repo seankoji-com/recovery-lab --workflow seed-buildkit-cache.yml --limit 10
gh run view RUN_ID --repo seankoji-com/recovery-lab --log
gh cache list --repo seankoji-com/recovery-lab --ref refs/heads/main --limit 100
```

Replace `RUN_ID` with the actual dispatch run ID and verify its event, ref,
workflow revision, checkout SHA and both job results. Check both seed exports
in the logs. The verify job runs on a separate GitHub-hosted runner with a new
Buildx builder and no earlier local builds. For each image, its own import must
succeed and the logs must show `CACHED` for applicable Dockerfile build steps
that were exported by its matching scope. A successful job, an import line
alone, or a cache list entry does not prove a hit. A base tag changing between
jobs can invalidate layers; record this as inconclusive or a miss, not a hit.
The two Dockerfiles share a base, so do not count shared base-image retrieval or
a layer available from the first verify build as proof of the second scope.
Inspect the browser-specific Playwright installation step in particular. Retain
the run URL, exact logs and cache IDs as evidence. This does not prove that PR
#16 later gets a hit; verify its own run separately.

## Risks and cleanup

Actions caches share repository quota and eviction policy with other caches.
These two `mode=max` exports can be large, evict useful entries, expire after
inactivity, or fail under quota or service limits. Check current GitHub quota,
usage and retention policy before dispatch. Concurrent writers to the same
scope can replace cache metadata; coordinate with main builds and avoid an
overlapping seed. A cache is an optimisation, not a trusted build attestation:
a writer with access to main's cache namespace can poison build results, and
mutable base tags or package repositories can change inputs. Restrict trusted
writers, review the base and dependencies, and use cold builds to check drift.
PRs may be able to read main caches, so never put secrets or sensitive data in
build inputs or layers. A pinned source commit does not pin external downloads.

After recording evidence, remove the temporary workflow through the normal
protected main change process. Delete only cache entries identified by the
operator as this run's unwanted entries, using their exact IDs from `gh cache
list` and `gh cache delete CACHE_ID --repo seankoji-com/recovery-lab`; do not
prune all caches. Keep the two entries if the bootstrap succeeded and they are
wanted. Workflow removal prevents later dispatches but does not delete caches;
cache deletion removes the optimisation and may require a new authorised seed.
