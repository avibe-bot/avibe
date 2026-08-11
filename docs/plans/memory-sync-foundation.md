# Memory Sync Foundation

This change establishes the process and artifact contract for the future
Repair index action. The only child command admitted by the contract is:

```text
<artifact-python> -I -m everos.entrypoints.cli.main cascade sync
```

The embedded runtime carries an inert `.pth` bootstrap and a minimal persistence
scrubber module. The bootstrap is enabled only for the exact `cascade sync`
argv, validates the parent/role/nonce envelope, installs scrubbers, and stops
before EverOS imports. The parent writes `memory/.rt/everos.sync.json` ahead of
spawn, validates the stopped child, atomically finalizes the record, releases
it, and cleans only the recorded process group. The sync record is independent
of `everos.sidecar.json` and rebuild ownership.

The current published Memory Runtime artifacts predate this contract. A release
must build all supported `linux-x64`, `linux-arm64`, and `darwin-arm64` bundles,
generate a manifest containing the sync bootstrap and scrubber digests, pass the
release guard, and publish those assets before a product Repair action can be
enabled. This branch intentionally does not publish artifacts, tags, or releases.
