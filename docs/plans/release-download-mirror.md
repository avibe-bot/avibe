# Release Download Mirror

## Background

Every Avibe download (desktop installers and updates, the managed runtimes, the
Show Runtime, and wheels used by install scripts) comes from GitHub Release
assets. Update discovery uses `api.github.com`. In mainland China, GitHub
downloads are often slow or stall, and the 60 requests/hour unauthenticated API
limit per IP has already surfaced as update-check 403s.

`dl.avibe.bot` serves the Cloudflare R2 bucket `avibe` through a custom domain,
with no egress fees. This plan makes it a byte-identical mirror of the
published releases plus a static release index. Clients try the mirror first
and fall back to GitHub.

## Principles

- GitHub Releases remain the source of truth. The mirror serves copies of
  published bytes and never authenticates them. Clients keep verifying what
  they already verify, whatever the source: updater signatures, pinned
  manifest sha256 values, and `SHA256SUMS`.
- A mirror miss is a fallback, not an error. Retention, lag, and outages
  degrade to today's GitHub path.

## Contract

### URLs

- `https://dl.avibe.bot/releases/<tag>/<asset>` is byte-identical to
  `https://github.com/avibe-bot/avibe/releases/download/<tag>/<asset>`.
  Clients derive the mirror URL only by substituting that prefix, which keeps
  percent-encoding identical (for example `+` as `%2B`). The bucket key is
  `releases/<tag>/<asset>` with the decoded asset name.
- Release objects are immutable:
  `Cache-Control: public, max-age=31536000, immutable`. Published assets are
  upload-once (release workflows replace assets only on drafts), so a tag and
  asset name never change bytes.
- `https://dl.avibe.bot/index/releases.json` is the release index:
  `Cache-Control: public, max-age=60`.
- Other top-level prefixes are reserved and never touched by the release
  mirror (for example a future `thirdparty/` for install-script dependencies).

### Release index, schema 1

```json
{
  "schema_version": 1,
  "repository": "avibe-bot/avibe",
  "releases": [
    {
      "tag": "gh-v3.1.2rc6",
      "prerelease": true,
      "published_at": "2026-09-29T07:35:48Z",
      "commit": "<40-hex commit the tag points to>",
      "assets": [{ "name": "<asset>", "size": 123, "sha256": "<64-hex>" }]
    }
  ]
}
```

- Releases are ordered newest `published_at` first; assets by name.
- The index lists exactly the releases the mirror holds. When it is written,
  every listed asset already exists in the bucket with those bytes.
- `sha256` is GitHub's asset digest, re-verified against the downloaded bytes
  before upload. `commit` is the peeled tag target.
- Producer: the release mirror workflow. Planned consumers: the desktop
  updater, the CLI update checker, and the runtime downloaders, replacing their
  `api.github.com` discovery calls.
- The index is unauthenticated metadata. A hostile index can at most withhold
  releases or name copies that then fail the consumer's existing verification.

### Retention

- Every full release (`prerelease: false`) is kept: stable `v*`,
  `model-hub-engine-*`, and `git-runtime-*`. Pinned runtime manifests depend on
  those URLs.
- The newest 20 prereleases by `published_at` are kept. Older prereleases leave
  the mirror and the index; their downloads fall back to GitHub.
- Drafts are never mirrored.
- At the time of writing that is 89 releases and about 19 GB, most of it the
  desktop bundles in recent prereleases (about 0.7 GB each).

## Reconciler

`.github/workflows/release-mirror.yml` runs `scripts/release_mirror.py`:

1. List published releases through the GitHub API and tag commits through
   `git ls-remote`. Refuse to continue if GitHub reports no published releases.
2. List the bucket and read the previous index.
3. Upload each selected asset that is missing, has the wrong size, or is not
   recorded with the same digest in the previous index, after verifying the
   downloaded size and sha256 against GitHub's digest.
4. Write the index if its bytes changed, then delete `releases/` objects that
   left the selection. Objects appear before the index names them and
   disappear only after it stops naming them.
5. If GitHub's digest for an indexed asset changed, re-upload it and fail the
   run: published bytes changed, and edge caches may hold the old copy until
   that URL is purged.

Triggers: completion of the release-publishing workflows (assets are uploaded
with `GITHUB_TOKEN`, which does not emit release events), an hourly schedule
as the safety net, and manual dispatch with an optional dry run. A dry run
reports the plan without writing and still fails when published bytes changed.

Credentials are the `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and
`R2_SECRET_ACCESS_KEY` repository secrets. They can write and delete every
object, so the workflow has no `pull_request` trigger: only code already on the
default branch receives them.

## Zone configuration

Cloudflare configuration lives outside the repository. The expected state:

- `dl.avibe.bot` is the bucket's custom domain; the `r2.dev` URL stays off.
- One Cache Rule for `http.host eq "dl.avibe.bot"`: eligible for cache, edge
  and browser TTL respect the origin. The object headers above carry the
  policy; the rule exists because default caching skips extensions such as
  `.whl`, `.tgz`, `.json`, and `.sig`.

## Rollout

1. Mirror and index (this change), then a full backfill by manual dispatch.
2. Cache Rule, then real-file tests from the three mainland carriers at the
   evening peak.
3. Clients, one at a time: desktop updater, then runtime and Show Runtime
   downloads, then `install.sh` and `install.ps1`. Each tries the mirror first,
   then GitHub, with a connect timeout and a stall watchdog, resumes with
   `Range` when switching source, and remembers the last source that worked.
4. Install-script third-party dependencies (the uv installer and
   python-build-standalone) through `UV_INSTALLER_GITHUB_BASE_URL` and
   `UV_PYTHON_INSTALL_MIRROR`, if step 2 shows GitHub is still their
   bottleneck. PyPI and npm keep using their existing mainland mirrors.
