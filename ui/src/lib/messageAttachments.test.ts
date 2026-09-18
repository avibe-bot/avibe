import { describe, expect, it } from 'vitest';

import { readMessageAttachments } from './messageAttachments';

// The shared read boundary every attachment surface goes through. The properties
// here are the ones the queue row, the transcript row and the lightbox gallery
// all depend on, so they are held once rather than three times.

const content = (attachments: unknown) => ({ attachments });

// What ``core/handlers/message_handler`` writes for a file that arrived over an
// IM channel, and what ``public_delivery_payload`` then passes through verbatim.
const IM_IMAGE = { token: 'med_im1', name: 'feishu-screenshot.png', mimetype: 'image/png', size: 4096 };
const IM_FILE = { token: 'med_im2', name: 'trace.log', mimetype: 'text/plain', size: 12 };
// What ``vibe/ui_server`` writes for a Web upload: the proxy URL already minted.
const WEB_IMAGE = { url: '/api/media/med_1', name: 'shot.png', mime: 'image/png', kind: 'image', width: 800, height: 600 };

describe('readMessageAttachments — both producer shapes read as one', () => {
  it('mints a proxy URL from a token, and reads mimetype beside mime', () => {
    const [image, file] = readMessageAttachments(content([IM_IMAGE, IM_FILE]));

    expect(image).toMatchObject({ url: '/api/media/med_im1', name: 'feishu-screenshot.png', image: true });
    // A text file names itself the same way; it is simply not something to fetch
    // as an <img>.
    expect(file).toMatchObject({ url: '/api/media/med_im2', name: 'trace.log', image: false });
  });

  it('keeps a Web upload reading exactly as before, dimensions included', () => {
    // Server-supplied pixel size is what lets the transcript reserve the box
    // before the bytes arrive, so it has to survive the boundary.
    expect(readMessageAttachments(content([WEB_IMAGE]))[0]).toEqual({
      url: '/api/media/med_1',
      name: 'shot.png',
      image: true,
      width: 800,
      height: 600,
    });
  });

  it('preserves source order across mixed shapes', () => {
    const out = readMessageAttachments(content([IM_FILE, WEB_IMAGE, IM_IMAGE]));
    expect(out.map((a) => a.name)).toEqual(['trace.log', 'shot.png', 'feishu-screenshot.png']);
  });

  // The escape is what makes the minted URL satisfy the proxy test by
  // construction rather than by hope: it covers exactly the ``/ ? #`` that would
  // otherwise push the path out of the route.
  it('does not let a token escape the media-proxy path', () => {
    const [att] = readMessageAttachments(content([{ token: '../secrets?x=1', mimetype: 'image/png' }]));
    expect(att.url).toBe('/api/media/..%2Fsecrets%3Fx%3D1');
    expect(att.image).toBe(true);
  });

  // The gallery's whole membership rule is ``att.image``: an arbitrary remote URL
  // must never end up in a list the lightbox pages through, because paging would
  // fetch it.
  it('refuses to call a third-party image fetchable', () => {
    const [att] = readMessageAttachments(
      content([{ url: 'https://files.example.com/a.png?sig=abc', name: 'a.png', mime: 'image/png' }]),
    );
    expect(att.image).toBe(false);
    expect(att.url).toBe('https://files.example.com/a.png?sig=abc');
  });

  it('counts every admitted element, and ignores what is not one', () => {
    // A record with neither URL nor token still occupies its place — the number
    // of things on a row has to be the number of things in the message — while a
    // string or a null is not an attachment at all.
    const out = readMessageAttachments(content([{ name: 'orphan.bin' }, 'nope', null, IM_FILE]));
    expect(out.map((a) => a.name)).toEqual(['orphan.bin', 'trace.log']);
    expect(out[0].url).toBe('');
  });

  it('reads nothing out of content that has no attachments', () => {
    expect(readMessageAttachments(undefined)).toEqual([]);
    expect(readMessageAttachments({})).toEqual([]);
    expect(readMessageAttachments(content('not-an-array'))).toEqual([]);
  });
});
