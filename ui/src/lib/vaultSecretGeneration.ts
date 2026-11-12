export type StaticSecretCharset = 'hex' | 'base64url' | 'custom';

export type StaticSecretGenerationRecipe = {
  length: number;
  charset: StaticSecretCharset;
  lowercase?: boolean;
  uppercase?: boolean;
  digits?: boolean;
  symbols?: boolean;
  exclude_lookalikes?: boolean;
};

export const MAX_GENERATED_STATIC_SECRET_LENGTH = 512;
export const DEFAULT_STATIC_SECRET_GENERATION_RECIPE: StaticSecretGenerationRecipe = {
  length: 32,
  charset: 'base64url',
};

const GROUPS = ['lowercase', 'uppercase', 'digits', 'symbols'] as const;
const GROUP_ALPHABETS: Record<(typeof GROUPS)[number], string> = {
  lowercase: 'abcdefghijklmnopqrstuvwxyz',
  uppercase: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
  digits: '0123456789',
  symbols: '!#$%&()*+,-./:;<=>?@[]^_{|}~',
};
const LOOKALIKE_CHARACTERS = new Set('0O1lI|');

export function normalizeStaticSecretGenerationRecipe(value: unknown): StaticSecretGenerationRecipe {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('generate must be an object');
  }
  const recipe = value as Record<string, unknown>;
  const allowed = new Set(['length', 'charset', ...GROUPS, 'exclude_lookalikes']);
  if (Object.keys(recipe).some((key) => !allowed.has(key))) throw new Error('generate contains unsupported fields');
  if (typeof recipe.length !== 'number' || !Number.isInteger(recipe.length)
    || recipe.length < 1 || recipe.length > MAX_GENERATED_STATIC_SECRET_LENGTH) {
    throw new Error(`generate.length must be an integer between 1 and ${MAX_GENERATED_STATIC_SECRET_LENGTH}`);
  }
  if (recipe.charset !== 'hex' && recipe.charset !== 'base64url' && recipe.charset !== 'custom') {
    throw new Error('generate.charset must be hex, base64url, or custom');
  }
  if (recipe.exclude_lookalikes !== undefined && typeof recipe.exclude_lookalikes !== 'boolean') {
    throw new Error('generate.exclude_lookalikes must be a boolean');
  }
  if (recipe.charset !== 'custom') {
    if (GROUPS.some((group) => recipe[group] !== undefined)) {
      throw new Error('generate character groups require the custom charset');
    }
    if (recipe.exclude_lookalikes === true) throw new Error('generate look-alike exclusion requires the custom charset');
    return { length: recipe.length, charset: recipe.charset };
  }

  const groups = Object.fromEntries(GROUPS.map((group) => {
    const enabled = recipe[group] ?? false;
    if (typeof enabled !== 'boolean') throw new Error(`generate.${group} must be a boolean`);
    return [group, enabled];
  })) as Pick<StaticSecretGenerationRecipe, (typeof GROUPS)[number]>;
  if (!GROUPS.some((group) => groups[group])) throw new Error('generate must enable at least one character group');
  return {
    length: recipe.length,
    charset: 'custom',
    ...groups,
    ...(recipe.exclude_lookalikes === true ? { exclude_lookalikes: true } : {}),
  };
}

export function isStaticSecretGenerationRecipeValid(value: unknown): value is StaticSecretGenerationRecipe {
  try {
    normalizeStaticSecretGenerationRecipe(value);
    return true;
  } catch {
    return false;
  }
}

function alphabetFor(recipe: StaticSecretGenerationRecipe): string {
  if (recipe.charset === 'hex') return '0123456789abcdef';
  if (recipe.charset === 'base64url') return 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  let alphabet = GROUPS.filter((group) => recipe[group]).map((group) => GROUP_ALPHABETS[group]).join('');
  if (recipe.exclude_lookalikes) alphabet = [...alphabet].filter((character) => !LOOKALIKE_CHARACTERS.has(character)).join('');
  if (!alphabet) throw new Error('generate has no usable characters');
  return alphabet;
}

export function generateStaticSecret(input: unknown): string {
  const recipe = normalizeStaticSecretGenerationRecipe(input);
  const alphabet = alphabetFor(recipe);
  const rejectionLimit = 256 - (256 % alphabet.length);
  const random = new Uint8Array(Math.min(4096, Math.max(32, recipe.length * 2)));
  const characters: string[] = [];
  try {
    while (characters.length < recipe.length) {
      crypto.getRandomValues(random);
      for (const sample of random) {
        if (sample >= rejectionLimit) continue;
        characters.push(alphabet[sample % alphabet.length]);
        if (characters.length === recipe.length) break;
      }
    }
    return characters.join('');
  } finally {
    random.fill(0);
  }
}
