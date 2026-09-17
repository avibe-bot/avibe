# Translation keys as a compiler-checked consumption contract

## Goal and starting evidence

Implement issue #1967 on master `06a864947556406062fe2e585c2e27784fcbaf58`, after #1969/#2016. Make locally authored translation names and their consumed value shapes a compiler-checked contract. Preserve runtime translation semantics and existing coverage guards.

The source currently has no i18next resource augmentation or bundle-derived TranslationKey. Carriers such as ProxyUrlField, SettingsLayout, app registry, agentGraph, sourceStatePresentation and error-key helpers widen keys to string. MemorySettingsPanel asserts returnObjects results as string[] before mapping. The app tsconfig excludes tests, and typecheck:tests only includes Model Hub tests. Installed i18next 25.x already has resource typing, ParseKeys, plural resolution and returnObjects-dependent returns; prefer these existing mechanisms over a second translation API or generated duplicate bundle.

## Contract

1. The canonical bundled resource shape owns locally authored translation keys. Text-carrying props/data/functions accept keys that resolve to text, not arbitrary strings or object prefixes. Lists and intentional prefixes have their own accurately derived types where consumed. Inventory actual producers/consumers, not the historical estimate of 18 prop names.
2. Type checks connect every local key carrier to the actual typed translation consumer. Valid literal and finite-union keys retain inference through data tables and component props. Existing plural families and intentional key prefixes remain valid with their appropriate options; a subtree cannot silently become a plain rendered label.
3. returnObjects list consumption gets its list element shape from resources. Remove the MemorySettingsPanel `as string[]` escape. An object-valued resource or a string at that call must cause a real consumer compile error. Do not claim TypeScript can outlaw deliberate double assertions/any; the implementation must not add them to evade this contract.
4. A key may be deliberately absent when the existing caller supplies a fallback. Preserve this behavior explicitly without making every static carrier a free string again. Existing backend/server-chosen dynamic key surfaces remain open at the transport boundary: validate/resolve them at a small existing ownership boundary, preserving fallback behavior. Do not cast arbitrary runtime input to TranslationKey and call it validated. Document and test whatever dynamic boundary is selected.
5. The guard and the compiler-contract witnesses belong to a maintained tsc project invoked by CI. `tsc --listFilesOnly` proves membership. Fixture i18next instances with synthetic resources must stay usable without pretending their keys belong to the app resource contract; isolate this test boundary honestly.
6. Keep #1969's presence/validity distinction and existing existence/parity/residue/blank/plural checks. Typechecking does not prove nonblank copy or equal locale content. No orphan pin, no wholesale scanner removal, no claims that genuinely dynamic inputs are now statically exhaustive.

## Scope

One isolated implementation lane owns this plan; ui/src/i18n type definitions and focused fixtures; genuine translation carrier props/data/return annotations across ui/src; the MemorySettingsPanel list consumer; existing dynamic-translation boundary helpers and necessary focused tests; minimal ui tsconfig/package script integration. This is semantic typing, not visual design; preserve displayed copy, layout and behavior. Inventory exact files in the plan before broad migration. No Python, desktop, service, deployment, #1968 or #1976 work. No dependencies or lockfile changes unless independently justified to PM first. Bundle edits are not a shortcut to silence types: report any real product-copy defect separately before editing bundles. Keep unrelated Model Hub/security work out of this PR.

## Approach and decisions

Start with a small compiler experiment against the installed i18next declarations to choose augmentation and exported carrier types. Native resource typing is the preferred direction, not a mandate to force a particular flag. Report the selected static/fallback/dynamic boundary with representative compiler evidence to PM before mass edits; proceed on the smallest contract-preserving design. If a change needs a parallel translation framework, broad caller rewrite, or altered fallback policy, stop and have PM diagnose scope first. Ordinary annotation/inference repairs within this contract do not need per-file approval.

## Acceptance evidence

- Real consuming negative controls: typo in a component carrier; typo in a data-table field; deletion/rename of a referenced key in the canonical bundle; object/list key given to a text carrier; wrong resource shape at MemorySettingsPanel list consumption. Compiler exits nonzero and names the consumer. Restore mutations afterward.
- Positive controls: valid carrier/data keys; finite template unions; plural counts; proper list returns; legitimate missing-key fallback; known and unknown server-provided keys with the existing honest fallback outcome.
- Type-level fixture uses consumed @ts-expect-error or equivalent existing tools, so widening the contract makes the fixture fail. Include representative real carrier types instead of testing only an unused helper type.
- Existing keyCoverage guard passes, including meaningful M21/M22/presence-vs-shape checks. Focused affected component/boundary tests pass. Run typecheck:tests, required UI build, lint, and appropriate broader UI validation; let GitHub run full CI.
- Non-draft PR to master, exact-head Codex PASS, all expected CI green, full paginated zero unresolved threads, CLEAN. PM independently verifies a diff and consuming tests each findings round. Same root-cause class on two reviewed heads or three findings-bearing heads after a rewrite pauses patching for PM diagnosis.

