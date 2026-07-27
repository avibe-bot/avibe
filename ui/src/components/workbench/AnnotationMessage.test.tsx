import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { isProxyMediaUrl } from '../../lib/mediaProxy';
import { isTranscriptMessage } from '../../lib/chatMessageTypes';
import { AnnotationMessage, annotationTitleKey, claimAnnotation, readAnnotationView } from './AnnotationMessage';

// The four rows of the frozen contract, verbatim:
//   docs/plans/show-annotation-message-type/examples.json
// They are copied rather than imported because the contract lives on Lane BE's
// branch — copying them here is what makes this a CONTRACT test: if Lane BE's
// serializer stops producing these shapes, its own assertions go red against
// the same file and the two halves are still pinned to one artifact.
//
// ``metadata`` is carried in full on purpose. Nothing in the card may read it,
// and the machine-text assertion below can only be honest if the machine text
// is actually present in the fixture.
const FORWARD_COMMENT = {
  id: 'msg_01J8XK2Q4W',
  type: 'annotation',
  author: 'harness',
  source: 'harness',
  author_name: 'show_annotation',
  text: '这里的标题太小了，正文和它几乎一样重',
  content: {
    text: '这里的标题太小了，正文和它几乎一样重',
    annotation: { direction: 'user', action: 'created', quote: 'Model Hub' },
  },
  metadata: {
    source: 'show_page',
    show_event_id: 'evt_7f3a91c2',
    show_event_type: 'human.annotation.created',
    anchor_kind: 'element',
    anchor_selector: 'main > section:nth-of-type(2) > h2',
    _queued_dispatch_text:
      "[show-annotation] comment\n\n这里的标题太小了，正文和它几乎一样重\n\nAnchor kind: element\n\nQuote: Model Hub\nAnchor: main > section:nth-of-type(2) > h2\n\nShow event id: evt_7f3a91c2\n\n如需在页面上原位回应，可执行：\n  vibe show reply evt_7f3a91c2 --message '<你的回答>'\n（也可以直接修改页面内容来响应，按场景选择。）",
  },
  created_at: '2026-07-27T10:14:02.417Z',
};

const FORWARD_QUEUED = {
  id: 'msg_01J8XK5M8T',
  type: 'queued',
  author: 'harness',
  source: 'harness',
  author_name: 'show_annotation',
  text: '这两个卡片的间距不一致',
  content: {
    text: '这两个卡片的间距不一致',
    annotation: { direction: 'user', action: 'created' },
    attachments: [
      {
        url: '/api/media/med_9a71c33f8b2e',
        name: 'annotation-region.png',
        mime: 'image/png',
        kind: 'image',
        width: 1240,
        height: 620,
      },
    ],
  },
  metadata: {
    source: 'show_page',
    show_event_id: 'evt_b0c4d5e6',
    anchor_kind: 'screenshot',
    screenshot_region: 'x:120, y:340, 1240x620',
    _queued_dispatch_text:
      "[show-annotation] comment\n\n这两个卡片的间距不一致\n\nAnchor kind: screenshot\nScreenshot: state/media/med_9a71c33f8b2e.png (1240x620)\nScreenshot region: x:120, y:340, 1240x620\n\nShow event id: evt_b0c4d5e6\n\n如需在页面上原位回应，可执行：\n  vibe show reply evt_b0c4d5e6 --message '<你的回答>'\n（也可以直接修改页面内容来响应，按场景选择。）",
  },
  created_at: '2026-07-27T10:15:48.006Z',
};

const REVERSE_MARK = {
  id: 'msg_01J8XKB1Q9',
  type: 'annotation',
  author: 'agent',
  source: null,
  author_name: null,
  text: '标题已经改成 20px/600，正文降到 14px，层级现在拉开了。',
  content: {
    text: '标题已经改成 20px/600，正文降到 14px，层级现在拉开了。',
    annotation: { direction: 'agent', action: 'created', quote: 'Model Hub' },
  },
  metadata: { source: 'show_page', show_event_id: 'evt_c8d9e0f1' },
  created_at: '2026-07-27T10:19:31.882Z',
};

