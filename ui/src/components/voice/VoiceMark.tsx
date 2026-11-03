import { Mic } from 'lucide-react';
import { useTranslation } from 'react-i18next';

/** The small mark a dictated message carries (ui/design: mobile/main-bot-voice/i-conversation):
 *  under its bubble in the conversation, and in the toast that says it was sent. */
export function VoiceMark() {
  const { t } = useTranslation();
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-mint-soft px-1.5 py-0.5 text-2xs font-semibold text-mint-ink">
      <Mic className="size-3" aria-hidden="true" />
      {t('voice.mark')}
    </span>
  );
}
