import * as React from 'react';
import { cn } from '@/lib/utils';
import { getBackendUiMeta, type BackendId } from '@/lib/agentBackends';
import { BACKEND_BRAND_MARKS } from './backendBrandMarks';

export type { BackendId } from '@/lib/agentBackends';

interface BackendIconProps extends React.HTMLAttributes<HTMLSpanElement> {
  backend: BackendId;
  size?: number;
  /**
   * `block` (default) renders a square tile with mono initials, tinted per-backend.
   * `glyph` renders just the lucide icon in the brand color (no tile, no border).
   * `brand` opts into the authored brand mark, without changing other surfaces.
   */
  variant?: 'block' | 'glyph' | 'brand';
  /** Canvas keeps authored whitespace; mark fits OpenCode's 4:5 bounds in logo wells. */
  brandFit?: 'canvas' | 'mark';
}

export const BackendIcon: React.FC<BackendIconProps> = ({
  backend,
  size = 40,
  variant = 'block',
  brandFit = 'canvas',
  className,
  ...props
}) => {
  const meta = getBackendUiMeta(backend);

  if (variant === 'brand' && Object.prototype.hasOwnProperty.call(BACKEND_BRAND_MARKS, backend)) {
    const mark = BACKEND_BRAND_MARKS[backend as keyof typeof BACKEND_BRAND_MARKS];
    const fittedOpenCode = backend === 'opencode' && brandFit === 'mark';
    return (
      <span
        className={cn('inline-flex shrink-0 items-center justify-center text-foreground', className)}
        style={{ width: size, height: size }}
        {...props}
      >
        <svg width={fittedOpenCode ? '80%' : '100%'} height="100%"
          viewBox={fittedOpenCode ? '4 2 16 20' : '0 0 24 24'}
          aria-hidden="true" focusable="false">
          <path d={mark.path} fill={mark.fill} fillRule={mark.fillRule} />
        </svg>
      </span>
    );
  }

  if (variant === 'glyph' || variant === 'brand') {
    const Icon = meta.Icon;
    return (
      <span
        className={cn('inline-flex shrink-0 items-center justify-center', meta.glyphCls, className)}
        style={{ width: size, height: size }}
        {...props}
      >
        <Icon size={size} />
      </span>
    );
  }

  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-xl border font-mono text-[13px] font-bold tracking-wider',
        meta.blockCls,
        className
      )}
      style={{ width: size, height: size }}
      {...props}
    >
      {meta.initials}
    </span>
  );
};
