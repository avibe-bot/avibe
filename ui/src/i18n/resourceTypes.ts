/** All actual resource paths, retaining containers for returnObjects consumers. */
type ResourcePaths<T> = {
  [K in keyof T & string]: T[K] extends readonly unknown[] ? K
    : T[K] extends object ? K | `${K}.${ResourcePaths<T[K]>}` : K;
}[keyof T & string];

type NestedValue<T, P extends string> = P extends `${infer Head}.${infer Tail}`
  ? Head extends keyof T ? ResourceValue<T[Head], Tail> : never
  : never;

/** i18next traverses objects first, then tries a literal dotted key. Never walk String methods. */
type ResourceValue<T, P extends string> = T extends object
  ? [NestedValue<T, P>] extends [never]
    ? P extends keyof T ? T[P] : never
    : NestedValue<T, P>
  : never;

/** Type-only adapter for nested resources mixed with literal dotted siblings. */
export type TranslationPaths<T> = { [P in ResourcePaths<T>]: ResourceValue<T, P> };
