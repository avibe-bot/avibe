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

The executable acceptance test builds an isolated virtual environment carrying
a behavioral fake identified as the pinned EverOS version. It invokes the exact
admitted argv through the artifact bootstrap and proves the expected observable
contract: Markdown files are scanned and queued work is drained. This is honest
evidence for Avibe's launch and acceptance boundary; it does not claim that a
source-string scan proves upstream EverOS behavior.

`cascade fix --apply` remains deliberately absent. The pinned upstream contract
only establishes that pathless `cascade sync` may coexist with the live server;
similar-looking implementation code is not an online-safety guarantee. The same
executable acceptance test proves the artifact bootstrap rejects `fix --apply`
before its CLI can mutate state. Adding that operation requires a separate issue
and an explicit upstream live-safety contract.
