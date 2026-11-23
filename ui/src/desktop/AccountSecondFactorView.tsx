import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ShieldCheck } from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Label } from '../components/ui/label';
import type { DesktopClientDirectResult } from './bridge';

export interface AccountSecondFactorViewProps {
  code: string;
  busy: boolean;
  material: Extract<DesktopClientDirectResult, { type: 'totp_enrollment' }> | null;
  errorMessage: string | null;
  onCodeChange: (value: string) => void;
  onVerify: () => void;
  onEnroll: () => void;
  onLocal: () => void;
  /** The way out under the card; by default "continue on this computer" (`onLocal`). */
  exitLabel?: string;
}
/** An account that must turn two-step verification on adds an authenticator here, in the app.
 *  The second step itself is the shared sign-in flow's (DesktopSignIn); the recovery codes this
 *  setup returns stay container-owned (AccountApp). ui/design: desktop/instances/enroll-*. */
export function AccountSecondFactorView({ code, busy, material, errorMessage,
  onCodeChange, onVerify, onEnroll, onLocal, exitLabel }: AccountSecondFactorViewProps) {
  const { t } = useTranslation();
  return <div data-account-factor className="mx-auto my-auto w-full max-w-md shrink-0 py-6">
    <ShieldCheck className="mx-auto mb-5 size-12 text-mint-ink" />
    <h1 className="text-center text-2xl font-semibold tracking-tight">{t('desktopAccount.enrollFactor')}</h1>
    <p className="mt-3 text-center text-sm leading-6 text-muted">{t('desktopAccount.enrollFactorBody')}</p>
    <div className="mt-6 rounded-3xl border border-edge bg-surface p-6">
      {material && <div className="mb-5 flex flex-wrap items-start gap-5">
        {/* Render the URI ourselves, never inject backend SVG. */}
        <QRCodeSVG value={material.uri} size={160} marginSize={2} />
        <div className="min-w-0 flex-1"><p className="text-sm text-muted">{t('desktopAccount.setupKey')}</p><p className="mt-2 break-all font-mono text-sm select-text">{material.secret}</p></div>
      </div>}
      {!material ? <Button variant="brand" className="w-full" disabled={busy} onClick={onEnroll}>{t('desktopAccount.addAuthenticator')}</Button>
        : <form onSubmit={(event) => { event.preventDefault(); onVerify(); }}>
          <Label htmlFor="factor-code">{t('desktopAccount.authenticatorCode')}</Label>
          <Input id="factor-code" className="mt-2 h-12" value={code} onChange={(event) => onCodeChange(event.target.value)} autoComplete="one-time-code" inputMode="numeric" disabled={busy} required />
          <Button variant="brand" className="mt-6 w-full rounded-full" disabled={busy || !code.trim()}>{t('desktopAccount.finishFactor')}</Button>
        </form>}
      {errorMessage && <p role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>}
    </div>
    <div className="mt-4 text-center"><Button variant="ghost" onClick={onLocal}>{exitLabel ?? t('desktopAccount.continueLocal')}</Button></div>
  </div>;
}

/** Recovery codes an authenticator setup returned, shown once until the person says they saved them. */
export function RecoveryCodesView({ codes, busy, onSaved, children }: { codes: string[]; busy: boolean; onSaved: () => void; children?: ReactNode }) {
  const { t } = useTranslation();
  return <div className="mx-auto max-w-xl pt-12">
    <div className="mb-7"><h1 className="text-2xl font-semibold tracking-tight">{t('desktopAccount.recoveryTitle')}</h1><p className="mt-3 max-w-xl text-sm leading-6 text-muted">{t('desktopAccount.recoveryBody')}</p></div>
    <ul className="grid grid-cols-2 gap-3 rounded-xl border border-edge bg-field p-5 font-mono text-sm select-text">{codes.map((value, index) => <li key={`${index}:${value}`}>{value}</li>)}</ul>
    <Button variant="brand" className="mt-7" disabled={busy} onClick={onSaved}>{t('desktopAccount.savedCodes')}</Button>
    {children}
  </div>;
}
