// Shared visual atoms for subscription/backend OAuth flows. Extracted from
// BackendOAuthPanel so both the Settings → Backends panel and the Model Hub
// connect-subscription dialog compose the SAME link / device-code / submit
// rows instead of forking near-duplicate markup (reuse ladder: promote the
// repeated pattern to a shared home).
//
// These are presentation-only and i18n-agnostic: callers pass already-
// translated strings and own the copy handler (which is where the defensive
// stopPropagation for the iOS radio-bounce bug lives — see BackendOAuthPanel).
import * as React from 'react';
import { Copy, ExternalLink } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

type CopyProps = {
  /** Receives the click event so the caller can stop propagation. */
  onCopy: (e: React.MouseEvent) => void;
  copyLabel: string;
};

/** Auth URL as a cyan link chip + a copy button (remote/phone operation). */
export const OAuthLinkRow: React.FC<{ url: string } & CopyProps> = ({ url, onCopy, copyLabel }) => (
  <div className="flex flex-wrap items-center gap-2">
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex max-w-full items-center gap-1.5 break-all rounded-md bg-cyan-soft/40 px-2 py-1 font-mono text-[12px] text-cyan transition-colors hover:bg-cyan-soft hover:text-cyan"
    >
      <ExternalLink className="size-3 shrink-0" />
      <span className="break-all">{url}</span>
    </a>
    <Button type="button" variant="secondary" size="xs" onClick={onCopy}>
      <Copy className="size-3" />
      {copyLabel}
    </Button>
  </div>
);

/** Device code as a spaced mono chip + a copy button. */
export const OAuthDeviceCodeRow: React.FC<{ code: string } & CopyProps> = ({ code, onCopy, copyLabel }) => (
  <div className="flex flex-wrap items-center gap-2">
    <code className="rounded-md bg-cyan-soft/40 px-2.5 py-1 font-mono text-[14px] font-semibold tracking-[0.18em] text-cyan">
      {code}
    </code>
    <Button type="button" variant="secondary" size="xs" onClick={onCopy}>
      <Copy className="size-3" />
      {copyLabel}
    </Button>
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
}> = ({ id, value, onChange, onSubmit, submitting, placeholder, submitLabel, submittingLabel }) => (
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
);
