import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { SIGN_IN_PROVIDERS, type SignInProvider } from './types';

export type { SignInProvider } from './types';

// The marks avibe.bot's login card draws (avibe-bot-backend app/login/login-form.tsx), and Apple's.
// Brand colours are part of the marks, not theme colours.
function GoogleMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={className}>
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.76h3.56c2.08-1.92 3.28-4.74 3.28-8.09Z" />
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.56-2.76c-.99.66-2.25 1.06-3.72 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23Z" />
      <path fill="#FBBC05" d="M5.84 14.11A6.6 6.6 0 0 1 5.5 12c0-.74.13-1.45.34-2.11V7.05H2.18A11 11 0 0 0 1 12c0 1.78.42 3.46 1.18 4.95l3.66-2.84Z" />
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.07.56 4.21 1.64l3.15-3.15C17.45 2.1 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.05l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38Z" />
    </svg>
  );
}

function GitHubMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={cn('fill-current', className)}>
      <path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.92.58.1.79-.25.79-.55 0-.27-.01-1-.02-1.96-3.2.7-3.87-1.54-3.87-1.54-.52-1.32-1.27-1.67-1.27-1.67-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.76 2.69 1.25 3.34.96.1-.74.4-1.25.72-1.54-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.28 1.18-3.09-.12-.29-.51-1.46.11-3.05 0 0 .97-.31 3.18 1.18.92-.26 1.91-.39 2.89-.39.98 0 1.97.13 2.89.39 2.21-1.49 3.18-1.18 3.18-1.18.62 1.59.23 2.76.11 3.05.74.81 1.18 1.83 1.18 3.09 0 4.42-2.7 5.39-5.27 5.68.41.36.78 1.06.78 2.14 0 1.55-.01 2.8-.01 3.18 0 .31.21.66.8.55 4.56-1.53 7.85-5.84 7.85-10.92C23.5 5.65 18.35.5 12 .5Z" />
    </svg>
  );
}

function AppleMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={cn('fill-current', className)}>
      <path d="M16.37 12.62c-.03-2.67 2.18-3.95 2.28-4.01-1.24-1.82-3.18-2.07-3.87-2.1-1.65-.17-3.21.97-4.05.97-.84 0-2.12-.95-3.49-.92-1.8.03-3.45 1.04-4.38 2.65-1.87 3.24-.48 8.04 1.34 10.67.89 1.29 1.95 2.73 3.34 2.68 1.34-.05 1.85-.87 3.47-.87 1.62 0 2.08.87 3.49.84 1.44-.02 2.36-1.31 3.24-2.6 1.02-1.49 1.44-2.94 1.47-3.01-.03-.01-2.81-1.08-2.84-4.3ZM13.72 4.79c.74-.9 1.24-2.14 1.1-3.39-1.07.04-2.36.71-3.12 1.6-.69.79-1.29 2.06-1.13 3.28 1.19.09 2.41-.61 3.15-1.49Z" />
    </svg>
  );
}

const MARKS: Record<SignInProvider, (props: { className?: string }) => ReactNode> = {
  google: GoogleMark,
  github: GitHubMark,
  apple: AppleMark,
};

const LABEL_KEYS = {
  google: 'mobileApp.welcome.continueWithGoogle',
  github: 'mobileApp.welcome.continueWithGitHub',
  apple: 'mobileApp.welcome.continueWithApple',
} as const;

/**
 * Google, GitHub and Apple below the email action (owner, 2026-11-18: layout B): one row of three
 * round marks, 48 pt each, labelled for VoiceOver with avibe.bot's wording and Apple's own title.
 * Google and GitHub use Google's sign-in button colours (the `provider-button` tokens, as on
 * avibe.bot's login card); Sign in with Apple keeps Apple's styles: white with a black outline on
 * light, white on dark (the `apple-sign-in` tokens). Design: mobile-app/welcome/providers-row-*.
 */
export function SignInProviderRow({
  providers,
  disabled,
  onProvider,
}: {
  /** The ones avibe.bot offers now; they keep the row's own order. */
  providers: readonly SignInProvider[];
  disabled?: boolean;
  onProvider: (provider: SignInProvider) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex justify-center gap-4">
      {SIGN_IN_PROVIDERS.filter((provider) => providers.includes(provider)).map((provider) => {
        const Mark = MARKS[provider];
        const apple = provider === 'apple';
        return (
          <Button
            key={provider}
            type="button"
            disabled={disabled}
            aria-label={t(LABEL_KEYS[provider])}
            className={cn(
              'size-12 rounded-full border p-0',
              apple
                ? 'border-apple-sign-in-border bg-apple-sign-in text-apple-sign-in-foreground hover:bg-apple-sign-in/90 active:bg-apple-sign-in/90'
                : 'border-provider-button-border bg-provider-button text-provider-button-foreground hover:bg-provider-button/90 active:bg-provider-button/90',
            )}
            onClick={() => onProvider(provider)}
          >
            <Mark className="size-5" />
          </Button>
        );
      })}
    </div>
  );
}

/** The divider between email and the other accounts: avibe.bot's 「或」. */
export function SignInDivider() {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-3 text-xs text-muted">
      <span className="h-px flex-1 bg-border-strong" />
      <span>{t('mobileApp.welcome.or')}</span>
      <span className="h-px flex-1 bg-border-strong" />
    </div>
  );
}
