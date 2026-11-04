import type { CSSProperties } from 'react';

import vibeyMascot from '@/assets/pet/vibey.png';
import { cn } from '@/lib/utils';
import { nameHue, nameInitial, type BotAvatar as BotAvatarSpec } from '@/lib/bots';

export interface BotAvatarProps {
  /** The Bot's avatar, or `null` for the default: the name's first character on
   *  a color derived from the name (D5). */
  avatar: BotAvatarSpec | null;
  /** The name the default avatar is drawn from. */
  name: string;
  /** Diameter in px. */
  size: number;
  /** An unsaved upload's preview URL; wins over `avatar`. */
  previewUrl?: string | null;
  /** Among other Bots (a strip, a list, a row of faces): the mascot is drawn larger and
   *  unclipped (`.bot-avatar-in-list`), so it reads as big as their discs while staying
   *  inside the circle the selected edge draws. A face standing alone keeps its size. */
  inList?: boolean;
  /** The selected Bot (`.bot-avatar-selected`): its edge lit mint, with a soft glow. */
  selected?: boolean;
  className?: string;
}

/** One Bot's round avatar: the Vibey mascot drawn straight on the surface (no well,
 *  alone or in a list), an emoji on its hue, an uploaded image cropped to a circle,
 *  or the name's first character. Every kind takes the same round box. */
export const BotAvatar: React.FC<BotAvatarProps> = ({ avatar, name, size, previewUrl, inList = false, selected = false, className }) => {
  const box: CSSProperties = { width: size, height: size };
  const shell = cn(
    'bot-avatar grid shrink-0 select-none place-items-center overflow-hidden rounded-full',
    inList && 'bot-avatar-in-list',
    selected && 'bot-avatar-selected',
    className,
  );
  if (previewUrl || avatar?.kind === 'image') {
    return (
      <span className={cn(shell, 'bg-surface-3')} style={box} aria-hidden="true">
        <img
          src={previewUrl || (avatar as { url: string }).url}
          alt=""
          draggable={false}
          className="size-full object-cover"
        />
      </span>
    );
  }
  if (avatar?.kind === 'mascot') {
    return (
      <span className={cn(shell, 'bot-avatar-mascot')} style={box} aria-hidden="true">
        <img src={vibeyMascot} alt="" draggable={false} className="size-full scale-[1.12] object-contain" />
      </span>
    );
  }
  if (avatar?.kind === 'emoji') {
    return (
      <span
        className={cn(shell, 'bot-avatar-emoji leading-none')}
        style={{ ...box, fontSize: Math.round(size * 0.48), ['--bot-hue' as string]: avatar.hue }}
        aria-hidden="true"
      >
        {avatar.emoji}
      </span>
    );
  }
  return (
    <span
      className={cn(shell, 'bot-avatar-initial font-semibold leading-none')}
      style={{ ...box, fontSize: Math.round(size * 0.42), ['--bot-hue' as string]: nameHue(name) }}
      aria-hidden="true"
    >
      {nameInitial(name)}
    </span>
  );
};
