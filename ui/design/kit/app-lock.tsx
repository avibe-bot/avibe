// App Lock's lock screen (iOS I7-7A, Desktop D7-7A). It is drawn natively (SwiftUI on iPhone,
// AppKit on Mac) above every window, so it never ships as a Web component: this kit stays as its
// visual spec in product tokens. The setting ships as `components/settings/AppLockSetting`.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Fingerprint, Info, KeyRound, Loader2, ScanFace } from 'lucide-react';

import logo from '@/assets/logo.png';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export type LockDevice = 'iphone' | 'mac';


// ── Lock screen ─────────────────────────────────────────────────────────────

export type LockState =
  | 'ready' // first appearance, and after Cancel (cancel is not an error)
  | 'verifying' // the system Face ID / Touch ID / password sheet is up
  | 'failed' // the system sheet returned a failure
  | 'passcodeOnly' // biometrics not enrolled, not allowed, or unavailable now
  | 'lockout' // too many biometric failures: the system asks for the passcode
  | 'cover'; // App Switcher snapshot / a backgrounded app: brand only, no controls

export type PasscodeReason = 'notEnrolled' | 'denied' | 'unavailable';

export function LockScreen({
  device,
  state = 'ready',
  reason = 'notEnrolled',
  compact = false,
  pending,
  className,
}: {
  device: LockDevice;
  state?: LockState;
  reason?: PasscodeReason;
  /** An independent window (Show, Files, Terminal…): the same lock, smaller. */
  compact?: boolean;
  /** Desktop: an entry that arrived while locked and runs after unlock (Ask Vibey, a link). */
  pending?: 'askVibey' | 'link';
  className?: string;
}) {
  // The native shells read these same keys (appLockCopy.test.ts keeps the iPhone catalog equal).
  const { t } = useTranslation();
  const usesSecret = state === 'passcodeOnly' || state === 'lockout';
  const Icon = usesSecret ? KeyRound : device === 'iphone' ? ScanFace : Fingerprint;
  const action = usesSecret
    ? device === 'iphone' ? t('appLock.lock.iphone.unlockPasscode') : t('appLock.lock.unlockPassword')
    : device === 'iphone' ? t('appLock.lock.iphone.unlockFaceID') : t('appLock.lock.unlockTouchID');

  let status: ReactNode = null;
  if (state === 'failed') {
    status = <p role="alert" className="text-[13px] text-destructive-ink">{t('appLock.lock.verificationFailed')}</p>;
  } else if (state === 'lockout') {
    status = <p className="text-[13px] text-muted">{device === 'iphone' ? t('appLock.lock.iphone.faceIDLockout') : t('appLock.lock.macLockout')}</p>;
  } else if (state === 'passcodeOnly') {
    const text = device === 'iphone'
      ? reason === 'denied' ? t('appLock.lock.iphone.faceIDDenied') : t('appLock.lock.iphone.faceIDNotEnrolled')
      : reason === 'notEnrolled' ? t('appLock.lock.macNotEnrolled') : t('appLock.lock.macUnavailable');
    status = <p className="text-[13px] text-muted">{text}</p>;
  }

  if (state === 'cover') {
    return (
      <div className={cn('flex h-full w-full items-center justify-center bg-background', className)}>
        <img src={logo} alt="Avibe" className={compact ? 'size-12' : 'size-16'} />
      </div>
    );
  }

  return (
    <div className={cn('flex h-full w-full flex-col items-center justify-center bg-background px-8 text-foreground page-glow-console', className)}>
      <div className={cn('flex w-full flex-col items-center text-center', compact ? 'max-w-[300px] gap-4' : 'max-w-[340px] gap-5')}>
        <img src={logo} alt="" className={compact ? 'size-12' : 'size-16'} />
        <div className="flex flex-col gap-1.5">
          <h1 className={cn('font-semibold tracking-tight', compact ? 'text-[17px]' : 'text-[22px]')}>{t('appLock.lock.lockedTitle')}</h1>
          {compact && <p className="text-[12px] text-muted">{t('appLock.lock.restoreAllWindows')}</p>}
        </div>
        {pending && <PendingNote kind={pending} />}
        <Button
          variant={compact ? 'outline' : 'brand'}
          size={compact ? 'default' : 'lg'}
          disabled={state === 'verifying'}
          className={cn('w-full', compact ? '' : 'rounded-full')}
        >
          {state === 'verifying' ? <Loader2 className="size-4 animate-spin" /> : <Icon className="size-[18px]" />}
          {state === 'verifying' ? t('appLock.lock.verifying') : action}
        </Button>
        <div className="min-h-[20px]">{status}</div>
      </div>
    </div>
  );
}

function PendingNote({ kind }: { kind: 'askVibey' | 'link' }) {
  const { t } = useTranslation();
  // Says what is waiting, never its content: the selection itself stays hidden until unlock.
  return (
    <div className="flex w-full items-start gap-2.5 rounded-lg border border-edge bg-field px-3.5 py-2.5 text-left text-[12px] leading-relaxed text-muted">
      <Info className="mt-[2px] size-[14px] shrink-0" />
      <span>{kind === 'askVibey' ? t('appLock.lock.pendingContent') : t('appLock.lock.pendingLink')}</span>
    </div>
  );
}