## Delivery

PM session sesg43e4mnngm. Owner authorized this issue's implementation and reviewable PR, not its merge; the earlier merge instruction named only #2014/#2016. Lane does not merge or close the issue. Maintain one durable combined PR/CI Watch with separate lane cursor, --forever and --timeout 0 on supervisor and waiter, unchanged across heads; no push over pending review. Deliver final report to PM before removing the Watch, then quiesce. PM owns independent gate Watch and final handback.

## Implementation ledger

Base and PM decisions were verified against the actual code and compiler throughout implementation. The final contract and evidence follow the producer inventory.

### Native typing experiment and inventory (before migration)

On base `06a864947556406062fe2e585c2e27784fcbaf58`, i18next's resource augmentation, `ParseKeys`, and native `defaultValue` overload passed a small compiler witness: text literals accepted; typo/object/list text carriers rejected; list inference accepted; string/object `.map` rejected; open strings require explicit fallback. Keep `strictKeyChecks` at its native false default to preserve intentional missing-key defaults. No custom t overload.

The synthetic-resource scanner gets its own tsc project without app augmentation; the app contract witnesses get augmentation and actual component/data types. TypeScript's overload diagnostic crashes on the old `(key: string) => string` helper call in FilePicker; fix the producer signature, not TypeScript or dependencies.

Exact producer inventory (consumer errors downstream do not necessarily require editing those files):

Text carrier props/data:
- `ui/src/components/ui/signing-address-list.tsx`
- `ui/src/components/settings/SettingsPlaceholderPage.tsx`
- `ui/src/components/settings/SettingsLayout.tsx`
- `ui/src/components/settings/memory/useMemoryResource.ts`
- `ui/src/components/settings/models/GuardGapList.tsx`
- `ui/src/components/settings/models/UsageTab.tsx`
- `ui/src/components/settings/models/sourceStatePresentation.ts`
- `ui/src/components/shared/ProxyUrlField.tsx`
- `ui/src/components/shared/RoutingConfigPanel.tsx` (message types are open server values; inheritsFromKey is a config key, not a translation)
- `ui/src/components/steps/DoctorPanel.tsx`
- `ui/src/components/steps/LogsPanel.tsx`
- `ui/src/components/workbench/AgentGraphDetail.tsx`
- `ui/src/components/workbench/TerminalView.tsx`
- `ui/src/components/workbench/ShowPageAnnotateControl.tsx`
- `ui/src/components/workbench/AgentActivityGroup.tsx`
- `ui/src/components/workbench/AgentGraphOrphanStrip.tsx`
- `ui/src/components/workbench/HarnessPage.tsx`
- `ui/src/components/workbench/WorkbenchSidebar.tsx`
- `ui/src/lib/agentGraph.ts`
- `ui/src/apps/registry.tsx`

Translation function signatures:
- `ui/src/components/settings/SettingsMessagingPage.tsx`
- `ui/src/components/settings/memory/MemoryProfilePanel.tsx`
- `ui/src/components/steps/UserList.tsx`
- `ui/src/components/workbench/AgentGraphCanvas.tsx`
- `ui/src/components/workbench/AgentActivityGroup.tsx`
- `ui/src/components/workbench/harnessRuns.ts`
- `ui/src/components/workbench/HarnessPage.tsx`
- `ui/src/components/workbench/harnessLifecycle.ts`
- `ui/src/lib/filesApi.ts`
- `ui/src/lib/agentGraph.ts`

Other local key producers and finite unions:
- `ui/src/components/settings/models/{addApiKeyState,manage,repair,serverCopy,sourcePresentation,sourceStatePresentation}.ts`
- `ui/src/components/settings/models/{AddApiKeyDialog,BackendModelCatalogDialog,BackendModelEditorDialog,MigrationDialog,RouteChainDialog,SourceMutationReport,SupplyGraph,SourcesCard,UsageTab}.tsx`
- `ui/src/lib/{actionShortcuts,agentBackends,annotationView,backgroundActivity,chatTrigger,workbenchUpload}.ts`
- `ui/src/components/{RemoteAccess,Workbench}.tsx`
- `ui/src/components/onboarding/AccessTiles.tsx`
- `ui/src/components/steps/LarkConfig.tsx`
- `ui/src/components/workbench/{AgentsPage,Composer,TerminalView,WorkbenchModulePlaceholder}.tsx`
- `ui/src/components/settings/{BackendTestPanel,OpencodeProviderTestPanel}.tsx`
- `ui/src/components/settings/memory/{MemorySettingsPanel,MemoryProcessingRecordPanel,memoryStatusPresentation}.tsx` (presentation is `.ts`)
- `ui/src/components/ui/directory-browser.tsx`

