import clsx from 'clsx';

// The tone-tinted 24px chip that leads every message head in Chat — agent,
// system, harness and annotation all draw the same geometry, so a new row kind
// inherits exactly the avatar its neighbours already have (design.pen m31JWV
// draws the Claude head and the annotation head identically).
//
// Lives in its own module rather than inside ChatPage so a row component can
// reuse it without importing the whole page back.
const TONE_AVATAR: Record<'mint' | 'cyan' | 'gold' | 'muted', string> = {
  mint: 'border-mint/30 bg-mint/[0.13] text-mint-ink',
  cyan: 'border-cyan/30 bg-cyan/[0.13] text-cyan-ink',
  gold: 'border-gold/30 bg-gold/[0.13] text-gold-ink',
  muted: 'border-border-strong bg-foreground/[0.06] text-muted',
};

export type RoleAvatarTone = keyof typeof TONE_AVATAR;

export const RoleAvatar: React.FC<{ tone: RoleAvatarTone; children: React.ReactNode }> = ({ tone, children }) => (
  <span
    className={clsx(
      'flex size-6 shrink-0 items-center justify-center rounded-lg border [&_svg]:size-3.5',
      TONE_AVATAR[tone],
    )}
  >
    {children}
  </span>
);
