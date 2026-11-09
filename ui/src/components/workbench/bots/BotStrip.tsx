import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import { Loader2, MessageCircle, Mic, Pencil, Plus, UserMinus } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../../context/ApiContext';
import { useBotOrderHold, useBots, type BotRuntimeStatus } from '../../../context/BotsContext';
import { useUnsavedChangesActionGuard } from '../../../context/useUnsavedChangesActionGuard';
import { botRole, botSessionPath, type Bot } from '@/lib/bots';
import { messageExcerpt } from '@/lib/messageExcerpt';
import { formatRelativeTime } from '@/lib/relativeTime';
import { Popover, PopoverAnchor, PopoverContent } from '../../ui/popover';
import { BotAvatar } from '../../ui/bot-avatar';
import { PageSectionTitle } from '../PageSectionTitle';
import { PressRing } from '../../voice/ComposerHold';
import { holdIsLive, useBotHold, type HeldBot } from './BotHoldContext';
import { RemoveBotDialog } from './RemoveBotDialog';

// A slot is 50px wide (the 40px avatar, its 2px ring, and room for the name) with
// 4px between slots and 2px of padding at either end: the main Bot, two more,
// and "Add" fill the default 216px sidebar column exactly.
const SLOT_WIDTH = 50;
const SLOT_GAP = 4;
const STRIP_PADDING = 4;
// How long an avatar takes to slide to its new place when the order changes.
const REORDER_MS = 220;

const unreadLabel = (count: number) => (count > 99 ? '99+' : String(count));

type PointerPoint = { x: number; y: number };

// Within this distance a pointer has not moved: pointer and mouse events of one
// position may differ by the fraction a mouse event rounds away.
const STILL_PX = 1;
const samePoint = (a: PointerPoint, b: PointerPoint) =>
  Math.abs(a.x - b.x) <= STILL_PX && Math.abs(a.y - b.y) <= STILL_PX;

// Slides each avatar from its old place to its new one when the order changes
// (docs/plans/bots.md D13), and moves it at once for a user who asks for reduced
// motion. Slots are measured within the strip, so a scroll is not a move.
function useSlideOnReorder(scroller: RefObject<HTMLDivElement | null>, order: string) {
  const placed = useRef<{ order: string; lefts: Map<string, number> } | null>(null);
  useLayoutEffect(() => {
    const root = scroller.current;
    if (!root) return;
    const lefts = new Map<string, number>();
    root.querySelectorAll<HTMLElement>('[data-bot-handle]').forEach((slot) => {
      lefts.set(slot.dataset.botHandle ?? '', slot.offsetLeft);
    });
    const before = placed.current;
    placed.current = { order, lefts };
    if (!before || before.order === order) return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    root.querySelectorAll<HTMLElement>('[data-bot-handle]').forEach((slot) => {
      const from = before.lefts.get(slot.dataset.botHandle ?? '');
      const to = lefts.get(slot.dataset.botHandle ?? '');
      if (from === undefined || to === undefined || from === to || typeof slot.animate !== 'function') return;
      slot.animate([{ transform: `translateX(${from - to}px)` }, { transform: 'none' }], {
        duration: REORDER_MS,
        easing: 'ease-out',
      });
    });
  }, [scroller, order]);
}

