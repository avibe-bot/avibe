import { describe, expect, it } from 'vitest';

import { parseSource, withoutNonRenderingText } from './nonRenderingText.mjs';
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
  // The third of JavaScript's three string quotes. Missing it let a hardcoded
  // glow ship with every guard green, and there is no fourth: the language has
  // exactly these, so this enumeration is closed by the grammar rather than by
  // whoever last thought about it.
  ['a backtick computed property', "const s = { [`boxShadow`]: '0 0 8px red' };"],
  ['a text-shadow property', "const s = { textShadow: '0 0 8px red' };"],
  // A hyphenated name is not a JavaScript identifier, so these two spellings can
  // only ever appear quoted -- and quoted, they are style writes like any other.
  ['a hyphenated object key', "const s = { 'box-shadow': '0 0 8px red' };"],
  ['a hyphenated CSSOM key', "el.style['box-shadow'] = '0 0 8px red';"],
  ['a vendor-prefixed property', "const s = { WebkitBoxShadow: '0 0 8px red' };"],
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
  // CSS, not JavaScript. Accepting the hyphenated names BARE here handed every
  // stylesheet declaration in the tree to a channel that reads JS expressions,
  // which returned nothing -- four correct declarations reported as values the
  // scan cannot read, in CI, on a pull request with no defect in it.
  ['a CSS declaration', '.a { box-shadow: 0 0 8px red; }'],
  ['a vendor-prefixed CSS declaration', '.a { -webkit-box-shadow: 0 0 8px red; }'],
  ['a CSS declaration after a string', '.a { content: "x"; box-shadow: 0 0 8px red; }'],
];

describe('SHADOW_KEY', () => {
  it.each(WRITES)('finds %s', (_label, source) => {
    expect(source.match(key())).not.toBeNull();
  });

  it.each(NOT_WRITES)('leaves %s alone', (_label, source) => {
    expect(source.match(key())).toBeNull();
  });

  // The rules, not the spellings above them. Stated separately because a list
  // can only ever hold the spellings someone thought of, and these are the two
  // sentences those spellings are examples of.
  it('requires an assignment to go through .style', () => {
    expect('el.style.boxShadow = x'.match(key())).not.toBeNull();
    expect('el.boxShadow = x'.match(key())).toBeNull();
  });

  // The second rule, and the one that makes the first affordable. A style object
  // and `element.style` both address real CSS properties, so a key that is not
  // one of those draws nothing whatever it is called -- which is why the list of
  // properties can be closed at all. `[A-Za-z]*[Ss]hadow` was the guess it
  // replaces, and every round of this review found another name satisfying it
  // without being a property.
  it('reads only names that are CSS properties', () => {
    expect('el.style.boxShadow = x'.match(key())).not.toBeNull();
    expect('el.style.rogueShadow = x'.match(key())).toBeNull();
  });

  // The rule the three CSS rows above are examples of, and the reason it is a
  // rule rather than three exceptions: a hyphenated name is not a JavaScript
  // identifier, so the quote is not decoration on how a key is written -- it is
  // the whole boundary between the two languages this scan reads.
  it('requires a quote around a name JavaScript cannot spell bare', () => {
    expect("const s = { 'box-shadow': x };".match(key())).not.toBeNull();
    expect('.a { box-shadow: x; }'.match(key())).toBeNull();
  });
});

