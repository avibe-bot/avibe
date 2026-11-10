# Model Hub engine candidates

A candidate is an engine build proposed for the signed engine channel
(`docs/plans/model-hub-engine-channel.md`). It is separate from the engine
Avibe packages (`vibe/model_hub_runtime/cliproxyapi_manifest.json`): merging a
candidate record does not change what any Avibe release installs, and does not
make the engine available to anyone. Only qualification and an approved
promotion do that.

No candidate is recorded here yet.

## One record per engine release

```text
patches/model-hub-engine-candidates/<asset_release_tag>/
  candidate.json
  model-hub-engine-manifest.json
```

`model-hub-engine-manifest.json` is the exact file that will be published with
the release: the packaged manifest's schema, with all six assets and their
sizes and digests, and the `build` section naming the Go version and the
patches under `patches/model-hub-cpa-*/`. Produce it with
`scripts/build_model_hub_engine.py --candidate`, then commit the generated
file unchanged. Qualification rebuilds the source and requires the same bytes.

`candidate.json`:

```json
{
  "schema_version": 1,
  "asset_release_tag": "model-hub-engine-v8.0.63-2",
  "min_avibe_version": "3.2.6",
  "manifest_sha256": "<SHA-256 of model-hub-engine-manifest.json>"
}
```

- `min_avibe_version` is a stable `X.Y.Z` release, 3.2.6 or later. It is fixed
  once the engine is promoted; a correction is a new build revision.
- `hosted_wire_changes` is optional. It maps a baseline engine release to the
  reviewed reason the hosted passthrough differs from it. Without an entry, a
  difference from that baseline blocks qualification.

The directory name, the record's tag and the manifest's `asset_release_tag`
must agree, and the directory holds only these two files.

Validate a record with:

```bash
python3 scripts/model_hub_engine_qualification.py candidate \
  --candidate-dir patches/model-hub-engine-candidates/<asset_release_tag>
```

Qualification, publication and promotion are described in `docs/release.md`.
