import { apiFetch } from './apiFetch';

// Product safety limit, enforced again by core/workbench_media.py. This is an
// Avibe attachment policy, not an upstream proxy limit.
export const MAX_WORKBENCH_ATTACHMENT_BYTES = 25 * 1024 * 1024;

export type WorkbenchUploadErrorCode =
  | 'empty_file'
  | 'network_error'
  | 'session_not_found'
  | 'too_large'
  | 'upload_failed';

export class WorkbenchUploadError extends Error {
  readonly code: WorkbenchUploadErrorCode;
  readonly status?: number;

  constructor(
    code: WorkbenchUploadErrorCode,
    message: string,
    status?: number,
  ) {
    super(message);
    this.name = 'WorkbenchUploadError';
    this.code = code;
    this.status = status;
  }
}

export type WorkbenchUploadResult = {
  token: string;
  name: string;
  mime: string;
  size: number;
  kind: 'image' | 'file';
  url: string;
  width?: number | null;
  height?: number | null;
};

type ErrorPayload = {
  error?: { code?: string; message?: string };
};

const normalizedErrorCode = (code: unknown): WorkbenchUploadErrorCode => {
  if (code === 'too_large' || code === 'empty_file' || code === 'session_not_found') return code;
  return 'upload_failed';
};

export async function uploadWorkbenchAttachment(
  sessionId: string,
  file: File,
  uploadId: string,
): Promise<WorkbenchUploadResult> {
  if (file.size === 0) throw new WorkbenchUploadError('empty_file', 'File is empty', 400);
  if (file.size > MAX_WORKBENCH_ATTACHMENT_BYTES) {
    throw new WorkbenchUploadError('too_large', 'File exceeds the attachment size limit', 413);
  }

  const form = new FormData();
  form.append('upload_id', uploadId);
  form.append('file', file, file.name);

  let response: Response;
  try {
    response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, {
      method: 'POST',
      body: form,
    });
  } catch (error) {
    throw new WorkbenchUploadError(
      'network_error',
      error instanceof Error ? error.message : 'Network request failed',
    );
  }

  const payload = await response.json().catch(() => null) as (WorkbenchUploadResult & ErrorPayload) | null;
  if (!response.ok || !payload?.token) {
    throw new WorkbenchUploadError(
      normalizedErrorCode(payload?.error?.code),
      payload?.error?.message || `Upload failed (${response.status})`,
      response.status,
    );
  }
  return payload;
}

export function workbenchUploadErrorTranslationKey(error: unknown): string {
  if (!(error instanceof WorkbenchUploadError)) return 'chat.compose.attachmentFailed';
  if (error.code === 'too_large') return 'chat.compose.attachmentTooLarge';
  if (error.code === 'empty_file') return 'chat.compose.attachmentEmpty';
  if (error.code === 'session_not_found') return 'chat.compose.attachmentSessionUnavailable';
  if (error.code === 'network_error') return 'chat.compose.attachmentNetwork';
  return 'chat.compose.attachmentFailed';
}
