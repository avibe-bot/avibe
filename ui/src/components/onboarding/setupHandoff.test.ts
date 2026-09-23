// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { captureSetupCards, playSetupHandoff, setupHandoffAllowed } from './setupHandoff';

afterEach(() => { document.body.innerHTML = ''; vi.useRealTimers(); vi.unstubAllGlobals(); delete (document as Partial<Document>).hidden; delete (HTMLElement.prototype as Partial<HTMLElement>).animate; });
const screen = (selector: string) => {
  const node = document.createElement('div');
  node.innerHTML = `<div class="${selector}"><span class="onboarding-card-logo" id="duplicate">icon</span><strong class="onboarding-card-name">Claude Code</strong></div>`;
  document.body.append(node);
  for (const child of [node, ...node.querySelectorAll<HTMLElement>('*')]) child.getBoundingClientRect = () => new DOMRect(20, 30, 100, 60);
  return node;
};
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
  const target = screen('onboarding-assistant');
  const finish = vi.fn();
  const stop = playSetupHandoff(document.body, captureSetupCards(source, 'intro'), target, 'assistants', finish);
  const layer = document.querySelector<HTMLElement>('.onboarding-handoff-layer')!;
  expect(layer.getAttribute('aria-hidden')).toBe('true'); expect(layer.inert).toBe(true);
  window.dispatchEvent(new Event('resize')); vi.runAllTimers();
  expect(finish).toHaveBeenCalledOnce(); expect(document.querySelector('.onboarding-handoff-layer')).toBeNull();
  stop();
});
it('bails out for explicit pause, hidden documents, and reduced motion', () => {
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })));
  expect(setupHandoffAllowed()).toBe(false);
  expect(setupHandoffAllowed(true)).toBe(false);
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  expect(setupHandoffAllowed()).toBe(false);
});
