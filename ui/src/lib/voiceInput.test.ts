/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { startVoiceInputTest } from './voiceInputTest';

import {
  listVoiceInputDevices,
  readVoiceInputSelection,
  requestVoiceInputStream,
  SYSTEM_DEFAULT_VOICE_INPUT,
  writeVoiceInputSelection,
} from './voiceInput';

const stream = () => ({ getTracks: () => [] }) as unknown as MediaStream;

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: vi.fn(async () => stream()),
      enumerateDevices: vi.fn(async () => []),
    },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('voice input selection', () => {
  it('keeps the system default outside local storage', () => {
    expect(readVoiceInputSelection()).toBe(SYSTEM_DEFAULT_VOICE_INPUT);
    expect(writeVoiceInputSelection('usb')).toBe(true);
    expect(readVoiceInputSelection()).toBe('usb');
    expect(writeVoiceInputSelection(SYSTEM_DEFAULT_VOICE_INPUT)).toBe(true);
    expect(readVoiceInputSelection()).toBe(SYSTEM_DEFAULT_VOICE_INPUT);
  });

  it('requests the selected device for a new capture', async () => {
    writeVoiceInputSelection('usb');

    await requestVoiceInputStream();

    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({
      audio: { deviceId: { exact: 'usb' } },
    });
  });

  it('falls back before capture begins when the saved device is unavailable', async () => {
    writeVoiceInputSelection('usb');
    const getUserMedia = vi.mocked(navigator.mediaDevices.getUserMedia);
    getUserMedia
      .mockRejectedValueOnce(new DOMException('missing', 'NotFoundError'))
      .mockResolvedValueOnce(stream());
    const onDeviceFallback = vi.fn();

    await requestVoiceInputStream({ onDeviceFallback });

    expect(onDeviceFallback).toHaveBeenCalledOnce();
    expect(getUserMedia).toHaveBeenNthCalledWith(2, { audio: true });
  });

  it('does not replace permission or busy errors with another device', async () => {
    writeVoiceInputSelection('usb');
    const error = new DOMException('blocked', 'NotAllowedError');
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(error);

    await expect(requestVoiceInputStream()).rejects.toBe(error);
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledOnce();
  });

  it('lists only audio input devices', async () => {
    vi.mocked(navigator.mediaDevices.enumerateDevices).mockResolvedValueOnce([
      { kind: 'audioinput', deviceId: 'builtin', label: 'Built-in' },
      { kind: 'videoinput', deviceId: 'camera', label: 'Camera' },
      { kind: 'audioinput', deviceId: '', label: 'Unnamed' },
    ] as MediaDeviceInfo[]);

    await expect(listVoiceInputDevices()).resolves.toEqual([
      { deviceId: 'builtin', label: 'Built-in' },
    ]);
  });
});

// Local-test acquisition owns its stream even when the audio graph cannot be created.
// Existing selection tests stop at getUserMedia and cannot observe graph cleanup.
describe('local microphone test initialization', () => {
  it.each(['constructor', 'analyser', 'source', 'connect'] as const)(
    'releases the acquired stream when %s fails', async (failure) => {
      const stop = vi.fn();
      const close = vi.fn(async () => undefined);
      const disconnect = vi.fn();
      const error = new Error('Synthetic graph initialization failure');
      vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue({
        getTracks: () => [{ stop }],
      } as unknown as MediaStream);
      vi.stubGlobal('AudioContext', class {
        constructor() { if (failure === 'constructor') throw error; }
        close = close;
        createAnalyser() {
          if (failure === 'analyser') throw error;
          return { fftSize: 2048, disconnect };
        }
        createMediaStreamSource() {
          if (failure === 'source') throw error;
          return { disconnect, connect: () => { throw error; } };
        }
      });
      await expect(startVoiceInputTest()).rejects.toBe(error);
      expect(stop).toHaveBeenCalledOnce();
      expect(close).toHaveBeenCalledTimes(failure === 'constructor' ? 0 : 1);
    },
  );
});
