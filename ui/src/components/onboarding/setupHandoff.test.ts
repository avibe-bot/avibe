// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { captureSetupCards, measureSetupScreen, playSetupHandoff, setupHandoffAllowed } from './setupHandoff';

afterEach(() => { document.body.innerHTML = ''; vi.useRealTimers(); vi.unstubAllGlobals(); delete (document as Partial<Document>).hidden; delete (HTMLElement.prototype as Partial<HTMLElement>).animate; });
const screen = (selector: string) => {
  const node = document.createElement('div');
  node.innerHTML = `<div class="${selector}"><span class="onboarding-card-logo" id="duplicate">icon</span><strong class="onboarding-card-name">Claude Code</strong></div>`;
  document.body.append(node);
  for (const child of [node, ...node.querySelectorAll<HTMLElement>('*')]) child.getBoundingClientRect = () => new DOMRect(20, 30, 100, 60);
  return node;
};
it('temporarily measures a hidden inert root and restores its inline presentation', () => {
  const node = screen('onboarding-assistant'); node.hidden = true; node.inert = true; node.style.color = 'red';
  const restore = measureSetupScreen(node);
  expect(node.hidden).toBe(false); expect(node.inert).toBe(true); expect(node.style.visibility).toBe('hidden');
  restore(); expect(node.hidden).toBe(true); expect(node.style.color).toBe('red'); expect(node.style.position).toBe('');
});
it('snapshots C3 hooks without duplicating IDs in the live tree', () => {
  const source = screen('onboarding-collaboration-card');
  const snapshot = captureSetupCards(source, 'intro');
  expect(snapshot.cards).toHaveLength(1);
  expect(snapshot.cards[0].icon.node.id).toBe('');
});
it('cancels cleanly and finishes only once after resize/visibility', () => {
  vi.useFakeTimers();
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  const cancel = vi.fn();
  HTMLElement.prototype.animate = vi.fn(() => ({ cancel }) as unknown as Animation);
  const source = screen('onboarding-collaboration-card');
  const target = screen('onboarding-assistant'); target.hidden = true;
  const finish = vi.fn();
  const stop = playSetupHandoff(document.body, source, target, 'intro', 'assistants', finish);
  expect(target.hidden).toBe(true);
  const layer = document.querySelector<HTMLElement>('.onboarding-handoff-layer')!;
  expect(layer.getAttribute('aria-hidden')).toBe('true'); expect(layer.inert).toBe(true);
  window.dispatchEvent(new Event('resize')); vi.runAllTimers();
  expect(finish).toHaveBeenCalledOnce(); expect(document.querySelector('.onboarding-handoff-layer')).toBeNull();
  stop(); expect(source.style.visibility).toBe('');
});
it('bails out for explicit pause, hidden documents, and reduced motion', () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })));
  expect(setupHandoffAllowed()).toBe(false);
  expect(setupHandoffAllowed(true)).toBe(false);
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  expect(setupHandoffAllowed()).toBe(false);
});
