// A counter every tab of this origin shares through localStorage, bumped when a
// reader's local snapshots are forgotten (sign-out, revocation). A tab captures
// it when it learns its reader and refuses to write snapshots once it has
// moved: a document-local flag cannot fence another tab, and another tab's
// pending write would restore what the sign-out removed.

const SIGN_OUT_GENERATION_KEY = 'avibe.sign-out-generation.v1';

export function readSignOutGeneration(): number {
  try {
    const raw = window.localStorage.getItem(SIGN_OUT_GENERATION_KEY);
    const value = raw === null ? 0 : Number(raw);
    return Number.isFinite(value) ? value : 0;
  } catch {
    return 0;
  }
}

export function bumpSignOutGeneration(): void {
  try {
    window.localStorage.setItem(SIGN_OUT_GENERATION_KEY, String(readSignOutGeneration() + 1));
  } catch {
    // Without storage there is nothing persisted to fence either.
  }
}
