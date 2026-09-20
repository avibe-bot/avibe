import * as React from 'react';
import { Check } from 'lucide-react';
import clsx from 'clsx';

import { Button } from '../ui/button';

// Quick-reply buttons under an agent message — the workbench counterpart of the
// IM channels' quick replies (parsed from the same trailing ``---\n[label]…``
// block; see core/reply_enhancer). Clicking one sends its label as a user
// message and LOCKS the group: the chosen button highlights (mint + ✓), the rest
// grey out and stop responding. ``chosen`` is the answer recorded on THIS agent
// message (``content.quick_reply_chosen``) — the single source of truth, so the
// locked state survives reload and is immune to how the user reply is queued or
// merged; a local click also locks instantly. There is no "only the latest
// message is clickable" gate — an older unanswered group stays clickable
// (deferred by product; see docs/plans/workbench-quick-replies.md).
export const QuickReplies: React.FC<{
  options: string[];
  chosen?: string | null;
  // Read-only transcript (an archived session): the group renders exactly as
  // recorded — options, and the ✓ on the chosen one — but is locked outright.
  // The lock already existed for "answered"; a session that can never accept
  // another message is the same non-interactive state, so it reuses it instead
  // of hiding the group and losing part of the transcript.
  readOnly?: boolean;
  onChoose: (choice: string) => boolean | void | Promise<boolean | void>;
}> = ({ options, chosen, readOnly = false, onChoose }) => {
  const [clicked, setClicked] = React.useState<string | null>(null);
  const selected = clicked ?? chosen ?? null;
  const locked = readOnly || selected !== null;

  // Once the authoritative answer arrives (``chosen`` loaded from the agent
  // message), drop the optimistic lock and let ``chosen`` own the displayed state
  // so the two never diverge.
  React.useEffect(() => {
    if (chosen) setClicked(null);
  }, [chosen]);

  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {options.map((opt, i) => {
        const isChosen = locked && opt === selected;
        return (
          <Button
            key={`${i}-${opt}`}
            type="button"
            variant="secondary"
            size="sm"
            disabled={locked}
            aria-pressed={isChosen}
            onClick={() => {
              if (locked) return;
              setClicked(opt); // optimistic lock
              // …but if the send never started (network/403), unlock so the user
              // can retry — there's no other retry path until reload.
              void Promise.resolve(onChoose(opt)).then((ok) => {
                if (ok === false) setClicked(null);
              });
            }}
            // Chosen stays full-strength (override the base disabled fade) and
            // goes mint; the others ride the base ``disabled:opacity-50`` to grey.
            className={clsx(
              'h-auto gap-1.5 whitespace-normal rounded-lg px-3 py-1.5 text-[13px] font-normal',
              isChosen && 'border-mint/55 bg-mint/15 text-mint-ink hover:bg-mint/15 disabled:opacity-100',
            )}
          >
            {isChosen && <Check className="size-3.5" />}
            {opt}
          </Button>
        );
      })}
    </div>
  );
};
