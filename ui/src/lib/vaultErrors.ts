import type { TFunction } from 'i18next';
import { errorMessage } from './errorMessage';

/** Use the same stable-code catalog as ApiContext's toast fallback. */
export function vaultErrorMessage(t: TFunction, error: unknown, fallback: string): string {
  const message = errorMessage(error) ?? fallback;
  const code = typeof error === 'object' && error !== null && 'code' in error ? error.code : null;
  return typeof code === 'string' && code ? t(`errors.${code}`, { defaultValue: message }) : message;
}
