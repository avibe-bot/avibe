import { useTranslation } from 'react-i18next';

// The Suspense fallback for a lazily-loaded app body. Its own module because
// `registry.tsx` is a data module — a component defined next to the registry
// object can never be hot-replaced, so every app body would need a full reload
// to pick up a change here.
export const AppBodyLoading: React.FC = () => {
  const { t } = useTranslation();
  return <div className="grid h-full w-full place-items-center bg-surface text-[12px] text-muted">{t('common.loading')}</div>;
};
