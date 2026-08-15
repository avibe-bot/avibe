import { describe, expect, it } from 'vitest';

import { SHADOW_KEY, propertyExpression, valueArgument } from './styleWrite.mjs';

// Both halves of this module produced a false positive in one review round, and
// both were the same mistake in different clothes: the scan decided what a span
// MEANT by looking at the bytes beside it. A name ending in `Shadow` was read as
// a style property when it was a variable, and a value on the line below its key
// was read as absent when it was a token. Each failed `validate:theme` for code
// that was correct, which is the failure mode this guard cannot afford -- a
// missed glow leaves the tree where it was, a false positive blocks a pull
// request that never had one.
//
// So the cases below are the property in both directions. What is a style write
// must still be found, and -- the half that keeps the first half honest -- what
// is not one must be left alone.

const key = () => new RegExp(SHADOW_KEY, 'g');

// A style write, and the browser can see it.
const WRITES = [
  ['an inline style property', "<div style={{ boxShadow: '0 0 8px red' }} />"],
  ['a quoted property', "const s = { 'boxShadow': '0 0 8px red' };"],
  ['a computed property', "const s = { ['boxShadow']: '0 0 8px red' };"],
  ['a text-shadow property', "const s = { textShadow: '0 0 8px red' };"],
  ['a CSSOM member assignment', "el.style.boxShadow = '0 0 8px red';"],
  ['a CSSOM bracket assignment', "el.style['boxShadow'] = '0 0 8px red';"],
  ['a CSSOM assignment with spaces', "el.style . boxShadow = '0 0 8px red';"],
];

// Not a style write, and no spelling of the name makes it one.
const NOT_WRITES = [
  // The finding this split exists for. The word is at the tail, exactly where a
  // property has it, and the statement is still a variable.
  ['a variable whose name ends in the word', "const cardShadow = 'compact';"],
  ['a let binding', "let hoverShadow = 'none';"],
  ['a reassigned variable', "cardShadow = 'compact';"],
  // The finding before it, which moving the word to the tail did fix.
  ['a variable whose name starts with the word', "const shadowPreset = 'compact';"],
  ['a shadow root property', 'const root = el.shadowRoot;'],
];

describe('SHADOW_KEY', () => {
  it.each(WRITES)('finds %s', (_label, source) => {
    expect(source.match(key())).not.toBeNull();
  });

  it.each(NOT_WRITES)('leaves %s alone', (_label, source) => {
    expect(source.match(key())).toBeNull();
  });

  // The rule, not the seven spellings above it: an assignment is a style write
  // only through `.style`. Stated separately because the list can only ever hold
  // the spellings someone thought of, and this is the sentence they are examples
  // of.
  it('requires an assignment to go through .style', () => {
    expect('el.style.rogueShadow = x'.match(key())).not.toBeNull();
    expect('el.rogueShadow = x'.match(key())).toBeNull();
  });
});

describe('propertyExpression', () => {
  // Offsets are counted from the character after the key's colon, which is what
  // the scan hands this function.
  const after = (source) => propertyExpression(source, source.indexOf(':') + 1);

  it('reads a value on the same line', () => {
    expect(after("{ boxShadow: '0 0 8px red' }")).toContain('0 0 8px red');
  });

  // The finding. A formatter breaks the line once it is long enough, and the
  // newline was read as the end of the expression -- so a site spelling the
  // required token correctly was reported as one this scan cannot read.
  it('reads a value that starts on the next line', () => {
    expect(after("{\n  boxShadow:\n    'var(--shadow-glow-md-mint)',\n}")).toContain('--shadow-glow-md-mint');
  });

  it('still ends at the newline once the value has begun', () => {
    // Without a semicolon the statement ends at the line break, and reading past
    // it would sweep the next statement's value up as a second shadow layer.
    expect(after("el.style.boxShadow = a\nel.style.color = 'red'")).not.toContain('red');
  });

  it('ends at the next property rather than swallowing it', () => {
    expect(after("{ boxShadow: 'none', color: 'red' }")).not.toContain('red');
  });

  it.each([
    ['a call', "{ boxShadow: rgba(0, 0, 0, 0.5), color: 'red' }", 'rgba'],
    ['an array', "{ boxShadow: [a, b].join(','), color: 'red' }", 'join'],
    ['an object', "{ boxShadow: pick({ a: 1, b: 2 }), color: 'red' }", 'pick'],
    ['a string holding a terminator', "{ boxShadow: '0 0 8px red, 0 0 4px', color: 'red' }", '0 0 4px'],
  ])('reads through %s without stopping inside it', (_label, source, kept) => {
    const expression = after(source);
    expect(expression).toContain(kept);
    expect(expression).not.toContain("color: 'red'");
  });

  // Null rather than an empty string, so an expression that never terminates is
  // reported as unreadable instead of quietly yielding no values to check.
  it('returns null when the expression never ends', () => {
    expect(after('{ boxShadow: rgba(0, 0, 0')).toBeNull();
  });
});

describe('valueArgument', () => {
  const after = (source) => valueArgument(source, source.indexOf(',') + 1);

  it('reads the value argument', () => {
    expect(after("setProperty('box-shadow', '0 0 8px red')")).toContain('0 0 8px red');
  });

  // `setProperty` takes a third argument. Reading every literal after the
  // property name accepted `none` and then failed the tree on `important`.
  it('stops before the priority argument', () => {
    expect(after("setProperty('box-shadow', 'none', 'important')")).not.toContain('important');
  });

  // A newline is not a terminator here, so a call argument written across lines
  // is read whole.
  it('reads an argument written across lines', () => {
    expect(after("setProperty('box-shadow',\n  'var(--shadow-glow-md-mint)')")).toContain('--shadow-glow-md-mint');
  });
});
