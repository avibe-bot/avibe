import { lazyPage } from '@/lib/lazyPage';

/**
 * The add/provide-secret dialog as chat cards use it: its code (the whole secret form) loads when a
 * card first opens it, not with the chat's first screen. Render it inside a `<Suspense>`.
 */
export const LazyVaultSecretDialog = lazyPage(() => import('./vault-secret-dialog').then((module) => module.VaultSecretDialog));
