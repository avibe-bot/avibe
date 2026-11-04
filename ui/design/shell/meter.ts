// The take card's meter, still, in each state as the live meter draws it
// (`src/components/voice/VoiceMeter.tsx`): bar heights 0–1, oldest first, newest on the right.
import type { HoldPhase } from '@/components/voice/useHoldToTalk';
import { stillVoiceLevel, type VoiceLevelSource } from '@/lib/voiceLevel';

/** Mid-sentence: syllables rising and falling, the newest still loud. */
export const speakingMeter = [
  0.12, 0.15, 0.32, 0.58, 0.74, 0.62, 0.4, 0.22, 0.16, 0.38, 0.66, 0.88,
  0.8, 0.52, 0.3, 0.18, 0.44, 0.72, 0.94, 0.82, 0.6, 0.42, 0.56, 0.7,
];
/** Nothing said yet: a faint breath moving across. */
export const quietMeter = [
  0.16, 0.17, 0.17, 0.16, 0.15, 0.14, 0.13, 0.12, 0.11, 0.1, 0.1, 0.1,
  0.11, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.17, 0.16, 0.15, 0.14, 0.13,
];
/** A release would cancel: flat. */
export const armedMeter = Array.from({ length: 24 }, () => 0.08);
/** Finishing: the swell part-way across. */
export const settlingMeter = [
  0.12, 0.12, 0.12, 0.12, 0.13, 0.15, 0.2, 0.27, 0.33, 0.34, 0.3, 0.23,
  0.17, 0.14, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12, 0.12,
];

const byPhase: Partial<Record<HoldPhase, readonly number[]>> = {
  listening: quietMeter,
  recording: speakingMeter,
  armed: armedMeter,
  settling: settlingMeter,
};

/** The meter a frame in `phase` draws, unless the frame gives its own. */
export const meterFor = (phase: HoldPhase): VoiceLevelSource => stillVoiceLevel(byPhase[phase] ?? quietMeter);
