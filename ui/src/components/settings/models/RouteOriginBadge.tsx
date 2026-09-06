import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { AgentBackend, RouteOrigin } from './types';

type RouteOriginBadgeProps = {
  origin: RouteOrigin;
  backend: AgentBackend;
} & ({ interactive: false; open?: never; onOpenChange?: never } | {
  interactive?: true;
  open: boolean;
  onOpenChange: (open: boolean) => void;
});

export function RouteOriginBadge({ origin, backend, interactive = true, open = false, onOpenChange }: RouteOriginBadgeProps) {
  const { t } = useTranslation();
  const pinned = React.useRef(false);
  const dismissal = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const trigger = React.useRef<HTMLButtonElement>(null);
  const content = React.useRef<HTMLDivElement>(null);
  const key = origin ?? 'unconfigured';
  const label = t(`settings.models.routing.origin.${key}`);
  const className = `model-hub-route-origin model-hub-route-origin--${key}`;
  const cancelDismissal = React.useCallback(() => {
    if (dismissal.current !== null) clearTimeout(dismissal.current);
    dismissal.current = null;
  }, []);
  React.useEffect(() => {
    if (!open) { cancelDismissal(); pinned.current = false; }
  }, [open, cancelDismissal]);
  React.useEffect(() => () => {
    cancelDismissal();
    onOpenChange?.(false);
  }, [cancelDismissal, onOpenChange]);
  if (!interactive) return <span className={className}>{label}</span>;
  const close = () => { cancelDismissal(); pinned.current = false; onOpenChange?.(false); };
  const activate = () => { cancelDismissal(); onOpenChange?.(true); };
  const leave = (event: React.PointerEvent) => {
    if (event.pointerType !== 'mouse' || pinned.current) return;
    const target = event.relatedTarget;
    if (target instanceof Node && (trigger.current?.contains(target) || content.current?.contains(target))) return;
    if (document.activeElement !== trigger.current) {
      cancelDismissal();
      dismissal.current = setTimeout(close, 120);
    }
  };
  return (
    <Popover open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <PopoverTrigger asChild>
        <button ref={trigger} type="button" className={className} aria-label={label}
          onPointerEnter={(event) => { if (event.pointerType === 'mouse') activate(); }}
          onPointerLeave={leave}
          onFocus={activate}
          onBlur={() => { if (!pinned.current) close(); }}
          onClick={(event) => { event.preventDefault(); event.stopPropagation(); cancelDismissal(); pinned.current = !pinned.current; onOpenChange?.(pinned.current); }}
        >{label}</button>
      </PopoverTrigger>
      <PopoverContent ref={content} className="model-hub-origin-help" sideOffset={6}
        onPointerEnter={cancelDismissal}
        onPointerLeave={leave}
        onOpenAutoFocus={(event) => event.preventDefault()}
        onCloseAutoFocus={(event) => event.preventDefault()}
        onEscapeKeyDown={(event) => { event.stopPropagation(); close(); }}
        onClick={(event) => event.stopPropagation()}
      >{t(`settings.models.routing.help.${key}`, { backend: t(`settings.models.backends.${backend}`) })}</PopoverContent>
    </Popover>
  );
}
