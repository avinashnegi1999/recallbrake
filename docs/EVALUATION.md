# Evaluation

How RecallBrake's results were measured. All data is fictional.

**Feasibility gate** (`python gate.py`). Each step runs in a fresh Python process against real
FalkorDB, with no model calls. It checks that memory survives a restart, that a failed attempt never
becomes reusable, that a relevant assumption change blocks a procedure while an irrelevant one does
not, that another tenant sees nothing, that only one of two simultaneous claimants owns a task, and
that a completion with an outdated evidence snapshot is rejected. Result: PASS, reproduced twice.
Trace in `results/gate_trace.json`.

**Deterministic eval** (`python eval.py`). Each case loads the base memory plus its own events,
then asks which procedures may be reused. An authored answer ledger supplies the expected set, and
it must agree with `oracle.py`, an independent reducer over the raw events that shares no code with
the graph queries. The reference baseline reads the same events and applies the common rule
"reuse the most recent fix that worked for this symptom on this version".

| | Graph, dev | Baseline, dev | Graph, held-out | Baseline, held-out |
|---|---:|---:|---:|---:|
| Correct choice | 12/12 | 8/12 | 12/12 | 9/12 |
| Exact reusable set | 12/12 | — | 12/12 | — |
| Stale reuse | 0 | 4 | 0 | 3 |
| Correct UNKNOWN | 3/3 | 1/3 | 3/3 | 1/3 |

Held-out histories were written after the dev fixture, oracle and agent prompt were committed
(commit `7fb880b`). They add a tenant with colliding ids, long noisy evidence histories, a success
later corrected to a failure, a failure corrected to a success, an assumption that flips back and
forth, a procedure scoped to another version, and a newer fix whose assumption later failed.

**Coordination and retry invariants** (in `eval.py`): 14/14 hold. A second claim is rejected;
another tenant cannot claim or complete; a reassigned task gets a new generation; the previous owner
cannot complete; a completion after the evidence changed is rejected; a fresh claim completes; a
duplicate completion without a request id is rejected; memory is still there after reconnecting; a
completion that arrives before its claim is rejected; a repeated claim or completion with the same
request id returns the original result without acting twice; and when a process is killed right
after its claim is written but before it hears back, the retry returns the original claim, the
generation does not move, and another agent is still blocked.

**Two-agent handoff** (`handoff.py`, each step in a fresh process, two runs, 12 and 13 requests).
On a team with empty memory, agent A (a model) tried the runbook's "clear cookies" fix (failed,
recorded) and then "restart the session cache worker" (worked, recorded). Agent B, restarted,
reused that fix; after an irrelevant dashboard change it still reused it; after the cache was
upgraded to Redis 7 it answered UNKNOWN. 4/4 steps passed in both runs
(`results/handoff_run1.json`, `results/handoff_run2.json`).

**Agent eval** (`agent_eval.py`, gpt-5.4-mini, two runs over the 12 held-out cases). Both modes
use the same model, prompt, six-turn budget and answer checker. Graph mode uses the FalkorDB memory
tools. JSON mode reads the same facts as the team's persisted JSON memory log.

| | Graph tools, run 1 | Graph tools, run 2 | JSON log, run 1 | JSON log, run 2 |
|---|---:|---:|---:|---:|
| Correct choice | 12/12 | 12/12 | 8/12 | 7/12 |
| Stale reuse | 0 | 0 | 1 | 0 |
| Unwarranted refusal | 0 | 0 | 3 | 5 |

The two runs took 119 requests, 87,698 input tokens and 6,817 output tokens. In JSON mode the
model usually found the right facts but then refused a procedure that was still safe, for example
after a correction to a different attempt or when a newer fix's assumption had failed. Once it
reused a procedure for a different symptom. These runs used the harness lessons from the author's
earlier project from the start (forced final answer, latest accepted answer counts, answer only
after a lookup); no harness change was made after seeing held-out results. One run per mode is
not a stable estimate; results vary between runs.
