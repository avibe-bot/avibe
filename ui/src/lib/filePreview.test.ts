import { describe, expect, it } from 'vitest';

import { isEditableFile, isEditableMeta, PREVIEW_MAX_BYTES, previewOverlayKind, previewWindowKind } from './filePreview';

const entry = (name: string, size = 100) => ({ kind: 'file', name, size });

describe('File Browser open classification', () => {
  it.each([
    ['photo.png', 'image'],
    ['document.pdf', 'pdf'],
    ['document.docx', 'docx'],
    ['notes.md', 'markdown'],
    ['drawing.svg', 'svg'],
    ['data.csv', 'csv'],
    ['data.tsv', 'csv'],
  ])('routes %s to the shared preview set', (name, kind) => {
    expect(previewWindowKind(entry(name))).toBe(kind);
  });

  it.each(['notes.md', 'drawing.svg', 'data.csv', 'data.tsv'])('keeps editable preview %s out of Editor read-only tabs', (name) => {
    expect(previewOverlayKind(entry(name))).toBeNull();
  });

  it.each(['settings.json', 'script.py', 'notes.txt'])('keeps %s editor-first', (name) => {
    expect(previewWindowKind(entry(name))).toBeNull();
    expect(isEditableFile(entry(name))).toBe(true);
  });

  it('uses content sniffing for extensionless text without previewing it', () => {
    expect(previewWindowKind(entry('LICENSE'))).toBeNull();
    expect(isEditableMeta({ ...entry('LICENSE'), text: true })).toBe(true);
  });

  it('downloads an unknown binary and oversized text-derived previews', () => {
    expect(previewWindowKind(entry('archive.bin'))).toBeNull();
    expect(isEditableMeta({ ...entry('archive.bin'), text: false })).toBe(false);
    expect(previewWindowKind(entry('large.csv', PREVIEW_MAX_BYTES + 1))).toBeNull();
  });
});
