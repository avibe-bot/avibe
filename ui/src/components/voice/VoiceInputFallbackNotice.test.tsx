/** @vitest-environment jsdom */
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ToastContext } from '../../context/ToastContext';
import { VOICE_INPUT_FALLBACK_EVENT } from '../../lib/voiceInput';
import { VoiceInputFallbackNotice } from './VoiceInputFallbackNotice';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

afterEach(cleanup);

describe('VoiceInputFallbackNotice', () => {
  it('shows the Settings unavailable copy once per announced recording fallback', () => {
    const showToast = vi.fn();
    const { unmount } = render(
      <ToastContext.Provider value={{ showToast }}>
        <VoiceInputFallbackNotice />
      </ToastContext.Provider>,
    );
    act(() => { window.dispatchEvent(new Event(VOICE_INPUT_FALLBACK_EVENT)); });
    expect(showToast).toHaveBeenCalledOnce();
    expect(showToast).toHaveBeenCalledWith(
      'settings.microphone.missingTitle\nsettings.microphone.recordingFallbackDescription',
      'warning',
    );
    unmount();
    window.dispatchEvent(new Event(VOICE_INPUT_FALLBACK_EVENT));
    expect(showToast).toHaveBeenCalledOnce();
  });
});