const REVERSE_RESOLVED = {
  id: 'msg_01J8XKF7R3',
  type: 'annotation',
  author: 'agent',
  source: null,
  author_name: null,
  text: '间距统一到 16px 了。',
  content: {
    text: '间距统一到 16px 了。',
    annotation: { direction: 'agent', action: 'resolved' },
  },
  metadata: { source: 'show_page', show_event_id: 'evt_d2e3f4a5' },
  created_at: '2026-07-27T10:21:07.140Z',
};

const makeI18n = (lng: 'en' | 'zh') => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return i18n;
};

// Mirrors how ChatPage feeds the card: the transcript builds the body,
// attachment and timestamp nodes and the card only arranges them. The stand-ins
// are marked so a presence assertion can tell them apart from card chrome.
//
// ``metadata`` is deliberately NOT a prop of the card, which is what makes the
// no-machine-text property structural rather than incidental — there is no
// parameter through which a dispatch string could reach the view.
type Row = { id: string; type: string; text: string; content: unknown; created_at: string };

const render = (row: Row, lng: 'en' | 'zh' = 'zh') => {
  const view = claimAnnotation(row);
  if (!view) throw new Error(`row ${row.id} was not claimed as an annotation`);
  const attachments = (row.content as { attachments?: unknown[] }).attachments ?? [];
  return renderToStaticMarkup(
    <I18nextProvider i18n={makeI18n(lng)}>
      <AnnotationMessage
        messageId={row.id}
        view={view}
        body={<div data-part="body">{row.text}</div>}
        attachments={attachments.length > 0 ? <div data-part="attachments" /> : null}
        time={<span data-part="time">{row.created_at}</span>}
        rowClass={(extra) => `flex w-full ${extra}`}
      />
    </I18nextProvider>,
  );
};

describe('annotation card / frozen contract rows', () => {
  it('titles and sides a forward user annotation by direction, not by author', () => {
    // The single easiest thing to get wrong. This row is authored by ``harness``
    // with author_name ``show_annotation``: read the author and it lands on the
    // left behind a harness chip, which is the bug. Cross-assert the fixture's
    // own author so the side assertion below cannot pass vacuously.
    expect(FORWARD_COMMENT.author).toBe('harness');
    expect(FORWARD_COMMENT.source).toBe('harness');

    const html = render(FORWARD_COMMENT);
    expect(html).toContain('用户批注');
    expect(html).not.toContain('Agent 批注');
    expect(html).toContain('justify-end');
    expect(html).not.toContain('justify-start');
  });

  it('titles and sides a reverse agent mark by the same rule', () => {
    const html = render(REVERSE_MARK);
    expect(html).toContain('Agent 批注');
    expect(html).not.toContain('用户批注');
    expect(html).toContain('justify-start');
    expect(html).not.toContain('justify-end');
  });

  it('draws the anchor quote only when the anchor carries copy', () => {
    // Present on the comment row...
    expect(render(FORWARD_COMMENT)).toContain('Model Hub');
    // ...and absent on the region row, which has no quote. Nothing stands in for
    // it: not the selector, not the anchor kind, not the event id, not the
    // screenshot path (rule 05).
    const queuedAsFlushed = { ...FORWARD_QUEUED, type: 'annotation' };
    const html = render(queuedAsFlushed);
    expect(readAnnotationView(queuedAsFlushed.content)?.quote).toBeUndefined();
    expect(html).not.toContain('border-l-2');
    expect(html).not.toContain('screenshot');
    expect(html).not.toContain('state/media/');
    expect(html).not.toContain('evt_b0c4d5e6');
  });

  it('marks only a resolved action, and never in the title', () => {
    const created = render(REVERSE_MARK);
    expect(created).not.toContain('已处理');

    const resolved = render(REVERSE_RESOLVED);
    expect(resolved).toContain('已处理');
    // Rule 02/07: the action decorates the card, it does not rename it.
    expect(resolved).toContain('Agent 批注');
  });

  it('routes the screenshot through the transcript attachment renderer', () => {
    // Rule 06: the card renders the node the transcript already builds — there
    // is no second image element in it.
    const html = render({ ...FORWARD_QUEUED, type: 'annotation' });
    expect(html).toContain('data-part="attachments"');
    expect(html).not.toContain('<img');
    // ...and the frozen url is one the existing renderer inlines rather than
    // degrading to a click-through card.
    const url = FORWARD_QUEUED.content.attachments[0].url;
    expect(isProxyMediaUrl(url)).toBe(true);
  });

  it('keeps the agent-facing dispatch text out of the view entirely', () => {
    // The machine text is real: it is in the fixture, on the row, right now.
    const dispatch = FORWARD_COMMENT.metadata._queued_dispatch_text;
    expect(dispatch).toContain('vibe show reply');
    expect(dispatch).toContain('main > section:nth-of-type(2) > h2');
    expect(dispatch).toContain('evt_7f3a91c2');

    // None of it reaches the reader. The card is given the human text and
    // nothing else — there is no ``metadata`` prop to leak through.
    const html = render(FORWARD_COMMENT);
    for (const machine of [
      'vibe show reply',
      '[show-annotation]',
      'Anchor kind',
      'main > section:nth-of-type(2) > h2',
      'evt_7f3a91c2',
      'show_annotation',
      '/state/media/',
    ]) {
      expect(html).not.toContain(machine);
    }
    // The human text, meanwhile, is there.
    expect(html).toContain('这里的标题太小了');
  });

  it('renders the same card in English', () => {
    expect(render(FORWARD_COMMENT, 'en')).toContain('User annotation');
    expect(render(REVERSE_MARK, 'en')).toContain('Agent annotation');
    expect(render(REVERSE_RESOLVED, 'en')).toContain('Resolved');
  });
});

