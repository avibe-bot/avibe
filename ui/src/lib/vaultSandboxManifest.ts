import release from '../../../vibe/data/vault_sandbox_release.json';

// Generated only from a locally reproduced, reviewed sandbox release. Network
// responses can prove equality to this pin; they can never define a new pin.
export const VAULT_SANDBOX_ORIGIN = release.sandboxOrigin;
export const VAULT_SANDBOX_EXPECTED_BUILD_HASH = release.buildHash;
export const VAULT_SANDBOX_VERSION = release.sandboxVersion;
export const VAULT_SANDBOX_PINNED_MANIFEST = release;
export const VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS = Object.keys(release.resources).sort();
export const VAULT_SANDBOX_RELEASE_PATH = `/v/${release.buildHash}/`;
export const VAULT_SANDBOX_MANIFEST_PATH = `${VAULT_SANDBOX_RELEASE_PATH}build-manifest.json`;
export const VAULT_SANDBOX_IFRAME_RESOURCE_PATH = `${VAULT_SANDBOX_RELEASE_PATH}index.html`;
export const VAULT_SANDBOX_IFRAME_URL = `${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_IFRAME_RESOURCE_PATH}`;
