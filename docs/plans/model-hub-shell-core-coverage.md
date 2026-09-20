# Bounded shell migration: pinned core role coverage

This ledger starts from upstream command tables, not parser dispatch. It is a
maintenance gate for persisted literal inventory and explicit related writers,
not a proof of an effective shell environment. Only literal top-level complete
assignment lines are importable; every role below only produces a refusal.
No shell, callback, module, included file, history or environment is executed.

## Primary sources and reproducible census

Sources were downloaded/read, never built or run. All paths below are relative
to these pinned distributions, so this ledger has no workstation dependency.

```text
Bash 5.3: https://ftp.gnu.org/gnu/bash/bash-5.3.tar.gz
SHA256: 0d5cd86965f869a26cf64f4b71be7b96f90a3ba8b3d74e27e8e9d9d5550f31ba
Zsh 5.9: https://github.com/zsh-users/zsh/tree/zsh-5.9
Doc/Zsh/builtins.yo: 9460fd0ca006f6d81d6ab6996ae27ae55a1d5d16dfea3a6a436d820cf81ce2bb
Src/builtin.c: f0de0056497f68ef6f1f3ee8b2f778673c616480cf5ffd74a5638729ef615537
Doc/Zsh/grammar.yo: 68ed5dcd2b8e6d6c29ada28770b29b077e1686cc6e1ee16fc2ff6e01472c6b65
Src/cond.c: da1f17d35d703120455c55124250af820f755b8ad62f276a6632d731a13d8952
Src/module.c: a0a407648c7cc7f451fb620c246cfb0e5d6d9356953602f9a36c86d42e4cf830
```

Bash census: collect every `$BUILTIN ` directive from `builtins/*.def`, then
deduplicate. There are **77** spellings, including the help-only `reserved.def`
entries; this is not a claim of 77 executable builtins. Zsh census: collect
`BUILTIN` and `BIN_PREFIX` names in `Src/builtin.c`, excluding the three
conditional debug entries `hashinfo`, `mem`, `patdebug`: **76** names.
The test pins independent SHA256s of sorted newline-joined upstream names,
rejects duplicate/missing/unclassified rows, checks handler bindings, and
requires both writer and nonwriter consuming cases for every role. It needs
neither source downloads nor a shell executable in CI.

For Bash rows, short `.def` locators are under `builtins/`; Zsh handler names
are in `Src/builtin.c` unless otherwise specified. The supplemental syntax
rows are the authorized finite syntax surface, not another full grammar census.

`role`: explicit output, code, expansion, argv or target syntax.
`none`: no relevant role under this boundary (ordinary/query/data).
`excluded`: behavior depends on deliberately unmodelled content or state.

## Complete pinned catalogue

