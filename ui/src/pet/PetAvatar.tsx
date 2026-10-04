import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import vibey from '@/assets/pet/vibey.png';
import vibeyCelebrate from '@/assets/pet/vibey-celebrate.png';
import vibeyError from '@/assets/pet/vibey-error.png';
import vibeyFocus from '@/assets/pet/vibey-focus.png';
import vibeyMessage from '@/assets/pet/vibey-message.png';
import vibeySleep from '@/assets/pet/vibey-sleep.png';
import vibeyThinking from '@/assets/pet/vibey-thinking.png';
import { usePageActive } from '@/lib/pageActivity';

import type { PetState } from './petState';
import './pet.css';

/** What the avatar draws: a pet state, plus the two pose-only states. */
export type PetPose = PetState | 'listening' | 'sleeping';

const POSES: Record<PetPose, string> = {
  idle: vibey,
  sleeping: vibeySleep,
  listening: vibeyFocus,
  running: vibeyThinking,
  ready: vibeyCelebrate,
  needs_input: vibeyMessage,
  blocked: vibeyError,
};

// Breathing stops after a while in a resting pose, so a pet left alone costs
// nothing; any other pose keeps breathing while the window is visible.
const BREATHING_SETTLE_MS = 30 * 1000;

export const PetAvatar: React.FC<{ pose: PetPose; badge?: number }> = ({ pose, badge = 0 }) => {
  const { t } = useTranslation();
  return (
    <div className="pet-avatar" data-pose={pose}>
      {/* Keyed by pose: a state change remounts the image, which plays the
          one-shot change animation and restarts the breathing timer. */}
      <PetPoseImage key={pose} pose={pose} alt={t(`pet.state.${pose}`)} />
      {badge > 0 && (
        <span
          className="absolute right-1 top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-destructive px-1.5 text-[11px] font-semibold leading-none text-destructive-foreground"
          aria-label={t('pet.unreadBadge', { count: badge })}
        >
          {badge > 99 ? '99+' : badge}
        </span>
      )}
    </div>
  );
};

const PetPoseImage: React.FC<{ pose: PetPose; alt: string }> = ({ pose, alt }) => {
  const pageActive = usePageActive();
  const resting = pose === 'idle' || pose === 'sleeping';
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    if (!resting) return undefined;
    const timer = window.setTimeout(() => setSettled(true), BREATHING_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [resting]);

  return (
    <img
      className="pet-avatar__pose"
      src={POSES[pose]}
      alt={alt}
      data-breathing={pageActive && !settled ? 'true' : 'false'}
      draggable={false}
    />
  );
};
