// Which custom properties a class can actually resolve where it renders.
//
// `customPropertiesIn` answers "is this name declared anywhere", which is the
// question that let this defect ship. Every `--model-hub-guard-*` token was
// declared -- on `.model-hub-guard-dialog` -- so the token layer looked whole.
// But `GuardImpact` and `GuardGapList` are the shared evidence body for a
// guarded Model Hub mutation, and three of the four dialogs that mount them are
// not that dialog. In those three the `var()` named a property no ancestor
// declares, which is invalid at computed-value time, so the declaration drew
// nothing: `gap` fell back to `normal`, `font-size` to the inherited size, and
// the count's `padding` to zero. Not a pill missing a margin -- "Hops that will
// be removed2 hops" in one unbroken run.
//
// A rendered test cannot catch it. jsdom computes no cascade and loads no
// stylesheet, so `getComputedStyle` there reports the inline truth and agrees
// with itself in every root. The only place the mistake exists is the
// stylesheet's scope graph, so that is what gets read.
//
// The property asserted is self-sufficiency, not a list of tokens: a class
// resolves every name it consumes from itself or from a scope every element
// inherits. It is asserted of EVERY class this project styles rather than of
// the ones known to travel, because "this one is only ever mounted there" is a
// fact about the React tree that the stylesheet cannot see and that the next
// caller can falsify. A token added to such a class later is covered without
// editing anything, and a token parked back on a dialog root fails here rather
// than in a screenshot nobody takes.
//
// "Resolves" is the whole difficulty, and it is not "is declared somewhere".
// A declaration counts only where it is in force, so both halves of its reach
// are compared against the use: which subtree its selector claims, and which
// conditions its enclosing at-rules put on it. A rule that answers only the
// first half sanctions exactly the kind of scope this file exists to catch.

import { customPropertiesIn } from './customProperties.mjs';

// A selector that matches the document root itself, so a custom property
// declared there is inherited by every element regardless of where it mounts.
// The whole selector has to match, not its prefix. A qualified root reaches
// every element only while its qualifier holds, which is a narrower promise
// than the one being consumed: `[data-theme="dark"]` names an attribute this
// repo also sets on nested elements (`AppWindow.tsx:259`,
// `AppsEditorPage.tsx:114`, `confirm-dialog.tsx:108`), and even on `<html>` it
// is one half of a theme pair, so a token declared only there is missing
// wherever the other half applies. Such tokens resolve because a `:root` base
// declares them and the themed block overrides it -- and that base is what
// this looks for.
const DOCUMENT_ROOT = /^(:root|html|body|\*)$/;

// An at-rule whose body applies only when its condition holds. A declaration
// inside one is in force for a use only if the use is inside it too, so the
// chain is compared rather than discarded -- 72 token declarations and 19 uses
// in this project sit under `@media`, so the difference is not hypothetical.
// `@layer` and `@theme` are deliberately absent: they set precedence and
// origin, not whether the body applies. An at-rule spelled some new way counts
// as unconditional, a miss rather than a false positive -- the same bias as
// the fallback rule below.
const CONDITIONAL = new Set(['media', 'supports', 'container', 'scope', 'document']);

// `var(--x)` on an undeclared name draws nothing; `var(--x, 6px)` draws 6px.
// The second is not this defect, so the fallback is what decides whether a use
// counts -- otherwise every deliberately-optional token reads as a violation
// and the gate fails correct CSS.
const UNGUARDED_USE = /var\(\s*(--[\w-]+)\s*\)/g;

function insideTheme(node) {
  for (let at = node.parent; at; at = at.parent) {
    if (at.type === 'atrule' && at.name === 'theme') return true;
  }
  return false;
}

// Every condition standing between a node and the stylesheet, innermost first.
// Params are whitespace-collapsed so one query spelled two ways still reads as
// one condition.
function conditionsOn(node) {
  const chain = [];
  for (let at = node.parent; at; at = at.parent) {
    if (at.type === 'atrule' && CONDITIONAL.has(at.name)) {
      chain.push(`@${at.name} ${at.params.replace(/\s+/g, ' ').trim()}`);
    }
  }
  return chain;
}

// Whether any of `chains` describes a declaration that is in force wherever a
// use guarded by `conditions` applies. That holds when every condition on the
// declaration also guards the use: the use cannot apply without them, so the
// declaration cannot be missing. An unconditional declaration has an empty
// chain and so is in force everywhere, which is the ordinary case.
function inForce(chains, conditions) {
  return (chains ?? []).some((chain) => chain.every((one) => conditions.includes(one)));
}

// The leftmost compound of a selector is the only part that says which subtree
// a rule belongs to. `.guard-label > span` and `.guard-hop strong` are the
// travelling body styling its own descendants, so their tokens are the
// travelling body's problem; `.catalog-dialog .guard-label` would be a rule
// about one dialog and is deliberately none of this fold's business.
function firstCompound(selector) {
  let depth = 0;
  for (let index = 0; index < selector.length; index += 1) {
    const character = selector[index];
    if (character === '(' || character === '[') depth += 1;
    else if (character === ')' || character === ']') depth -= 1;
    else if (depth === 0 && ' >+~'.includes(character)) return selector.slice(0, index);
  }
  return selector;
}

