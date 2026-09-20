import type { TFunction } from 'i18next';

import type { BackendNotice } from '@/context/ApiContext';

export type ShowToast = (
  message: string,
  type?: 'success' | 'error' | 'warning',
) => void;

/**
 * Surface server-side ``notices`` (from a save / remove response) as
 * warning toasts. Known notices get user-facing copy here so every
 * provider page presents the same meaning; unknown codes fall through to
 * a generic ``codexNoticeGeneric``-style toast so the user at least sees
 * that the server reported a notable side-effect.
 *
 * Previously each provider page duplicated this switch inline.
 */
export function surfaceBackendNotices(
  notices: BackendNotice[] | undefined,
  showToast: ShowToast,
  t: TFunction,
): void {
  if (!notices || notices.length === 0) return;
  for (const notice of notices) {
    if (notice.code === 'v2_clear_failed') {
      showToast(
        t('settings.backends.backendNoticeV2ClearFailed', {
          detail: notice.detail || t('settings.backends.backendNoticeDetailUnknown'),
        }),
        'warning',
      );
      continue;
    }
    if (notice.code === 'cleared_custom_relay_pointer') {
      showToast(
        t('settings.backends.codexNoticeClearedRelayPointer', {
          provider: notice.provider_id || 'custom',
          url: notice.base_url || '',
        }),
        'warning',
      );
      continue;
    }
    showToast(
      t('settings.backends.codexNoticeGeneric', { code: notice.code }),
      'warning',
    );
  }
}