describe('annotation claim (type decides, direction places)', () => {
  it('claims the two annotation rows and refuses the queued one', () => {
    expect(claimAnnotation(FORWARD_COMMENT)?.direction).toBe('user');
    expect(claimAnnotation(REVERSE_MARK)?.direction).toBe('agent');
    expect(claimAnnotation(REVERSE_RESOLVED)?.resolved).toBe(true);

    // Same row, same ``show_page`` origin, same display record — only the type
    // differs, and the type is the whole decision. It is not a card yet, and
    // (asserted at its source) not in the transcript yet either.
    expect(claimAnnotation(FORWARD_QUEUED)).toBeNull();
    expect(isTranscriptMessage(FORWARD_QUEUED)).toBe(false);
    expect(claimAnnotation({ ...FORWARD_QUEUED, type: 'annotation' })).not.toBeNull();
    expect(isTranscriptMessage({ ...FORWARD_QUEUED, type: 'annotation' })).toBe(true);
  });

  it('degrades a row with no usable display record to an ordinary bubble', () => {
    // A contract violation must not produce a card that cannot say whose
    // annotation it is; it narrows to a plain row instead.
    expect(claimAnnotation({ type: 'annotation', content: {} })).toBeNull();
    expect(claimAnnotation({ type: 'annotation', content: { annotation: { direction: 'system' } } })).toBeNull();
    expect(claimAnnotation({ type: 'annotation', content: null })).toBeNull();
  });

  it('maps each direction to its own title key, and only those two', () => {
    expect(annotationTitleKey('user')).toBe('chat.annotation.titleUser');
    expect(annotationTitleKey('agent')).toBe('chat.annotation.titleAgent');
    expect(en.chat.annotation.titleUser).toBe('User annotation');
    expect(zh.chat.annotation.titleUser).toBe('用户批注');
    expect(zh.chat.annotation.titleAgent).toBe('Agent 批注');
    expect(zh.chat.annotation.resolved).toBe('已处理');
  });
});
