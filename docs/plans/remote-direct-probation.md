# Remote direct paths: probation before adoption

Status: **the rule and its shared cases are written; neither shell implements it yet.** The iPhone App and the Desktop core each implement it against `tests/fixtures/direct_probation_cases.json`.

Decided on 2026-11-28 for 3.2.9, after the owner asked that a page never flap between a direct path and the tunnel.

## 1. Why

A page moves to a direct path within seconds of one coming up, and falls back to the tunnel when it dies. On a path that keeps dying, that is a loop, and every turn of it costs the user something: requests on the dead link hang until the transport reports the loss, then fail, and the stream reconnects.

What bounded it before (read from the code on 2026-11-28):

- The iPhone waited longer before each new dial (0.25, 1, 5, 15, 60, 300 s), but forgot after 30 s of a usable link, at every return to the foreground and at a network change. A path dying every 20 s was adopted about 11 times an hour, without end.
- The Desktop dialled a lost link again 5 s later, every time.
- The page's own bounds on hand-overs (6 until 10 minutes pass without one) limit how often the open page is moved, not how often new connections follow a link that will die.

No such path has been seen in the field. The rule is for when one is.

## 2. The rule

**A link that comes up is used only after it has stayed up for a wait. The wait grows when links die early.**

| | |
| --- | --- |
| Levels | 0, 30 s, 600 s |
| Where the level lives | per account and instance, on the device; it survives a relaunch |
| A link is used | when the shell's own check finds it still up, in the foreground, `wait` after it came up. The wait is the level's at the moment the link came up and does not change after that |
| An early death | a link **lost** on probation, or within 600 s of its adoption. The level goes up by one, 600 s at most |
| Held | an adopted link that has been up 600 s since its adoption: the level is 0 |
| Expiry | 1800 s after the last early death: the level is 0 |
| Going to the tunnel | immediate, as before. The rule delays only the way back |

All bounds are inclusive: a wait is over at exactly `wait`, a death at exactly 600 s after adoption is not early, and the level expires at exactly 1800 s.

**On probation** the link is open and kept alive, carries no page traffic, and the shell reports no path. The page therefore shows the tunnel and asks for no hand-over. No copy changes. A death there costs the user nothing.

**Only a loss raises the level.** These never do, however soon they come:

| Cause | What it is |
| --- | --- |
| `background` | The App or the window went to the background and closed its link |
| `choice` | The user chose another connection mode |
| `superseded` | A newer rendezvous answer replaced the link |
| `instance_ended` | The instance closed the connection: a typed application close from the peer (a restart, an ended admission, its own idle rule) |
| `network_change` | A change of network that the shell answered by dialling again, when the loss came from the probe that change started, not merely near it |

A loss also counts only for a link that came up in the present foreground stretch, while in the foreground: a computer that slept and woke to a dead link is not put on probation.

**With the tunnel ruled out by the user** (「仅直连和中继」) there is nothing to wait on: a link is used at once, whatever the level. Its deaths still count, so the level is right when the choice changes.

**Nothing moves mid-request.** Adoption concerns new connections only; the hand-over of an open page already waits for a quiet page and closes only quiet connections. The rule never closes a link in use.

## 3. What it gives

| A path that | is adopted | then |
| --- | --- | --- |
| is healthy | at link-up | nothing changes |
| dies 20 s after each link-up | once | stays on the tunnel: it never outlives 30 s |
| dies 3 minutes after each link-up | twice | stays on the tunnel: it never outlives 600 s |
| lives longer than 10 minutes | every time | the worst case, under six falls an hour |
| is left and returned to every minute, healthy | at every return | the level never moves |

## 4. Limits