Open runtime boundary consumers: existing `serverText` and `fileBrowserErrorMessage`; backend platform titles/descriptions in `PlatformSelection`, `ChannelList`, `UserList`, `SettingsPlatformsPage`; dependency ids in `SettingsDependenciesPage`; source names in Memory processing records; message types in RoutingConfigPanel. These remain open with existing key/raw/generic fallback outcomes. Closed server vocabularies remain compiler checked. No transport schema narrowing to the installed UI bundle.

### Final representation and boundaries

- Native i18next `CustomTypeOptions.resources` derives solely from `en.json` through `TranslationPaths<T>`. The bundle contains 21 literal dotted keys mixed with nested resources, including `vendor` alongside `vendor.search`; native recursive return typing otherwise walks JavaScript String methods. The adapter enumerates full paths, retains real objects/arrays, and looks up nested paths before literal dotted fallbacks. String methods are never traversed. No runtime resources, translations, dependencies or resolver behavior change.
- Type-only `keySeparator: never` treats the expanded paths atomically in native i18next types. Runtime keeps its default dot separator. Native `keyPrefix` is unsupported by this adapter and has no current source consumer; the existing Workbench placeholder uses a finite, bundle-derived full-path prefix. Arrays retain their element shape; this adapter does not expose indexed translation paths because no app consumer needs them.
- `TranslationKey` filters native `ParseKeys` to actual text available without count, excluding plural-only family bases. Counted `t` calls still use native plural typing. Concrete plural suffix text remains a valid plain key. This avoids claiming a generic label can resolve a family that requires count.
- Local props, data tables, callback signatures and key-producing functions keep type information through to native `t`. Finite template producers retain literal inference. Memory disclosure maps the inferred string array without an assertion.
- Deliberate missing-key fallback remains native `defaultValue`. That overload also admits arbitrary strings and even known malformed shapes: it is an explicit API escape boundary, not a universal static guarantee. The existing presence/validity guard remains required. A server restore-preview `manual` origin, for which the bundle has no copy, retains its existing key-string fallback explicitly rather than inventing copy.
- Open server keys remain strings. `serverText` resolves raw values and returns only text or its existing generic/null fallback. Its immutable options object is an opaque shape-inspection boundary to the source scanner, which cannot infer list consumption there; real i18next runtime tests prove text/missing/object/array behavior. `fileBrowserErrorMessage` keeps raw-error fallback. The existing platform catalog owner now resolves open title/description keys with the existing key-string fallback. Neither boundary casts arbitrary data to TranslationKey.
- The coverage guard has its own maintained tsc project, without app augmentation, so its synthetic resources remain honestly untyped. App/compiler witnesses and Model Hub tests include augmentation explicitly. All three projects run under CI's existing `typecheck:tests` invocation. No new test framework or workflow needed.
- Scanner preservation: only type nodes are omitted from runtime literal collection. Runtime literals inside assertions, satisfies expressions, JSX, enums, calls and plain data remain covered. Exactly six old residual pins disappear because OAuth full-key selection and ShowPage equality narrowing remove unreachable template products. No new pin, presence/shape weakening, or dynamic/orphan completeness claim.

### Validation evidence

- Normal app tsc and all three `typecheck:tests` projects pass. `--listFilesOnly` proves guard/witness membership and the synthetic guard's exclusion of application augmentation.
- Eight temporary real-source negative controls each exit 2 and name the expected consumer: Telegram prop typo, app registry typo, object as registry title, list as Telegram label, deletion of Telegram's resource key, and Memory disclosure changed to object, string, or object array. Each mutation was restored in `finally`; canonical bundles are unchanged.
- Maintained expect-error witnesses cover actual ProxyUrlField, app registry, SourceStatePresentation and placeholder prop types; typo, prefix/list text misuse, plural-family-without-count, list `.map`, phantom descendant and String.search/String.sub callability errors. Positive witnesses cover text, finite prefixes, plural count, list elements and explicit missing defaults. Runtime coverage checks every actual dotted resource key in both locales and nested-vs-literal collision precedence.
- Initial focused run: 83 tests pass across keyCoverage, translationContract, serverCopy and MemorySettingsPanel. Subsequent guard fixture strengthening passes all 20 guard tests, including existing M21/M22 and presence/shape controls. Final local verification: all 305 UI test files / 4,149 tests pass, all three test typecheck projects pass, lint baseline passes (204 remaining legacy violations), and production build passes. Existing chunk-size/dependency annotation warnings remain. PR gates follow below when terminal.
- Lint baseline only decreases: replacing permissive translation callbacks removes seven existing any violations. No new debt is added.

### Delivery state

No Codex findings-bearing heads yet. No merge/deployment/runtime restart authorized. UI runtime verification is unit/render and build-based; cross-platform manual/Incus verification is not required for this type-contract change and has not been performed.