describe('propertyExpression', () => {
  // Composed the way the scan composes it -- key regex, blanked text, tree of
  // the file as written -- rather than from a hand-counted offset. That is not
  // tidiness: the offset used to be `indexOf(':') + 1`, which finds the
  // TERNARY's colon in `boxShadow = on ? a : b` and finds nothing at all in an
  // assignment, so the ASI case below ran from offset 0 and passed while
  // asserting nothing about the question it was named for.
  const after = (source, file = 'probe.ts') => {
    const blanked = withoutNonRenderingText(source, file);
    const key = new RegExp(SHADOW_KEY, 'g').exec(blanked);

    expect(key, `no style write in ${source}`).not.toBeNull();

    return propertyExpression(blanked, key.index + key[0].length, parseSource(source, file));
  };

  it('reads a value on the same line', () => {
    expect(after("const s = { boxShadow: '0 0 8px red' };")).toContain('0 0 8px red');
  });

  // The finding. A formatter breaks the line once it is long enough, and the
  // newline was read as the end of the expression -- so a site spelling the
  // required token correctly was reported as one this scan cannot read.
  it('reads a value that starts on the next line', () => {
    expect(after("const s = {\n  boxShadow:\n    'var(--shadow-glow-md-mint)',\n};")).toContain('--shadow-glow-md-mint');
  });

  it('still ends at the newline once the value has begun', () => {
    // Without a semicolon the statement ends at the line break, and reading past
    // it would sweep the next statement's value up as a second shadow layer.
    expect(after("el.style.boxShadow = a\nel.style.color = 'red';")).not.toContain('red');
  });

  // The pair that has no punctuation answer, and the reason this asks the
  // parser at all. Both values are an assignment with no semicolon and a line
  // break after it; the first line break ends the statement and the second does
  // not, because `?` cannot begin one. Any rule about characters gets one of
  // these two wrong, and six rounds of this module's history are that rule being
  // rewritten.
  it('follows a value across the line breaks that do not end the statement', () => {
    const source = "el.style.boxShadow = on\n  ? 'var(--shadow-glow-md-mint)'\n  : 'none'\nel.style.color = 'red';";
    const expression = after(source);

    expect(expression).toContain('--shadow-glow-md-mint');
    expect(expression).toContain('none');
    expect(expression).not.toContain('red');
  });

  it('reads both branches of a ternary written across lines', () => {
    const source = "const s = {\n  boxShadow: on\n    ? 'var(--shadow-glow-md-mint)'\n    : 'none',\n  color: 'red',\n};";
    const expression = after(source);

    expect(expression).toContain('--shadow-glow-md-mint');
    expect(expression).not.toContain('red');
  });

  it('ends at the next property rather than swallowing it', () => {
    expect(after("const s = { boxShadow: 'none', color: 'red' };")).not.toContain('red');
  });

  it.each([
    ['a call', "const s = { boxShadow: rgba(0, 0, 0, 0.5), color: 'red' };", 'rgba'],
    ['an array', "const s = { boxShadow: [a, b].join(','), color: 'red' };", 'join'],
    ['an object', "const s = { boxShadow: pick({ a: 1, b: 2 }), color: 'red' };", 'pick'],
    ['a string holding a terminator', "const s = { boxShadow: '0 0 8px red, 0 0 4px', color: 'red' };", '0 0 4px'],
  ])('reads through %s without stopping inside it', (_label, source, kept) => {
    const expression = after(source);
    expect(expression).toContain(kept);
    expect(expression).not.toContain("color: 'red'");
  });

  // A value is allowed to carry a comment, so the span comes from the BLANKED
  // text while its boundary comes from the tree of the file as written. Reading
  // the comment back would hand the caller a colour that never renders -- the
  // guard's own defect, one layer down.
  it('skips a comment between the key and the value', () => {
    const expression = after("const s = { boxShadow: /* red */ 'none' };");

    expect(expression).toContain('none');
    expect(expression).not.toContain('red');
  });

  // The tree is what answers the question, so a language that has none gets no
  // answer rather than a guess. Nothing in a stylesheet should reach here at all
  // -- the key regex hands CSS to the declaration channel -- and this is the
  // second lock on that door, because the round where it opened turned four
  // correct declarations into CI failures.
  it('reads nothing without a tree', () => {
    const css = '.a { box-shadow: 0 0 8px red; }';

    expect(propertyExpression(css, css.indexOf(':') + 1, null)).toBeNull();
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
