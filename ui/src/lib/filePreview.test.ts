import { describe, expect, it } from 'vitest';

import { isEditableFile, isEditableMeta, PREVIEW_MAX_BYTES, previewOverlayKind, previewRenderKind, previewWindowKind } from './filePreview';

const entry = (name: string, size = 100) => ({ kind: 'file', name, size });

describe('File Browser open classification', () => {
  it.each([
    ['photo.png', 'image'],
    ['voice.wav', 'audio'],
    ['clip.mp4', 'video'],
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

describe('audio / video preview classification', () => {
  it.each([
    ...['wav', 'mp3', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'flac', 'weba'].map((ext) => [`clip.${ext}`, 'audio'] as const),
    ...['mp4', 'm4v', 'webm', 'mov', 'ogv'].map((ext) => [`clip.${ext}`, 'video'] as const),
  ])('renders %s as %s', (name, kind) => {
    expect(previewRenderKind(name)).toBe(kind);
  });

  it.each(['clip.aiff', 'clip.aif', 'voice.silk', 'voice.amr'])('does not offer a player for undecodable %s', (name) => {
    expect(previewRenderKind(name)).toBeNull();
  });

  it.each([
    ['audio/x-wav', 'audio'],
    ['audio/mp4a-latm', 'audio'],
    ['audio/mpeg; charset=binary', 'audio'],
    ['video/quicktime', 'video'],
    ['audio/x-aiff', null],
    ['audio/silk', null],
  ])('classifies a label-only chat link by content type %s', (mime, kind) => {
    expect(previewRenderKind('Listen here', mime)).toBe(kind);
  });

  it.each([
    ['clip.ogg', null, 'audio'],
    ['clip.ogg', 'video/ogg', 'video'],
    ['clip.webm', null, 'video'],
    ['clip.webm', 'audio/webm', 'audio'],
  ])('lets an explicit content type pick the track kind of container %s (%s)', (name, mime, kind) => {
    expect(previewRenderKind(name, mime)).toBe(kind);
  });

  it('routes media within the content cap to read-only preview and oversized media to download', () => {
    expect(previewOverlayKind(entry('voice.wav'))).toBe('audio');
    expect(previewOverlayKind(entry('clip.mov', 25 * 1024 * 1024 + 1))).toBeNull();
  });

  it('trusts the server ext over a misleading label suffix', () => {
    expect(previewRenderKind('notes.txt', 'audio/x-wav', 'wav')).toBe('audio');
    expect(previewRenderKind('song.mp3', 'application/pdf', 'pdf')).toBe('pdf');
  });
});
