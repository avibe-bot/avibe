// A counter every tab of this origin shares through localStorage, bumped when
// any tab signs out. Work that started before a sign-out (a guard check, a
// pending snapshot write) captures the value when it begins and refuses to
// persist anything if it has moved: a document-local flag cannot fence another
// tab, and another tab's write would restore what the sign-out removed.

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
