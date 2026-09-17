# UI dependency security repair

## Goal and boundary

Repair the npm advisories diagnosed against the 3.1.0 source, including the
sanitizer actually bundled through Monaco. Do not suppress audit findings,
replace the editor, change application navigation policy, or alter release,
Memory, host/runtime, or user-data behavior.

The audit names 14 packages (10 development-only and two production dependency
chains), not 14 demonstrated application exploits. Current routing and Monaco
consumers avoid several advisory prerequisites, but that is not a reason to keep
known-vulnerable versions.

## Change contract

- Use compatible patched dependency releases; preserve existing major versions
  and the pinned editor unless a demonstrated compatibility constraint requires
  otherwise.
- Promote the existing transitive DOMPurify to an explicit patched dependency.
  Reconcile Monaco's package dependency and redirect its vendored import through
  Vite to this same sanitizer. A lockfile override alone cannot replace copied
  vendor source.
- Preserve Monaco's default-export sanitizer API, existing allowlists/hooks,
  URI restrictions, Markdown output and self-hosted workers.
- CSS-only validation has no need to read previous source maps. Disable that
  behavior in the existing parsing consumers, in addition to updating PostCSS.
- Add regressions for actual stylesheet consumers, navigation and Monaco's
  resolved sanitizer. Verify the production build excludes the vendored old
  sanitizer and includes the declared patched package.

## Acceptance

1. Clean install from the committed lockfile; npm audit reports no remaining
   known vulnerabilities, including development dependencies.
2. Synthetic CSS cannot read an outside-tree source map through the actual
   validation helper; ordinary CSS/token validation still works.
3. The actual Vite/Monaco consumer uses the patched sanitizer, strips dangerous
   markup/URLs and preserves normal Markdown. Inspect the built module graph,
   not just dependency metadata.
4. Routing regressions, UI tests, theme/catalog/import validation, type checks,
   lint policy, and production build pass. Existing unrelated lint findings
   remain governed by the unchanged baseline.
5. Exact-head Codex clean review, no unresolved whole-PR threads and every
   expected CI job green before delivery. No merge without separate authority.

## Known by design

- No live host update, browser/profile interaction, real personal data or
  remote tenant operation. Hermetic tests do not claim released-host E2E.
- No CDN loader, postinstall mutation of node_modules, vendored security fork,
  major toolchain migration, audit ignore list, or automatic publication.
- Monaco's unused copied sanitizer can remain inside its npm tarball; it must
  not be part of the application dev or production module graph.

## Local validation

The clean install and audit report zero known vulnerabilities (development
included). All 4,161 UI tests pass with no skips, along with theme validation,
the unchanged lint baseline, test type checking, and the production build.
The emitted editor chunk contains DOMPurify 3.4.15, not the copied 3.2.7.
The consumer-bundle test also checks module identities and spies on the
sanitizer called by Monaco, so the evidence does not depend on version text
alone. Existing build warnings about large chunks and third-party pure
annotations remain unchanged. GitHub review and CI are separate delivery gates.

The lockfile is regenerated with CI's npm 10.9.2 to retain the root optional
peer entries required by `@napi-rs/wasm-runtime`. A clean npm 10.9.2 install and
consistency checks with npm 10.9.2 and 11.4.2 pass on local Node 22.18.0.
The complete UI suite, audit and production build also pass after that clean
install. This is not a claim that local tests covered Node 24 or Linux; the
exact-head CI remains required for its own environment.