// The Bots strip (docs/plans/bots.md D5): the Owner's standing members as a row
// of 40px avatars in the Inbox's old place, the main Bot first, then by when the
// user last wrote to each (D13). A Bot does not move while the pointer is on the
// strip or a card or menu is open over it; it slides there once they are gone.
// The open Bot has
// a mint ring, unread is a cyan count, a running Turn a pulsing mint dot. With
// room to spare "Add" follows the avatars; once they overflow they scroll
// sideways with a fading right edge and "Add" stays pinned at the right. With
// every place for a custom Bot taken there is no "Add" at all (D12; ui/design:
// sidebar/bots/at-limit): the strip's room goes to the Bots, and the Agents page
// says why adding waits.
export const BotStrip: React.FC<{
  /** The Inbox's per-session unread map: a Bot's count is its Session's (D5). */
  unreadBySession: Record<string, number>;
  /** `sidebar`: the desktop sidebar's small section label. `page`: a section of a
   *  phone's tab page (the Workbench tab), titled like the sections beside it. */
  placement?: 'sidebar' | 'page';
}> = ({ unreadBySession, placement = 'sidebar' }) => {
  const { t } = useTranslation();
  const { enabled, bots, atLimit, statusBySession, displayName, openProfile } = useBots();
  const navigate = useNavigate();
  const location = useLocation();
  // The Bot whose removal waits on the confirmation; the strip has one dialog for all its slots.
  const [removing, setRemoving] = useState<Bot | null>(null);
  const [rowWidth, setRowWidth] = useState<number | null>(null);
  const [fade, setFade] = useState(false);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  // The pointer on the strip holds the order (D13): nothing moves under it.
  const [pointerInside, setPointerInside] = useState(false);
  useBotOrderHold(pointerInside);
  // Once a hold has the microphone, the finger moving is the hold's (slide up to cancel), not a
  // scroll of the strip or the page. Before that, a move is the page's: it scrolls, and the hold
  // gives the press back. React's touch listeners are passive, so this one is attached here.
  const { held } = useBotHold();
  const sectionRef = useRef<HTMLElement | null>(null);
  const recordingRef = useRef(false);
  useEffect(() => {
    recordingRef.current = held !== null && held.phase !== 'pressing' && holdIsLive(held.phase);
  }, [held]);
  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    const onTouchMove = (event: TouchEvent) => {
      if (recordingRef.current && event.cancelable) event.preventDefault();
    };
    section.addEventListener('touchmove', onTouchMove, { passive: false });
    return () => section.removeEventListener('touchmove', onTouchMove);
  }, [enabled]);
  useSlideOnReorder(scrollerRef, bots.map((bot) => bot.handle).join(' '));
  // Scrolling takes the hover card away at once, and no card opens again until
  // the pointer itself moves onto an avatar: a scroll slides avatars under a
  // still pointer, and the enter the browser reports for that is not the user
  // pointing at one. `scrollHold` keeps where the pointer stood when the strip
  // scrolled; `scrollEpoch` tells every slot to close.
  const lastPointer = useRef<PointerPoint | null>(null);
  const scrollHold = useRef<PointerPoint | null>(null);
  const [scrollEpoch, setScrollEpoch] = useState(0);
  const notePointer = (event: React.PointerEvent | React.WheelEvent) => {
    const point = { x: event.clientX, y: event.clientY };
    lastPointer.current = point;
    if (scrollHold.current && !samePoint(scrollHold.current, point)) scrollHold.current = null;
  };
  const onStripScroll = () => {
    updateFade();
    if (scrollHold.current) return;
    // A touch scroll leaves no pointer position; the next enter is then admitted.
    scrollHold.current = lastPointer.current ?? { x: Number.NaN, y: Number.NaN };
    setScrollEpoch((epoch) => epoch + 1);
  };
  const admitsHover = useCallback((event: React.MouseEvent) => {
    const held = scrollHold.current;
    if (!held) return true;
    if (samePoint(held, { x: event.clientX, y: event.clientY })) return false;
    scrollHold.current = null;
    return true;
  }, []);

  useLayoutEffect(() => {
    const row = rowRef.current;
    if (!row) return;
    const measure = () => setRowWidth(row.clientWidth);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    return () => observer.disconnect();
  }, [enabled]);

  // Inline while every avatar and "Add" fit; pinned "Add" once they do not.
  const slots = bots.length + (atLimit ? 0 : 1);
  const inline = rowWidth === null || slots * SLOT_WIDTH + (slots - 1) * SLOT_GAP + STRIP_PADDING <= rowWidth;

  const updateFade = useCallback(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    setFade(scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 2);
  }, []);
  useEffect(() => {
    updateFade();
  }, [updateFade, bots.length, inline, rowWidth]);

  if (!enabled) return null;

  const slotProps = (bot: Bot, index: number) => ({
    bot,
    index,
    name: displayName(bot),
    unread: unreadBySession[bot.session_id] ?? 0,
    status: statusBySession[bot.session_id],
    scrollEpoch,
    admitsHover,
    onRemove: setRemoving,
  });

  const addSlot = atLimit ? null : (
    <div className="flex w-[50px] shrink-0 flex-col items-center gap-1">
      <button
        type="button"
        onClick={() => openProfile({ kind: 'create' })}
        aria-label={t('bots.addLabel')}
        className="group rounded-full p-[2px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60"
      >
        <span className="grid size-10 place-items-center rounded-full border border-dashed border-border-strong text-muted transition group-hover:border-mint group-hover:bg-mint-soft group-hover:text-mint-ink">
          <Plus className="size-4" />
        </span>
      </button>
      <span className="text-[11px] leading-tight text-muted">{t('bots.add')}</span>
    </div>
  );

  return (
    <section
      ref={sectionRef}
      aria-label={t('bots.sectionLabel')}
      className={clsx('flex shrink-0 flex-col', placement === 'sidebar' ? 'mb-2.5 gap-1' : 'gap-0')}
      onPointerEnter={() => setPointerInside(true)}
      onPointerMove={notePointer}
      onPointerLeave={() => {
        scrollHold.current = null;
        setPointerInside(false);
      }}
      onWheel={notePointer}
    >
      {placement === 'sidebar' ? (
        <div className="flex h-4 items-center px-1">
          <span className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-muted">
            {t('bots.sectionLabel')}
          </span>
        </div>
      ) : (
        <PageSectionTitle title={t('bots.sectionLabel')} />
      )}
      <div ref={rowRef} data-bot-strip-row="" className="flex min-w-0 items-start">
        <div
          ref={scrollerRef}
          onScroll={onStripScroll}
          className={clsx(
            // `bot-strip-scroller` gives the selected avatar's halo room inside this
            // scroll box, which would otherwise clip it to a hard-edged rectangle.
            'bot-strip-scroller flex min-w-0 items-start gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
            !inline && 'flex-1',
            !inline && fade && 'bot-strip-fade',
          )}
        >
          {bots.map((bot, index) => <BotSlot key={bot.handle} {...slotProps(bot, index)} />)}
          {inline && addSlot}
        </div>
        {!inline && addSlot && <div className="shrink-0 pt-1.5">{addSlot}</div>}
      </div>
      {removing && (
        <RemoveBotDialog
          bot={removing}
          open
          onOpenChange={(open) => {
            if (!open) setRemoving(null);
          }}
          onRemoved={() => {
            if (location.pathname === botSessionPath(removing)) navigate('/');
          }}
        />
      )}
    </section>
  );
};

