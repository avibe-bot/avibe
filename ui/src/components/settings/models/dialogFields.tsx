// The credential dialogs' one field primitive: a label, the control, an optional
// icon inset, an optional hint — with the label always NAMING the control.
//
// Promoted here from AddApiKeyDialog when the key-replacement dialog needed the
// same 「新的 API Key」 field: two dialogs asking for the same credential must look
// like the same field, and the alternative — a second pair of local copies — is
// how the two drift by a pixel and a font weight.
//
// It exports ONE concept now, and the id is the reason. As a label and a wrapper
// exported separately, the association was every call site's job, and every call
// site skipped it: a password input reached assistive tech unnamed while a visible
// 「New API key」 label sat directly above it. `Field` mints the id and hands it to
// the control, so the pairing is not something a caller can forget — there is no
// arrangement of these parts that yields an unlabelled input.
import * as React from 'react';

import { cn } from '@/lib/utils';

/** Never exported: a label that does not name a control is the defect above, so
 *  `htmlFor` is required and `Field` is the only thing that can supply it. */
const FieldLabel: React.FC<{ htmlFor: string; mono?: boolean; className?: string; children: React.ReactNode }> = ({
  htmlFor,
  mono,
  className,
  children,
}) => (
  <label
    htmlFor={htmlFor}
    className={cn(
      'text-muted',
      mono
        ? 'font-mono text-[11px] font-medium uppercase tracking-normal'
        : 'text-[12px] font-semibold text-foreground',
      className,
    )}
  >
    {children}
  </label>
);

/** The glyph inset. Local for the same reason: on its own it invites a bare
 *  `<Input>` child, which is exactly the child that arrives unnamed. */
const IconField: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}> = ({ icon: Icon, children }) => (
  <div className="relative">
    <Icon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
    {children}
  </div>
);

/**
 * One labelled field. `children` is a function because the id has to reach the
 * control: taking it as a prop would let a caller pass its own and drift, and
 * cloning the child to inject it would do the same thing invisibly.
 *
 * `icon` is optional because a picker carries its own chevron; `hint` because
 * only two of the four fields explain themselves.
 */
export const Field: React.FC<{
  label: React.ReactNode;
  children: (id: string) => React.ReactNode;
  mono?: boolean;
  icon?: React.ComponentType<{ className?: string }>;
  hint?: React.ReactNode;
  className?: string;
  labelClassName?: string;
  hintClassName?: string;
}> = ({ label, children, mono, icon, hint, className, labelClassName, hintClassName }) => {
  const id = React.useId();
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <FieldLabel htmlFor={id} mono={mono} className={labelClassName}>
        {label}
      </FieldLabel>
      {icon ? <IconField icon={icon}>{children(id)}</IconField> : children(id)}
      {hint ? <p className={cn('text-[12px] leading-relaxed text-muted', hintClassName)}>{hint}</p> : null}
    </div>
  );
};
