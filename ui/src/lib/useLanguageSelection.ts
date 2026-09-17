import { useRef } from 'react';
import { useTranslation } from 'react-i18next';

import { useApi } from '../context/ApiContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { useToast } from '../context/ToastContext';
import { setConfigField } from './configMutations';

export interface LanguageOption {
  code: string;
  label: string;
}

export interface LanguageSelection {
  languages: LanguageOption[];
  current: LanguageOption;
  /** Apply the pick to this browser immediately, then persist it as the instance
   *  language when the caller may manage the instance. There is no Save button,
   *  so a failed save is reported where the user can act on it instead of being
   *  swallowed under a page that claims changes save themselves. The local
   *  change still stands: the pick they can see is the one they made. */
  select: (code: string) => Promise<void>;
}

/**
 * One owner for "which language is this, and what happens when you pick another"
 * — shared by the compact header switcher and the General settings selector, so
 * the two cannot drift on persistence or on which codes exist.
 */
export const useLanguageSelection = (): LanguageSelection => {
  const { i18n, t } = useTranslation();
  const { mutateConfig } = useApi();
  const { capabilities } = useInstanceAuthorization();
  const { showToast } = useToast();
  // Saves run one at a time and only the newest pick is worth writing: flipping
  // through the list must not leave the instance on whichever request happened
  // to answer last, nor complain about a language the user already left.
  const latestRef = useRef<string | null>(null);
  const queueRef = useRef<Promise<void>>(Promise.resolve());

  const codes = Object.keys(i18n.options.resources ?? {});
  const languages = (codes.length ? codes : ['en']).map((code) => ({
    code,
    label: t(`language.${code}`, { defaultValue: code }),
  }));
  const current = languages.find((lang) => lang.code === i18n.language) || languages[0];

  // Re-applying a failed pick means redoing the whole selection, so this is a
  // plain hoisted function the retry action can call again by name.
  function apply(code: string): Promise<void> {
    void i18n.changeLanguage(code);
    if (!capabilities.can_manage_instance) return Promise.resolve();
    latestRef.current = code;
    queueRef.current = queueRef.current.then(async () => {
      if (latestRef.current !== code) return;
      try {
        await mutateConfig([setConfigField(['language'], code)]);
      } catch {
        if (latestRef.current !== code) return;
        // Translated when it is shown, not when the save was started: by now the
        // interface is already in the language that failed to save.
        showToast(i18n.t('settings.general.languageSaveFailed'), 'error', {
          label: i18n.t('common.retry'),
          onClick: () => { void apply(code); },
        });
      }
    });
    return queueRef.current;
  }

  const select = (code: string): Promise<void> => (
    code === i18n.language ? Promise.resolve() : apply(code)
  );

  return { languages, current, select };
};
