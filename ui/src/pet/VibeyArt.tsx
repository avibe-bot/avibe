import type { CSSProperties } from 'react';
import { Wrench } from 'lucide-react';

import vibey from '@/assets/pet/vibey.png';
import vibeyFocus from '@/assets/pet/vibey-focus.png';

import { GLYPH_COLOR, LAYERS, type LayerPart } from './vibeyLayers';
import './vibey-art.css';

export type VibeyPose =
  | 'listening'
  | 'needs_input'
  | 'blocked'
  | 'running'
  | 'ready'
  | 'team'
  | 'idle'
  | 'sleeping';

const ART = 192;
const pct = (value: number, of = ART) => `${(value / of) * 100}%`;

const box = (
  part: Pick<LayerPart, 'x' | 'y' | 'w' | 'h'>,
  offsetX = 0,
  offsetY = 0,
  outerWidth = ART,
  outerHeight = ART,
): CSSProperties => ({
  left: pct(part.x - offsetX, outerWidth),
  top: pct(part.y - offsetY, outerHeight),
  width: pct(part.w, outerWidth),
  height: pct(part.h, outerHeight),
});

function Part({
  part,
  offsetX,
  offsetY,
  outerWidth,
  outerHeight,
}: {
  part: LayerPart;
  offsetX?: number;
  offsetY?: number;
  outerWidth?: number;
  outerHeight?: number;
}) {
  const style = {
    ...box(part, offsetX, offsetY, outerWidth, outerHeight),
    '--step': part.step,
    '--dx': part.dx ?? 0,
    '--dy': part.dy ?? 0,
  } as CSSProperties;
  return (
    <img
      className={`va-part va-${part.role}`}
      data-step={part.step}
      src={part.src}
      alt=""
      draggable={false}
      style={style}
    />
  );
}

function ThoughtLayers({ tool }: { tool: boolean }) {
  const parts = LAYERS.thinking.parts;
  const dots = parts.filter((part) => part.role === 'tdot');
  const bubble = parts.find((part) => part.role === 'bubble')!;
  const glyph = parts.find((part) => part.role === 'glyph')!;
  const side = glyph.h * 0.92;
  const toolBox = {
    x: glyph.x + glyph.w / 2 - side / 2,
    y: glyph.y + glyph.h / 2 - side / 2,
    w: side,
    h: side,
  };

  return (
    <>
      {dots.map((part) => <Part key={part.src} part={part} />)}
      <span className="va-bubble-group" style={box(bubble)}>
        <img className="va-bubble" src={bubble.src} alt="" draggable={false} />
        {tool ? (
          <span className="va-tool" style={box(toolBox, bubble.x, bubble.y, bubble.w, bubble.h)}>
            <Wrench color={GLYPH_COLOR} strokeWidth={3.2} width="100%" height="100%" />
          </span>
        ) : (
          <Part part={glyph} offsetX={bubble.x} offsetY={bubble.y} outerWidth={bubble.w} outerHeight={bubble.h} />
        )}
      </span>
    </>
  );
}

const CONFIG: Record<VibeyPose, { body: string; mood: 'is-breathing' | 'is-sleeping' }> = {
  listening: { body: vibeyFocus, mood: 'is-breathing' },
  needs_input: { body: LAYERS.message.body, mood: 'is-breathing' },
  blocked: { body: LAYERS.error.body, mood: 'is-breathing' },
  running: { body: LAYERS.thinking.body, mood: 'is-breathing' },
  ready: { body: LAYERS.celebrate.body, mood: 'is-breathing' },
  team: { body: LAYERS.thinking.body, mood: 'is-breathing' },
  idle: { body: vibey, mood: 'is-breathing' },
  sleeping: { body: LAYERS.sleep.body, mood: 'is-sleeping' },
};

/**
 * Layered Vibey art. The body breathes while accessories run their own loops.
 * `active` is controlled by page activity so a hidden desktop window pauses all
 * animation work without changing the rendered composition.
 */
export function VibeyArt({
  pose,
  active = true,
  breathing = true,
  still = false,
  className = '',
  label,
}: {
  pose: VibeyPose;
  active?: boolean;
  breathing?: boolean;
  still?: boolean;
  className?: string;
  label?: string;
}) {
  const { body, mood } = CONFIG[pose];
  return (
    <span
      className={`pet-avatar__pose vibey-art va-${pose} ${breathing ? mood : 'is-resting'} ${active ? 'is-active' : 'is-paused'} ${still ? 'is-still' : ''} ${className}`}
      data-active={active ? 'true' : 'false'}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <img className="va-body" src={body} alt="" draggable={false} />
      {pose === 'needs_input' && LAYERS.message.parts.map((part) => <Part key={part.src} part={part} />)}
      {pose === 'blocked' && LAYERS.error.parts.map((part) => <Part key={part.src} part={part} />)}
      {pose === 'ready' && LAYERS.celebrate.parts.map((part) => <Part key={part.src} part={part} />)}
      {pose === 'sleeping' && LAYERS.sleep.parts.map((part) => <Part key={part.src} part={part} />)}
      {(pose === 'running' || pose === 'team') && <ThoughtLayers tool={pose === 'team'} />}
    </span>
  );
}
