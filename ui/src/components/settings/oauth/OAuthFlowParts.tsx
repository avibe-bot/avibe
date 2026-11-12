// Shared visual atoms for subscription/backend OAuth flows. Extracted from
// BackendOAuthPanel so both the Settings → Backends panel and the Model Hub
// connect-subscription dialog compose the SAME link / device-code / submit
// rows instead of forking near-duplicate markup (reuse ladder: promote the
// repeated pattern to a shared home).
//
// Copying is not one of those caller concerns: every caller wanted the same
// clipboard call, the same stopPropagation, and the same confirmation, so the
// row hands the value to the shared `CopyButton` and no caller owns a handler.
import * as React from 'react';
import { ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/copy-button';
import { Input } from '@/components/ui/input';

/**
 * The sign-in page as a button that opens it, and its link to copy for another device.
 *
 * The link is not printed: a provider's authorization URL runs to hundreds of
 * characters of query string, which reads as noise and pushes the step it belongs to
 * off a phone screen. `onUse` reports either use, for a caller that waits on what
 * the person does next.
 */
export const OAuthLinkRow: React.FC<{ url: string; onUse?: (how: 'open' | 'copy') => void }> = ({ url, onUse }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button asChild variant="brand" size="sm">
        <a href={url} target="_blank" rel="noopener noreferrer" onClick={() => onUse?.('open')}>
          <ExternalLink aria-hidden="true" />
          {t('settings.models.oauth.openSignIn')}
        </a>
      </Button>
      <span className="contents" onClickCapture={() => onUse?.('copy')}>
        <CopyButton value={url} label={t('settings.models.oauth.copyLink') as string} variant="outline" size="sm" />
      </span>
    </div>
  );
};

/** Device code as a spaced mono chip + a copy button. */
export const OAuthDeviceCodeRow: React.FC<{ code: string }> = ({ code }) => (
  <div className="flex flex-wrap items-center gap-2">
    <code className="rounded-md bg-cyan-soft/40 px-2.5 py-1 font-mono text-[14px] font-semibold tracking-[0.18em] text-cyan-ink">
      {code}
    </code>
    <CopyButton value={code} />
  </div>
);

/** Mono text input + brand submit button (paste auth code / callback URL). */
export const OAuthSubmitRow: React.FC<{
  id?: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  submitting: boolean;
  placeholder?: string;
  submitLabel: string;
  submittingLabel: string;
  /** Why the last submission was refused; the value stays editable for another try. */
  error?: string;
}> = ({ id, value, onChange, onSubmit, submitting, placeholder, submitLabel, submittingLabel, error }) => {
  const errorId = React.useId();
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex gap-2">
        <Input
          id={id}
          type="text"
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="font-mono"
          disabled={submitting}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
        />
        <Button
          type="button"
          variant="brand"
          size="sm"
          onClick={onSubmit}
          disabled={submitting || !value.trim()}
        >
          {submitting ? submittingLabel : submitLabel}
        </Button>
      </div>
      {error && (
        <p id={errorId} role="alert" className="text-[12px] leading-relaxed text-destructive-ink">
          {error}
        </p>
      )}
    </div>
  );
};
