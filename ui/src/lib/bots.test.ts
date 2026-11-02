import { describe, expect, it } from 'vitest';

import { conversationDetails, parseBot } from './bots';

// What a Bot's conversation shows besides what the Bot says (docs/plans/bots.md D5):
// "Hide details" (per Bot, on by default) and the Settings switch "Show Agent
// Activity" together decide it.
describe('conversationDetails', () => {
  it.each([
    // hide details, Settings switch → execution process, Harness messages, background tasks
    [true, true, { executionProcess: false, harnessMessages: false, backgroundTasks: false }],
    [true, false, { executionProcess: false, harnessMessages: false, backgroundTasks: false }],
    [false, true, { executionProcess: true, harnessMessages: true, backgroundTasks: true }],
    [false, false, { executionProcess: false, harnessMessages: true, backgroundTasks: true }],
  ])('hide details %s with the Settings switch %s', (hideDetails, showAgentActivity, shown) => {
    expect(conversationDetails(hideDetails, showAgentActivity)).toEqual(shown);
  });
});

describe('parseBot', () => {
  it('reads "Hide details" as on unless the server says it is off', () => {
    const payload = { handle: 'cso', display_name: 'CSO', avatar: null, role: '', builtin: false };

    // A server from before the switch existed sends no field.
    expect(parseBot(payload)?.hide_details).toBe(true);
    expect(parseBot({ ...payload, hide_details: true })?.hide_details).toBe(true);
    expect(parseBot({ ...payload, hide_details: false })?.hide_details).toBe(false);
  });
});