/** The avatar while it is held, as the tab bar's center is: pressed (a finger, from the gesture's
 *  ring delay, so a tap does not flash), recording, about to cancel, or sending (ui/design:
 *  sidebar/bots/hold-*). */
const holdFaceClass = (held: HeldBot) => {
  switch (held.phase) {
    case 'pressing':
      return held.pointer === 'touch' && held.progress > 0 ? 'scale-95' : undefined;
    case 'listening':
    case 'recording':
      return 'shadow-glow-md-mint ring-2 ring-mint';
    case 'armed':
      return 'opacity-70 ring-2 ring-destructive/70';
    case 'settling':
      return 'ring-2 ring-mint';
    default:
      return undefined;
  }
};

/** What sits on the held avatar: a mouse's press ring while it fills, then the microphone, red when
 *  a release would cancel, and the spinner while the take is sent. */
function HoldMark({ held }: { held: HeldBot }) {
  if (held.phase === 'pressing') {
    if (held.pointer !== 'mouse' || held.progress <= 0) return null;
    return (
      <span className="pointer-events-none absolute inset-0 grid place-items-center" aria-hidden="true">
        <PressRing progress={held.progress} />
      </span>
    );
  }
  if (held.phase === 'settling') {
    return (
      <span className="absolute -bottom-0.5 -right-0.5 grid size-4 place-items-center rounded-full bg-surface text-mint-ink" aria-hidden="true">
        <Loader2 className="size-3 animate-spin" />
      </span>
    );
  }
  if (!holdIsLive(held.phase)) return null;
  return (
    <>
      {held.phase !== 'armed' && (
        <span className="pointer-events-none absolute inset-0.5 animate-ping rounded-full border-2 border-mint/50 motion-reduce:animate-none" aria-hidden="true" />
      )}
      <span
        className={clsx(
          'absolute -bottom-0.5 -right-0.5 grid size-4 place-items-center rounded-full text-primary-foreground',
          held.phase === 'armed' ? 'bg-destructive' : 'bg-mint',
        )}
        aria-hidden="true"
      >
        <Mic className="size-2.5" />
      </span>
    </>
  );
}

const RUNNING_DOT = (
  <span className="absolute -bottom-0.5 -right-0.5 grid size-3.5 place-items-center rounded-full bg-[var(--sidebar-background)]">
    <span className="relative size-2">
      <span className="absolute inset-0 animate-ping rounded-full bg-mint opacity-70 motion-reduce:animate-none" />
      <span className="relative block size-2 rounded-full bg-mint shadow-glow-dot-mint" />
    </span>
  </span>
);

type BotSlotProps = {
  bot: Bot;
  /** Its place in the strip: the open Bot stays in view when its place changes. */
  index: number;
  name: string;
  unread: number;
  status: BotRuntimeStatus | undefined;
  /** Bumped when the strip starts to scroll: an open or pending card closes at once. */
  scrollEpoch: number;
  /** False for an enter that only a scroll caused, with the pointer still where it was. */
  admitsHover: (event: React.MouseEvent) => boolean;
  /** Asks to remove this Bot: the strip confirms first (ui/design: agents/bots-redesign/remove-bot). */
  onRemove: (bot: Bot) => void;
};

