import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { AgentBackend, RouteOrigin } from './types';

export function RouteOriginBadge({ origin, backend, interactive = true }: {
  origin: RouteOrigin;
  backend: AgentBackend;
  interactive?: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const pinned = React.useRef(false);
  const dismissal = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const trigger = React.useRef<HTMLButtonElement>(null);
  const content = React.useRef<HTMLDivElement>(null);
  const key = origin ?? 'unconfigured';
  const label = t(`settings.models.routing.origin.${key}`);
  const className = `model-hub-route-origin model-hub-route-origin--${key}`;
  const cancelDismissal = () => { if (dismissal.current) clearTimeout(dismissal.current); };
  React.useEffect(() => () => { if (dismissal.current) clearTimeout(dismissal.current); }, []);
  if (!interactive) return <span className={className}>{label}</span>;
  const close = () => { cancelDismissal(); pinned.current = false; setOpen(false); };
  const leave = (event: React.PointerEvent) => {
    if (event.pointerType !== 'mouse' || pinned.current) return;
    const target = event.relatedTarget;
    if (target instanceof Node && (trigger.current?.contains(target) || content.current?.contains(target))) return;
    if (document.activeElement !== trigger.current) dismissal.current = setTimeout(() => setOpen(false), 120);
  };
  return (
    <Popover open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <PopoverTrigger asChild>
        <button ref={trigger} type="button" className={className} aria-label={label}
          onPointerEnter={(event) => { if (event.pointerType === 'mouse') { cancelDismissal(); setOpen(true); } }}
          onPointerLeave={leave}
          onFocus={() => setOpen(true)}
          onBlur={() => { if (!pinned.current) setOpen(false); }}
          onClick={(event) => { event.preventDefault(); event.stopPropagation(); pinned.current = !pinned.current; setOpen(pinned.current); }}
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
