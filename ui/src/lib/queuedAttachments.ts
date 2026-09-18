import { isProxyMediaUrl } from '@/lib/mediaProxy';

// What a queued message's ``content.attachments`` amounts to on the queue strip.
// Pure, and separate from the row that draws it, for the same reason
// ``annotationView`` is: the rule about which URL may become an <img> is a
// safety property, and belongs somewhere a test can hold it on its own.

export type QueuedAttachment = {
  url: string;
  name: string;
  /** True only when the file is an image AND its URL is one we may fetch. */
  image: boolean;
};

const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;

const asString = (value: unknown): string => (typeof value === 'string' ? value : '');

// A queued message carries its attachments in one of two shapes, because two
// different paths write them: a Web upload records ``{url, mime, kind}``
// (``vibe/ui_server`` mints the proxy URL at upload time), while an IM inbound
// records ``{token, mimetype}`` (``core/handlers/message_handler``) and leaves
// the URL to whoever renders it. Both name the same media object, so both are
// read into the one shape the row draws — otherwise a screenshot pasted into
// Feishu would arrive on the Web queue as a nameless, unopenable chip.
const mediaUrl = (record: Record<string, unknown>): string => {
  const url = asString(record.url);
  if (url) return url;
  const token = asString(record.token);
  // ``encodeURIComponent`` here is not cosmetic: it is what makes the minted URL
  // satisfy ``isProxyMediaUrl`` by construction, since the escape covers exactly
  // the ``/ ? #`` that would otherwise push the path outside the proxy route.
  return token ? `/api/media/${encodeURIComponent(token)}` : '';
};

// Reads the attachments in source order. Every admitted element produces an
// entry — one with no usable URL still counts and still carries its name,
// because the number of things on the row has to be the number of things in the
// message.
export function readQueuedAttachments(content: unknown): QueuedAttachment[] {
  const raw = asRecord(content)?.attachments;
  if (!Array.isArray(raw)) return [];
  const out: QueuedAttachment[] = [];
  for (const entry of raw) {
    const record = asRecord(entry);
    if (!record) continue;
    const url = mediaUrl(record);
    const mime = asString(record.mime) || asString(record.mimetype);
    // Only a same-origin media-proxy URL is rendered as an <img>: an arbitrary
    // remote URL would be fetched by the browser the moment the row paints, and
    // a queue row paints without anyone asking it to.
    const image = (record.kind === 'image' || mime.startsWith('image/')) && isProxyMediaUrl(url);
    out.push({ url, name: asString(record.name), image });
  }
  return out;
}
