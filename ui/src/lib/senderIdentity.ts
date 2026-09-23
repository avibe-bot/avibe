// How a human sender is drawn in Chat, derived from what the server resolved.
//
// On an Organization instance the server attaches ``sender_label`` to a human
// row (storage/sender_identity.py). Everything visual comes from here so the
// same person always gets the same avatar -- in the live frame, in the tail, in
// an older page, on another reader's machine. The tone is a pure function of
// ``author_id``, never of render order or of the row's position in a window.

export type SenderTone = 'gold' | 'violet';

// The two solid-fill accents the design draws for senders (design.pen nlrCu).
// Mint is the agent's own avatar and stays reserved for it.
const SENDER_TONES: readonly SenderTone[] = ['gold', 'violet'];

export const SENDER_TONE_CLASS: Record<SenderTone, string> = {
  gold: 'bg-gold text-gold-foreground',
  violet: 'bg-violet text-violet-foreground',
};

/** Pick a sender's avatar tone from their principal id. */
export function senderTone(authorId: string | null | undefined): SenderTone {
  // FNV-1a over the id: same answer across reloads, processes and machines,
  // which nothing seeded from render order or Math.random could promise.
  let hash = 0x811c9dc5;
  for (const ch of String(authorId ?? '')) {
    hash ^= ch.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return SENDER_TONES[hash % SENDER_TONES.length];
}

/** The single character an avatar shows for a sender label. */
export function senderInitial(label: string): string {
  // By code point, so a name opening with an emoji or an astral character
  // yields that whole character instead of half a surrogate pair.
  const [first] = Array.from(label.trim());
  return first ? first.toUpperCase() : '?';
}
