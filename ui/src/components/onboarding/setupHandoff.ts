import type { SetupScreenId } from './setupFlow';

type Piece = { node: HTMLElement; bounds: DOMRect };
/** A piece the departing card carries but the arriving one has no place for, and how
 *  long it is given to leave. The prototype dissolves them rather than cutting them
 *  with the screen, which is what keeps the shrinking card readable mid-flight. */
type FadingPiece = Piece & { duration: number; lift: number };
type CardSnapshot = {
  bounds: DOMRect; surface: string; border: string; radius: string;
  /** Whether the source card wore the mint halo; the flight fades it through the token. */
  glow: boolean;
  icon: Piece; name: Piece; fading: FadingPiece[];
};
export type SetupSnapshot = { cards: CardSnapshot[]; retiring?: Piece };
const timing: KeyframeAnimationOptions = { duration: 860, easing: 'cubic-bezier(.32,0,.16,1)', fill: 'both' };

const hooks = (screen: SetupScreenId) => screen === 'providers'
  ? { card: '.setup-destination', icon: '.setup-destination-logo', name: '.setup-destination-name' }
  : { card: screen === 'intro' ? '.onboarding-collaboration-card' : '.onboarding-assistant', icon: '.onboarding-card-logo', name: '.onboarding-card-name' };

/**
 * What a departing card holds beyond its identity: the story's role, caption and
 * written work. The identity flies to the next screen; these have nowhere to land,
 * so they leave on their own timing — the role first, because it sits on the line
 * the identity is still moving along.
 */
const FADING: Partial<Record<SetupScreenId, { selector: string; duration: number; lift: number }[]>> = {
  intro: [
    { selector: '.onboarding-card-role', duration: 130, lift: 0 },
    { selector: '.onboarding-story-status', duration: 190, lift: 12 },
    { selector: '.onboarding-skeleton', duration: 190, lift: 12 },
  ],
};

function capturePiece(element: HTMLElement): Piece {
  const node = element.cloneNode(true) as HTMLElement;
  // A snapshot is presentation only. Duplicate IDs must never enter the live tree.
  for (const child of [node, ...node.querySelectorAll<HTMLElement>('[id]')]) child.removeAttribute('id');
  const style = getComputedStyle(element);
  Object.assign(node.style, { font: style.font, color: style.color, letterSpacing: style.letterSpacing, lineHeight: style.lineHeight });
  return { node, bounds: element.getBoundingClientRect() };
}

export function captureSetupCards(root: HTMLElement, screen: SetupScreenId): SetupSnapshot {
  const selectors = hooks(screen);
  const cards = Array.from(root.querySelectorAll<HTMLElement>(selectors.card)).flatMap((card) => {
    const icon = card.querySelector<HTMLElement>(selectors.icon);
    const name = card.querySelector<HTMLElement>(selectors.name);
    if (!icon || !name) return [];
    const style = getComputedStyle(card);
    const fading = (FADING[screen] ?? []).flatMap(({ selector, duration, lift }) => {
      const element = card.querySelector<HTMLElement>(selector);
      return element ? [{ ...capturePiece(element), duration, lift }] : [];
    });
    return [{ bounds: card.getBoundingClientRect(), surface: style.backgroundColor, border: style.borderColor,
      radius: style.borderRadius, glow: style.boxShadow !== 'none', icon: capturePiece(icon), name: capturePiece(name), fading }];
  });
  const stage = screen === 'providers' ? root.querySelector<HTMLElement>('.setup-provider-stage') : null;
  const retiring = stage ? capturePiece(stage) : undefined;
  retiring?.node.querySelector('.setup-destinations')?.remove();
  return { cards, retiring };
}

/** Safe media reading: the shell runs in browsers and in jsdom, which ships no matchMedia. */
export const mediaQuery = (query: string): boolean =>
  typeof window.matchMedia === 'function' && window.matchMedia(query).matches;

export function setupHandoffAllowed(paused = false): boolean {
  return !paused && !document.hidden && !mediaQuery('(prefers-reduced-motion: reduce)')
    && typeof HTMLElement.prototype.animate === 'function';
}

/** Measure the incoming mounted screen without activating its effects or changing flow. */
export function measureSetupScreen(root: HTMLElement): () => void {
  const hidden = root.hidden;
  const style = root.getAttribute('style');
  root.hidden = false;
  root.inert = true;
  Object.assign(root.style, { position: 'absolute', inset: '0', visibility: 'hidden', pointerEvents: 'none' });
  return () => {
    root.hidden = hidden;
    if (style === null) root.removeAttribute('style'); else root.setAttribute('style', style);
  };
}

