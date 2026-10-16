<h1 align="center">RecallBrake</h1>
<p align="center">Shared agent memory that knows when yesterday's fix stops applying.</p>

<p align="center"><sub>Graph Hacks · Agent Memory and Coordination · Built on FalkorDB</sub></p>

## Remembering a fix isn't enough.

A support agent fixes a problem and saves the procedure. Tomorrow a different agent restarts,
inherits that memory, and sees the same symptom. Should it reuse the fix? Only if the fix really
worked, it was used on this version, and every assumption it depended on still holds. Reusing the
latest fix that worked is how teams repeat yesterday's answer after the system changed.

## Memory that carries its own limits.

- **Episodic.** Every case and attempt is kept, with who tried what and how it went. Corrected
  results keep the original.
- **Procedural.** A procedure becomes reusable only after a successful attempt, scoped to the
  versions it was used on.
- **Semantic.** Each procedure points to the assumptions it needs. Evidence about an assumption is
  added as a new revision; the latest revision decides.
- **Shared and isolated.** Memory survives restarts and is shared by every agent on a team. Another
  team cannot see it, even with the same ids.

## Agents share work without stepping on each other.

All writes go through one serialized writer. Only one agent can claim a task. A completion counts
only if it comes from the current owner, with the same claim generation, and the evidence has not
changed since the claim. Requests carry ids: a retry returns the original result instead of acting
twice, even if the agent crashed after the write and before it heard back.

## Two agents, one memory.

Agent A, a model on duty with an empty team memory, tries runbook fixes on a new case. A simulator
decides whether each try worked, and the server saves every attempt; the model never writes a result.
Agent B then starts in a new process, finds A's proven fix and reuses it. After an irrelevant change
it still reuses it. After the cache is upgraded, it refuses it.

## Tested on held-out histories.

**12/12** held-out cases with the graph · **9/12** for the common "reuse the latest fix" rule ·
an agent with graph memory tools **12/12 in both runs**, versus **8/12 and 7/12** for the same
model reading the same memory as a JSON log · two-agent handoff **4/4 in both runs** ·
**14/14** coordination and retry invariants ·
[How it was measured](docs/EVALUATION.md)

## Run it.

```bash
docker run -d --name falkordb -p 6379:6379 falkordb/falkordb:latest
pip install -r requirements.txt
python gate.py           # feasibility gate: restart, assumption change, claim race (no model)
python eval.py           # deterministic eval vs oracle and baseline, plus invariants (no model)
python server.py         # demo on http://localhost:8003
python handoff.py        # agent A solves, agent B restarts and reuses or refuses (needs OPENAI_API_KEY)
```

The agent needs `OPENAI_API_KEY` set in your environment. Run `python agent.py h03 graph --heldout`
for one case.

---

## Reference

### Graph model

| Node | Meaning |
|---|---|
| `Tenant {id}` | A team; all memory hangs off its tenant |
| `Case {id, tenant, symptom}` | A support case |
| `Attempt {id, tenant, agent, result, original_result, seq}` | One try at a case; corrections keep the original |
| `Procedure {id, tenant, title, fixes}` | A fix that an attempt produced |
| `ComponentVersion {id}` | A version of the service, such as `checkout@2` |
| `Assumption {id, tenant, text}` | Something a procedure depends on |
| `EvidenceRevision {rev, holds, note}` | One observation about an assumption; the latest decides |
| `Task {id, tenant, owner, gen, snapshot, done}` | Shared work with a guarded claim |

| Edge | From → to | Meaning |
|---|---|---|
| `HAS_CASE`, `HAS_PROCEDURE`, `HAS_ASSUMPTION`, `HAS_TASK` | Tenant → … | Tenant ownership and isolation |
| `TRIED` | Case → Attempt | What was tried |
| `PRODUCED` | Attempt → Procedure | The procedure an attempt used |
| `APPLIES_TO` | Procedure → ComponentVersion | Versions it was used on |
| `REQUIRES` | Procedure → Assumption | What must hold for it to work |
| `HAS_REVISION` | Assumption → EvidenceRevision | Evidence history |
| `DEPENDS_ON` | Task → Assumption | Evidence a task's completion is bound to |

All data is fictional, in `fixture.json` and `heldout.json`.

### Core queries

Which remembered procedures may be reused for a team, version and symptom (from
`recallbrake.py`):

```cypher
MATCH (t:Tenant {id:$tenant})-[:HAS_PROCEDURE]->(p:Procedure)-[:APPLIES_TO]->(:ComponentVersion {id:$cv})
WHERE $symptom IS NULL OR p.fixes = $symptom
MATCH (at:Attempt)-[:PRODUCED]->(p)
WITH p, any(r IN collect(at.result) WHERE r = 'success') AS proven
OPTIONAL MATCH (p)-[:REQUIRES]->(a:Assumption)-[:HAS_REVISION]->(r:EvidenceRevision)
WITH p, proven, a, collect([r.rev, r.holds, r.note]) AS revs
WITH p, proven, a, reduce(best = [0, null, null], x IN revs | CASE WHEN x[0] > best[0] THEN x ELSE best END) AS latest
WITH p, proven, collect(CASE WHEN a IS NULL THEN NULL ELSE {assumption:a.id, holds:latest[1]} END) AS needs
RETURN p.id, needs, proven AND all(x IN needs WHERE x.holds = true) AS applicable
```

Completing shared work is one guarded statement. If the owner, the claim generation or the
evidence changed, nothing matches and the completion is rejected:

```cypher
MATCH (:Tenant {id:$t})-[:HAS_TASK]->(k:Task {id:$k})-[:DEPENDS_ON]->(:Assumption)-[:HAS_REVISION]->(r)
WITH k, max(r.rev) AS cur
WHERE k.owner = $agent AND k.gen = $gen AND k.snapshot = $snapshot AND cur = $snapshot AND k.done IS NULL
SET k.done = true
RETURN k.id
```

### Agent

Both agents use the model (default `gpt-5.4-mini`) and act only through tools; neither can write
Cypher. Agent A (`agent_a.py`) uses `find_applicable_procedures`, `list_runbook`, `try_procedure`
and `finish`; `try_procedure` runs a deterministic simulator and the server records the attempt,
scoped to the version tested. Agent B (`agent.py`) uses `read_case_memory`,
`find_applicable_procedures`, `inspect_support` and `choose_procedure`. The checker accepts only
known procedure ids and an answer submitted after a lookup. It does not reveal the right answer.

### Limitations

The writer is serialized: it coordinates its own clients, not multiple independent writers.
Applicability is the rule stated above; it does not diagnose new problems or invent procedures.
One service family and about two dozen histories cannot show general performance. Recursive SQL
could run the same joins; the point is that FalkorDB holds the shared state and returns the reason
a fix is or is not reusable.

### AI disclosure

An AI coding assistant was used to generate and debug code and to draft documentation. At
runtime, an LLM (OpenAI `gpt-5.4-mini` by default) runs agents A and B; a simulator decides
whether a fix worked, and the graph decides whether it may be reused. Every result reported here came from running the code in this repository.

### Files

`recallbrake.py` graph model, queries and single writer · `agent_a.py` responder agent ·
`agent.py` successor agent · `handoff.py` two-agent handoff test ·
`oracle.py` independent reducer · `baseline.py` reuse-latest-fix rule and JSON memory log ·
`gate.py` feasibility gate · `eval.py` deterministic eval and invariants · `agent_eval.py` paired
agent runs · `server.py`, `static/` demo UI · `results/` raw records · `docs/EVALUATION.md`
