import type { MouseEvent } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from './apiFetch';
import { handleMediaDownloadClick } from './downloadMedia';
import { isIosDevice, isStandalonePwa } from './platform';

vi.mock('./apiFetch', () => ({ apiFetch: vi.fn() }));
vi.mock('./platform', () => ({ isIosDevice: vi.fn(), isStandalonePwa: vi.fn() }));

const share = vi.fn();
const canShare = vi.fn();
const url = '/api/media/test-file';

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(isIosDevice).mockReturnValue(true);
  vi.mocked(isStandalonePwa).mockReturnValue(true);
  share.mockResolvedValue(undefined);
  canShare.mockReturnValue(true);
  vi.stubGlobal('navigator', { share, canShare });
});

afterEach(() => vi.unstubAllGlobals());

function click(filename?: string, href = url) {
  const event = { preventDefault: vi.fn() };
  handleMediaDownloadClick(event as unknown as MouseEvent, href, filename);
  return event;
}

describe('media download filenames', () => {
  it.each([
    ['report.pdf', 'application/pdf', 'Download report'],
    ['报告 (最终).docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'report.pdf'],
    ['data.v2.tar.gz', 'application/gzip', 'Download v2.1'],
    ["100%; user's report.pdf", 'application/pdf', undefined],
    ['README', 'text/plain', 'Notes'],
  ])('shares the server filename unchanged: %s', async (filename, mime, label) => {
    vi.mocked(apiFetch).mockResolvedValue(new Response('file contents', {
      headers: {
        'Content-Type': mime,
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
      },
    }));

    expect(click(label).preventDefault).toHaveBeenCalledOnce();

    await vi.waitFor(() => expect(share).toHaveBeenCalledOnce());
    const file = share.mock.calls[0][0].files[0] as File;
    expect(file.name).toBe(filename);
    expect(file.type).toBe(mime);
    expect(canShare).toHaveBeenCalledWith({ files: [file] });
  });

  it.each([null, "attachment; filename*=UTF-8''bad%ZZ"])(
    'retains the filename fallback for an unavailable server name: %s', async (disposition) => {
      const headers = new Headers({ 'Content-Type': 'application/pdf' });
      if (disposition) headers.set('Content-Disposition', disposition);
      vi.mocked(apiFetch).mockResolvedValue(new Response('file contents', { headers }));
      click('report');
      await vi.waitFor(() => expect(share).toHaveBeenCalledOnce());
      expect(share.mock.calls[0][0].files[0].name).toBe('report.pdf');
    },
  );

  it('leaves browser downloads and external links to native navigation', () => {
    vi.mocked(isStandalonePwa).mockReturnValue(false);
    expect(click().preventDefault).not.toHaveBeenCalled();
    vi.mocked(isStandalonePwa).mockReturnValue(true);
    expect(click(undefined, 'https://example.test/report.pdf').preventDefault).not.toHaveBeenCalled();
    expect(apiFetch).not.toHaveBeenCalled();
  });
});