/** The returned cleanup cancels without navigating. Visibility/resize finish instantly. */
export function playSetupHandoff(
  host: HTMLElement, outgoing: HTMLElement, incoming: HTMLElement,
  from: SetupScreenId, to: SetupScreenId, onComplete: () => void,
): () => void {
  const snapshot = captureSetupCards(outgoing, from);
  const restoreIncoming = measureSetupScreen(incoming);
  const targets = Array.from(incoming.querySelectorAll<HTMLElement>(hooks(to).card));
  const origin = host.getBoundingClientRect();
  const layer = document.createElement('div');
  layer.className = 'onboarding-handoff-layer';
  layer.setAttribute('aria-hidden', 'true');
  layer.inert = true;
  const animations: Animation[] = [];
  const animate = (node: HTMLElement, frames: Keyframe[], options = timing) => {
    animations.push(node.animate(frames, options));
  };
  const position = (piece: Piece) => Object.assign(piece.node.style, {
    position: 'absolute', left: `${piece.bounds.x - origin.x}px`, top: `${piece.bounds.y - origin.y}px`,
    width: `${piece.bounds.width}px`, height: `${piece.bounds.height}px`, margin: '0',
  });
  if (snapshot.retiring) {
    position(snapshot.retiring);
    layer.append(snapshot.retiring.node);
    animate(snapshot.retiring.node, [{ opacity: 1 }, { opacity: 0, transform: 'translateY(-36px)' }], { ...timing, duration: 300 });
  }
  // The next provider stage enters behind the moving assistant identities.
  const providerStage = to === 'providers' ? incoming.querySelector<HTMLElement>('.setup-provider-stage') : null;
  if (providerStage) {
    const arriving = capturePiece(providerStage);
    arriving.node.querySelector('.setup-destinations')?.remove();
    position(arriving);
    arriving.node.style.visibility = 'visible';
    layer.append(arriving.node);
    animate(arriving.node, [{ opacity: 0, transform: 'translateY(-24px)' }, { opacity: 1, transform: 'translateY(0)' }]);
  }
  // The screens swap their title at the end of the flight, so without this the
  // journey spends the whole transition with no heading at all. The arriving one
  // enters with the stage, from the position it will hold when it lands.
  const heading = incoming.querySelector<HTMLElement>('.onboarding-heading');
  if (heading) {
    const arriving = capturePiece(heading);
    position(arriving);
    arriving.node.style.visibility = 'visible';
    layer.append(arriving.node);
    animate(arriving.node, [{ opacity: 0, transform: 'translateY(-12px)' }, { opacity: 1, transform: 'translateY(0)' }],
      { ...timing, duration: 420 });
  }
  snapshot.cards.forEach((source, index) => {
    const target = targets[index];
    if (!target) return;
    const bounds = target.getBoundingClientRect();
    if (!bounds.width || !bounds.height || !source.bounds.width || !source.bounds.height) return;
    const style = getComputedStyle(target);
    const card = document.createElement('div');
    card.className = 'onboarding-handoff-card';
    Object.assign(card.style, { left: `${source.bounds.x - origin.x}px`, top: `${source.bounds.y - origin.y}px`,
      width: `${bounds.width}px`, height: `${bounds.height}px` });
    const surface = document.createElement('div');
    surface.className = 'onboarding-handoff-surface';
    card.append(surface);
    animate(card, [{ transform: 'translate(0,0)' }, { transform: `translate(${bounds.x - source.bounds.x}px,${bounds.y - source.bounds.y}px)` }]);
    if (source.glow) surface.dataset.glow = 'true';
    animate(surface, [
      { transform: `scale(${source.bounds.width / bounds.width},${source.bounds.height / bounds.height})`, background: source.surface, borderColor: source.border, borderRadius: source.radius },
      { transform: 'scale(1,1)', background: style.backgroundColor, borderColor: style.borderColor, borderRadius: style.borderRadius },
    ]);
    for (const [piece, selector] of [[source.icon, hooks(to).icon], [source.name, hooks(to).name]] as const) {
      const child = target.querySelector<HTMLElement>(selector);
      if (!child || !piece.bounds.width || !piece.bounds.height) continue;
      const end = child.getBoundingClientRect();
      Object.assign(piece.node.style, { position: 'absolute', left: `${piece.bounds.x - source.bounds.x}px`, top: `${piece.bounds.y - source.bounds.y}px`,
        width: `${piece.bounds.width}px`, height: `${piece.bounds.height}px`, margin: '0', transformOrigin: 'top left' });
      card.append(piece.node);
      animate(piece.node, [{ transform: 'translate(0,0) scale(1)' }, {
        transform: `translate(${end.x - bounds.x - (piece.bounds.x - source.bounds.x)}px,${end.y - bounds.y - (piece.bounds.y - source.bounds.y)}px) scale(${end.width / piece.bounds.width},${end.height / piece.bounds.height})`,
        color: getComputedStyle(child).color,
      }]);
    }
    for (const piece of source.fading) {
      Object.assign(piece.node.style, { position: 'absolute', left: `${piece.bounds.x - source.bounds.x}px`, top: `${piece.bounds.y - source.bounds.y}px`,
        width: `${piece.bounds.width}px`, height: `${piece.bounds.height}px`, margin: '0', padding: '0' });
      card.append(piece.node);
      animate(piece.node, [{ opacity: 1, transform: 'translateY(0)' }, { opacity: 0, transform: `translateY(${piece.lift}px)` }],
        { duration: piece.duration, easing: 'ease-out', fill: 'both' });
    }
    layer.append(card);
  });
  restoreIncoming();
  host.append(layer);
  // Start the halo fade one frame in, so the transition runs against the mounted layer.
  const fade = window.requestAnimationFrame(() => {
    for (const node of layer.querySelectorAll<HTMLElement>('[data-glow]')) node.removeAttribute('data-glow');
  });
  const previousVisibility = outgoing.style.visibility;
  outgoing.style.visibility = 'hidden';
  let settled = false;
  const media = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const cleanup = () => {
    window.clearTimeout(timer);
    window.cancelAnimationFrame(fade);
    animations.forEach((animation) => animation.cancel());
    layer.remove();
    outgoing.style.visibility = previousVisibility;
    window.removeEventListener('resize', finish);
    document.removeEventListener('visibilitychange', visibility);
    media?.removeEventListener('change', finish);
  };
  const finish = () => { if (settled) return; settled = true; cleanup(); onComplete(); };
  const visibility = () => { if (document.hidden) finish(); };
  const timer = window.setTimeout(finish, 900);
  window.addEventListener('resize', finish, { once: true });
  document.addEventListener('visibilitychange', visibility);
  media?.addEventListener('change', finish, { once: true });
  return () => { settled = true; cleanup(); };
}