const BotSlot: React.FC<BotSlotProps> = ({ bot, index, name, unread, status, scrollEpoch, admitsHover, onRemove }) => {
  const { t } = useTranslation();
  const { held, bind } = useBotHold();
  const heldHere = held?.handle === bot.handle ? held : null;
  const api = useApi();
  const navigate = useNavigate();
  const location = useLocation();
  const { openProfile } = useBots();
  const authorizeRouteAction = useUnsavedChangesActionGuard();
  const path = botSessionPath(bot);
  const selected = location.pathname === path;
  const running = status === 'running';
  // The main Bot's stored role is written for Agents; the card shows its UI copy.
  const role = botRole(bot, t('bots.mainRole'));
  // A card belongs to the scroll epoch it opened in, so a scroll of the strip
  // closes it, and one still waiting on its delay, at once.
  const [hoverEpoch, setHoverEpoch] = useState<number | null>(null);
  const hoverOpen = hoverEpoch === scrollEpoch;
  const setHoverOpen = (open: boolean) => setHoverEpoch(open ? scrollEpoch : null);
  const [menuOpen, setMenuOpen] = useState(false);
  // The latest message as the card shows it: when, and a short plain-text excerpt.
  const [latest, setLatest] = useState<{ at: string; excerpt: string } | null | undefined>(undefined);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const slotRef = useRef<HTMLDivElement | null>(null);
  // The open Bot stays in view on a strip that scrolls sideways, also when it moves.
  useEffect(() => {
    if (selected) slotRef.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [selected, index]);

  const clearTimers = () => {
    if (openTimer.current !== null) window.clearTimeout(openTimer.current);
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    openTimer.current = null;
    closeTimer.current = null;
  };
  useEffect(() => clearTimers, []);

  const queueOpen = (event: React.MouseEvent) => {
    if (menuOpen || held || !admitsHover(event)) return;
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
    if (hoverOpen || openTimer.current !== null) return;
    const epoch = scrollEpoch;
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null;
      setHoverEpoch(epoch);
    }, 350);
  };
  const queueClose = () => {
    if (openTimer.current !== null) window.clearTimeout(openTimer.current);
    openTimer.current = null;
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setHoverOpen(false);
    }, 180);
  };

  // The latest message is read when the card opens, so the strip itself costs
  // nothing per Bot. A Bot that has said nothing yet shows its role alone.
  useEffect(() => {
    if (!hoverOpen) return;
    let live = true;
    api
      .listInbox({ onlySession: bot.session_id, limit: 1, handleError: false })
      .then((feed) => {
        if (!live) return;
        const row = feed.sessions.find((session) => session.session_id === bot.session_id);
        setLatest(row ? { at: row.last_activity_at, excerpt: messageExcerpt(row.preview_text) } : null);
      })
      .catch(() => {
        if (live) setLatest(null);
      });
    return () => {
      live = false;
    };
  }, [api, bot.session_id, hoverOpen]);

  const open = () => {
    clearTimers();
    setHoverOpen(false);
    const authorization = authorizeRouteAction();
    if (!authorization) return;
    authorization.runNavigation(() => navigate(path));
  };

  const label = unread > 0 ? t('bots.slotLabelUnread', { name, count: unread }) : name;
  const showCard = hoverOpen && !menuOpen && !held;
  // Hold to talk (BotHold): the press is the hold's until it is a tap, a scroll, or a right-click.
  const hold = bind?.(bot);
  const voice = heldHere !== null && heldHere.phase !== 'pressing' && heldHere.phase !== 'interrupted';
  // A card or menu open over the strip holds the order, also once the pointer is on it (D13).
  useBotOrderHold(showCard || menuOpen);
  return (
    <Popover
      open={showCard || menuOpen}
      onOpenChange={(next) => {
        if (!next) {
          setHoverOpen(false);
          setMenuOpen(false);
        }
      }}
    >
      <PopoverAnchor asChild>
        <div
          ref={slotRef}
          data-bot-handle={bot.handle}
          className="flex w-[50px] shrink-0 flex-col items-center gap-1"
          onMouseEnter={queueOpen}
          onMouseLeave={queueClose}
        >
          <button
            type="button"
            data-bot-hold-anchor={bot.handle}
            onPointerMove={hold?.onPointerMove}
            onPointerUp={hold?.onPointerUp}
            onPointerCancel={hold?.onPointerCancel}
            onLostPointerCapture={hold?.onLostPointerCapture}
            style={hold?.style}
            onPointerDown={(event) => {
              // A hold starting takes the card away.
              if (event.button === 0) {
                clearTimers();
                setHoverOpen(false);
              }
              hold?.onPointerDown(event, open);
            }}
            onClick={(event) => {
              hold?.onClick(event);
              if (!event.defaultPrevented) open();
            }}
            onContextMenu={(event) => {
              // A long press the hold has (a phone's menu gesture) is not the menu.
              hold?.onContextMenu(event);
              if (event.defaultPrevented) return;
              event.preventDefault();
              clearTimers();
              setHoverOpen(false);
              setMenuOpen(true);
            }}
            aria-label={label}
            aria-current={selected ? 'page' : undefined}
            className="bot-avatar-hover relative rounded-full p-[2px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/60"
          >
            {/* The open Bot's own edge lights up (the shared selected look); a hovered
                one shows a quiet edge in the same place. */}
            <BotAvatar
              avatar={bot.avatar}
              name={name}
              size={40}
              inList
              selected={selected && !voice}
              className={clsx('transition', heldHere && holdFaceClass(heldHere))}
            />
            {unread > 0 && !voice && (
              <span className="absolute -right-1.5 -top-1 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-cyan px-1 font-mono text-[9px] font-bold text-accent-foreground">
                {unreadLabel(unread)}
              </span>
            )}
            {running && !voice && RUNNING_DOT}
            {heldHere && <HoldMark held={heldHere} />}
          </button>
          <span
            className={clsx(
              'w-full truncate text-center text-[11px] leading-tight',
              selected ? 'font-semibold text-foreground' : 'text-muted',
            )}
            title={name}
          >
            {name}
          </span>
        </div>
      </PopoverAnchor>
      {menuOpen ? (
        <PopoverContent
          side="bottom"
          align="start"
          sideOffset={6}
          role="menu"
          aria-label={t('bots.menu.label', { name })}
          data-settings-interaction-owner="true"
          onOpenAutoFocus={(event) => event.preventDefault()}
          className="flex w-[208px] flex-col gap-0.5 rounded-2xl p-1.5"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenuOpen(false);
              openProfile({ kind: 'edit', handle: bot.handle });
            }}
            className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] text-foreground transition hover:bg-foreground/[0.05]"
          >
            <Pencil className="size-4 text-muted" />
            {t('bots.menu.editProfile')}
          </button>
          {!bot.builtin && (
            <>
              <div className="my-1 h-px bg-border" />
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onRemove(bot);
                }}
                className="flex items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] text-destructive-ink transition hover:bg-destructive/[0.08]"
              >
                <UserMinus className="size-4" />
                {t('bots.menu.remove')}
              </button>
            </>
          )}
        </PopoverContent>
      ) : (
        <PopoverContent
          side="right"
          align="start"
          sideOffset={10}
          data-settings-interaction-owner="true"
          onOpenAutoFocus={(event) => event.preventDefault()}
          onMouseEnter={queueOpen}
          onMouseLeave={queueClose}
          className="w-[248px] rounded-2xl p-0"
        >
          <button
            type="button"
            onClick={open}
            className="flex w-full flex-col gap-3 rounded-2xl p-3.5 text-left"
          >
            <span className="flex items-center gap-3">
              <BotAvatar avatar={bot.avatar} name={name} size={44} inList />
              <span className="min-w-0 flex-1 leading-tight">
                <span className="block truncate text-[14px] font-bold text-foreground">{name}</span>
                <span className="mt-0.5 block font-mono text-[11px] text-muted">{t('bots.handle', { handle: bot.handle })}</span>
              </span>
            </span>
            {role && <span className="line-clamp-3 text-[12px] leading-relaxed text-muted">{role}</span>}
            {(running || latest) && (
              <span className="block rounded-lg bg-foreground/[0.04] px-2.5 py-2 text-[12px] leading-relaxed text-foreground">
                <span className={clsx('mb-0.5 block text-[10px] font-semibold', running ? 'text-mint-ink' : 'text-muted')}>
                  {latest
                    ? t('bots.statusAt', {
                      status: running ? t('bots.running') : t('bots.latest'),
                      time: formatRelativeTime(latest.at, t),
                    })
                    : running ? t('bots.running') : t('bots.latest')}
                </span>
                {latest?.excerpt && <span className="line-clamp-2 break-words">{latest.excerpt}</span>}
              </span>
            )}
            <span className="flex items-center gap-1.5 text-[12px] font-semibold text-cyan-ink">
              <MessageCircle className="size-3.5" />
              {t('bots.openConversation')}
            </span>
          </button>
        </PopoverContent>
      )}
    </Popover>
  );
};