// `:not(.model-hub-guard-dialog)` names a class it excludes, so the
// parenthesised groups come out before the class names go in.
function classesOf(compound) {
  const bare = compound.replace(/\([^)]*\)/g, '');
  return [...bare.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((match) => match[1]);
}

// The value of one JSX attribute, wherever it is spelled in a source. A quoted
// literal ends at its closing quote; a braced expression is read whole, so
// `cn('a', flag && 'b')` contributes both halves rather than its first.
function* attributeValues(source, attribute) {
  const opening = new RegExp(`\\b${attribute}\\s*=\\s*`, 'g');

  for (let match = opening.exec(source); match; match = opening.exec(source)) {
    const cursor = match.index + match[0].length;
    let end = cursor;

    if (source[cursor] === '{') {
      let depth = 0;
      for (; end < source.length; end += 1) {
        if (source[end] === '{') depth += 1;
        else if (source[end] === '}') {
          depth -= 1;
          if (depth === 0) { end += 1; break; }
        }
      }
    } else {
      const quote = source[cursor];
      if (quote !== '"' && quote !== "'" && quote !== '`') continue;
      end = source.indexOf(quote, cursor + 1) + 1;
      if (end === 0) continue;
    }

    yield source.slice(cursor, end);
  }
}

/**
 * Every class name a component renders, read from its own source.
 *
 * The component is the authority on this, not a list kept next to the check: a
 * class it stops rendering stops being asserted, and one it starts rendering
 * starts. Only `className` is read, so a class named in a comment or a log line
 * is not mistaken for one that ships.
 */
