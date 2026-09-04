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
//
// The same question has a second half, and moving a declaration is when it gets
// asked. A `var()` inside a custom property's value is substituted where that
// declaration applies, not where the value is finally read: descendants inherit
// the answer, not the question. So widening a declaration's scope can leave the
// name resolving perfectly while resolving to the wrong value -- `frozenAliases`
// is that half, and it exists because this file's own refactor tripped it.

import ts from 'typescript';

import { customPropertiesIn } from './customProperties.mjs';
import { parseSource } from './nonRenderingText.mjs';

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

// The value of one JSX attribute, read whole: a quoted literal with its quotes,
// a braced expression with its braces, so `cn('a', flag && 'b')` contributes
// both halves rather than its first.
//
// Tree, not text. `className` is an ordinary identifier, so the question "is
// this one an attribute" is a question about position, and every answer read off
// the bytes is an approximation of the grammar that is wrong for some spelling.
// This one was wrong twice: first for a commented-out `<div className="…">`
// left in place while a component is reworked, then -- once comments were
// blanked -- for `const className = cn(…)`, which is a variable whose name
// happens to match. Both are excluded here by construction rather than by a
// third subtraction, because a `JsxAttribute` node is only ever an attribute.
//
// `nonRenderingText.mjs` already parses each of these files and already asks
// this exact question of the tree (`NAMES_A_STYLE_SINK`), so the parse is shared
// rather than repeated -- its cache keys on the source text, and the two walks
// below hand over the same one.
function* attributeValues(source, file, attribute) {
  const tree = parseSource(source, file);

  const visit = function* visit(node) {
    if (node.kind === ts.SyntaxKind.JsxAttribute
      && node.initializer
      && node.name?.getText(tree) === attribute) {
      yield node.initializer.getText(tree);
    }
    for (const child of node.getChildren(tree)) yield* visit(child);
  };

  yield* visit(tree);
}

/**
 * Every class name a component renders, read from its own source.
 *
 * The component is the authority on this, not a list kept next to the check: a
 * class it stops rendering stops being asserted, and one it starts rendering
 * starts. Only `className` is read, and only where it ships, so a class named
 * in a comment or a log line is not mistaken for one that renders.
 */
