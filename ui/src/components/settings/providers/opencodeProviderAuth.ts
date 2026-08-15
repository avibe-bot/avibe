import type { OpencodeProvider } from '@/context/ApiContext';

// The in-card OAuth panel must only report "signed in" when OpenCode's
// auth store actually holds an OAuth entry for the provider. ``configured``
// alone is true for API-key saves too, which used to render "OAuth
// credentials stored" over a plain API-key setup.
export const providerOauthSignedIn = (provider: OpencodeProvider): boolean =>
  provider.active_auth_type === 'oauth';
