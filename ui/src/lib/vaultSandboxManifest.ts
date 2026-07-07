export const VAULT_SANDBOX_ORIGIN = 'https://sandbox.avibe.bot';
export const VAULT_SANDBOX_VERSION = '0.1.0';
export const VAULT_SANDBOX_EXPECTED_BUILD_HASH = 'dev';

export type VaultSandboxPinnedManifest = {
  algorithm: 'sha256';
  resources: Record<string, string>;
};

export const VAULT_SANDBOX_PINNED_MANIFEST: VaultSandboxPinnedManifest = {
  algorithm: 'sha256',
  resources: {
    '/assets/index.CWzpiVRF.js': 'sha256-I6MUHfT7DaD2Mg5OVURvZBsY1uA6TA7sf7XcBkZBo0A=',
    '/assets/index.CWzpiVRF.js.map': 'sha256-iYuql8cyRd3rf5gc6Ugx3L41WW2lPtoDf/zr+7XtnTo=',
    '/assets/index.DpR35930.css': 'sha256-uk4usRuXbzaL7jOMlnzNPB+yaMVpnTMgriiC/zqRHoY=',
    '/assets/nodeCryptoShim.DTwgsOT4.js': 'sha256-2Jr4p1Eu6bEPGgodAN8w7H7v/nkdob9vyK5uAmjpmrI=',
    '/assets/nodeCryptoShim.DTwgsOT4.js.map': 'sha256-XeygI0Eab/VrjUng2V68o/bOP8UijmyexNz7YkwH4j4=',
    '/index.html': 'sha256-d5Q3RQE/yyoqhghSbme/DDoxkIUeTzZuzrUPW/gx+oY=',
  },
};

export const VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS = Object.keys(VAULT_SANDBOX_PINNED_MANIFEST.resources)
  .filter((path) => !path.endsWith('.map'))
  .sort();

export const VAULT_SANDBOX_MANIFEST_PATH = '/build-manifest.json';
export const VAULT_SANDBOX_IFRAME_RESOURCE_PATH = '/index.html';

// The iframe navigates to the exact document URL that the parent fetches and hashes.
// Sandbox hosting should move this resource to an immutable /v/<version>/ path as soon as
// the static host publishes one; until then the identical-URL gate closes the prior query
// string equivocation gap while the pinned manifest remains the authority.
export const VAULT_SANDBOX_IFRAME_URL = `${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_IFRAME_RESOURCE_PATH}`;
