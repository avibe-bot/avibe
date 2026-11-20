const VOICE_INPUT_STORAGE_KEY = 'avibe.voiceInput.deviceId';

export const SYSTEM_DEFAULT_VOICE_INPUT = 'default' as const;

export type VoiceInputSelection = typeof SYSTEM_DEFAULT_VOICE_INPUT | string;

export type VoiceInputDevice = {
  deviceId: string;
  label: string;
};

export type VoiceInputStreamOptions = {
  onDeviceFallback?: () => void;
};

const storage = (): Storage | null => {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

export const readVoiceInputSelection = (): VoiceInputSelection => {
  const value = storage()?.getItem(VOICE_INPUT_STORAGE_KEY);
  return value || SYSTEM_DEFAULT_VOICE_INPUT;
};

export const writeVoiceInputSelection = (selection: VoiceInputSelection): boolean => {
  const target = storage();
  if (!target) return false;
  try {
    if (selection === SYSTEM_DEFAULT_VOICE_INPUT) target.removeItem(VOICE_INPUT_STORAGE_KEY);
    else target.setItem(VOICE_INPUT_STORAGE_KEY, selection);
    return true;
  } catch {
    return false;
  }
};

const mediaDevices = (): MediaDevices | null => (
  typeof navigator !== 'undefined' && navigator.mediaDevices ? navigator.mediaDevices : null
);

export const listVoiceInputDevices = async (): Promise<VoiceInputDevice[]> => {
  const devices = await mediaDevices()?.enumerateDevices?.();
  if (!devices) return [];
  return devices
    .filter((device) => device.kind === 'audioinput' && device.deviceId)
    .map((device) => ({
      deviceId: device.deviceId,
      label: device.label,
    }));
};

const isUnavailableSelectedDevice = (error: unknown): boolean => (
  error instanceof DOMException
    ? error.name === 'NotFoundError' || error.name === 'OverconstrainedError'
    : error instanceof Error && (error.name === 'NotFoundError' || error.name === 'OverconstrainedError')
);

/**
 * Opens the input selected in this browser profile. A missing saved device is
 * retried with the system input before the new recording starts; a live stream
 * is never switched after capture has begun.
 */
export const requestVoiceInputStream = async (
  options: VoiceInputStreamOptions = {},
): Promise<MediaStream> => {
  const devices = mediaDevices();
  if (!devices?.getUserMedia) throw new Error('Microphone capture unavailable');

  const selection = readVoiceInputSelection();
  if (selection === SYSTEM_DEFAULT_VOICE_INPUT) {
    return devices.getUserMedia({ audio: true });
  }

  try {
    return await devices.getUserMedia({ audio: { deviceId: { exact: selection } } });
  } catch (error) {
    if (!isUnavailableSelectedDevice(error)) throw error;
    options.onDeviceFallback?.();
    return devices.getUserMedia({ audio: true });
  }
};

export const voiceInputStorageKey = VOICE_INPUT_STORAGE_KEY;
