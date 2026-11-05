import { describe, expect, it } from 'vitest';

import { createVoiceActivity, VOICE_SILENCE_RMS, voiceSilenceGateEvent } from './voiceActivity';
// The loudness a take reports every 32 ms (RMS of the 48 kHz input), for one Chinese and one
// English sentence (macOS speech synthesis), each normalized so the whole take's RMS is 1.
import speech from './voiceActivity.test-levels.json' with { type: 'json' };

// The silence gate keeps near-silent takes away from recognition, where the hotwords can come
// back as text. It must never hold back real speech, however softly spoken (宁松勿紧).

const fromDb = (db: number) => 10 ** (db / 20);

/** A take's readings, its whole-take RMS at `db` dBFS, over a room at `floorDb`. */
const spoken = (envelope: number[], db: number, floorDb = -55) => envelope.map((level) => Math.max(level * fromDb(db), fromDb(floorDb)));

const run = (readings: number[]) => {
  const activity = createVoiceActivity();
  for (const rms of readings) activity.observe(rms);
  return activity;
};

describe('the silence gate', () => {
  it.each([
    ['ordinary speech on an iPhone', -14],
    ['speech well under it', -30],
    ['soft speech', -40],
    ['speech 31 dB under ordinary', -45],
    ['speech 33 dB under ordinary', -47],
  ])('lets %s through, in Chinese and in English (%d dBFS)', (_case, db) => {
    expect(run(spoken(speech.zh, db)).silent()).toBe(false);
    expect(run(spoken(speech.en, db)).silent()).toBe(false);
  });

  it.each([
    // The owner's iPhone with nobody speaking measured −49 to −86 dBFS over the take.
    ['a quiet room', Array.from({ length: 16 }, () => fromDb(-50))],
    ['a quiet room with a click', [...Array.from({ length: 10 }, () => fromDb(-50)), fromDb(-36), fromDb(-44), ...Array.from({ length: 4 }, () => fromDb(-50))]],
    ['a long silent take', Array.from({ length: 280 }, (_value, index) => fromDb(index % 50 === 0 ? -38 : -52))],
  ])('holds back %s', (_case, readings) => {
    expect(run(readings).silent()).toBe(true);
  });

  it('gates nothing when no loudness was reported at all', () => {
    expect(run([]).silent()).toBe(false);
  });

  it('takes three readings at the threshold within a third of a second as sound, and no fewer', () => {
    const quiet = fromDb(-55);
    expect(run([VOICE_SILENCE_RMS, quiet, VOICE_SILENCE_RMS, quiet, VOICE_SILENCE_RMS]).silent()).toBe(false);
    expect(run([VOICE_SILENCE_RMS, VOICE_SILENCE_RMS, quiet]).silent()).toBe(true);
    expect(run([VOICE_SILENCE_RMS, ...Array.from({ length: 9 }, () => quiet), VOICE_SILENCE_RMS, VOICE_SILENCE_RMS]).silent()).toBe(true);
  });
});

describe('readings from the recorded PCM', () => {
  it('reads 32 ms windows across however the recorder chunks the samples', () => {
    const activity = createVoiceActivity();
    const speech = new Int16Array(16 * 160).fill(Math.round(0.05 * 32_767));
    // 160 ms of speech-level audio, in uneven chunks.
    activity.observePcm(speech.subarray(0, 700));
    activity.observePcm(speech.subarray(700, 1_900));
    activity.observePcm(speech.subarray(1_900));
    expect(activity.loudReadings()).toBe(5);
    expect(activity.silent()).toBe(false);
    expect(Math.round(20 * Math.log10(activity.peak()))).toBe(-26);
  });
});

describe('a dropped take in the reliability log', () => {
  it('says how long it was, how many readings reached the gate, and its loudest, and nothing more', () => {
    const readings = [...Array.from({ length: 12 }, () => fromDb(-50)), fromDb(-36), ...Array.from({ length: 3 }, () => fromDb(-50))];
    expect(voiceSilenceGateEvent(run(readings), 512.4)).toEqual({
      event: 'dictation_gated',
      outcome: 'empty',
      providerStage: 'finalization',
      totalDurationMs: 512,
      voicedReadings: 1,
      peakDbfs: -36,
    });
  });
});