| Dialect | Name | Class | Roles | Primary locator / disposition |
| --- | --- | --- | --- | --- |
| bash | ! | role | prefix | reserved.def; pipeline prefix |
| bash | % | excluded | - | reserved.def; job state |
| bash | (( ... )) | role | arithmetic | reserved.def; arithmetic syntax |
| bash | . | excluded | - | source.def; filename and argv, no following |
| bash | : | none | - | colon.def |
| bash | [ | none | - | test.def; decimal/string operands |
| bash | [[ ... ]] | excluded | - | reserved.def; no full condition/pattern grammar |
| bash | alias | excluded | - | alias.def; alias resolution |
| bash | bg | none | - | fg_bg.def |
| bash | bind | role | bind | bind.def; bashline.c bind_keyseq_to_unix_command |
| bash | break | none | - | break.def; integer operand, not evaluation |
| bash | builtin | role | wrapper | builtin.def; argv |
| bash | caller | none | - | caller.def |
| bash | case | role | nested | reserved.def; written body only |
| bash | cd | none | - | cd.def; implicit state excluded |
| bash | command | role | wrapper | command.def; argv, not queries |
| bash | compgen | role | compgen-target,compgen-source,compgen-expansion | complete.def; pcomplete.c |
| bash | complete | role | complete-source,complete-expansion | complete.def; pcomplete.c |
| bash | compopt | none | - | complete.def; completion options only |
| bash | continue | none | - | break.def |
| bash | coproc | role | coproc | reserved.def; parse.y; execute_cmd.c |
| bash | declare | role | declaration | declare.def |
| bash | dirs | none | - | pushd.def |
| bash | disown | none | - | jobs.def |
| bash | echo | none | - | echo.def |
| bash | enable | excluded | - | enable.def; custom/loadable builtins |
| bash | eval | role | eval-source,code-worklist | eval.def |
| bash | exec | excluded | - | exec.def; external commands, redirections handled separately |
| bash | exit | none | - | exit.def |
| bash | export | role | declaration | setattr.def |
| bash | false | none | - | colon.def |
| bash | fc | role | fc | fc.def; explicit editor only, no history reconstruction |
| bash | fg | none | - | fg_bg.def |
| bash | for | role | loop-target,nested | reserved.def |
| bash | for (( | role | arithmetic,nested | reserved.def |
| bash | function | role | nested | reserved.def; written body, no resolution |
| bash | getopts | role | getopts | getopts.def |
| bash | hash | none | - | hash.def |
| bash | help | none | - | help.def |
| bash | history | excluded | - | history.def; stored history is not inline code |
| bash | if | role | nested | reserved.def |
| bash | jobs | role | jobs | jobs.def execute_list_with_replacements; argv |
| bash | kill | none | - | kill.def |
| bash | let | role | arithmetic | let.def |
| bash | local | role | declaration | declare.def |
| bash | logout | none | - | exit.def |
| bash | mapfile | role | array-reader,array-callback | mapfile.def |
| bash | popd | none | - | pushd.def |
| bash | printf | role | printf-target,printf-format | printf.def |
| bash | pushd | none | - | pushd.def |
| bash | pwd | none | - | cd.def |
| bash | read | role | read-output | read.def |
| bash | readarray | role | array-reader,array-callback | mapfile.def |
| bash | readonly | role | declaration | setattr.def |
| bash | return | none | - | return.def |
| bash | select | role | loop-target,nested | reserved.def |
| bash | set | none | - | set.def; positional and option state excluded |
| bash | shift | none | - | shift.def; positional state excluded |
| bash | shopt | none | - | shopt.def |
| bash | source | excluded | - | source.def; filename and argv, no following |
| bash | suspend | none | - | suspend.def |
| bash | test | none | - | test.def; decimal/string operands |
| bash | time | role | prefix | reserved.def |
| bash | times | none | - | times.def |
| bash | trap | role | trap | trap.def; written action only |
| bash | true | none | - | colon.def |
| bash | type | none | - | type.def |
| bash | typeset | role | declaration | declare.def |
| bash | ulimit | none | - | ulimit.def |
| bash | umask | none | - | umask.def |
| bash | unalias | excluded | - | alias.def; alias resolution |
| bash | unset | role | unset | set.def |
| bash | until | role | nested | reserved.def |
| bash | variables | excluded | - | reserved.def; implicit special state |
| bash | wait | role | wait | wait.def; unset before jobs, last -p, array lvalue |
| bash | while | role | nested | reserved.def |
| bash | { ... } | role | nested | reserved.def |
| zsh | - | role | zsh-prefix | BIN_PREFIX |
| zsh | . | excluded | - | bin_dot; filename and argv |
| zsh | : | none | - | bin_true |
| zsh | [ | role | test-fd | bin_test; Src/cond.c COND_ISTTY |
| zsh | alias | excluded | - | bin_alias; alias resolution |
| zsh | autoload | excluded | - | bin_functions; external/named functions |
| zsh | bg | none | - | bin_fg |
| zsh | break | role | zsh-control | bin_break; arithmetic before control transfer |
| zsh | builtin | role | wrapper | BIN_PREFIX |
| zsh | bye | role | zsh-control | bin_break |
| zsh | cd | none | - | bin_cd; implicit state excluded |
| zsh | chdir | none | - | bin_cd |
| zsh | command | role | wrapper | BIN_PREFIX |
| zsh | continue | role | zsh-control | bin_break |
| zsh | declare | role | declaration,numeric-declaration,tied | bin_typeset |
| zsh | dirs | none | - | bin_dirs |
| zsh | disable | none | - | bin_enable |
| zsh | disown | none | - | bin_fg |
| zsh | echo | none | - | bin_print; no format/target options |
| zsh | emulate | role | emulate | bin_emulate; written local -c only |
| zsh | enable | excluded | - | bin_enable; enabled/custom command state |
| zsh | eval | role | eval-source,code-worklist | bin_eval |
| zsh | exec | role | zsh-prefix | BIN_PREFIX; argv0 is data |
| zsh | exit | role | zsh-control | bin_break |
| zsh | export | role | declaration,numeric-declaration,tied | bin_typeset |
| zsh | false | none | - | bin_false |
| zsh | fc | role | zsh-fc | bin_fc; fcedit passes editor text to execstring |
| zsh | fg | none | - | bin_fg |
| zsh | float | role | numeric-declaration | bin_typeset; default E |
| zsh | functions | excluded | - | bin_functions; named-function resolution |
| zsh | getln | role | getln | bin_read; default zr, echo control |
| zsh | getopts | role | getopts | bin_getopts |
| zsh | hash | none | - | bin_hash |
| zsh | history | excluded | - | bin_fc; default l, no history reconstruction |
| zsh | integer | role | numeric-declaration | bin_typeset; default i |
| zsh | jobs | none | - | bin_fg; no Bash -x |
| zsh | kill | none | - | bin_kill |
| zsh | let | role | arithmetic | bin_let |
| zsh | local | role | declaration,numeric-declaration,tied | bin_typeset |
| zsh | logout | role | zsh-control | table maximum arity 1; bin_break evaluates it |
| zsh | noglob | role | zsh-prefix | BIN_PREFIX |
| zsh | popd | none | - | bin_cd |
| zsh | print | role | print-target,print-format | bin_print; -R/query/conflict boundaries |
| zsh | printf | role | printf-target,printf-format | bin_print; percent-n and numeric format slots |
| zsh | pushd | none | - | bin_cd |
| zsh | pushln | none | - | bin_print; default nz, no target/format options |
| zsh | pwd | none | - | bin_pwd |
| zsh | r | excluded | - | bin_fc; implicit history replay |
| zsh | read | role | read-output,read-timeout | bin_read; optional numeric timeout evaluation |
| zsh | readonly | role | declaration,numeric-declaration,tied | bin_typeset |
| zsh | rehash | none | - | bin_hash |
| zsh | return | role | zsh-control | bin_break |
| zsh | set | role | set-array | bin_set; +/-A, no positional inference |
| zsh | setopt | none | - | bin_setopt |
| zsh | shift | role | shift | bin_shift; explicit count/named arrays |
| zsh | source | excluded | - | bin_dot; filename and argv |
| zsh | suspend | none | - | bin_suspend |
| zsh | test | role | test-fd | bin_test; Src/cond.c decimal comparisons are data |
| zsh | times | none | - | bin_times |
| zsh | trap | role | zsh-trap | bin_trap |
| zsh | true | none | - | bin_true |
| zsh | ttyctl | none | - | bin_ttyctl |
| zsh | type | none | - | bin_whence |
| zsh | typeset | role | declaration,numeric-declaration,tied | bin_typeset |
| zsh | umask | none | - | bin_umask |
| zsh | unalias | excluded | - | bin_unhash; alias resolution |
| zsh | unfunction | excluded | - | bin_unhash; named-function resolution |
| zsh | unhash | none | - | bin_unhash |
| zsh | unset | role | unset | bin_unset; no pattern-to-variable expansion |
| zsh | unsetopt | none | - | bin_setopt |
| zsh | wait | none | - | bin_fg; no Bash -p |
| zsh | whence | none | - | bin_whence |
| zsh | where | none | - | bin_whence |
| zsh | which | none | - | bin_whence |
| zsh | zcompile | excluded | - | bin_zcompile; external source/file content |
| zsh | zmodload | role | zmodload | Src/module.c bin_zmodload_features; output only |
| syntax | assignment | role | assignment | Bash parse.y; Zsh grammar.yo; direct literal cleanup only |
| syntax | named-fd | role | fd | Bash redir.c redir_varassign; Zsh grammar.yo |
| syntax | parameter-assignment | role | parameter-assignment | Bash bashref.texi; Zsh expansion roles |
| syntax | command-substitution | role | substitution | written source, not its output |
| syntax | include-operands | role | include | only outer expansions; filename/argv never code |
| syntax | other-grammar | excluded | - | no full interpreter, condition/pattern/module grammar |

## Role-to-handler and consuming-test bindings

Every role below is a `CASES` tag in
`tests/test_model_hub_shell_core_inventory.py`. Each tag has both positive
and query/data/invalid controls, consumed by exact cleanup and the actual
fixture migration service. Every positive also runs standalone writer scan.
The pre-existing shell and writer-role suites remain additional coverage.

| Role | Handler |
| --- | --- |
| wait | _builtin_writers |
| compgen-target | _completion_writers |
| compgen-source | _completion_writers |
| compgen-expansion | _completion_writers |
| complete-source | _completion_writers |
| complete-expansion | _completion_writers |
| trap | _trap_writers |
| bind | _bind_source |
| jobs | _jobs_writers |
| fc | _fc_writers |
| coproc | _command_writers |
| fd | _command_writers |
| include | _WrittenCode |
| code-worklist | _WrittenCode |
| print-target | _zsh_print_writers |
| print-format | _zsh_print_writers |
| set-array | _zsh_writers |
| tied | _declaration_writers |
| getln | _read_writers |
| shift | _zsh_writers |
| read-timeout | _read_writers |
| test-fd | _zsh_test_writers |
| zsh-trap | _trap_writers |
| emulate | _zsh_writers |
| zsh-fc | _fc_writers |
| zmodload | _zsh_writers |
| zsh-prefix | _argv_writers |
| numeric-declaration | _declaration_writers |
| zsh-control | _zsh_writers |
| declaration | _declaration_writers |
| wrapper | _argv_writers |
| read-output | _read_writers |
| getopts | _builtin_writers |
| unset | _builtin_writers |
| printf-target | _printf_writers |
| printf-format | _printf_operand_writers |
| arithmetic | _arithmetic_writers |
| substitution | _WrittenCode |
| parameter-assignment | _WrittenCode |
| assignment | _assignment_writers |
| eval-source | _builtin_writers |
| loop-target | _command_writers |
| nested | _parse |
| prefix | _segments |
| array-reader | _array_reader_writers |
| array-callback | _array_reader_writers |

## Invariants and deliberate limits

- Bash `wait` may unset before checking jobs or `-n`; its target allows array
  lvalues. Bash 5.3 `compgen -V` requires an identifier, not an array lvalue.
- `-C` completion callbacks and trap/editor/Readline command parts are source.
  `-W` is only expansion text: literal `KEY=value` is data. `jobs -x` and
  command modifiers route argv, never prefix assignments. Includes have no
  source role, even for an assignment-shaped filename or argument.
- Completion F/V/A/o operands are validated as each option is consumed,
  including overwritten occurrences. Unknown values are not proven invalid.
  `complete.def` clears F's WORD flags, then uses `general.c` check_identifier
  with runtime POSIX mode and `syntax.h` shell_break_chars. Without inferring
  that mode, hyphens, equals signs, digits and empty F values do not prove
  rejection; break characters do. F remains a function name, never code.
  V still requires an identifier; A and o use the complete pinned tables
  (including Bash 5.3 `fullquote`). Independent fixtures cover every named
  action/option, unknown values, repeated occurrences and query/data boundaries.
- Named/default Bash coprocs account for both the array and `_PID`. The simple
  command form has no named-coproc slot. A named fd requires unquoted adjacent
  braces; closing an existing fd is not an allocation.
- Zsh numeric coercion has a written destination even without `=`; ordinary
  attribute-only export/readonly remains nonwriting. Tied declarations inspect
  only the two written targets, never propagate a tie/nameref to another line.
- Profile filenames select dialect. `.profile` conservatively inventories
  explicit Bash extension writers without claiming POSIX execution. `emulate`
  changes Zsh options, not its builtin table: every recognized written `-c`
  body keeps Zsh roles, including with sh/ksh/csh/unknown/dynamic mode names.
  No compatibility-option state or host environment is inferred.
- Zsh `eval` has a NULL option alphabet in `Src/builtin.c`: `execbuiltin`
  removes only one initial standalone `--`, then `bin_eval` joins written
  source. Other dash-leading text is not an invalid option. An unknown initial
  word can supply that one boundary, but it does not enable letter options.
  Tests distinguish Bash invalid options, Zsh command/data text, split source,
  quoted/unknown boundaries, and a second `--` that must remain source.
- The worklist keys dialect, role and written source/argv. It has no recursion
  cutoff. Tests bound visited contexts for 1100 nested evals and 200 duplicate
  callbacks; both the related writer and echo/data controls run. Option tests
  bound call counts, not wall time; role projection does not retain combinations
  of unrelated callback/format/target values.
- Excluded: modules/custom builtins; alias, nameref and named-function
  resolution; include/history/runtime-environment inference; expansion outputs,
  computed/pattern lvalues; implicit shell-special state; execution order,
  reachability and command success. Reader/default destinations already in the
  finite contract remain recognized. `env` keeps its existing bounded GNU
  argv/assignment handler; it is not presented as a Bash or Zsh core builtin.
- This gate cannot prove source interpretation or effective shell behavior.
  Any new upstream version/role requires source review and independent fixtures,
  not regeneration of the census from production dispatch.