function classesRenderedBy(source) {
  const found = new Set();

  for (const expression of attributeValues(source, 'className')) {
    for (const literal of expression.matchAll(/["'`]([^"'`]*)["'`]/g)) {
      for (const name of literal[1].split(/\s+/)) if (name) found.add(name);
    }
  }

  return found;
}

/**
 * Every custom property a component consumes from its own markup.
 *
 * A Tailwind arbitrary value is a `var()` like any other -- `gap-[var(--x)]`
 * compiles to `gap: var(--x)` -- but it is written on an element, not in a rule,
 * so no walk of the stylesheet can see it and no selector says which scope it
 * expects. `BackendModelCatalogDialog` renders one on a `<ul>` carrying no
 * `model-hub-*` class at all, which is a use with no subject to attribute it
 * to: the only thing that can answer for it is a scope every element inherits.
 * `style` is read alongside `className` because `style={{ gap: 'var(--x)' }}`
 * is the same use spelled the other way.
 */
function tokensUsedInMarkup(source) {
  const found = new Set();

  for (const attribute of ['className', 'style']) {
    for (const expression of attributeValues(source, attribute)) {
      for (const match of expression.matchAll(UNGUARDED_USE)) found.add(match[1]);
    }
  }

  return found;
}

// A selector made of nothing but classes, so every element it matches is known
// from its class list alone. That is what lets a declaration on `.segment`
// answer for a use whose subject is `.segment.is-selected`: both classes sit on
// one element, so the declaration is on the element the use applies to. Any
// other ingredient -- an attribute, a state pseudo, a tag -- adds a condition
// the use's own subject does not carry, and counting it would sanction a
// declaration that is only sometimes there.
const PURE_CLASSES = /^(?:\.-?[_a-zA-Z][\w-]*)+$/;

/**
 * Every custom property some scope every element inherits, keyed to the
 * conditions each declaration of it was made under.
 *
 * Being declared somewhere is not the question -- that is what
 * `customPropertiesIn` answers, and what let this defect ship. A declaration
 * counts only where it is in force, so the conditions travel with it.
 */
function inheritableTokens(sheets) {
  const inheritable = new Map();
  const record = (key, conditions) => {
    if (!inheritable.has(key)) inheritable.set(key, []);
    inheritable.get(key).push(conditions);
  };

  for (const [, root] of sheets) {
    // A registered name resolves to its initial value everywhere, so a use of
    // one is not this defect. A registration carrying no initial value under
    // `syntax: "*"` does draw nothing, which this treats as resolvable and so
    // will not report -- a miss rather than a false positive, on the same
    // grounds `customProperties.mjs` records the name at all.
    root.walkAtRules('property', (rule) => record(rule.params.trim(), conditionsOn(rule)));

    root.walkDecls((declaration) => {
      if (!declaration.prop.startsWith('--')) return;

      const owner = declaration.parent;
      const fromRoot = owner.type === 'rule'
        && owner.selectors.some((one) => DOCUMENT_ROOT.test(one.trim()));
      if (insideTheme(declaration) || fromRoot) record(declaration.prop, conditionsOn(declaration));
    });
  }

  return inheritable;
}

/**
 * Every class name some rule in ``sheets`` takes as its subject.
 *
 * Derived rather than listed, so the invariant can be asserted over the whole
 * token layer without naming a component root anywhere. A class this project
 * styles is a class this project has to be able to resolve tokens for, under
 * whichever root a caller mounts it. A rule whose subject is an element, an
 * attribute or the document root contributes nothing: there is no class to
 * mount elsewhere, so there is nothing for the scope question to be about.
 */
function styledSubjects(sheets) {
  const subjects = new Set();

  for (const [, root] of sheets) {
    root.walkRules((rule) => {
      for (const one of rule.selectors) {
        for (const name of classesOf(firstCompound(one.trim()))) subjects.add(name);
      }
    });
  }

  return subjects;
}

/**
 * The tokens ``classNames`` consume but cannot resolve where they render.
 *
 * ``sheets`` is the materialised `eachStylesheet` walk -- materialised because
 * this needs two passes over the same trees, and a generator gives one.
 * Returns one entry per unresolvable use, naming the class whose subtree
 * consumes it and the origin to report, so a failure reads as an instruction.
 */
function unscopedTokens(sheets, classNames) {
  const audited = new Set(classNames);
  const inheritable = inheritableTokens(sheets);
  // Keyed by property rather than by class, because what makes a declaration
  // count is which elements its whole selector claims -- `.a.b` answers for a
  // use on `.a.b.c` and for nothing else -- and that is a question about the
  // set, not about any one name in it.
  const declaredOn = new Map();

  for (const [, root] of sheets) {
    root.walkDecls((declaration) => {
      if (!declaration.prop.startsWith('--')) return;

      const owner = declaration.parent;
      if (owner.type !== 'rule') return;
      const conditions = conditionsOn(declaration);

      for (const one of owner.selectors) {
        const selector = one.trim();
        // Only a declaration on the element ITSELF is inherited by its whole
        // subtree. One on `.guard-label > span` reaches that span's children
        // and no sibling, so counting it would sanction a scope narrower than
        // the one being consumed.
        if (!PURE_CLASSES.test(selector)) continue;
        if (!declaredOn.has(declaration.prop)) declaredOn.set(declaration.prop, []);
        declaredOn.get(declaration.prop).push({ classes: classesOf(selector), conditions });
      }
    });
  }

  const carried = (property, subject, conditions) => (declaredOn.get(property) ?? []).some(
    (site) => site.classes.every((name) => subject.includes(name))
      && site.conditions.every((one) => conditions.includes(one)),
  );

  const unresolved = [];
  for (const [origin, root] of sheets) {
    root.walkDecls((declaration) => {
      const owner = declaration.parent;
      if (owner.type !== 'rule') return;

      const used = [...declaration.value.matchAll(UNGUARDED_USE)].map((match) => match[1]);
      if (used.length === 0) return;

      const conditions = conditionsOn(declaration);
      for (const one of owner.selectors) {
        const selector = one.trim();
        const subject = classesOf(firstCompound(selector));
        for (const name of subject) {
          if (!audited.has(name)) continue;
          for (const property of used) {
            if (inForce(inheritable.get(property), conditions)) continue;
            if (carried(property, subject, conditions)) continue;
            unresolved.push({ className: name, property, origin, selector, declaration: declaration.prop });
          }
        }
      }
    });
  }

  return unresolved;
}

/**
 * The tokens ``sources`` consume from markup without a scope that answers.
 *
 * The stylesheet half above can attribute a use to the subject its rule names.
 * A use written on an element cannot be attributed at all -- the element's
 * ancestors are a fact about the React tree, and the one that happens to
 * declare the token today is one refactor away from not being an ancestor. So
 * the requirement here is the strict one: the name is declared by a scope every
 * element inherits, unconditionally.
 *
 * A name no stylesheet here declares at all is somebody else's to declare --
 * `--radix-popover-trigger-width` is written by Radix onto the element at open
 * time, and this project could not anchor it if it wanted to. So what gets
 * reported is a name THIS token layer declares somewhere but not everywhere,
 * which is the defect; an externally provided name is left alone, a miss rather
 * than a false positive, on the same grounds as the fallback rule above.
 *
 * ``sources`` is ``[origin, text]`` per shipped file.
 */
function unanchoredMarkupTokens(sheets, sources) {
  const inheritable = inheritableTokens(sheets);
  const ours = new Map();
  for (const [, root] of sheets) customPropertiesIn(root, ours);
  const unresolved = [];

  for (const [origin, text] of sources) {
    for (const property of tokensUsedInMarkup(text)) {
      if (!ours.has(property)) continue;
      if (inForce(inheritable.get(property), [])) continue;
      unresolved.push({ origin, property });
    }
  }

  return unresolved;
}

export {
  classesRenderedBy,
  firstCompound,
  inheritableTokens,
  styledSubjects,
  tokensUsedInMarkup,
  unanchoredMarkupTokens,
  unscopedTokens,
};
