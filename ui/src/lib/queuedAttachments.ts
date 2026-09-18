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
    const url = asString(record.url);
    const mime = asString(record.mime);
    // Only a same-origin media-proxy URL is rendered as an <img>: an arbitrary
    // remote URL would be fetched by the browser the moment the row paints, and
    // a queue row paints without anyone asking it to.
    const image = (record.kind === 'image' || mime.startsWith('image/')) && isProxyMediaUrl(url);
    out.push({ url, name: asString(record.name), image });
  }
  return out;
}
