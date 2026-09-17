// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Welcome } from '../steps/Welcome';
import { CollaborationStory } from './CollaborationStory';
import { AccessTiles } from './AccessTiles';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const mocks = vi.hoisted(() => ({ reduced: false, api: { detectCli: vi.fn() } }));
vi.mock('framer-motion', () => ({ useReducedMotion: () => mocks.reduced }));
vi.mock('../../context/ApiContext', () => ({ useApi: () => mocks.api }));
const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
const wrap = (element: React.ReactNode) => <I18nextProvider i18n={i18n}>{element}</I18nextProvider>;
beforeEach(() => { mocks.reduced = false; vi.clearAllMocks(); void i18n.changeLanguage('en'); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('Welcome', () => {
  it('checks saved paths before advancing without persisting configuration', async () => {
    mocks.api.detectCli.mockImplementation(async (binary: string) => ({ found: binary !== 'codex', path: `/test/${binary}` }));
    const next = vi.fn();
    render(wrap(<Welcome data={{ agents: { claude: { cli_path: '/custom/claude' } } }} onNext={next} />));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    await waitFor(() => expect(next).toHaveBeenCalledOnce());
    expect(mocks.api.detectCli.mock.calls.map(([binary]) => binary)).toEqual(['/custom/claude', 'codex', 'opencode']);
    expect(next.mock.calls[0][0]).toMatchObject({ __onboardingDetected: true, agents: { claude: { status: 'ok' }, codex: { status: 'missing' }, opencode: { status: 'ok' } } });
  });
  it('retains a failed detection on Welcome and permits retry', async () => {
    mocks.api.detectCli.mockRejectedValueOnce(new Error('Probe unavailable')).mockResolvedValue({ found: false });
    const next = vi.fn();
    render(wrap(<Welcome onNext={next} />));
    fireEvent.click(screen.getByRole('button', { name: 'Get started' }));
    await screen.findByRole('alert');
    expect(next).not.toHaveBeenCalled();
    expect(screen.getByText('Probe unavailable')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(next).toHaveBeenCalledOnce());
  });
  it('renders the approved Chinese copy', async () => {
    await i18n.changeLanguage('zh');
    render(wrap(<Welcome onNext={vi.fn()} />));
    expect(screen.getByRole('heading', { name: '各有所长，接力完成' })).toBeTruthy();
    expect(screen.getAllByRole('listitem')).toHaveLength(6);
  });
});

describe('collaboration playback and access interaction', () => {
  it('advances the pulse and freezes the entire state on pause', () => {
    vi.useFakeTimers();
    const { container, rerender } = render(wrap(<CollaborationStory paused={false} onPausedChange={vi.fn()} />));
    act(() => vi.advanceTimersByTime(1800));
    expect(screen.getByTestId('handoff-pulse')).toBeTruthy();
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(1);
    rerender(wrap(<CollaborationStory paused onPausedChange={vi.fn()} />));
    const before = container.innerHTML;
    act(() => vi.advanceTimersByTime(3000));
    expect(container.innerHTML).toBe(before);
  });
  it('replays every card and pulse from the same beginning', () => {
    vi.useFakeTimers();
    const resume = vi.fn();
    const { container } = render(wrap(<CollaborationStory paused={false} onPausedChange={resume} />));
    act(() => vi.advanceTimersByTime(5000));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Replay animation' }));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(0);
    expect(screen.queryByTestId('handoff-pulse')).toBeNull();
    expect(resume).toHaveBeenCalledWith(false);
  });
  it('shows completed work and stops all timers for reduced motion', () => {
    vi.useFakeTimers(); mocks.reduced = true;
    const { container } = render(wrap(<Welcome onNext={vi.fn()} />));
    expect(container.querySelectorAll('[data-state="complete"]')).toHaveLength(3);
    expect(screen.queryByTestId('handoff-pulse')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Pause animation' })).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });
  it('pauses idle emphasis throughout pointer and keyboard interaction', () => {
    vi.useFakeTimers();
    const { container } = render(wrap(<AccessTiles paused={false} />));
    act(() => vi.advanceTimersByTime(2000));
    expect(container.querySelectorAll('[data-emphasis="true"]')).toHaveLength(1);
    fireEvent.pointerEnter(screen.getByRole('list'));
    expect(container.querySelectorAll('[data-emphasis="true"]')).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
    fireEvent.pointerLeave(screen.getByRole('list'));
    fireEvent.focus(screen.getAllByRole('listitem')[0]);
    expect(vi.getTimerCount()).toBe(0);
    fireEvent.blur(screen.getAllByRole('listitem')[0], { relatedTarget: null });
    expect(vi.getTimerCount()).toBe(1);
  });
});
