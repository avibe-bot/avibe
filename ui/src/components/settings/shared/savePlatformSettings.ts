import type { ApiContextType } from '@/context/ApiContext';

/** Persist the platform editor's auxiliary settings through the Settings owner. */
export async function savePlatformSettings(
  api: Pick<ApiContextType, 'saveSettings'>,
  platform: string,
  data: Record<string, unknown>,
  canManageAccessMembers: boolean,
): Promise<void> {
  const guilds = data.discordGuildAllowlist;
  if (canManageAccessMembers && platform === 'discord' && Array.isArray(guilds)
    && (guilds.length > 0 || data.discordGuildAllowlistTouched === true)) {
    await api.saveSettings({
      guilds: Object.fromEntries(guilds.map((id: string) => [id, { enabled: true }])),
    }, 'discord');
  }
}
