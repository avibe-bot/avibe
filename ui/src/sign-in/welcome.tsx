import { Sparkles } from 'lucide-react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import heroMascot from '../../../assets/mascot/cloud-tuanzi-v2.png';
import { cn } from '@/lib/utils';

/**
 * The first sign-in page's body both apps draw: Vibey's hero, the title, the app's own subtitle,
 * the ways in (`children`) and the footer. The iPhone draws it under its brand row on the cloud
 * page (ui/design: mobile-app/passkey/passkey-link); the Desktop draws it `compact`, with a
 * hero and spacing that adapt to window height, with no brand row (ui/design: desktop/sign-in/start).
 */
export function SignInWelcome({
  subtitle,
  compact = false,
  children,
}: {
  /** Desktop uses one sentence; iOS keeps its two explicit lines. */
  subtitle: string | readonly [string, string];
  compact?: boolean;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className={cn('relative flex flex-1 flex-col items-center justify-center', compact ? '' : 'py-8')}>
        <div className={cn('relative grid place-items-center', compact ? 'mb-8 size-48 welcome-short:mb-5 welcome-short:size-32 welcome-tall:mb-10 welcome-tall:size-64' : 'mb-8 size-64')}>
          <div className="absolute inset-0 rounded-full border border-edge bg-surface/30 shadow-elevation-0" />
          <div className={cn('absolute rounded-full border border-surface/70 bg-surface/30', compact ? 'inset-5 welcome-short:inset-3 welcome-tall:inset-6' : 'inset-6')} />
          <img
            src={heroMascot}
            alt={t('mobileApp.welcome.mascotAlt')}
            className={cn(
              'relative object-contain motion-safe:animate-[mobile-mascot-arrive_var(--motion-spring-smooth-duration)_var(--motion-spring-smooth)_both]',
              compact ? 'size-40 welcome-short:size-28 welcome-tall:size-52' : 'size-52',
            )}
          />
          <span
            className={cn(
              'absolute -right-1 grid rotate-12 place-items-center rounded-2xl border border-edge bg-surface shadow-elevation-0',
              compact ? 'top-6 size-11 welcome-short:top-3 welcome-short:size-9 welcome-tall:top-9 welcome-tall:size-12' : 'top-9 size-12',
            )}
          >
            <Sparkles className={cn('text-mint-ink', compact ? 'size-4 welcome-short:size-3.5 welcome-tall:size-5' : 'size-5')} />
          </span>
          <span
            className={cn(
              'absolute -left-1 rounded-full border border-edge bg-surface text-sm shadow-elevation-0',
              compact ? 'bottom-3 px-3.5 py-1.5 welcome-short:bottom-0 welcome-short:whitespace-nowrap welcome-short:px-3 welcome-short:py-1 welcome-tall:bottom-5 welcome-tall:px-4 welcome-tall:py-2' : 'bottom-5 px-4 py-2',
            )}
          >
            {t('mobileApp.welcome.greeting')}
          </span>
        </div>
        <h1 className={cn('text-center font-semibold', compact ? 'text-3xl welcome-short:text-2xl welcome-tall:text-4xl' : 'text-4xl', 'leading-tight')}>
          {t('mobileApp.welcome.titleLine1')}
          <br />
          {t('mobileApp.welcome.titleLine2')}
        </h1>
        <p className={cn('text-center text-sm text-muted', compact ? 'mt-3 w-max max-w-none whitespace-nowrap leading-6 welcome-short:mt-2 welcome-tall:mt-4 welcome-tall:leading-7' : 'mt-5 max-w-xs leading-7')}>
          {typeof subtitle === 'string' ? subtitle : <>{subtitle[0]}<br />{subtitle[1]}</>}
        </p>
      </div>
      <div className={cn('flex flex-col', compact ? 'mt-10 gap-4 welcome-short:mt-6 welcome-short:gap-3 welcome-tall:mt-12 welcome-tall:gap-5' : 'pb-3 gap-4')}>
        {children}
        {!compact && <p className="text-center text-xs leading-relaxed text-muted">
          {t('mobileApp.welcome.footerLine1')}
          <br />
          {t('mobileApp.welcome.footerLine2')}
        </p>}
      </div>
    </>
  );
}
