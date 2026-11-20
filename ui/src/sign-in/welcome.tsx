import { Sparkles } from 'lucide-react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import heroMascot from '../../../assets/mascot/cloud-tuanzi-v2.png';
import { cn } from '@/lib/utils';

/**
 * The first sign-in page's body both apps draw: Vibey's hero, the title, the app's own subtitle,
 * the ways in (`children`) and the footer. The iPhone draws it under its brand row on the cloud
 * page (ui/design: mobile-app/passkey/passkey-link); the Desktop draws it `compact`, a smaller
 * hero for a window shorter than a phone, with no brand row (ui/design: desktop/sign-in/start).
 */
export function SignInWelcome({
  subtitle,
  compact = false,
  children,
}: {
  /** Two lines: each app says what it adds. */
  subtitle: readonly [string, string];
  compact?: boolean;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className={cn('relative flex flex-1 flex-col items-center justify-center', compact ? 'pb-2' : 'py-8')}>
        <div className={cn('relative grid place-items-center', compact ? 'mb-6 size-48' : 'mb-8 size-64')}>
          <div className="absolute inset-0 rounded-full border border-edge bg-surface/30 shadow-elevation-0" />
          <div className={cn('absolute rounded-full border border-surface/70 bg-surface/30', compact ? 'inset-5' : 'inset-6')} />
          <img
            src={heroMascot}
            alt={t('mobileApp.welcome.mascotAlt')}
            className={cn(
              'relative object-contain motion-safe:animate-[mobile-mascot-arrive_var(--motion-spring-smooth-duration)_var(--motion-spring-smooth)_both]',
              compact ? 'size-40' : 'size-52',
            )}
          />
          <span
            className={cn(
              'absolute -right-1 grid rotate-12 place-items-center rounded-2xl border border-edge bg-surface shadow-elevation-0',
              compact ? 'top-6 size-11' : 'top-9 size-12',
            )}
          >
            <Sparkles className={cn('text-mint-ink', compact ? 'size-4' : 'size-5')} />
          </span>
          <span
            className={cn(
              'absolute -left-1 rounded-full border border-edge bg-surface text-sm shadow-elevation-0',
              compact ? 'bottom-3 px-3.5 py-1.5' : 'bottom-5 px-4 py-2',
            )}
          >
            {t('mobileApp.welcome.greeting')}
          </span>
        </div>
        <h1 className={cn('text-center font-semibold', compact ? 'text-3xl' : 'text-4xl', 'leading-tight')}>
          {t('mobileApp.welcome.titleLine1')}
          <br />
          {t('mobileApp.welcome.titleLine2')}
        </h1>
        <p className={cn('max-w-xs text-center text-sm text-muted', compact ? 'mt-3 leading-6' : 'mt-5 leading-7')}>
          {subtitle[0]}
          <br />
          {subtitle[1]}
        </p>
      </div>
      <div className={cn('flex flex-col pb-3', compact ? 'gap-3' : 'gap-4')}>
        {children}
        <p className="text-center text-xs leading-relaxed text-muted">
          {t('mobileApp.welcome.footerLine1')}
          <br />
          {t('mobileApp.welcome.footerLine2')}
        </p>
      </div>
    </>
  );
}