function classesRenderedBy(source, file) {
  const found = new Set();

  for (const expression of attributeValues(source, file, 'className')) {
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
function tokensUsedInMarkup(source, file) {
  const found = new Set();

  for (const attribute of ['className', 'style']) {
    for (const expression of attributeValues(source, file, attribute)) {
      for (const match of expression.matchAll(UNGUARDED_USE)) found.add(match[1]);
    }
  }

  return found;
}

// One compound split into its ingredients, so each can be asked about
// separately. Boundaries are the characters that start one -- `.`, `#`, `[`, and
// a `:` that is not the second of a `::` -- read at depth zero, so `:not(.b)`
// and `[data-x="a.b"]` stay whole.
function ingredientsOf(compound) {
  const parts = [];
  let start = 0;
  let depth = 0;
  const cut = (index) => {
    if (index > start) parts.push(compound.slice(start, index));
    start = index;
  };

  for (let index = 0; index < compound.length; index += 1) {
    const character = compound[index];
    if (character === '(' || character === '[') {
      if (depth === 0 && character === '[') cut(index);
      depth += 1;
    } else if (character === ')' || character === ']') depth -= 1;
    else if (depth === 0 && (character === '.' || character === '#')) cut(index);
    else if (depth === 0 && character === ':' && compound[index - 1] !== ':') cut(index);
  }
  cut(compound.length);

  return parts;
}

// A compound as the two questions a declaration on it raises: WHICH elements
// (its classes) and WHEN (everything else).
//
// Every non-class ingredient -- a state pseudo, an attribute, a tag -- is a
// condition, and the first rule written here treated conditions as
// disqualifications: any selector that was not classes alone was dropped, so a
// declaration could not answer for a use. That is right about
// `.a:hover { --ink: red }` answering a use on plain `.a`, and wrong about
// `.control:hover { --hover-ink: red; color: var(--hover-ink) }`, where the
// declaration and the use carry the SAME guard and the property therefore exists
// on the element whenever the use applies. Dropping it failed correct CSS, which
// is the one direction this gate is not allowed to be wrong in.
//
// So a guard is compared rather than counted, exactly as an `@media` condition
// already was -- the file's own rule, applied to the other half of a
// declaration's reach.
function compoundParts(compound) {
  const classes = [];
  const guards = [];

  for (const part of ingredientsOf(compound)) {
    if (part.startsWith('.')) classes.push(part.slice(1));
    else guards.push(part);
  }

  return { classes, guards };
}

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

// A declaration whose entire value is one unguarded `var()`. That is a rename
// and nothing else, which is what makes it checkable here: a rename has to be
// transparent, so if it resolves to a different value than the name it renames
// would have, it has failed at the only thing it does. A value that computes --
// `color-mix(…, var(--gold), …)` -- is doing work, and where that work happens
// is a decision with reasons on both sides; this makes no claim about those.
const PURE_ALIAS = /^var\(\s*(--[\w-]+)\s*\)$/;

// A selector matching the document root itself, unqualified. `DOCUMENT_ROOT`
// answers the same question, but `insideTheme` is deliberately not folded in
// here: a `@theme inline` entry is a Tailwind bridge, substituted into the
// utility rather than emitted as a variable an element inherits (`index.css`
// says so at its own `--color-*` block), so it is not a scope anything inherits
// a value from.
function atDocumentRoot(declaration) {
  const owner = declaration.parent;
  return owner.type === 'rule'
    && owner.selectors.some((one) => DOCUMENT_ROOT.test(one.trim()));
}

// The scope one declaration applies in: its selector and the conditions above
// it, as a single comparable name. Two declarations share a scope when they
// apply to exactly the same elements under exactly the same conditions, which
// is the only sense in which one can be said to accompany the other.
function scopeOf(declaration) {
  const owner = declaration.parent;
  const where = owner.type === 'rule'
    ? owner.selector.replace(/\s+/g, ' ').trim()
    : `@${owner.name} ${owner.params}`.replace(/\s+/g, ' ').trim();
  return [where, ...conditionsOn(declaration)].join(' && ');
}

// The one scope that is not a subtree. Every scope narrower than this is a
// place a value can differ, so it gets its own name; this one is where "the
// value everywhere" is declared, and there is only ever one of those.
//
// "At the root" is not enough to be it: `@media (prefers-color-scheme: light)
// { :root { --ink: black } }` is the ordinary way to write a theme, and it is a
// place the value differs. Collapsing it here would file the override under the
// same name as the base it overrides, and an alias frozen above it would read as
// accompanied by the very declaration it fails to follow.
const EVERYWHERE = ':root';

function scopeUnder(declaration) {
  const conditions = conditionsOn(declaration);
  const everywhere = atDocumentRoot(declaration) && conditions.length === 0;
  return everywhere ? EVERYWHERE : scopeOf(declaration);
}

/**
 * Aliases declared where the value they rename is not the one their readers see.
 *
 * `var()` in a custom property's value is substituted at computed-value time on
 * the element the declaration applies to. So `:root { --a: var(--b) }` does not
 * mean "`--a` is `--b`"; it means "`--a` is whatever `--b` was at the root", and
 * every descendant inherits that answer. Where `--b` is a theme token the
 * project restates under `[data-theme="light"]` -- an attribute this repo sets
 * on nested elements, not only on `<html>` -- an element inside that subtree
 * reading `--a` gets the value from outside it. The theme switches and the
 * alias does not.
 *
 * This is the failure mode of exactly one edit: moving a declaration outward.
 * On its consuming class the alias sat inside whichever theme scope its element
 * was in and was substituted there; at `:root` it is substituted once, above
 * every boundary. Nothing about the name resolving changes, so the check above
 * still passes -- which is why this one exists.
 *
 * The remedy is either half of what the freeze removed: read the token directly
 * at the point of use, where substitution happens under the reader's own theme,
 * or restate the alias in each scope that restates what it reads. The second is
 * what gets checked for, so an alias that is genuinely maintained per theme is
 * not reported.
 */
function frozenAliases(sheets) {
  const scopes = new Map();
  const record = (property, scope) => {
    if (!scopes.has(property)) scopes.set(property, new Set());
    scopes.get(property).add(scope);
  };

  for (const [, root] of sheets) {
    root.walkDecls((declaration) => {
      if (!declaration.prop.startsWith('--')) return;
      record(declaration.prop, insideTheme(declaration) ? EVERYWHERE : scopeUnder(declaration));
    });
  }

  const frozen = [];
  for (const [origin, root] of sheets) {
    root.walkDecls((declaration) => {
      if (!declaration.prop.startsWith('--')) return;
      // Only an alias at the document root is reported. One on a class freezes
      // its value too, but only for a theme boundary mounted inside that class,
      // and it is the class's own token to place. At the root the promise is
      // that every element inherits it, and that is the promise a freeze breaks.
      if (insideTheme(declaration) || !atDocumentRoot(declaration)) return;

      const alias = PURE_ALIAS.exec(declaration.value.trim());
      if (!alias) return;

      const restated = scopes.get(declaration.prop);
      const missing = [...(scopes.get(alias[1]) ?? [])]
        .filter((scope) => scope !== EVERYWHERE && !restated.has(scope));
      if (missing.length > 0) {
        frozen.push({ origin, property: declaration.prop, reads: alias[1], missing });
      }
    });
  }

  return frozen;
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
        for (const name of compoundParts(firstCompound(one.trim())).classes) subjects.add(name);
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
        // A declaration is inherited by the whole subtree of the element it
        // lands on, so what a use may lean on is: this declaration is in force
        // on every element that use applies to. Whether the selector reaches
        // the element from the outside -- no combinator, so it says only what
        // the element itself carries -- decides which of the two clauses below
        // can answer for it. One on `.guard-label > span` reaches that span's
        // children and no sibling, so it cannot answer for `.guard-label`.
        const { classes, guards } = compoundParts(firstCompound(selector));
        if (!declaredOn.has(declaration.prop)) declaredOn.set(declaration.prop, []);
        declaredOn.get(declaration.prop).push({
          selector, classes, guards, conditions, itself: firstCompound(selector) === selector,
        });
      }
    });
  }

  // A declaration answers for a use when everything it asks for, the use asks
  // for too, so the use cannot be reached without the declaration being in
  // force. Two ways that happens. Either the declaration names the element
  // itself -- its classes are on the use's element and every guard it applies
  // under holds there too -- and then it is in force whatever the ancestors
  // are. Or it is the very same selector, where there is nothing to compare
  // because there is no second element: a rule that declares a token and reads
  // it back resolves it, and a combinator in the path it took to get there says
  // nothing about that. Either way the at-rule conditions must hold as well.
  const carried = (property, selector, subject, guards, conditions) => (declaredOn.get(property) ?? []).some(
    (site) => site.conditions.every((one) => conditions.includes(one))
      && (site.selector === selector
        || (site.itself
          && site.classes.every((name) => subject.includes(name))
          && site.guards.every((one) => guards.includes(one)))),
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
        const { classes: subject, guards } = compoundParts(firstCompound(selector));
        for (const name of subject) {
          if (!audited.has(name)) continue;
          for (const property of used) {
            if (inForce(inheritable.get(property), conditions)) continue;
            if (carried(property, selector, subject, guards, conditions)) continue;
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
    for (const property of tokensUsedInMarkup(text, origin)) {
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
  frozenAliases,
  inheritableTokens,
  styledSubjects,
  tokensUsedInMarkup,
  unanchoredMarkupTokens,
  unscopedTokens,
};
