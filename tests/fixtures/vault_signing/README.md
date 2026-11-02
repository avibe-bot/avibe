# Public signing fixtures

All keys/seeds here are intentionally public synthetic test material. Never use
these keys to receive funds or to authenticate a production instance.

- `bip340.csv`: bitcoin/bips `bip-0340/test-vectors.csv`, downloaded 2026-11-02.
  SHA256 `34c9d1d9c3a88d524bc80778540dc43f8306ec249a7485293063c376db851c2d`.
- `bip341-wallet.json`: bitcoin/bips `bip-0341/wallet-test-vectors.json`.
  SHA256 `403e19fb81dd1f31e745699216308f61fb403774b2aafa87b631b8f7c042d37f`.
  The integration consumes the empty-tree first input only; tree signing is not
  supported. Its witness includes a final sighash byte, not part of the signature.
- `protected-sign-context.json`: byte-for-byte copy of `avibe-bot/vault-sandbox`
  PR #25, commit `fee07fe7f0153ae4e4b29dd0154703dc236fbce4`, `src/fixtures/`.
  The sandbox consumes the same bytes. This fixture must traverse the full
  Avibe input validator, not only its signed-context serialization helper.
- `canonical-json-utf16.json`: canonical bytes and SHA256 regenerated with that
  commit's actual JavaScript consumer (`src/operationContext.ts::stableJson`),
  not with the Python producer under test. The independent output agrees with
  the previous fixture bytes; its provenance is corrected accordingly.

To regenerate the canonical expectation independently, bundle the **consumer**
from that sandbox checkout using its own installed dependencies:

```bash
./node_modules/.bin/esbuild src/operationContext.ts --bundle --platform=node \
  --format=esm --outfile=/absolute/path/to/scratch/sandbox-operation-context.mjs
```

Then from the Avibe checkout, print the regenerated fixture (review the output
before replacing it). This invokes no Avibe Python serializer:

```bash
node --input-type=module - /absolute/path/to/scratch/sandbox-operation-context.mjs \
  tests/fixtures/vault_signing/canonical-json-utf16.json <<'JS'
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const { stableJson } = await import(pathToFileURL(process.argv[2]).href);
const { payload } = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const canonical = stableJson(payload);
console.log(JSON.stringify({
  source: 'vault-sandbox src/operationContext.ts stableJson, commit fee07fe7f0153ae4e4b29dd0154703dc236fbce4; generated with the actual JavaScript consumer via esbuild and Node.js',
  payload, canonical,
  sha256: createHash('sha256').update(canonical, 'utf8').digest('hex'),
}, null, 2));
JS
```

The same consumer's `signedOperationContextMessage` was also checked against
`protected-sign-context.json`'s `canonicalUnsignedContext`. The scalar limits
and astral/BMP key pair intentionally distinguish JS UTF-16 ordering from
Python's default code-point ordering.

Upstream BIP sources and licenses:
https://github.com/bitcoin/bips/tree/master/bip-0340
https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
https://github.com/bitcoin/bips/tree/master/bip-0341
https://github.com/bitcoin/bips/blob/master/bip-0341.mediawiki

BIP86 address expected values in `test_vault_addresses.py` are from its three
published receive/change vectors, not computed by the implementation under test:
https://github.com/bitcoin/bips/blob/master/bip-0086.mediawiki
