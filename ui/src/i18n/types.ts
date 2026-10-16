import type { ParseKeys, TFunctionReturn } from 'i18next';
import type en from './en.json';
import type { TranslationPaths } from './resourceTypes';

/** Text leaves that resolve without count. Native t also accepts plural families with count. */
type ResourceTextKey = {
  [K in keyof TranslationPaths<typeof en>]: TranslationPaths<typeof en>[K] extends string ? K : never;
}[keyof TranslationPaths<typeof en>];
type TextKey<K extends ParseKeys> = K extends unknown
  ? [TFunctionReturn<'translation', K, { returnObjects: false }>] extends [never] ? never
    : TFunctionReturn<'translation', K, { returnObjects: false }> extends string ? K : never
  : never;
export type TranslationKey = TextKey<Extract<ResourceTextKey, ParseKeys>>;

/** Finite suffixes/prefixes for consumers that intentionally compose a path. */
export type TranslationSuffix<Prefix extends string> =
  Extract<TranslationKey, `${Prefix}.${string}`> extends `${Prefix}.${infer Suffix}` ? Suffix : never;
export type TranslationPrefix<Suffix extends string> =
  Extract<TranslationKey, `${string}.${Suffix}`> extends `${infer Prefix}.${Suffix}` ? Prefix : never;