- **A network change never raises the level.** An interface that comes and goes is not caught. The App's report says `network_change` for those losses, so the field shows it if it happens.
- **Every return starts a new probation.** At level 30 s, stretches in the foreground shorter than 30 s never reach a direct path until the level expires.
- **A path that is slow but alive is not judged.** Nothing here measures quality.
- **An instance that is killed** sends no close, so its death reads as a loss and counts once.
- **The wall clock.** The expiry is the only thing kept across a relaunch, so it is the only thing on the wall clock. A clock set forward ends a probation early; one set back restarts the 1800 s from the first read after it.

## 5. The shape both shells implement

The same names in Swift and in Rust, so that the runner of the cases is a small simulated shell and nothing exists for tests only.

| Call | Does |
| --- | --- |
| `link_up(key, link, now, wall, strict) -> wait_ms` | Notes the link and fixes its wait (0 when `strict`) |
| `adopt(key, link, now) -> bool` | The shell's check. True once, for the current link, in the foreground, in the stretch it came up in, with `now - up >= wait`. The shell calls it at once when the wait is 0, and from its own timer otherwise |
| `link_down(key, link, cause, now, wall)` | Ends the link. A link that is not the current one changes nothing |
| `foreground()`, `background()` | The stretch the device is in |
| `level(key, now, wall)`, `rose(key)` | For the report: 0, 30 or 600, and `none`, `adopted` or `probation` |
| the stored form | level, why it last rose, and the wall time of the last early death |

`now` is a monotonic clock: the wait, "held" and "early" are measured on it. `wall` is used for the expiry only. `rose` is `none` whenever the level is 0, and otherwise names the latest death that counted, a repeat at the top level included.

**Adoption is never inferred.** A link whose wait has passed but whose check has not run is not adopted, and its death is one on probation. A timer that fires late adopts late or not at all.

**The probe is the shell's, not the rule's.** The instance closes a connection that has had no stream for five minutes, and keep-alives do not show that a path carries anything. So on probation, in the foreground, the shell runs its existing CONNECT probe once every 60 s: one stream, bounded, never two at once, closed at once, no page traffic, no usage count, no hand-over count, no lease renewal. It stops at adoption and at the background. A failed probe is a loss only after the link's ownership, its deadline, its close reason and the lifecycle are read again.

## 6. The cases

`tests/fixtures/direct_probation_cases.json`, version 1. It is written by `tests/fixtures/make_direct_probation_cases.py`, which holds the rule as a reference model; `tests/test_direct_probation_cases.py` fails when the file is not what the model writes. The expected values are the model's, never either shell's, and the numbers the rule was agreed for are also typed by hand, as each case's `milestones` and in that test.

A case is a list of events in order. `at_ms` is elapsed monotonic time; the wall clock is `at_ms` plus an offset that only `clock_back` moves.

| Event | Fields | The runner |
| --- | --- | --- |
| `foreground`, `background` | | calls the same |
| `relaunch` | | writes the stored form, makes a new rule from it, with no link and in the background |
| `clock_back` | `by_ms` | moves the wall offset back |
| `link_up` | `key`, `link`, optional `strict` | calls it and compares `wait_ms` |
| `adopt` | `key`, `link` | calls it, compares `adopted`, and counts a true one |
| `link_down` | `key`, `link`, `cause` | calls it |
| `check` | `key` | reads only |

After every event with a `key` the runner compares `expect`: `level_s`, `rose`, `usable` (the link is up and adopted) and `adoptions` (the count for that key so far).

## 7. The report

The level reaches the instance in the rendezvous request that already carries `path_use`, as a sibling, `path_guard`: 1 to 10 rows, each exactly `client`, `age_min`, `path`, `guard_level_s` (0, 30, 600), `guard_rose` (`none`, `adopted`, `probation`) and `held` (links newly put on probation in that minute). A level of 0 goes with `none` and with nothing else. An instance that does not know `path_guard` ignores it and still takes `path_use`; a malformed `path_guard` does not cost the report its `path_use`.

The App sends a row only for a minute in which the level was above 0 or a link was held. `path` is the kind of the link the row speaks of, and `none` when no link was up.
