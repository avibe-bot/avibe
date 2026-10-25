// Sink-local analysis, not a translation taint engine. See
// docs/plans/ui-localized-text-guard.md for the rollout and analysis boundary.
import path from 'node:path';

const USER_PROPS = new Set(['aria-label', 'title', 'placeholder', 'alt', 'children']);
const WRAPPERS = new Set(['TSAsExpression', 'TSTypeAssertion', 'TSNonNullExpression', 'TSSatisfiesExpression', 'ChainExpression']);
// Deliberately excludes sentence/list punctuation (including ·). aria-hidden
// alone never proves that a glyph is decorative: #2559's separator had it too.
const DECORATIVE_GLYPHS = new Set(['×', '✓', '✔', '✕', '✖', '←', '→', '↑', '↓', '↗', '↙', '⌘', '⌥', '⇧']);

function unwrap(node) {
  while (node && WRAPPERS.has(node.type)) node = node.expression;
  return node;
}

function tagOf(element) {
  const name = element?.openingElement?.name;
  return name?.type === 'JSXIdentifier' ? name.name : null;
}

function attribute(element, name) {
  return element?.openingElement?.attributes.find((attr) => attr.type === 'JSXAttribute' && attr.name.name === name);
}

function literalAttribute(attr) {
  return attr?.value?.type === 'JSXExpressionContainer' ? unwrap(attr.value.expression) : attr?.value;
}

function hidden(element) {
  const attr = attribute(element, 'aria-hidden');
  const value = literalAttribute(attr);
  return Boolean(attr && !element.openingElement.attributes.some((entry) => entry.type === 'JSXSpreadAttribute')
    && (!attr.value || value?.value === true || value?.value === 'true'));
}

// Recognizable machine syntax only, never a whole <code>/<samp> subtree or
// arbitrary words. These are content classifiers, not a parser for source code.
function resourceLiteral(text) {
  if (/\s/u.test(text)) return false;
  if (/^(?:https?:\/\/|wss?:\/\/)/u.test(text)) {
    try { return Boolean(new URL(text).hostname); } catch { return false; }
  }
  return /^(?:\.{0,2}\/|~\/)[\w./@+-]*[\w][\w./@+-]*$/u.test(text);
}

function machineToken(text) {
  return /^--[a-z][a-z0-9-]*(?:=[\w./:-]+)?$/u.test(text)
    || /^[a-zA-Z_$][\w$]*(?:[._-][\w$]+)+$/u.test(text);
}

