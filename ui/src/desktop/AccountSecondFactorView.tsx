import { useTranslation } from 'react-i18next';
import { ShieldCheck } from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Label } from '../components/ui/label';
import type { DesktopClientDirectResult } from './bridge';

export interface AccountSecondFactorViewProps {
  enroll: boolean;
  methods: ('passkey' | 'totp')[];
  method: 'totp' | 'recovery';
  code: string;
  busy: boolean;
  material: Extract<DesktopClientDirectResult, { type: 'totp_enrollment' }> | null;
  errorMessage: string | null;
  onCodeChange: (value: string) => void;
  onMethodChange: (method: 'totp' | 'recovery') => void;
  onVerify: () => void;
  onEnroll: () => void;
  onPasskey: () => void;
  onLocal: () => void;
}
/** ui/design: desktop/instances/two-factor-*, recovery-*. Transient material stays container-owned. */
export function AccountSecondFactorView({ enroll, methods, method, code, busy, material, errorMessage,
  onCodeChange, onMethodChange, onVerify, onEnroll, onPasskey, onLocal }: AccountSecondFactorViewProps) {
  const { t } = useTranslation();
  return <div data-account-factor className="mx-auto my-auto w-full max-w-md shrink-0 py-6">
    <ShieldCheck className="mx-auto mb-5 size-12 text-mint-ink" />
    <h1 className="text-center text-2xl font-semibold tracking-tight">{t(enroll ? 'desktopClient.enrollFactor' : 'desktopClient.verifyFactor')}</h1>
    <p className="mt-3 text-center text-sm leading-6 text-muted">{t(enroll ? 'desktopClient.enrollFactorBody' : 'desktopClient.verifyFactorBody')}</p>
    <div className="mt-6 rounded-3xl border border-edge bg-surface p-6">
      {material && <div className="mb-5 flex flex-wrap items-start gap-5">
        {/* Render the URI ourselves, never inject backend SVG. */}
        <QRCodeSVG value={material.uri} size={160} marginSize={2} />
        <div className="min-w-0 flex-1"><p className="text-sm text-muted">{t('desktopClient.setupKey')}</p><p className="mt-2 break-all font-mono text-sm select-text">{material.secret}</p></div>
      </div>}
      {enroll && !material ? <Button variant="brand" className="w-full" disabled={busy} onClick={onEnroll}>{t('desktopClient.addAuthenticator')}</Button> : <>
        {!enroll && methods.includes('passkey') && <Button variant="outline" className="mb-5 w-full" disabled={busy} onClick={onPasskey}>{t('desktopClient.usePasskey')}</Button>}
        {(enroll || methods.includes('totp') || method === 'recovery') && <form onSubmit={(event) => { event.preventDefault(); onVerify(); }}>
          <Label htmlFor="factor-code">{t(!enroll && method === 'recovery' ? 'desktopClient.recoveryCode' : 'desktopClient.authenticatorCode')}</Label>
          <Input id="factor-code" className="mt-2 h-12" value={code} onChange={(event) => onCodeChange(event.target.value)} autoComplete="one-time-code" inputMode={enroll || method === 'totp' ? 'numeric' : 'text'} disabled={busy} required />
          <Button variant="brand" className="mt-6 w-full rounded-full" disabled={busy || !code.trim()}>{t(enroll ? 'desktopClient.finishFactor' : 'desktopClient.verify')}</Button>
        </form>}
        {!enroll && <Button variant="link" className="mt-3 w-full" disabled={busy} onClick={() => onMethodChange(method === 'totp' ? 'recovery' : 'totp')}>{t(method === 'totp' ? 'desktopClient.useRecovery' : 'desktopClient.useAuthenticator')}</Button>}
      </>}
      {errorMessage && <p role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>}
    </div>
    <div className="mt-4 text-center"><Button variant="ghost" onClick={onLocal}>{t('desktopClient.continueLocal')}</Button></div>
  </div>;
}
