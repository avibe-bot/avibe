import * as React from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
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
  confirmLabel: string;
  initialHop?: RouteHop;
  label: string;
  onApply: (candidate: RouteCandidate) => void;
  onReturnFocus?: () => void;
  trigger: React.ReactElement;
  width: "route" | "trigger";
}> = ({
  candidates,
  confirmLabel,
  initialHop,
  label,
  onApply,
  onReturnFocus,
  trigger,
  width,
}) => {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [candidate, setCandidate] = React.useState<RouteCandidate | null>(null);
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
  const unchanged =
    candidate !== null &&
    initialHop !== undefined &&
    candidateKey(candidate.hop) === candidateKey(initialHop);

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
        } else {
          pendingCandidateRef.current = null;
          setQuery("");
          setCandidate(null);
        }
        setOpen(nextOpen);
      }}
    >
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      {/* This picker is body-portalled inside a scroll-locked dialog. A modal
          popover owns the nested scroll lock, while a bounded downward panel
          stays attached to either the full-width Add trigger or a row action. */}
      <PopoverContent
        side="bottom"
        avoidCollisions={false}
        align={width === "route" ? "end" : "start"}
        sideOffset={6}
        collisionPadding={16}
        className={cn(
          "model-hub-route-selector flex max-w-[calc(100vw-64px)] flex-col p-0",
          width === "trigger"
            ? "w-[var(--radix-popover-trigger-width)]"
            : "w-[420px]",
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
              <CommandGroup
                key={group.source.id}
                heading={group.source.display_name}
                className="model-hub-route-selector-group [&_[cmdk-group-heading]]:sr-only"
              >
                {group.items.map((item, itemIndex) => (
                  <CommandItem
                    key={candidateKey(item.hop)}
                    value={candidateKey(item.hop)}
                    onSelect={() => setCandidate(item)}
                    className="model-hub-route-candidate model-hub-route-selector-row text-foreground"
                  >
                    <span className="model-hub-route-candidate-source truncate">
                      {itemIndex === 0 ? group.source.display_name : ""}
                    </span>
                    <span className="model-hub-route-candidate-model truncate font-mono">
                      {item.hop.model_id}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ))}
          </CommandList>
          <div className="model-hub-route-selector-foot flex shrink-0 items-center justify-end border-t border-border">
            <Button
              type="button"
              className="model-hub-route-selector-confirm px-4"
              disabled={!candidate || unchanged}
              onClick={() => {
                if (!candidate || unchanged) return;
                pendingCandidateRef.current = candidate;
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
