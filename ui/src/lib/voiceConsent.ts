// This account's answer to "send voice to a third-party AI service?" on this Avibe
// (core/voice_consent.py; ui/design: composer/voice-consent/*). The instance owns the answer
// and reports it with `/api/asr/status` (`consent`); it refuses voice tokens and transcription
// without a yes. This module is what the page knows of it, and the one place a voice control
// asks: the question itself is drawn by `VoiceConsentHost`.
//
// A voice control calls `gateVoicePress()` before it opens the microphone. Only `granted` goes
// on; `ask` puts the question up and the press records nothing; `declined` leaves the press to
// the control's own "voice input is off" note.

import { useSyncExternalStore } from 'react';

import { apiFetch } from './apiFetch';
import { primeCloudToken } from './avibeFetch';
import { subscribeLocalSnapshotPrincipal } from './localSnapshots';

export type VoiceConsent = 'granted' | 'declined' | null;
export type VoicePress = 'granted' | 'declined' | 'ask';

// undefined: not read yet for this reader.
let known: VoiceConsent | undefined;
let asking = false;
const listeners = new Set<() => void>();

const emit = () => listeners.forEach((listener) => listener());

const set = (next: VoiceConsent | undefined) => {
  if (next === known) return;
  known = next;
  emit();
};

// The answer belongs to the reader it was read for.
subscribeLocalSnapshotPrincipal(() => set(undefined));

/** Take the answer from a `/api/asr/status` body. A body without the field comes from an
 *  instance that asks no question and refuses nothing, so voice goes on as before. */
export function noteVoiceStatus(data: unknown): void {
  if (typeof data !== 'object' || data === null) return;
  if (!('consent' in data)) {
    set('granted');
    return;
  }
  const consent = (data as { consent?: unknown }).consent;
  set(consent === 'granted' || consent === 'declined' ? consent : null);
}

export function knownVoiceConsent(): VoiceConsent | undefined {
  return known;
}

/** Whether the question is up. */
export function voiceConsentAsking(): boolean {
  return asking;
}

export function subscribeVoiceConsent(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Put the question up (`VoiceConsentHost` draws it). */
export function askVoiceConsent(): void {
  if (asking) return;
  asking = true;
  emit();
}

export function closeVoiceConsent(): void {
  if (!asking) return;
  asking = false;
  emit();
}

/** What a press may do now; `ask` when the answer is not known yet (the question reads it first). */
export function voiceConsentForPress(): VoicePress {
  if (known === 'granted') return 'granted';
  if (known === 'declined') return 'declined';
  return 'ask';
}

/** Before a voice control opens the microphone: `granted` records; otherwise this press records
 *  nothing, and `ask` has put the question up. */
export function gateVoicePress(): VoicePress {
  const press = voiceConsentForPress();
  if (press === 'ask') askVoiceConsent();
  return press;
}

/** Read the answer now (the question does when it opens on an unknown answer). */
export async function refreshVoiceConsent(): Promise<VoiceConsent | undefined> {
  try {
    const response = await apiFetch('/api/asr/status');
    if (response.ok) noteVoiceStatus(await response.json());
  } catch {
    // Unknown stays unknown: the question stays up and the answer is saved when given.
  }
  return known;
}

/** Save the answer; resolves whether the instance took it. */
export async function answerVoiceConsent(choice: 'granted' | 'declined'): Promise<boolean> {
  try {
    const response = await apiFetch('/api/asr/consent', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ choice }),
    });
    if (!response.ok) return false;
  } catch {
    return false;
  }
  set(choice);
  if (choice === 'granted') primeCloudToken();
  return true;
}

/** This account's answer as the page knows it (undefined: not read yet). */
export const useVoiceConsent = (): VoiceConsent | undefined =>
  useSyncExternalStore(subscribeVoiceConsent, knownVoiceConsent, knownVoiceConsent);

/** Whether the question is up. */
export const useVoiceConsentAsking = (): boolean =>
  useSyncExternalStore(subscribeVoiceConsent, voiceConsentAsking, voiceConsentAsking);
