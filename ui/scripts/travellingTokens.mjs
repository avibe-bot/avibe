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

// Names another runtime writes onto the element itself. Radix sets
// `--radix-popover-trigger-width` and its siblings on the popover at open time,
// which is the same element the use is on; this project could not declare them
// if it wanted to. Named as a prefix allowlist rather than inferred from "no
// stylesheet here declares it", because that reading is indistinguishable from a
// typo -- and a name nothing declares draws nothing, which is the defect itself
// rather than an exception to it.
const RUNTIME_PROVIDED = /^--radix-/;

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

/**
 * Every custom property a component consumes from its own markup.
 *
 * A Tailwind arbitrary value is a `var()` like any other -- `gap-[var(--x)]`
 * compiles to `gap: var(--x)` -- but it is written on an element, not in a rule,
 * so no walk of the stylesheet can see it and no selector says which scope it
 * expects. `BackendModelCatalogDialog` renders one on a `<ul>` carrying no
 * `model-hub-*` class at all. Every attribute is read rather than a chosen two,
 * because `style={{ gap: 'var(--x)' }}` is the same use as `gap-[var(--x)]`
 * spelled the other way, and a styling prop a component forwards is a third
 * spelling of it. The value is read whole -- a braced expression with its
 * braces -- so `cn('a', flag && 'b')` contributes both halves rather than its
 * first.
 *
 * Names, and nothing else. Which classes an element carries when it renders is
 * the question this walk used to answer alongside them, and it is not answerable
 * from one file: a class arrives through a forwarded prop, a variant map, or a
 * branch nothing here can evaluate. `unanchoredMarkupTokens` therefore asks
 * nothing of it.
 *
 * Tree, not text. `className` is an ordinary identifier, so the question "is
 * this one an attribute" is a question about position, and every answer read off
 * the bytes is an approximation of the grammar that is wrong for some spelling.
 * This one was wrong twice: first for a commented-out `<div style={{…}}>` left
 * in place while a component is reworked, then -- once comments were blanked --
 * for `const className = cn(…)`, which is a variable whose name happens to
 * match. Both are excluded here by construction rather than by a third
 * subtraction, because a `JsxAttribute` node is only ever an attribute.
 *
 * `nonRenderingText.mjs` already parses each of these files and already asks
 * this exact question of the tree (`NAMES_A_STYLE_SINK`), so the parse is shared
 * rather than repeated -- its cache keys on the source text, and the walk below
 * hands over the same one.
 */
function tokensUsedInMarkup(source, file) {
  const tree = parseSource(source, file);
  const found = new Set();

  const visit = (node) => {
    if (node.kind === ts.SyntaxKind.JsxAttributes) {
      // The attribute list of an element that renders, read whole: a nested
      // element written inside one of these values is inside this text too.
      for (const attribute of node.properties) {
        if (attribute.kind !== ts.SyntaxKind.JsxAttribute || !attribute.initializer) continue;
        for (const match of attribute.initializer.getText(tree).matchAll(UNGUARDED_USE)) {
          found.add(match[1]);
        }
      }
      return;
    }

    for (const child of node.getChildren(tree)) visit(child);
  };
  visit(tree);

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

// A selector matching the document root itself, unqualified. This asks where a
// declaration is WRITTEN, which is a narrower question than which elements can
// read it, and `insideTheme` is deliberately not folded in on those grounds
// rather than on the ones first written here. A `@theme` entry IS emitted, into
// `@layer theme { :root, :host { … } }`, and every element does inherit it --
// `inline` decides how a generated utility references the value, not whether the
// variable exists, which is why `inheritableTokens` counts one. What a `@theme`
// entry is not is a declaration this project placed at a scope of its own
// choosing, and that is the whole subject of `frozenAliases`, the one caller
// below: whether moving a declaration outward froze it.
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
 * The tokens ``sources`` read from markup that nothing guarantees is there.
 *
 * The stylesheet half above attributes a use to the subject its rule names. A
 * use written on an element names no subject, and the obvious substitute -- the
 * classes that element carries -- is not readable from the file the use is
 * written in. A class arrives through a forwarded prop, a variant map, or a
 * branch, so every rule of the form "the element also carries the class that
 * declares it" is a guess about the React tree, and a guess in that direction
 * fails correct markup.
 *
 * So the rule is the one that needs no such answer. A token read from markup is
 * one of three things, each decidable from the token layer alone:
 *
 * - global: declared in a `:root` block or a `@theme` block, both emitted at the
 *   document root and inherited by every element whatever the React tree does;
 * - runtime-provided: named by `RUNTIME_PROVIDED`, written onto the element by
 *   somebody this project does not speak for;
 * - fallback-bearing, which never reaches here at all -- `UNGUARDED_USE` does
 *   not match `var(--x, 6px)`, because that draws 6px and is not this defect.
 *
 * Anything else resolves only under some ancestor, and which ancestor that is,
 * is exactly the fact one refactor falsifies. The remedy is either half: declare
 * the token globally, or read it from the stylesheet, where a selector says
 * which elements the value is for. A component-scoped token is consumed in the
 * stylesheet only.
 *
 * ``sources`` is ``[origin, text]`` per shipped file.
 */
function unanchoredMarkupTokens(sheets, sources) {
  const inheritable = inheritableTokens(sheets);
  const unresolved = [];

  for (const [origin, text] of sources) {
    for (const property of tokensUsedInMarkup(text, origin)) {
      if (RUNTIME_PROVIDED.test(property)) continue;
      // In force unconditionally, because a use on an element is not itself
      // guarded by the `@media` a declaration may sit under: one made only there
      // is missing wherever the query does not hold.
      if (inForce(inheritable.get(property), [])) continue;
      unresolved.push({ origin, property });
    }
  }

  return unresolved;
}

export {
  firstCompound,
  frozenAliases,
  inheritableTokens,
  styledSubjects,
  tokensUsedInMarkup,
  unanchoredMarkupTokens,
  unscopedTokens,
};
