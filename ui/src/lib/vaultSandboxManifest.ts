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

// The deployed sandbox currently serves the document at /index.html and pins the executable
// assets by content hash. Keep the load URL cache-keyed by the pinned build metadata; the
// fetch-and-hash gate below remains the authority and fails closed before the iframe is trusted.
export const VAULT_SANDBOX_IFRAME_URL = `${VAULT_SANDBOX_ORIGIN}/index.html?version=${encodeURIComponent(
  VAULT_SANDBOX_VERSION,
)}&build=${encodeURIComponent(VAULT_SANDBOX_EXPECTED_BUILD_HASH)}`;
