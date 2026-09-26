import { ChevronRight } from "lucide-react";
import * as React from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Field } from './dialogFields';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { RouteCandidate } from "./routeChainDraft";
import type { RouteHop, Source } from "./types";

const candidateKey = (hop: RouteHop): string =>
  JSON.stringify([hop.source_id, hop.model_id]);

export const RouteCandidatePopover: React.FC<{
  candidates: RouteCandidate[];
  sources: Source[];
  confirmLabel: string;
  initialHop?: RouteHop;
  label: string;
  onApply: (candidate: RouteCandidate) => void;
  onReturnFocus?: () => void;
  trigger: React.ReactElement;
  width: "route" | "trigger";
  /** Which trigger edge a route-width panel lines up with: a row action sits at
   *  the right of its row, the Add trigger at the left of the tools. */
  align?: "start" | "end";
  /** Opened by another control (a phone's row menu) instead of `trigger`,
   *  which is then only the anchor the panel hangs from. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}> = ({
  candidates,
  sources,
  confirmLabel,
  initialHop,
  label,
  onApply,
  onReturnFocus,
  trigger,
  width,
  align = "end",
  open: controlledOpen,
  onOpenChange,
}) => {
  const { t } = useTranslation();
  const [innerOpen, setInnerOpen] = React.useState(false);
  const controlled = controlledOpen !== undefined;
  const open = controlled ? controlledOpen : innerOpen;
  const setOpen = (next: boolean) => {
    if (!controlled) setInnerOpen(next);
    onOpenChange?.(next);
  };
  const [query, setQuery] = React.useState("");
  const [candidate, setCandidate] = React.useState<RouteCandidate | null>(null);
  const [customModel, setCustomModel] = React.useState('');
  const [customSource, setCustomSource] = React.useState('');
  const [manualOpen, setManualOpen] = React.useState(false);
  const searchRef = React.useRef<HTMLInputElement | null>(null);
  const pendingCandidateRef = React.useRef<RouteCandidate | null>(null);
  const term = query.trim().toLowerCase();
  const matched = React.useMemo(
    () =>
      term
        ? candidates.filter((item) =>
            `${item.source.display_name}\n${item.hop.model_id}`
              .toLowerCase()
              .includes(term),
          )
        : candidates,
    [candidates, term],
  );
  const groups = matched.reduce<
    Array<{ source: Source; items: RouteCandidate[] }>
  >((result, item) => {
    const previous = result.at(-1);
    if (previous?.source.id === item.source.id) previous.items.push(item);
    else result.push({ source: item.source, items: [item] });
    return result;
  }, []);
  const typedSources = sources.filter((source) => source.kind === 'api_key');
  const typedSource = typedSources.find((source) => source.id === customSource);
  const typedCandidate: RouteCandidate | null = customModel && typedSource
    && customModel === customModel.trim()
    && !typedSource.models.some((model) => model.id === customModel && model.retired)
    ? { source: typedSource, hop: { source_id: typedSource.id, model_id: customModel } } : null;
  const selected = customModel ? typedCandidate : candidate;
  const unchanged =
    selected !== null &&
    initialHop !== undefined &&
    candidateKey(selected.hop) === candidateKey(initialHop);

  React.useEffect(() => {
    if (!open) return;
    setCandidate((current) => {
      if (
        current &&
        matched.some(
          (item) => candidateKey(item.hop) === candidateKey(current.hop),
        )
      ) {
        return current;
      }
      return matched[0] ?? null;
    });
  }, [matched, open]);

  const prime = () => {
    setCustomModel('');
    setManualOpen(false);
    setCustomSource(typedSources.find((source) => source.id === initialHop?.source_id)?.id ?? typedSources[0]?.id ?? '');
    setQuery("");
    setCandidate(
      (initialHop
        ? candidates.find(
            (item) => candidateKey(item.hop) === candidateKey(initialHop),
          )
        : null) ??
        candidates[0] ??
        null,
    );
  };
  // A controlled open arrives as a prop, not through Radix's onOpenChange, so
  // it is primed here — before paint, so the panel never shows a stale pick.
  const primeRef = React.useRef(prime);
  React.useLayoutEffect(() => { primeRef.current = prime; });
  const primedOpen = React.useRef(false);
  React.useLayoutEffect(() => {
    if (controlled && controlledOpen && !primedOpen.current) primeRef.current();
    primedOpen.current = Boolean(controlled && controlledOpen);
  }, [controlled, controlledOpen]);

  const chooseCandidate = (next: string) => {
    setCandidate(
      matched.find((item) => candidateKey(item.hop) === next) ?? null,
    );
  };

  return (
    <Popover
      modal
      open={open}
      onOpenChange={(nextOpen) => {
        if (nextOpen) {
          prime();
        } else {
          pendingCandidateRef.current = null;
          setQuery("");
          setCandidate(null);
        }
        setOpen(nextOpen);
      }}
    >
      {controlled
        ? <PopoverAnchor asChild>{trigger}</PopoverAnchor>
        : <PopoverTrigger asChild>{trigger}</PopoverTrigger>}
      {/* This picker is body-portalled inside a scroll-locked dialog. A modal
          popover owns the nested scroll lock, while a bounded downward panel
          stays attached to either the full-width Add trigger or a row action. */}
      <PopoverContent
        side="bottom"
        avoidCollisions={false}
        align={width === "route" ? align : "start"}
        sideOffset={6}
        collisionPadding={16}
        className={cn(
          "model-hub-route-selector flex max-w-[calc(100vw-64px)] flex-col p-0",
          // The disclosure and its fields are bands only where they are drawn:
          // an inventory with nothing to type by hand has neither, and an
          // unopened one has only its toggle. The budget has a term for each,
          // and these are what turn them on.
          typedSources.length > 0 && "model-hub-route-selector--manual",
          manualOpen && "model-hub-route-selector--manual-open",
          // Borrowing the trigger's width contains the panel by construction;
          // a width of its own does not, and this variant hangs off a row
          // action a panel-width from the right edge. The bound lives in CSS
          // next to its vertical twin, because it is the same reading of the
          // same report: what the engine says is on screen for this placement.
          width === "trigger"
            ? "w-[var(--radix-popover-trigger-width)]"
            : "model-hub-route-selector--width-route",
        )}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          searchRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const pendingCandidate = pendingCandidateRef.current;
          pendingCandidateRef.current = null;
          if (pendingCandidate) onApply(pendingCandidate);
          else onReturnFocus?.();
        }}
      >
        <Command
          shouldFilter={false}
          disablePointerSelection
          label={label}
          value={candidate ? candidateKey(candidate.hop) : ""}
          onValueChange={chooseCandidate}
          className="model-hub-route-selector-command min-h-0 bg-transparent"
        >
          <CommandInput
            ref={searchRef}
            value={query}
            onValueChange={setQuery}
            placeholder={t("settings.models.routeDialog.add.search") as string}
          />
          <p
            className="model-hub-route-selector-head model-hub-route-selector-row"
            aria-hidden="true"
          >
            <span>{t("settings.models.routeDialog.add.source")}</span>
            <span>{t("settings.models.routeDialog.add.model")}</span>
          </p>
          <CommandList className="model-hub-route-selector-list">
            {matched.length === 0 && (
              <CommandEmpty>
                {t("settings.models.routeDialog.add.noMatch")}
              </CommandEmpty>
            )}
            {groups.map((group) => (
              // The source is a property of the group, not of its first row.
              // Printed into that row it was the first thing to scroll away,
              // and a reader six models down had a blank column where the name
              // belonged. cmdk already emits one heading per group — the
              // accessible label for this very set — so the name is drawn there
              // instead of duplicated into a cell, and CSS keeps it in view for
              // as long as any of its own models are.
              <CommandGroup
                key={group.source.id}
                heading={group.source.display_name}
                className="model-hub-route-selector-group"
              >
                {group.items.map((item) => (
                  <CommandItem
                    key={candidateKey(item.hop)}
                    value={candidateKey(item.hop)}
                    onSelect={() => { setCustomModel(''); setCandidate(item); }}
                    className="model-hub-route-candidate model-hub-route-selector-row text-foreground"
                  >
                    <span className="model-hub-route-candidate-model truncate font-mono">
                      {item.hop.model_id}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
          {/* The rare path, folded away. Left permanently open it was a third
              fixed band on a bounded panel — with the search, column head and
              confirm foot it took ~197px of a 300px box, and the candidate list,
              the only child that can shrink, was what paid for it. */}
          {typedSources.length > 0 && <div className="model-hub-route-selector-manual shrink-0 border-t border-border">
            <button
              type="button"
              aria-expanded={manualOpen}
              className="model-hub-route-selector-manual-toggle flex w-full items-center gap-1.5"
              onKeyDown={(event) => {
                // cmdk answers Enter on the command root by selecting the
                // highlighted candidate and preventing the default — which,
                // for the button the user is focused on, is its own click.
                if (event.key === 'Enter') event.stopPropagation();
              }}
              onClick={() => {
                const next = !manualOpen;
                setManualOpen(next);
                // Closing drops the draft: a hidden value must never be what the
                // confirm button is acting on.
                if (!next) setCustomModel('');
              }}
            >
              <ChevronRight aria-hidden="true" className={cn('size-3 shrink-0 transition-transform', manualOpen && 'rotate-90')} />
              {t('settings.models.routeDialog.add.manual')}
            </button>
            {manualOpen && <div className="model-hub-route-custom">
              <Field label={t('settings.models.routeDialog.add.source')}>{(id) => <select id={id} value={customSource} onChange={(event) => setCustomSource(event.target.value)}>
                {typedSources.map((source) => <option key={source.id} value={source.id}>{source.display_name}</option>)}
              </select>}</Field>
              <Field label={t('settings.models.routing.exactModel')}>{(id) => <Input id={id} value={customModel} onChange={(event) => setCustomModel(event.target.value)} placeholder={t('settings.models.routing.exactModel')} />}</Field>
            </div>}
          </div>}
          <div className="model-hub-route-selector-foot flex shrink-0 items-center justify-end border-t border-border">
            <Button
              type="button"
              className="model-hub-route-selector-confirm px-4"
              disabled={!selected || unchanged}
              onClick={() => {
                if (!selected || unchanged) return;
                pendingCandidateRef.current = selected;
                setOpen(false);
                setQuery("");
                setCandidate(null);
              }}
            >
              {confirmLabel}
            </Button>
          </div>
        </Command>
      </PopoverContent>
    </Popover>
  );
};
