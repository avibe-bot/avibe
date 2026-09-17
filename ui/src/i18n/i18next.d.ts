import type en from './en.json';
import type { TranslationPaths } from './resourceTypes';
import 'i18next';

declare module 'i18next' {
  interface CustomTypeOptions {
    resources: { translation: TranslationPaths<typeof en> };
    // Paths are already expanded in the TYPE representation. The runtime retains
    // its default '.' separator and the original JSON. Native keyPrefix is not
    // supported by this adapter; consumers compose full, finite paths instead.
    keySeparator: never;
  }
}