function structuredJson(text) {
  if (!/^[\s]*[\[{]/u.test(text)) return false;
  try { return typeof JSON.parse(text) === 'object'; } catch { return false; }
}

function keyboardToken(text) {
  return /^(?:(?:Ctrl|Alt|Shift|Meta|Cmd|⌘|⌥|⇧)\+)*(?:[A-Z0-9]|F(?:[1-9]|1[0-2])|Enter|Escape|Tab|Space|Backspace|Delete|Home|End|Arrow(?:Up|Down|Left|Right)|[⌘⌥⇧←→↑↓])$/u.test(text);
}

export default {
  meta: {
    type: 'problem',
    docs: { description: 'Keep user-visible text and sentence/list composition in locale resources.' },
    schema: [],
    messages: {
      literal: 'Localize this user-facing text with a locale key or Trans; literal copy and separators do not belong in JSX.',
      composition: 'Let a locale template, Trans, or locale-aware formatter compose this text instead of literal string glue.',
    },
  },
  create(context) {
    // The rule stays configured identically for ALL TS/TSX, preserving the
    // effective-policy invariant. Only this rule's behavior is production-TSX.
    const relative = path.relative(context.cwd, context.filename).split(path.sep).join('/');
    if (!relative.startsWith('src/') || !relative.endsWith('.tsx')
      || /\.(?:test|spec)\.tsx$/u.test(relative) || relative.split('/').includes('__tests__')) return {};

    const source = context.sourceCode;
    function variable(node, name = node.name) {
      for (let scope = source.getScope(node); scope; scope = scope.upper) {
        if (scope.set.has(name)) return scope.set.get(name);
      }
      return null;
    }
    function initializer(node) {
      const binding = variable(node);
      const def = binding?.defs[0];
      if (binding?.defs.length !== 1 || def?.type !== 'Variable' || def.parent.kind !== 'const'
        || def.node.id.type !== 'Identifier' || binding.references.some((ref) => ref.isWrite() && !ref.init)) return null;
      return def.node.init;
    }
    function isTrans(element) {
      const name = element?.openingElement?.name;
      if (name?.type !== 'JSXIdentifier') return false;
      const binding = variable(name);
      const def = binding?.defs[0];
      const key = literalAttribute(attribute(element, 'i18nKey'));
      return def?.type === 'ImportBinding' && def.parent.source.value === 'react-i18next'
        && def.node.type === 'ImportSpecifier' && def.node.imported.name === 'Trans'
        && key && !(key.type === 'Literal' && !key.value);
    }
    function translatedChild(node) {
      for (let parent = node.parent; parent; parent = parent.parent) {
        // A JSX attribute is a separate sink even inside a translated subtree.
        if (parent.type === 'JSXAttribute') return false;
        if (parent.type === 'JSXElement' && isTrans(parent)) return true;
      }
      return false;
    }
    function exemptText(text, element) {
      // A complete URL/path is data even in a title or placeholder, unlike a
      // surrounding sentence. Never apply this exception to string glue.
      if (resourceLiteral(text)) return true;
      if (tagOf(element) === 'span' && hidden(element) && DECORATIVE_GLYPHS.has(text)) return true;
      if (['code', 'samp'].includes(tagOf(element)) && machineToken(text)) return true;
      if (['code', 'samp', 'pre'].includes(tagOf(element)) && structuredJson(text)) return true;
      return tagOf(element) === 'kbd' && keyboardToken(text);
    }

    // Collect only actual return expressions, never conditions, logging,
    // declarations or a nested function's return. Lexical bindings used by a
    // returned expression are followed separately through ESLint's scope graph.
    function returns(fn) {
      fn = unwrap(fn);
      if (fn?.type === 'Identifier') fn = unwrap(initializer(fn));
      if (!['ArrowFunctionExpression', 'FunctionExpression'].includes(fn?.type)) return [];
      if (fn.body.type !== 'BlockStatement') return [fn.body];
      const found = [];
      function visit(node) {
        if (!node || /Function/u.test(node.type)) return;
        if (node.type === 'ReturnStatement') { if (node.argument) found.push(node.argument); return; }
        for (const key of source.visitorKeys[node.type] ?? []) {
          const value = node[key];
          if (Array.isArray(value)) value.forEach(visit);
          else visit(value);
        }
      }
      visit(fn.body);
      return found;
    }

    function inspect(root, element) {
      const active = new Set();
      const memo = [new Map(), new Map()];
      function check(raw, allowExemption = true) {
        const node = unwrap(raw);
        if (!node || active.has(node)) return null;
        const cache = memo[Number(allowExemption)];
        if (cache.has(node)) return cache.get(node);
        active.add(node);
        const result = analyze(node, allowExemption);
        active.delete(node);
        cache.set(node, result);
        return result;
      }
      function any(nodes, allowExemption = true) { return nodes.map((node) => check(node, allowExemption)).find(Boolean) ?? null; }
      function analyze(node, allowExemption) {
        const allowed = (text) => allowExemption && exemptText(text, element);
        switch (node.type) {
          case 'Literal':
            return typeof node.value === 'string' && node.value !== '' && !allowed(node.value) ? 'literal' : null;
          case 'Identifier': return check(initializer(node), allowExemption);
          case 'TemplateLiteral':
            if (node.quasis.some((part) => part.value.cooked !== '')) {
              if (node.expressions.length === 0) return allowed(node.quasis[0].value.cooked) ? null : 'literal';
              return 'composition';
            }
            return any(node.expressions, allowExemption);
          case 'BinaryExpression':
            return node.operator === '+' && any([node.left, node.right], false) ? 'composition' : null;
          case 'ConditionalExpression': return any([node.consequent, node.alternate], allowExemption);
          // &&'s left side is a condition, not the displayed string. || and ??
          // may display their left value, so both alternatives are examined.
          case 'LogicalExpression': return node.operator === '&&' ? check(node.right, allowExemption) : any([node.left, node.right], allowExemption);
          case 'ArrayExpression': return any(node.elements, allowExemption);
          case 'SpreadElement': return check(node.argument, allowExemption);
          case 'SequenceExpression': return check(node.expressions.at(-1), allowExemption);
          case 'CallExpression': {
            const callee = unwrap(node.callee);
            if (callee?.type !== 'MemberExpression') return null;
            const method = callee.computed ? callee.property.value : callee.property.name;
            if (method === 'join') {
              // Omitted/undefined separator is a literal comma too. Dynamic or
              // translated separators are opaque unless a local const exposes it.
              const separator = unwrap(node.arguments[0]);
              if (!separator || (separator.type === 'Identifier' && separator.name === 'undefined' && !variable(separator)?.defs.length)) return 'composition';
              if (check(separator, false)) return 'composition';
              return check(callee.object, allowExemption);
            }
            if (method === 'filter') return check(callee.object, allowExemption);
            if (method === 'map' || method === 'flatMap') return any(returns(node.arguments[0]), allowExemption);
            // t(), Intl format(), formatNameList(), and other helpers own their
            // output. Their keys, options and inputs are not returned copy.
            return null;
          }
          default: return null;
        }
      }
      return check(root);
    }
    function report(node, expression, element = null) {
      const messageId = inspect(expression, element);
      if (messageId) context.report({ node, messageId });
    }
    return {
      JSXText(node) {
        const text = node.value.trim();
        if (text && !translatedChild(node) && !exemptText(text, node.parent)) {
          context.report({ node, messageId: 'literal' });
        }
      },
      JSXExpressionContainer(node) {
        if (['JSXElement', 'JSXFragment'].includes(node.parent.type)) {
          report(node, node.expression, node.parent);
        }
      },
      JSXAttribute(node) {
        if (!USER_PROPS.has(node.name.name) || !node.value) return;
        const expression = literalAttribute(node);
        // children= has the same content policy as actual children, except that
        // attributes never inherit a surrounding Trans exemption.
        report(node, expression, node.name.name === 'children' ? node.parent.parent : null);
      },
    };
  },
};
