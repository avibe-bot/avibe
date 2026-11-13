import { lazyPage } from '@/lib/lazyPage';

/** The add / provide dialog, fetched when a chat card first needs it (docs/plans/web-ui-first-load.md):
 *  the chat's first screen draws the cards, not the dialog with its key and seed-wallet code. */
export const LazyVaultSecretDialog = lazyPage(() => import('./vault-secret-dialog').then((module) => module.VaultSecretDialog));
