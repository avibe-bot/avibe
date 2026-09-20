import { isProxyMediaUrl } from '@/lib/mediaProxy';

// The one place a message's ``content.attachments`` is turned into something the
// UI can draw. Every surface that shows a user's files reads it here — the queue
// strip, the transcript row, and the lightbox gallery — because the same message
// passes through all three as it is queued, flushed and delivered, and a file
// that is recognisable in one of them has to stay recognisable in the next.
//
// Pure, and separate from the components that draw it, for the same reason
// ``annotationView`` is: the rule about which URL may become an <img> is a safety
// property, and belongs somewhere a test can hold it on its own.

export type MessageAttachment = {
  url: string;
  name: string;
  /** True only when the file is an image AND its URL is one we may fetch. */
  image: boolean;
  /** Server-supplied pixel size, when the producer recorded one. */
  width?: number;
  height?: number;
};

const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;

const asString = (value: unknown): string => (typeof value === 'string' ? value : '');

const asSize = (value: unknown): number | undefined =>
  typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : undefined;

// A message carries its attachments in one of two shapes, because two different
// paths write them: a Web upload records ``{url, mime, kind, width, height}``
// (``vibe/ui_server`` mints the proxy URL at upload time), while an IM inbound
// records ``{token, name, mimetype, size}`` (``core/handlers/message_handler``)
// and leaves the URL to whoever renders it. The delivery projection passes
// ``content`` through verbatim, so the second shape survives the flush intact and
// reaches every surface below exactly as it was written. Both name the same media
// object, so both are read into the one shape the UI draws — otherwise a
// screenshot pasted into Feishu would be a nameless, unopenable chip on the
// queue and then vanish from the transcript entirely.
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
// because the number of things on a row has to be the number of things in the
// message. What each surface then does with a URL-less entry is its own
// decision, not this function's.
export function readMessageAttachments(content: unknown): MessageAttachment[] {
  const raw = asRecord(content)?.attachments;
  if (!Array.isArray(raw)) return [];
  const out: MessageAttachment[] = [];
  for (const entry of raw) {
    const record = asRecord(entry);
    if (!record) continue;
    const url = mediaUrl(record);
    const mime = asString(record.mime) || asString(record.mimetype);
    // Only a same-origin media-proxy URL is rendered as an <img>: an arbitrary
    // remote URL would be fetched by the browser the moment the row paints, and
    // neither a queue row nor a transcript row paints because anyone asked it to.
    const image = (record.kind === 'image' || mime.startsWith('image/')) && isProxyMediaUrl(url);
    out.push({
      url,
      name: asString(record.name),
      image,
      width: asSize(record.width),
      height: asSize(record.height),
    });
  }
  return out;
}
