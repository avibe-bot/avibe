// The two field primitives the page's credential dialogs share: a label (plain or
// mono/uppercase for machine-shaped values) and an icon-prefixed input wrapper.
//
// Promoted here from AddApiKeyDialog when the key-replacement dialog needed the
// same 「新的 API Key」 field: two dialogs asking for the same credential must look
// like the same field, and the alternative — a second pair of local copies — is
// how the two drift by a pixel and a font weight.
import * as React from 'react';

import { cn } from '@/lib/utils';

export const FieldLabel: React.FC<{ mono?: boolean; children: React.ReactNode }> = ({ mono, children }) => (
  <label
    className={cn(
      'text-muted',
      mono
        ? 'font-mono text-[11px] font-medium uppercase tracking-wide'
        : 'text-[12px] font-semibold text-foreground',
    )}
  >
    {children}
  </label>
);

export const IconField: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}> = ({ icon: Icon, children }) => (
  <div className="relative">
    <Icon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
    {children}
  </div>
);
