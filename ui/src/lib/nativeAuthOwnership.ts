export const NATIVE_AUTH_HUB_OWNED = 'native_auth_hub_owned';

type NativeAuthOwnershipPayload = {
  error?: unknown;
  reauth_channel?: unknown;
};

export function isNativeAuthHubOwned(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const payload = value as NativeAuthOwnershipPayload;
  return payload.error === NATIVE_AUTH_HUB_OWNED && payload.reauth_channel === 'hub';
}
