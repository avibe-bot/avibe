// The one line Avibe says about a Claude subscription held by Model Hub.
//
// Owner decision (2026-11-11, item 2): Model Hub holds a Claude subscription by
// default, and the account-restriction warning that used to stand in front of that
// choice becomes one quiet line with a way to read more. It says what the terms
// cover and leaves the reading to the person.
import { useTranslation } from 'react-i18next';

const DOCS = {
  en: 'https://docs.avibe.bot/concepts/model-hub#add-a-subscription',
  zh: 'https://docs.avibe.bot/zh/concepts/model-hub#添加订阅',
} as const;

export function SubscriptionTermsNote({ className }: { className?: string }) {
  const { t, i18n } = useTranslation();
  const href = i18n.language?.startsWith('zh') ? DOCS.zh : DOCS.en;
  return (
    <p className={className ?? 'model-hub-add-sub-hint text-[12px] leading-relaxed text-muted'}>
      <span>{t('settings.models.addSub.tos.claude')}</span>
      <a href={href} target="_blank" rel="noopener noreferrer" className="ml-1 underline underline-offset-2 hover:text-foreground">
        {t('settings.models.addSub.tos.learnMore')}
      </a>
    </p>
  );
}
