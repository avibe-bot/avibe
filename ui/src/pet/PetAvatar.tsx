import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { usePageActive } from '@/lib/pageActivity';

import { VibeyArt, type VibeyPose } from './VibeyArt';
import type { PetState } from './petState';
import './pet.css';

/** What the avatar draws: a pet state, plus pose-only listening, sleeping, and team states. */
export type PetPose = PetState | 'listening' | 'sleeping' | 'team';

// Breathing stops after a while in idle, so a pet left alone costs nothing.
// Sleeping is deliberate motion and keeps looping while the window is visible.
const BREATHING_SETTLE_MS = 30 * 1000;

/** The caller derives the pose from eligible activities; badge counts unread replies. */
export const PetAvatar: React.FC<{ pose: PetPose; badge?: number }> = ({
  pose,
  badge = 0,
}) => {
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
  const resting = pose === 'idle';
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    if (!resting) return undefined;
    const timer = window.setTimeout(() => setSettled(true), BREATHING_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [resting]);

  return (
    <VibeyArt
      pose={pose as VibeyPose}
      label={alt}
      active={pageActive}
      breathing={!resting || !settled}
    />
  );
};
