// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { VaultSandboxClient } from './vaultSandboxClient';
import type { VaultConfirmSurface } from './vaultConfirmSurface';

const visibleSurface: VaultConfirmSurface = {
  frame: {
    frameWidth: 440,
    frameHeight: 640,
    intersectionRatio: 1,
    visibleByIntersectionObserver: true,
    visibleByHitTest: true,
    opacity: 1,
    pointerEvents: true,
  },
  sampledAt: 1_700_000_000_000,
};

type TestPendingRequest = {
  timer: number;
  resolve: (value: unknown) => void;
};

type TestVaultSandboxClient = {
  iframe: HTMLIFrameElement;
  backdrop: HTMLDivElement | null;
  pending: Map<string, TestPendingRequest>;
  readyPromise: Promise<unknown>;
  handshaken: boolean;
  modalVisible: boolean;
  interactiveRequests: Set<string>;
  surfaceRefreshTimer: number | null;
  setModalVisible: (visible: boolean) => void;
  startSurfaceRefresh: () => void;
  measureSurface: () => Promise<VaultConfirmSurface | null>;
  request: <T>(
    op: string,
    payload: unknown,
    options: { timeoutMs?: number; interactive?: boolean },
  ) => Promise<T>;
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, 'elementFromPoint');
  document.body.replaceChildren();
});

describe('VaultSandboxClient interactive request surface', () => {
  it('attests a fully hit-tested frame when browser visibility tracking is conservatively false', async () => {
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);
    vi.spyOn(iframe, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 50,
      left: 100,
      top: 50,
      right: 540,
      bottom: 690,
      width: 440,
      height: 640,
      toJSON: () => ({}),
    } as DOMRect);
    const hitTest = vi.fn((): Element | null => iframe);
    Object.defineProperty(document, 'elementFromPoint', {
      configurable: true,
      value: hitTest,
    });
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(private readonly callback: IntersectionObserverCallback) {}
        observe(): void {
          this.callback(
            [{ intersectionRatio: 1, isVisible: false } as unknown as IntersectionObserverEntry],
            this as unknown as IntersectionObserver,
          );
        }
        disconnect(): void {}
      },
    );

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, { iframe });
    const surface = await client.measureSurface();

    expect(surface?.frame).toMatchObject({
      frameWidth: 440,
      frameHeight: 640,
      intersectionRatio: 1,
      visibleByIntersectionObserver: false,
      visibleByHitTest: true,
    });
    expect(hitTest).toHaveBeenCalledTimes(9);

    const overlay = document.createElement('div');
    hitTest.mockReturnValue(overlay);
    const occluded = await client.measureSurface();
    expect(occluded?.frame.visibleByHitTest).toBe(false);
  });

  it('expands the iframe before measuring and sending an interactive RPC', async () => {
    const iframe = document.createElement('iframe');
    iframe.style.width = '0';
    iframe.style.height = '0';
    iframe.style.visibility = 'hidden';
    iframe.style.pointerEvents = 'none';
    document.body.appendChild(iframe);

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, {
      iframe,
      backdrop: null,
      pending: new Map(),
      readyPromise: Promise.resolve({}),
      handshaken: true,
      modalVisible: false,
      interactiveRequests: new Set(),
      surfaceRefreshTimer: null,
    });
    vi.spyOn(client, 'startSurfaceRefresh').mockImplementation(() => undefined);
    const setModalVisible = vi.spyOn(client, 'setModalVisible');
    const measure = vi.spyOn(client, 'measureSurface').mockImplementation(async () => {
      expect(setModalVisible).toHaveBeenCalledWith(true);
      expect(client['modalVisible']).toBe(true);
      expect(iframe.style.visibility).toBe('visible');
      expect(iframe.style.pointerEvents).toBe('auto');
      return visibleSurface;
    });
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);

    const result = client.request<{ blindBoxes: unknown[] }>(
      'approveRelease',
      { items: [] },
      { interactive: true },
    );
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledTimes(1));

    const [wire] = postMessage.mock.calls[0];
    expect(measure).toHaveBeenCalledOnce();
    expect(wire).toMatchObject({ op: 'approveRelease', surface: visibleSurface });
    const pending = client.pending.get((wire as { id: string }).id);
    expect(pending).toBeDefined();
    if (!pending) throw new Error('interactive request was not registered');
    window.clearTimeout(pending.timer);
    pending.resolve({ blindBoxes: [] });
    await expect(result).resolves.toEqual({ blindBoxes: [] });
  });

  it('collapses the modal when interactive surface measurement fails before send', async () => {
    const iframe = document.createElement('iframe');
    document.body.appendChild(iframe);

    const client = Object.create(VaultSandboxClient.prototype) as TestVaultSandboxClient;
    Object.assign(client, {
      iframe,
      backdrop: null,
      pending: new Map(),
      readyPromise: Promise.resolve({}),
      handshaken: true,
      modalVisible: false,
      interactiveRequests: new Set(),
      surfaceRefreshTimer: null,
    });
    vi.spyOn(client, 'startSurfaceRefresh').mockImplementation(() => undefined);
    vi.spyOn(client, 'measureSurface').mockRejectedValue(new Error('visibility API failed'));
    const postMessage = vi.spyOn(iframe.contentWindow!, 'postMessage').mockImplementation(() => undefined);

    await expect(
      client.request('approveRelease', { items: [] }, { interactive: true }),
    ).rejects.toThrow('visibility API failed');

    expect(postMessage).not.toHaveBeenCalled();
    expect(client.interactiveRequests.size).toBe(0);
    expect(client.pending.size).toBe(0);
    expect(client.modalVisible).toBe(false);
    expect(client.backdrop).toBeNull();
    expect(iframe.style.visibility).toBe('hidden');
    expect(iframe.style.pointerEvents).toBe('none');
  });
});
