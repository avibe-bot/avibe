import { getVaultSandboxClient, openVaultAuthorizationWindow, resetVaultSandboxClient } from '../../src/lib/vaultSandboxClient';
import type { VaultSignedOperationContext } from '../../src/context/ApiContext';
import fixture from './public-context.json';

// This page imports the real client. It has no daemon, real record, VMK, private
// key, credential, signing or decryption path. Tests cancel at the loaded UI.
const output = document.querySelector('output')!;
let authorization: ReturnType<typeof openVaultAuthorizationWindow>;
function report(action: () => Promise<unknown>): void {
  output.textContent = 'pending';
  void action().then(() => { output.textContent = 'ready'; }, (error: unknown) => {
    output.dataset.error = error instanceof Error ? error.message : String(error);
    output.textContent = error && typeof error === 'object' && 'code' in error ? String(error.code) : String(error);
  });
}
document.querySelector('#load')!.addEventListener('click', () => report(async () => (await getVaultSandboxClient()).status()));
document.querySelector('#preopen')!.addEventListener('click', () => {
  authorization = openVaultAuthorizationWindow();
  report(async () => (await getVaultSandboxClient()).status());
});
document.querySelector('#setup')!.addEventListener('click', () => report(async () => (await getVaultSandboxClient()).setup({
  vaultUserHandle: 'cHVibGljLWludGVncml0eS1maXh0dXJl', displayName: 'Public test only', existingProtectedVault: false,
})));
async function sign(authorizationWindow?: string) {
  return (await getVaultSandboxClient()).sign({
  material: { name: 'PRIVATE_TEST_KEY', envelope: {
    ciphertext: 'not-ciphertext', nonce: 'not-a-nonce',
    wrap_meta: JSON.stringify({ v: 1, copies: [], rp_id: 'sandbox.avibe.bot', vault_user_handle: 'public-fixture',
      dek_nonce: 'not-a-nonce', wrapped_dek: 'not-a-key', record_meta: { kind: 'keypair', public_key: fixture.publicKey } }),
  } },
  scheme: 'schnorr-secp256k1-bip340', signingContext: fixture.signedContext.release.signingContext,
  context: fixture.signedContext as VaultSignedOperationContext,
  }, authorizationWindow);
}
document.querySelector('#authorize')!.addEventListener('click', () => report(() => sign()));
document.querySelector('#preopen-sign')!.addEventListener('click', () => {
  authorization = openVaultAuthorizationWindow();
  report(() => sign(authorization?.id));
});
document.querySelector('#reset')!.addEventListener('click', () => {
  authorization?.close();
  resetVaultSandboxClient();
  output.textContent = 'reset';
});
