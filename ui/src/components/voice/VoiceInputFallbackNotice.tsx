import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '../../context/ToastContext';
import { VOICE_INPUT_FALLBACK_EVENT } from '../../lib/voiceInput';

/**
 * Tells the user, once per recording, that the microphone chosen in Settings
 * was unavailable and the system input is recording instead. The choice stays
 * saved; `requestVoiceInputStream` announces each fallback.
 */
export function VoiceInputFallbackNotice(): null {
  const { t } = useTranslation();
  const { showToast } = useToast();
  useEffect(() => {
    const notify = () => showToast(
      `${t('settings.microphone.missingTitle')}\n${t('settings.microphone.missingDescription')}`,
      'warning',
    );
    window.addEventListener(VOICE_INPUT_FALLBACK_EVENT, notify);
    return () => window.removeEventListener(VOICE_INPUT_FALLBACK_EVENT, notify);
  }, [showToast, t]);
  return null;
}
