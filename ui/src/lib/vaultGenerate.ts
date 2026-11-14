// The browser side of "generate a password or application secret in Vaults" (avibe#3338; design:
// ui/design/frames/vault/generate-secret). One recipe, the same limits as the request spec's
// `generate` hint and `avault generate-secret`: a password counts characters, hex and Base64 count
// random bytes. Randomness is crypto.getRandomValues only; characters are picked by rejection
// sampling, so every character of the alphabet is equally likely (no modulo bias).

export type GenerateType = 'password' | 'hex' | 'base64';

export type GenerateRecipe =
  | { type: 'password'; length: number; digits: boolean; symbols: boolean; noLookalikes: boolean }
  | { type: 'hex' | 'base64'; bytes: number };

export const PASSWORD_LENGTH = { min: 12, max: 128, default: 24 } as const;
export const RANDOM_BYTES = { min: 16, max: 64, default: 32 } as const;

/** Safe in env files, shells and URLs: no quotes, backslash, space, comma or backtick. */
export const VAULT_GENERATE_SYMBOLS = '!#$%&*+-.:=?@^_~';
const LOWER = 'abcdefghijklmnopqrstuvwxyz';
const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const DIGITS = '0123456789';
const LOOKALIKES = new Set(['0', 'O', '1', 'l', 'I']);

export const DEFAULT_PASSWORD_RECIPE: GenerateRecipe = {
  type: 'password',
  length: PASSWORD_LENGTH.default,
  digits: true,
  symbols: true,
  noLookalikes: false,
};

/** The recipe a type starts from when the user switches to it. */
export function defaultRecipe(type: GenerateType): GenerateRecipe {
  return type === 'password' ? DEFAULT_PASSWORD_RECIPE : { type, bytes: RANDOM_BYTES.default };
}

/** The length the slider and number box show: characters for a password, bytes otherwise. */
export function recipeSize(recipe: GenerateRecipe): number {
  return recipe.type === 'password' ? recipe.length : recipe.bytes;
}

export function sizeLimits(type: GenerateType): { min: number; max: number } {
  return type === 'password' ? PASSWORD_LENGTH : RANDOM_BYTES;
}

/** The same recipe with its size clamped to the type's limits (whole numbers only). */
export function withSize(recipe: GenerateRecipe, size: number): GenerateRecipe {
  const { min, max } = sizeLimits(recipe.type);
  const next = Math.min(max, Math.max(min, Math.round(Number.isFinite(size) ? size : min)));
  return recipe.type === 'password' ? { ...recipe, length: next } : { ...recipe, bytes: next };
}

/** Characters the value will have: Base64 is 4 per started 3 bytes, padding included. */
export function outputLength(recipe: GenerateRecipe): number {
  if (recipe.type === 'password') return recipe.length;
  return recipe.type === 'hex' ? recipe.bytes * 2 : 4 * Math.ceil(recipe.bytes / 3);
}

function passwordClasses(recipe: Extract<GenerateRecipe, { type: 'password' }>): string[] {
  const classes = [LOWER, UPPER];
  if (recipe.digits) classes.push(DIGITS);
  if (recipe.symbols) classes.push(VAULT_GENERATE_SYMBOLS);
  return recipe.noLookalikes
    ? classes.map((set) => [...set].filter((char) => !LOOKALIKES.has(char)).join(''))
    : classes;
}

/** Bits of randomness, rounded down: what the popup shows as a strength hint. */
export function recipeBits(recipe: GenerateRecipe): number {
  if (recipe.type !== 'password') return recipe.bytes * 8;
  const alphabet = passwordClasses(recipe).join('').length;
  return Math.floor(recipe.length * Math.log2(alphabet));
}

/** Whether a recipe is within the limits; the generator is only offered for a valid one. */
export function isValidRecipe(recipe: GenerateRecipe): boolean {
  const size = recipeSize(recipe);
  const { min, max } = sizeLimits(recipe.type);
  return Number.isInteger(size) && size >= min && size <= max;
}

/** Fills `bytes` with random values; crypto.getRandomValues in the product, a fixed source in tests. */
export type RandomSource = (bytes: Uint8Array<ArrayBuffer>) => void;
const cryptoSource: RandomSource = (bytes) => {
  crypto.getRandomValues(bytes);
};

/** `count` indexes in [0, size), each uniform: a byte at or above the largest multiple of `size`
 *  below 256 is thrown away and drawn again. */
function uniformIndexes(count: number, size: number, random: RandomSource): number[] {
  const limit = 256 - (256 % size);
  const out: number[] = [];
  const buffer = new Uint8Array(Math.max(16, count * 2));
  while (out.length < count) {
    random(buffer);
    for (const byte of buffer) {
      if (byte < limit) out.push(byte % size);
      if (out.length === count) break;
    }
  }
  buffer.fill(0);
  return out;
}

function randomPassword(recipe: Extract<GenerateRecipe, { type: 'password' }>, random: RandomSource): string {
  const classes = passwordClasses(recipe);
  const alphabet = classes.join('');
  // Every chosen class must appear. A string that misses one is drawn again whole; changing one
  // position instead would make some strings likelier than others.
  for (;;) {
    const value = uniformIndexes(recipe.length, alphabet.length, random).map((index) => alphabet[index]).join('');
    if (classes.every((set) => [...value].some((char) => set.includes(char)))) return value;
  }
}

function toBase64(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** A new random value for `recipe`. Hex is lowercase; Base64 is the standard alphabet with `=`
 *  padding, what `openssl rand -base64 <bytes>` prints. */
export function generateSecretValue(recipe: GenerateRecipe, random: RandomSource = cryptoSource): string {
  if (!isValidRecipe(recipe)) throw new RangeError('recipe out of range');
  if (recipe.type === 'password') return randomPassword(recipe, random);
  const bytes = new Uint8Array(recipe.bytes);
  random(bytes);
  const value = recipe.type === 'hex'
    ? Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    : toBase64(bytes);
  bytes.fill(0);
  return value;
}

/** A request spec's `generate` hint (snake_case on the wire), as a recipe; null when absent or
 *  not one the generator can make. The hint only presets the generator. */
export function recipeFromSpec(hint: unknown): GenerateRecipe | null {
  if (!hint || typeof hint !== 'object') return null;
  const spec = hint as Record<string, unknown>;
  let recipe: GenerateRecipe;
  if (spec.type === 'password') {
    recipe = {
      type: 'password',
      length: typeof spec.length === 'number' ? spec.length : PASSWORD_LENGTH.default,
      digits: spec.digits !== false,
      symbols: spec.symbols !== false,
      noLookalikes: spec.no_lookalikes === true,
    };
  } else if (spec.type === 'hex' || spec.type === 'base64') {
    recipe = { type: spec.type, bytes: typeof spec.bytes === 'number' ? spec.bytes : RANDOM_BYTES.default };
  } else {
    return null;
  }
  return isValidRecipe(recipe) ? recipe : null;
}
