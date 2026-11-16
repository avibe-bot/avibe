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
import { subscribeLocalSnapshotPrincipal } from './localSnapshots';

export type VoiceConsent = 'granted' | 'declined' | null;
export type VoicePress = 'granted' | 'declined' | 'ask';

// undefined: not read yet for this reader.
let known: VoiceConsent | undefined;
let asking = false;
const listeners = new Set<() => void>();

const emit = () => listeners.forEach((listener) => listener());

// Reads and saved answers in the order they happened. A status read takes its number when it
// starts, a saved answer when the instance took it; a read is applied only when nothing newer
// was, so a slow read cannot bring back an answer the reader has since changed.
let sequence = 0;
let applied = 0;

/** Number a `/api/asr/status` read as it starts; hand it to `noteVoiceStatus` with the body. */
export function beginVoiceConsentRead(): number {
  sequence += 1;
  return sequence;
}

const set = (next: VoiceConsent | undefined) => {
  if (next === known) return;
  known = next;
  emit();
};

/** The instance refused voice because this account has not allowed it (`voice_consent_required`
 *  from `/api/cloud/token` or `/api/asr/transcribe`): the answer here is stale, so read it again. */
export const VOICE_CONSENT_REQUIRED_EVENT = 'avibe:voice-consent-required';

// The answer belongs to the reader it was read for: a new reader reads its own.
subscribeLocalSnapshotPrincipal((next) => {
  applied = beginVoiceConsentRead();
  set(undefined);
  if (next) void refreshVoiceConsent();
});
if (typeof window !== 'undefined') {
  window.addEventListener(VOICE_CONSENT_REQUIRED_EVENT, () => {
    void refreshVoiceConsent();
  });
}

/** Take the answer from a `/api/asr/status` body. A body without the field comes from an
 *  instance that asks no question and refuses nothing, so voice goes on as before. */
export function noteVoiceStatus(data: unknown, read: number = beginVoiceConsentRead()): void {
  if (typeof data !== 'object' || data === null) return;
  // Superseded by a newer read or a saved answer.
  if (read <= applied) return;
  applied = read;
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

/** Whether speech may leave this device now. Every upload and the realtime socket ask this
 *  (lib/avibeFetch, lib/voiceTranscription), so a take recorded under a yes that has since been
 *  taken back, here or on another device, is not sent. An answer not read yet does not refuse:
 *  a press never records before the answer is read (`resolveVoicePress`). */
export function voiceSendAllowed(): boolean {
  return known !== 'declined' && known !== null;
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

/** Before a voice control opens the microphone, with the answer read first when it is not known
 *  yet (so a reader who said yes is not asked, and one who said no gets the off note). */
export async function resolveVoicePress(): Promise<VoicePress> {
  if (known === undefined) await refreshVoiceConsent();
  return gateVoicePress();
}

/** Read the answer now (the question does when it opens on an unknown answer). */
export async function refreshVoiceConsent(): Promise<VoiceConsent | undefined> {
  const read = beginVoiceConsentRead();
  try {
    const response = await apiFetch('/api/asr/status');
    if (response.ok) noteVoiceStatus(await response.json(), read);
  } catch {
    // Unknown stays unknown: the question stays up and the answer is saved when given.
  }
  return known;
}

/** Save the answer; resolves whether the instance took it. Other tabs and devices of this
 *  account hear of it from the event stream (`voice.consent.updated`). */
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
  applied = beginVoiceConsentRead();
  set(choice);
  return true;
}

/** This account's answer as the page knows it (undefined: not read yet). */
export const useVoiceConsent = (): VoiceConsent | undefined =>
  useSyncExternalStore(subscribeVoiceConsent, knownVoiceConsent, knownVoiceConsent);

/** Whether the question is up. */
export const useVoiceConsentAsking = (): boolean =>
  useSyncExternalStore(subscribeVoiceConsent, voiceConsentAsking, voiceConsentAsking);
