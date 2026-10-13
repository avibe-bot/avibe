// A hosted tool call's card, as a ``toolcall`` row carries it in ``content.hosted``
// (``core/hosted_activity.py``, version 1; the shared fixture is
// ``hostedActivity.fixture.json``). The row's ``text`` is the IM line; the Web
// renders the card from these fields instead.

export type HostedToolName = 'web_search' | 'x_search' | 'image_generation';

export type HostedActivitySource = {
  url: string;
  title?: string;
  snippet?: string;
  page_age?: string;
};

export type HostedActivityImage = {
  path: string;
  mime: string;
  width?: number;
  height?: number;
  /** Workbench only: the media-proxy URL the row registered for the file. */
  media_url?: string;
  bytes?: number;
};

export type HostedActivity = {
  version: 1;
  call_id: string;
  tool: HostedToolName;
  mode: 'native' | 'proxy';
  status: 'completed' | 'failed' | 'incomplete' | 'pending';
  error?: string;
  /** The query, prompt, page or thread the call named. */
  subject?: string;
  action?: string;
  extra_queries?: string[];
  /** Vendor id of whoever ran it: the response's for native, the executor's for proxy. */
  provider?: string;
  source_id?: string;
  source_name?: string;
  model?: string;
  sources: HostedActivitySource[];
  images: HostedActivityImage[];
  size?: string;
  quality?: string;
  revised_prompt?: string;
  duration_ms?: number;
  /** Absent when no price is known: unknown is never shown as zero. ``calls`` is how many calls it covers. */
  cost?: { basis: 'reported' | 'list' | 'included'; usd?: number; calls: number };
};

const TOOLS: readonly string[] = ['web_search', 'x_search', 'image_generation'];
const STATUSES: readonly string[] = ['completed', 'failed', 'incomplete', 'pending'];

/** The card a row carries, or undefined for anything that is not one this client reads. */
export const hostedActivityFrom = (raw: unknown): HostedActivity | undefined => {
  if (!raw || typeof raw !== 'object') return undefined;
  const card = raw as Record<string, unknown>;
  if (card.version !== 1 || typeof card.call_id !== 'string') return undefined;
  if (typeof card.tool !== 'string' || !TOOLS.includes(card.tool)) return undefined;
  if (card.mode !== 'native' && card.mode !== 'proxy') return undefined;
  if (typeof card.status !== 'string' || !STATUSES.includes(card.status)) return undefined;
  return {
    ...(card as unknown as HostedActivity),
    sources: Array.isArray(card.sources) ? (card.sources as HostedActivitySource[]).filter((s) => typeof s?.url === 'string') : [],
    images: Array.isArray(card.images) ? (card.images as HostedActivityImage[]).filter((i) => typeof i?.path === 'string') : [],
  };
};

/** The media token of a proxy URL (``/api/media/<token>?w=&h=``), which joins a card's image to the answer's. */
export const mediaToken = (url: string | undefined): string | null => {
  if (!url) return null;
  const match = /\/api\/media\/([^/?#]+)/.exec(url);
  return match ? match[1] : null;
};
