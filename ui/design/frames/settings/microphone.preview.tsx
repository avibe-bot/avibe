import {
  MicrophoneSettingsView,
  type MicrophoneSettingsState,
} from '@/components/settings/MicrophoneSettingsPage';
import { SYSTEM_DEFAULT_VOICE_INPUT, type VoiceInputDevice } from '@/lib/voiceInput';
import { stillVoiceLevel } from '@/lib/voiceLevel';

import { useDesignFixtures } from '../../shell/fixturesContext';

const devices: VoiceInputDevice[] = [
  { deviceId: 'builtin', label: 'Built-in microphone' },
  { deviceId: 'usb', label: 'USB microphone' },
];

export const MicrophoneSettingsPreview = () => {
  const fixture = useDesignFixtures().microphone ?? { platform: 'desktop', state: 'default' as const };
  const selected = ['selected', 'missing', 'disconnected', 'testing', 'quiet'].includes(fixture.state)
    ? 'usb'
    : SYSTEM_DEFAULT_VOICE_INPUT;
  const testing = fixture.state === 'testing' || fixture.state === 'quiet';
  const level = testing
    ? stillVoiceLevel(
      fixture.state === 'quiet'
        ? [0.12, 0.08, 0.1, 0.06, 0.08, 0.07, 0.09, 0.06]
        : [0.24, 0.42, 0.68, 0.52, 0.76, 0.58, 0.72, 0.64],
    )
    : null;

  return (
    <MicrophoneSettingsView
      platform={fixture.platform}
      state={fixture.state as MicrophoneSettingsState}
      selection={selected}
      devices={fixture.devices ?? devices}
      missing={fixture.state === 'missing' || fixture.state === 'disconnected'}
      level={level}
    />
  );
};
