import type { AppId } from './registry';
import type { AppLaunchIntent } from './appLaunch';

export type AppLaunchSource = 'dock' | 'apps-launcher' | 'library' | 'command-palette' | 'dock-shortcut' | 'show-control' | 'show-route' | 'file-browser' | 'chat-attachment' | 'deep-link';
export interface AppLaunchParams {
  path?: string;
  cwd?: string;
  filename?: string;
  name?: string;
  mtime?: number | null;
  newFileDir?: string;
  rootDir?: string;
  mime?: string;
  line?: number;
  column?: number;
  endColumn?: number;
  initialTab?: 'apps' | 'showpages';
}
export interface AppLaunchTarget {
  appId: AppId;
  sessionId?: string;
  title?: string;
  params?: AppLaunchParams;
}
export interface AppLaunchRequest extends AppLaunchTarget {
  source: AppLaunchSource;
  mode: AppLaunchIntent;
  newWindow: boolean;
  origin: string;
  requestId: string;
}
export interface AppLaunchSnapshot extends AppLaunchTarget {
  requestId: string;
  windowId: string;
  source: AppLaunchSource;
}

declare global {
  interface Window {
    __AVIBE_APP_LAUNCH_PROTOCOL__?: number;
    __AVIBE_APP_LAUNCH__?: AppLaunchSnapshot;
  }
}

export const APP_LAUNCH_EVENT = 'avibe:app-launch';
export const APP_LAUNCH_RESULT_EVENT = 'avibe:app-launch-result';
export type AppLaunchOutcome = 'opened' | 'fallback' | 'stale' | 'unknown';
const pendingRequests = new Set<string>();

// A navigation is an intent, never an authority grant. Only a correlated native
// fallback guarantees that creating a Workbench window cannot create a duplicate.
export function requestNativeAppLaunch(request: AppLaunchRequest, signal: AbortSignal): Promise<AppLaunchOutcome> {
  if (signal.aborted) return Promise.resolve('stale');
  if (window.__AVIBE_APP_LAUNCH_PROTOCOL__ !== 1) return Promise.resolve('fallback');
  if (pendingRequests.size >= 64 || pendingRequests.has(request.requestId)) return Promise.resolve('unknown');
  pendingRequests.add(request.requestId);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (outcome: AppLaunchOutcome) => {
      if (settled) return;
      settled = true;
      pendingRequests.delete(request.requestId);
      clearTimeout(timer);
      window.removeEventListener(APP_LAUNCH_RESULT_EVENT, result);
      window.removeEventListener('pagehide', retired);
      signal.removeEventListener('abort', retired);
      resolve(outcome);
    };
    const retired = () => finish('stale');
    const result = (event: Event) => {
      const detail: unknown = (event as CustomEvent).detail;
      if (!detail || typeof detail !== 'object') return;
      const value = detail as Record<string, unknown>;
      if (value.requestId !== request.requestId) return;
      if (value.status === 'opened' || value.status === 'fallback' || value.status === 'stale') finish(value.status);
    };
    const timer = setTimeout(() => finish('unknown'), 10_000);
    window.addEventListener(APP_LAUNCH_RESULT_EVENT, result);
    window.addEventListener('pagehide', retired);
    signal.addEventListener('abort', retired, { once: true });
    try {
      const link = document.createElement('a');
      link.href = `avibe-app://launch?request=${encodeURIComponent(JSON.stringify(request))}`;
      document.body.append(link);
      try { link.click(); } finally { link.remove(); }
    } catch {
      // Dispatch may have reached native even if the Webview reports a navigation
      // error. Only an explicit fallback result permits another window.
      finish('unknown');
    }
  });
}

/**
 * Asks the shell for this native App window's ordinary close, the one its
 * close button starts, so the unsaved-state question still applies. False
 * where there is no native owner to ask.
 */
export function requestNativeWindowClose(): boolean {
  if (window.__AVIBE_NATIVE_APP_WINDOW__ !== true || window.__AVIBE_APP_LAUNCH_PROTOCOL__ !== 1) return false;
  const link = document.createElement('a');
  link.href = 'avibe-app://close';
  document.body.append(link);
  try { link.click(); } finally { link.remove(); }
  return true;
}

export function appBodyParams(snapshot: AppLaunchSnapshot | AppLaunchRequest): Record<string, unknown> {
  return {
    ...snapshot.params,
    ...(snapshot.sessionId ? { sessionId: snapshot.sessionId } : {}),
    ...(snapshot.title ? { title: snapshot.title } : {}),
    launchRequestId: snapshot.requestId,
    // Library's existing navigation consumer also uses these for repeated tabs.
    navKey: snapshot.requestId,
    navTab: snapshot.params?.initialTab,
  };
}

const TARGET_FIELDS: Record<AppId, readonly string[]> = {
  files: ['path', 'rootDir'],
  terminal: ['cwd'],
  editor: ['path', 'filename', 'mtime', 'newFileDir', 'rootDir', 'line', 'column', 'endColumn'],
  preview: ['path', 'name', 'mime', 'mtime'],
  library: ['initialTab'],
  showpage: [],
};
const SOURCES: readonly string[] = ['dock', 'apps-launcher', 'library', 'command-palette', 'dock-shortcut', 'show-control', 'show-route', 'file-browser', 'chat-attachment', 'deep-link'];
const byteLength = (value: string) => new TextEncoder().encode(value).length;
const boundedString = (value: unknown, limit: number): value is string => typeof value === 'string' && !value.includes('\0') && byteLength(value) <= limit;
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const requestIdValid = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);

export function validAppLaunchTarget(value: unknown): value is AppLaunchTarget {
  if (!object(value) || typeof value.appId !== 'string' || !Object.hasOwn(TARGET_FIELDS, value.appId)) return false;
  if (value.title !== undefined && !boundedString(value.title, 512)) return false;
  if (value.appId === 'showpage') {
    if (typeof value.sessionId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value.sessionId) || value.sessionId === '.' || value.sessionId === '..' || byteLength(value.sessionId) > 128) return false;
  } else if (value.sessionId !== undefined) return false;
  if (value.params === undefined) return true;
  if (!object(value.params)) return false;
  for (const [key, field] of Object.entries(value.params)) {
    if (field === undefined) continue;
    if (!TARGET_FIELDS[value.appId as AppId].includes(key)) return false;
    if (key === 'mtime') {
      if (field !== null && (typeof field !== 'number' || !Number.isFinite(field))) return false;
    } else if (key === 'line' || key === 'column' || key === 'endColumn') {
      if (typeof field !== 'number' || !Number.isSafeInteger(field) || field < (key === 'line' ? 1 : 0)) return false;
    } else if (key === 'initialTab') {
      if (field !== 'apps' && field !== 'showpages') return false;
    } else if (!boundedString(field, ['path', 'cwd', 'newFileDir', 'rootDir'].includes(key) ? 4096 : key === 'mime' ? 256 : 512)) return false;
  }
  if (typeof value.params.endColumn === 'number' && value.params.endColumn < (typeof value.params.column === 'number' ? value.params.column : 0)) return false;
  return true;
}

export function readAppLaunchSnapshot(value: unknown): AppLaunchSnapshot | null {
  if (!object(value) || !validAppLaunchTarget(value) || !requestIdValid(value.requestId) || !boundedString(value.windowId, 512) || !value.windowId || !SOURCES.includes(value.source as string)) return null;
  return value as unknown as AppLaunchSnapshot;
}
