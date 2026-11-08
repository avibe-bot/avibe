export const accountInitialFor = (email: string | null): string => {
  if (email === null) return 'A';
  const local = email.split('@')[0] || email;
  const cleaned = local.trim();
  return cleaned ? cleaned[0]!.toUpperCase() : 'A';
};

const accountAvatarBaseClassName = 'inline-flex items-center justify-center rounded-full border border-cyan/35 bg-cyan/[0.08] font-semibold text-cyan-ink';

export const accountAvatarClassName = `${accountAvatarBaseClassName} size-8 text-[11px]`;

export const accountDrawerAvatarClassName = `${accountAvatarBaseClassName} size-9 shrink-0 text-[13px]`;

/** The avatar in the phone app's 44px header: drawn at 28px, near a native navigation bar's,
 *  inside its 44px touch target, a little above the 24px logo beside it. */
export const accountHeaderAvatarClassName = `${accountAvatarBaseClassName} size-7 shrink-0 text-[11px]`;
