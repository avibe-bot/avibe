import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('./apiFetch', () => api);

import {
  isWorkbenchUploadRetryable,
  MAX_WORKBENCH_ATTACHMENT_BYTES,
  uploadWorkbenchAttachment,
  WorkbenchUploadError,
} from './workbenchUpload';

describe('workbench attachment upload', () => {
  beforeEach(() => {
    api.apiFetch.mockReset();
  });

  it('uploads the original binary as multipart form data', async () => {
    api.apiFetch.mockResolvedValue(Response.json({
      token: 'media-token',
      name: '报告.txt',
      mime: 'text/plain',
      size: 5,
      kind: 'file',
      url: '/api/media/media-token',
    }, { status: 201 }));
    const file = new File(['hello'], '报告.txt', { type: 'text/plain' });

    await expect(uploadWorkbenchAttachment('ses/one', file, 'upload-id-123456')).resolves.toMatchObject({
      token: 'media-token',
      size: 5,
    });

    const [path, init] = api.apiFetch.mock.calls[0];
    expect(path).toBe('/api/sessions/ses%2Fone/attachments');
    expect(init.method).toBe('POST');
    expect(init.headers).toBeUndefined();
    expect((init.body as FormData).get('upload_id')).toBe('upload-id-123456');
    expect((init.body as FormData).get('file')).toMatchObject({
      name: '报告.txt',
      size: 5,
      type: 'text/plain',
    });
  });

  it('rejects oversized files before starting a request', async () => {
    const file = new File(['x'], 'large.bin');
    Object.defineProperty(file, 'size', { value: MAX_WORKBENCH_ATTACHMENT_BYTES + 1 });

    await expect(uploadWorkbenchAttachment('ses-1', file, 'upload-id-123456')).rejects.toMatchObject({
      code: 'too_large',
      status: 413,
    });
    expect(api.apiFetch).not.toHaveBeenCalled();
  });

  it('preserves a structured server failure for localized UI handling', async () => {
    api.apiFetch.mockResolvedValue(Response.json({
      ok: false,
      error: { code: 'session_not_found', message: 'Session not found' },
    }, { status: 404 }));

    const promise = uploadWorkbenchAttachment(
      'missing',
      new File(['x'], 'x.txt'),
      'upload-id-123456',
    );

    await expect(promise).rejects.toEqual(expect.objectContaining({
      name: 'WorkbenchUploadError',
      code: 'session_not_found',
      status: 404,
    }));
  });

  it('classifies fetch failures as retryable network errors', async () => {
    api.apiFetch.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(
      uploadWorkbenchAttachment('ses-1', new File(['x'], 'x.txt'), 'upload-id-123456'),
    ).rejects.toMatchObject({ code: 'network_error' });
  });

  it('only retries failures that can succeed with the same file and session', () => {
    expect(isWorkbenchUploadRetryable(new WorkbenchUploadError('network_error', 'offline'))).toBe(true);
    expect(isWorkbenchUploadRetryable(new WorkbenchUploadError('upload_failed', 'server', 503))).toBe(true);
    expect(isWorkbenchUploadRetryable(new WorkbenchUploadError('too_large', 'large', 413))).toBe(false);
    expect(isWorkbenchUploadRetryable(new WorkbenchUploadError('empty_file', 'empty', 400))).toBe(false);
    expect(isWorkbenchUploadRetryable(
      new WorkbenchUploadError('session_not_found', 'missing', 404),
    )).toBe(false);
    expect(isWorkbenchUploadRetryable(new WorkbenchUploadError('upload_failed', 'invalid', 400))).toBe(false);
  });
});
