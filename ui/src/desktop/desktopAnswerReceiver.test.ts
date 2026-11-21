import { describe, expect, it, vi } from 'vitest';

import { answerBody, createAnswerReceiver, parseAnswer, settle, type AnswerRequest } from './desktopAnswerReceiver';

// From tests/fixtures/message_answer_contract_vectors.json (contract 1). The UI
// build copies nothing from outside ui/, so the vectors this test needs are kept here.
const vectors = {
  questions: [{ options: ['Yes', 'No'], revision: '591e4424a227942cfaa898ff551771f8d762d17663e9e4b77d74c8d983582885' }],
  outcomes: { accepted: 201, duplicate: 200, already_handled: 409, stale: 409, forbidden: 403, not_found: 404 },
};
const revision = vectors.questions[0].revision;
const choice: AnswerRequest = { id: 'op-1', sessionId: 'ses a', messageId: 'msg_1', revision, kind: 'choice', text: 'Yes' };
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('desktop answer receiver', () => {
  it('accepts only a complete request with a contract revision', () => {
    expect(parseAnswer(choice)).toEqual(choice);
    expect(parseAnswer({ ...choice, kind: 'text', text: '先补 changelog' })?.kind).toBe('text');
    for (const broken of [
      { ...choice, id: '' },
      { ...choice, id: 'x'.repeat(129) },
      { ...choice, revision: 'short' },
      { ...choice, revision: revision.toUpperCase() },
      { ...choice, kind: 'vote' },
      { ...choice, text: '  ' },
      { ...choice, sessionId: undefined },
      null,
      'op-1',
    ]) expect(parseAnswer(broken)).toBeNull();
  });

  it('names the option group for a choice and the replied message for text', () => {
    expect(answerBody(choice)).toEqual({ text: 'Yes', operation_id: 'op-1', revision, metadata: { quick_reply_for: 'msg_1' } });
    expect(answerBody({ ...choice, kind: 'text', text: '先补 changelog' })).toEqual({
      text: '先补 changelog', operation_id: 'op-1', revision, in_reply_to: 'msg_1',
    });
  });

  it('reads every contract outcome from the answer receipt, not the status code', async () => {
    for (const [outcome, status] of Object.entries(vectors.outcomes)) {
      expect((await settle(json(status, { answer: { outcome } }))).outcome).toBe(outcome);
    }
    expect(await settle(json(409, { answer: { outcome: 'already_handled', answer_kind: 'choice', choice: '继续 ✅' } })))
      .toEqual({ outcome: 'already_handled', answerKind: 'choice', choice: '继续 ✅' });
    expect(await settle(json(409, { answer: { outcome: 'already_handled', answer_kind: 'text', choice: null } })))
      .toEqual({ outcome: 'already_handled', answerKind: 'text', choice: null });
    expect(await settle(json(200, { answer: { outcome: 'duplicate', original_outcome: null } })))
      .toEqual({ outcome: 'duplicate', originalOutcome: null });
  });

  it('treats outer refusals as refusals and everything uncertain as not delivered', async () => {
    expect((await settle(json(401, { error: 'auth' }))).outcome).toBe('forbidden');
    expect((await settle(json(403, { code: 'agent_access_forbidden' }))).outcome).toBe('forbidden');
    expect((await settle(json(404, { error: 'missing' }))).outcome).toBe('not_found');
    expect((await settle(json(504, { dispatch_error: 'dispatch_pending', answer: { outcome: null } }))).outcome).toBe('unavailable');
    expect((await settle(json(201, { ok: true }))).outcome).toBe('unavailable');
    expect((await settle(null)).outcome).toBe('unavailable');
  });

  it('posts once per click and reports pending, then the settled result', async () => {
    const post = vi.fn().mockResolvedValue(json(201, { answer: { outcome: 'accepted' } }));
    const receiver = createAnswerReceiver(post, 'doc-1');
    expect(receiver.status('op-1')).toBe('unknown');
    expect(receiver.begin(choice)).toBe('started');
    expect(receiver.begin(choice)).toBe('started');
    expect(receiver.status('op-1')).toBe('pending');
    await flush();
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith('/api/sessions/ses%20a/messages', answerBody(choice));
    expect(JSON.parse(receiver.status('op-1'))).toEqual({ outcome: 'accepted' });
    expect(receiver.begin({ ...choice, revision: 'bad' })).toBe('invalid');
  });

  it('posts again for the same click only while its fate is unknown', async () => {
    const post = vi.fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(json(200, { answer: { outcome: 'duplicate', original_outcome: null } }))
      .mockResolvedValueOnce(json(200, { answer: { outcome: 'duplicate', original_outcome: 'accepted' } }));
    const receiver = createAnswerReceiver(post, 'doc-1');
    for (let attempt = 0; attempt < 3; attempt += 1) {
      receiver.begin(choice);
      await flush();
      await flush();
    }
    expect(post).toHaveBeenCalledTimes(3);
    expect(JSON.parse(receiver.status('op-1'))).toEqual({ outcome: 'duplicate', originalOutcome: 'accepted' });
    receiver.begin(choice);
    await flush();
    expect(post).toHaveBeenCalledTimes(3);
  });

  it('settles a failed request as not delivered without retrying', async () => {
    const post = vi.fn().mockRejectedValue(new TypeError('offline'));
    const receiver = createAnswerReceiver(post, 'doc-1');
    receiver.begin(choice);
    await flush();
    await flush();
    expect(post).toHaveBeenCalledTimes(1);
    expect(JSON.parse(receiver.status('op-1'))).toEqual({ outcome: 'unavailable' });
  });
});
