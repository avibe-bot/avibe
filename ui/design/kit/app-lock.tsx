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

function useCopy() {
  const { i18n } = useTranslation();
  const en = i18n.language?.startsWith('en');
  return (zh: string, english: string) => (en ? english : zh);
}

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
  const t = useCopy();
  const usesSecret = state === 'passcodeOnly' || state === 'lockout';
  const Icon = usesSecret ? KeyRound : device === 'iphone' ? ScanFace : Fingerprint;
  const action = usesSecret
    ? device === 'iphone' ? t('使用设备密码解锁', 'Unlock with Passcode') : t('使用登录密码解锁', 'Unlock with Password')
    : device === 'iphone' ? t('使用面容 ID 解锁', 'Unlock with Face ID') : t('使用触控 ID 解锁', 'Unlock with Touch ID');

  let status: ReactNode = null;
  if (state === 'failed') {
    status = <p role="alert" className="text-[13px] text-destructive-ink">{t('没有验证成功，再试一次。', 'Not verified. Try again.')}</p>;
  } else if (state === 'lockout') {
    status = <p className="text-[13px] text-muted">{device === 'iphone'
      ? t('面容 ID 多次未识别，请用设备密码解锁。', 'Face ID did not recognize you several times. Use your passcode.')
      : t('触控 ID 多次未识别，请用登录密码解锁。', 'Touch ID did not recognize you several times. Use your Mac password.')}</p>;
  } else if (state === 'passcodeOnly') {
    const text = device === 'iphone'
      ? reason === 'denied'
        ? t('Avibe 没有使用面容 ID 的权限。可在 iOS 设置 › Avibe 中允许。', 'Avibe is not allowed to use Face ID. You can allow it in iOS Settings › Avibe.')
        : t('这台 iPhone 没有录入面容 ID。', 'This iPhone has no Face ID set up.')
      : reason === 'notEnrolled'
        ? t('这台 Mac 没有录入指纹。', 'No fingerprints are set up on this Mac.')
        : t('触控 ID 现在不可用，例如笔记本合盖时。', 'Touch ID is not available right now, for example with the lid closed.');
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
          <h1 className={cn('font-semibold tracking-tight', compact ? 'text-[17px]' : 'text-[22px]')}>{t('Avibe 已锁定', 'Avibe is locked')}</h1>
          {compact && <p className="text-[12px] text-muted">{t('解锁后，所有 Avibe 窗口一起恢复。', 'Unlocking restores every Avibe window.')}</p>}
        </div>
        {pending && <PendingNote kind={pending} />}
        <Button
          variant={compact ? 'outline' : 'brand'}
          size={compact ? 'default' : 'lg'}
          disabled={state === 'verifying'}
          className={cn('w-full', compact ? '' : 'rounded-full')}
        >
          {state === 'verifying' ? <Loader2 className="size-4 animate-spin" /> : <Icon className="size-[18px]" />}
          {state === 'verifying' ? t('正在验证…', 'Verifying…') : action}
        </Button>
        <div className="min-h-[20px]">{status}</div>
      </div>
    </div>
  );
}

function PendingNote({ kind }: { kind: 'askVibey' | 'link' }) {
  const t = useCopy();
  // Says what is waiting, never its content: the selection itself stays hidden until unlock.
  return (
    <div className="flex w-full items-start gap-2.5 rounded-lg border border-edge bg-field px-3.5 py-2.5 text-left text-[12px] leading-relaxed text-muted">
      <Info className="mt-[2px] size-[14px] shrink-0" />
      <span>{kind === 'askVibey'
        ? t('收到一段要发给 Vibey 的内容。解锁后会放进输入框，由你确认再发送。', 'Something you sent to Vibey is waiting. After you unlock, it goes into the message box for you to review and send.')
        : t('收到一个 Avibe 链接。解锁后会打开它。', 'An Avibe link is waiting. It opens after you unlock.')}</span>
    </div>
  );
}
