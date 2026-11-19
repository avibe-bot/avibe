# Isolate interactive control from Turn execution

**Decision requested for 3.2.8 (draft; not an implementation or a 3.2.7 gate).** The component that accepts Stop must not need the interpreter, heap, database transaction, executor slot or output pipe of the Turn being stopped. Recommend a small local controller and process-isolated execution. Moving the same code to threads or making collection less frequent cannot give that property. Do not start by changing prompt storage or rewriting a provider.

## Evidence and scope

The Web/UI server already has its own process; IM transports have their own threads, and native agent CLIs and shell job wrappers already have process boundaries. The common dependency is controller admission, scheduling, Vibey's `Agent` loop and the Model Hub gateway. `modules/agents/vibey/agent.py` retires idle Session runtimes, but concurrent active turns build contexts and consume streams in that same interpreter. Sending a request to a different process and then forwarding its entire JSON body or synchronously draining its stream through the controller would recreate the problem.

- Before #3937, a completed model attempt retained its request graph until the Turn ended. In a 617k-token, 96-tool-round synthetic Turn, releasing that graph reduced peak RSS from 323 to 70 MiB and longest automatic full collection from 343 to 56 ms. This was a retention bug, not the whole architecture.
- A 7.4-hour rc14 field window had 3,066 requests, 41 distinct Sessions (not 41 simultaneous), and a longest Turn of 82 requests. Slow collections still took 39 seconds/hour; median 0.25 s, maximum 0.72 s, none over 1 s. Four ~2-second heartbeat misses instead sampled a reverse successor SQL lookup and job setup. #3960 bounded the lookup and moved setup off-loop without changing Stop ownership.
- A 32-context synthetic workload retained 1.42M tracked objects. Simply sharing immutable scalars and tuples saved **zero** tracked objects: Python was already sharing them. Sharing frozen text records reduced objects, but a mutating hook could alter stored history. Our first safe-sharing experiment therefore shipped nothing. A 3.15 startup freeze missed the later heap; raising allocation thresholds reduced collection frequency, not the worst observed pause. On rc16 at light use the field report was still **103 collections over 0.2 s/hour**, median 0.23 s, max 0.78 s. It is not a throughput-matched comparison.

## Alternatives to compare

| Choice | What it isolates or removes | What still couples users | Cost and disposition |
|---|---|---|---|
| Stop at rc16 | Already removes old-attempt retention and two specific stalls | Future collections, accidental blocking calls, long callbacks, gateway work | No migration; credible if observed responsiveness is sufficient, not a structural guarantee |
| Enforce a non-blocking controller | Worker/executor for filesystem, spawn and database waits; bounded query plans | Python heap/GC and CPU remain shared; queueing a critical operation behind normal work is still a wait | Useful in every design; lint known blocking APIs, test actual event-loop deadlines, fault-inject slow calls. A watchdog sample alone is not a deterministic CI test |
| Executor process per active Turn | Independent interpreter and heap; a crashed or collecting worker cannot stop another interpreter | CPU, memory, SQLite writer and I/O are physical shared resources; starting 32 full application runtimes may itself make the machine unusable | Preferred boundary, with bounded admission and measured worker footprint. Idle Sessions get no process |
| Small fixed pool, multiplex many Sessions per worker | Lower startup/RSS; control process still independent | One slow Turn stops unrelated streams on the same worker; a wedged Turn cannot be killed alone | Useful comparison, not equivalent to one execution owner per active Turn. Prefer exclusive worker leases and an explicit queue over silently sharing a failure domain |
| Immutable append-only context segments | Share immutable *bytes*, not frozen Python objects; decode into private mutable values only where required | GC for active decoded requests, DB scans, spawn stalls and gateway CPU still exist | Potential worker memory optimization, not the separation boundary. Introduce only after equivalent rendered bytes and measurable memory/admission gain |
| Native hot path | Native buffering/byte relay can avoid Python object churn in a measured bottleneck | A blocking native call on the controller thread still blocks it; crash/ABI/packaging risks | Defer language choice. A narrow independently restartable helper is preferable to rewriting hooks, projection and all four adapters |

## Target boundary

```text
Web/UI process   IM transports   local clients
       \              |             /
        controller: routing, bounded status, independent control mailbox
             | control commands / durable receipts        ^ small events
             v                                            |
        turn authority / persistence lane            event relay (bounded)
        SQLite Turn FSM, generation, checkpoint            ^
             | launch/resume by IDs                       |
             +--> supervised exclusive Turn worker -------+
                  projection, hooks, provider adapter, tools
                    | direct model payload/stream | job wrapper
                    v                              v
                local Model Hub data plane      jobs / existing custody services
```

Draft directions: keep `SessionTurnManager` authoritative; do not add a second queue or transcript. Stop *accepted* means intent was durably recorded; it is not *stopped*. Replace preserves the existing durable successor link. Workers are disposable; the record is not. Separate gateway payload processing and bound event bytes so moving only the inner agent loop does not leave the same unbounded work on the controller. This is fault containment for one user's machine, not a security sandbox or a remote cluster.

## Evidence plan and open decision

Run the same synthetic 32 × ~600k-token, 80-attempt workload with a shared loop versus isolated processes, measuring a lightweight control round trip and worst heartbeat lateness, total memory, completions and duration. Include IPC framing, a slow consumer and worker loss; distinguish measured mechanisms from untested production guarantees. Decide worker cardinality from the throughput–memory–latency trade, not from a CPU-count default. Full ownership protocol, migration costs, OS risks, benchmark method/results and rejection criteria follow before owner review.
