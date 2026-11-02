import { useTranslation } from 'react-i18next';

/** Kit copy is draft copy (ui/design/AGENTS.md): written inline in both languages, and moved to
 *  `src/i18n` when the component ships. */
export function useDraft(): (zh: string, en: string) => string {
  const { i18n } = useTranslation();
  return (zh, en) => (i18n.language.startsWith('en') ? en : zh);
}
