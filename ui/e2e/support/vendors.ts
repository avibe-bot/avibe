// The shipped API-key vendor catalog, as this suite sees it.
//
// The dialog offers one option per row of `vibe/data/api_key_vendors.json`, and
// the server pins the created source's interface from the same file. A spec that
// typed a vendor id and its interface by hand would agree with neither after the
// catalog moved: it would keep passing while testing a vendor that no longer
// ships, or pin an interface the row no longer declares. So the file is read
// here, the way `copy.ts` reads the i18n bundles — from disk, in Node, with no
// bundler in the path.
//
// Only rows the mock upstream can actually speak are offered. The mock is how
// this suite reaches an upstream at all, so a row whose interface it cannot
// serve is not drivable from here — that is a gap in the mock, not a vendor to
// assert against. The list is asserted non-empty at import, because silently
// having nothing to pick would read as coverage.
import { readFileSync } from 'node:fs';

import { MOCK_PROTOCOLS, type MockProtocol } from './mock';

/** One catalog row, narrowed to what a spec needs to drive it. */
export type CatalogVendor = {
  /** The id the dialog sends and the created source is stored under. */
  id: string;
  label: string;
  official_base_url: string;
  /** The interface the catalog pins for this vendor — never detected. */
  protocol: MockProtocol;
};

const rows = JSON.parse(
  readFileSync(new URL('../../../vibe/data/api_key_vendors.json', import.meta.url), 'utf8'),
) as CatalogVendor[];

/** Every shipped vendor whose pinned interface the mock upstream can serve. */
export const CATALOG_VENDORS: readonly CatalogVendor[] = rows.filter(
  (row) => (MOCK_PROTOCOLS as readonly string[]).includes(row.protocol),
);

if (CATALOG_VENDORS.length === 0) {
  throw new Error(
    'vibe/data/api_key_vendors.json has no row whose protocol the mock upstream can serve, '
    + `so no vendor can be driven from this suite. Mock protocols: ${MOCK_PROTOCOLS.join(', ')}.`,
  );
}
