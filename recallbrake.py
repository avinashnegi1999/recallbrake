"""RecallBrake core: procedural memory in FalkorDB that survives restarts,
is shared between agents, and stops being reused when an assumption it
depends on no longer holds. All writes go through one serialized writer.

Memory model (every node carries its tenant; ids are unique only per tenant):
  (Tenant)-[:HAS_CASE]->(Case {symptom})-[:TRIED]->(Attempt {agent, result})-[:PRODUCED]->(Procedure {fixes})
  (Tenant)-[:HAS_PROCEDURE]->(Procedure)-[:APPLIES_TO]->(ComponentVersion)
  (Procedure)-[:REQUIRES]->(Assumption)-[:HAS_REVISION]->(EvidenceRevision {rev, holds, note})
  (Tenant)-[:HAS_TASK]->(Task {owner, gen, snapshot, done})-[:DEPENDS_ON]->(Assumption)
History is never deleted: a changed assumption adds a revision, a corrected
result keeps the original on the attempt.
"""
import threading

import falkordb

HOST, PORT = "localhost", 6379


def connect(name):
    return falkordb.FalkorDB(host=HOST, port=PORT).select_graph(name)


def rows(g, q, params=None):
    res = g.query(q, params or {})
    cols = [c[1] if isinstance(c, (list, tuple)) else c for c in res.header]
    return [dict(zip(cols, r)) for r in res.result_set]


def reset(g):
    try:
        g.delete()
    except Exception:
        pass


# Every remembered procedure for (tenant, version[, symptom]) with the reason it
# does or does not apply: produced by a successful attempt, scoped to that
# version, and every required assumption holds in its latest evidence revision.
PROCEDURES = """
MATCH (t:Tenant {id:$tenant})-[:HAS_PROCEDURE]->(p:Procedure)-[:APPLIES_TO]->(:ComponentVersion {id:$cv})
WHERE $symptom IS NULL OR p.fixes = $symptom
MATCH (at:Attempt)-[:PRODUCED]->(p)
WITH p, collect(at.result) AS results, collect(at.id) AS attempts
WITH p, attempts, any(r IN results WHERE r = 'success') AS proven
OPTIONAL MATCH (p)-[:REQUIRES]->(a:Assumption)-[:HAS_REVISION]->(r:EvidenceRevision)
WITH p, attempts, proven, a, collect([r.rev, r.holds, r.note]) AS revs
WITH p, attempts, proven, a, reduce(best = [0, null, null], x IN revs | CASE WHEN x[0] > best[0] THEN x ELSE best END) AS latest
WITH p, attempts, proven,
     collect(CASE WHEN a IS NULL THEN NULL ELSE {assumption:a.id, text:a.text, rev:latest[0], holds:latest[1], note:latest[2]} END) AS needs
RETURN p.id AS procedure, p.title AS title, p.fixes AS fixes, attempts, proven, needs,
       proven AND all(x IN needs WHERE x.holds = true) AS applicable
ORDER BY procedure
"""


def procedures(g, tenant, cv, symptom=None):
    return rows(g, PROCEDURES, {"tenant": tenant, "cv": cv, "symptom": symptom})


def applicable(g, tenant, cv, symptom=None):
    return [r["procedure"] for r in procedures(g, tenant, cv, symptom) if r["applicable"]]


CASES = """
MATCH (:Tenant {id:$tenant})-[:HAS_CASE]->(c:Case)-[:TRIED]->(at:Attempt)-[:PRODUCED]->(p:Procedure)
WHERE $symptom IS NULL OR c.symptom = $symptom
RETURN c.id AS case, c.symptom AS symptom, at.id AS attempt, at.agent AS agent, at.seq AS seq,
       at.result AS result, at.original_result AS original_result, p.id AS procedure, p.title AS title
ORDER BY seq
"""


def case_memory(g, tenant, symptom=None):
    """Episodic memory: what was tried for this tenant, by whom, and how it went."""
    return rows(g, CASES, {"tenant": tenant, "symptom": symptom})


SUPPORT = """
MATCH (:Tenant {id:$tenant})-[:HAS_PROCEDURE]->(p:Procedure {id:$procedure})
OPTIONAL MATCH (p)-[:REQUIRES]->(a:Assumption)-[:HAS_REVISION]->(r:EvidenceRevision)
WITH p, a, r ORDER BY r.rev
WITH p, a, collect({rev:r.rev, holds:r.holds, note:r.note}) AS history
OPTIONAL MATCH (p)-[:APPLIES_TO]->(v:ComponentVersion)
RETURN p.id AS procedure, p.title AS title, collect(DISTINCT v.id) AS versions,
       collect(DISTINCT CASE WHEN a IS NULL THEN NULL ELSE {assumption:a.id, text:a.text, history:history} END) AS assumptions
"""


def support(g, tenant, procedure):
    """Why a procedure was trusted: its versions and every revision of each assumption it needs."""
    out = rows(g, SUPPORT, {"tenant": tenant, "procedure": procedure})
    return out[0] if out else {"error": f"no procedure {procedure} for this tenant"}


class Writer:
    """The single serialized writer. Every mutation is one guarded Cypher statement."""

    def __init__(self, g):
        self.g, self.lock, self.seq = g, threading.Lock(), 0

    def _w(self, q, params):
        with self.lock:
            return rows(self.g, q, params)

    def tenant(self, tenant):
        return self._w("MERGE (:Tenant {id:$t}) RETURN $t AS tenant", {"t": tenant})

    def version(self, cv):
        return self._w("MERGE (:ComponentVersion {id:$v}) RETURN $v AS version", {"v": cv})

    def assumption(self, tenant, aid, text):
        return self._w("""MATCH (t:Tenant {id:$t}) MERGE (t)-[:HAS_ASSUMPTION]->(a:Assumption {id:$a, tenant:$t})
                          SET a.text = $text RETURN a.id AS assumption""", {"t": tenant, "a": aid, "text": text})

    def revise(self, tenant, aid, holds, note):
        """Add an evidence revision. Older revisions stay; the latest decides."""
        out = self._w("""
            MATCH (:Tenant {id:$t})-[:HAS_ASSUMPTION]->(a:Assumption {id:$a})
            OPTIONAL MATCH (a)-[:HAS_REVISION]->(r)
            WITH a, coalesce(max(r.rev), 0) + 1 AS nxt
            CREATE (a)-[:HAS_REVISION]->(:EvidenceRevision {rev:nxt, holds:$holds, note:$note})
            RETURN nxt AS rev""", {"t": tenant, "a": aid, "holds": holds, "note": note})
        return out[0]["rev"] if out else None

    def attempt(self, tenant, case, symptom, attempt, agent, procedure, title, result, versions, requires):
        """Record one attempt at a case, and the procedure it tried, scoped to versions and assumptions."""
        self.seq += 1
        return self._w("""
            MATCH (t:Tenant {id:$t})
            MERGE (t)-[:HAS_CASE]->(c:Case {id:$case, tenant:$t}) SET c.symptom = $symptom
            MERGE (c)-[:TRIED]->(at:Attempt {id:$attempt, tenant:$t})
            SET at.agent = $agent, at.result = $result, at.seq = $seq
            MERGE (t)-[:HAS_PROCEDURE]->(p:Procedure {id:$procedure, tenant:$t})
            SET p.title = $title, p.fixes = $symptom
            MERGE (at)-[:PRODUCED]->(p)
            WITH t, p
            UNWIND $versions AS vid
            MATCH (v:ComponentVersion {id:vid}) MERGE (p)-[:APPLIES_TO]->(v)
            WITH DISTINCT t, p
            UNWIND (CASE WHEN size($requires) = 0 THEN [null] ELSE $requires END) AS aid
            OPTIONAL MATCH (t)-[:HAS_ASSUMPTION]->(a:Assumption {id:aid})
            FOREACH (_ IN CASE WHEN a IS NULL THEN [] ELSE [1] END | MERGE (p)-[:REQUIRES]->(a))
            RETURN DISTINCT p.id AS procedure""",
            dict(t=tenant, case=case, symptom=symptom, attempt=attempt, agent=agent, procedure=procedure,
                 title=title, result=result, versions=versions, requires=requires, seq=self.seq))

    def correct(self, tenant, attempt, result, note):
        """Correct a recorded result. The original is kept; derived reuse follows the correction."""
        return self._w("""
            MATCH (:Tenant {id:$t})-[:HAS_CASE]->(:Case)-[:TRIED]->(at:Attempt {id:$a})
            SET at.original_result = coalesce(at.original_result, at.result), at.result = $result, at.correction = $note
            RETURN at.id AS attempt""", {"t": tenant, "a": attempt, "result": result, "note": note})

    def task(self, tenant, tid, title, depends_on):
        return self._w("""MATCH (t:Tenant {id:$t})-[:HAS_ASSUMPTION]->(a:Assumption {id:$a})
                          MERGE (t)-[:HAS_TASK]->(k:Task {id:$k, tenant:$t}) SET k.title = $title
                          MERGE (k)-[:DEPENDS_ON]->(a) RETURN k.id AS task""",
                       {"t": tenant, "k": tid, "title": title, "a": depends_on})

    def _replay(self, tenant, rid):
        """A request id already applied returns its original result instead of running again."""
        if rid is None:
            return None
        r = rows(self.g, "MATCH (q:Request {id:$rid, tenant:$t}) RETURN q.op AS op, q.ok AS ok, q.gen AS gen, q.snapshot AS snapshot",
                 {"rid": rid, "t": tenant})
        if not r:
            return None
        out = {k: v for k, v in r[0].items() if v is not None and k != "op"}
        return {**out, "replayed": True}

    def claim(self, tenant, task, agent, rid=None):
        """Claim an unowned task. Binds the claim to a generation and the evidence snapshot.
        With a request id, the claim and its record are written in one statement, so a retry
        after a crash returns the original result and never claims twice."""
        with self.lock:
            prev = self._replay(tenant, rid)
            if prev:
                return prev
            out = rows(self.g, """
                MATCH (:Tenant {id:$t})-[:HAS_TASK]->(k:Task {id:$k})-[:DEPENDS_ON]->(:Assumption)-[:HAS_REVISION]->(r)
                WITH k, max(r.rev) AS snap
                WHERE k.owner IS NULL AND k.done IS NULL
                SET k.owner = $agent, k.gen = coalesce(k.gen, 0) + 1, k.snapshot = snap
                FOREACH (_ IN CASE WHEN $rid IS NULL THEN [] ELSE [1] END |
                    CREATE (:Request {id:$rid, tenant:$t, op:'claim', ok:true, gen:k.gen, snapshot:snap}))
                RETURN k.gen AS gen, k.snapshot AS snapshot""", {"t": tenant, "k": task, "agent": agent, "rid": rid})
        return {"ok": True, **out[0]} if out else {"ok": False, "reason": "not available: already claimed, done, or not this tenant's task"}

    def release(self, tenant, task, agent):
        """The owner gives the task back (for a handoff); the next claim gets a new generation."""
        out = self._w("""MATCH (:Tenant {id:$t})-[:HAS_TASK]->(k:Task {id:$k}) WHERE k.owner = $agent AND k.done IS NULL
                         SET k.owner = null RETURN k.id AS task""", {"t": tenant, "k": task, "agent": agent})
        return {"ok": bool(out)}

    def complete(self, tenant, task, agent, gen, snapshot, rid=None):
        """Complete only as the current owner, with the same claim generation and unchanged evidence.
        A retried request id returns the original acknowledgment."""
        with self.lock:
            prev = self._replay(tenant, rid)
            if prev:
                return prev
            out = rows(self.g, """
                MATCH (:Tenant {id:$t})-[:HAS_TASK]->(k:Task {id:$k})-[:DEPENDS_ON]->(:Assumption)-[:HAS_REVISION]->(r)
                WITH k, max(r.rev) AS cur
                WHERE k.owner = $agent AND k.gen = $gen AND k.snapshot = $snapshot AND cur = $snapshot AND k.done IS NULL
                SET k.done = true
                FOREACH (_ IN CASE WHEN $rid IS NULL THEN [] ELSE [1] END |
                    CREATE (:Request {id:$rid, tenant:$t, op:'complete', ok:true}))
                RETURN k.id AS task""", {"t": tenant, "k": task, "agent": agent, "gen": gen, "snapshot": snapshot, "rid": rid})
        if out:
            return {"ok": True}
        st = rows(self.g, """MATCH (:Tenant {id:$t})-[:HAS_TASK]->(k:Task {id:$k})-[:DEPENDS_ON]->(:Assumption)-[:HAS_REVISION]->(r)
                             RETURN k.owner AS owner, k.gen AS gen, k.snapshot AS snapshot, max(r.rev) AS current, k.done AS done""",
                  {"t": tenant, "k": task})
        return {"ok": False, "state": st[0] if st else "not this tenant's task"}


def apply(w, ev):
    """Apply one fixture event through the writer."""
    op = ev["op"]
    if op == "tenant":
        return w.tenant(ev["id"])
    if op == "version":
        return w.version(ev["id"])
    if op == "assumption":
        return w.assumption(ev["tenant"], ev["id"], ev["text"])
    if op == "revise":
        return w.revise(ev["tenant"], ev["id"], ev["holds"], ev["note"])
    if op in ("success", "failure"):
        return w.attempt(ev["tenant"], ev["case"], ev["symptom"], ev["attempt"], ev["agent"], ev["procedure"],
                         ev["title"], op, ev["versions"], ev.get("requires", []))
    if op == "correct":
        return w.correct(ev["tenant"], ev["attempt"], ev["result"], ev["note"])
    if op == "task":
        return w.task(ev["tenant"], ev["id"], ev["title"], ev["depends_on"])
    raise ValueError(op)


def load(g, events):
    reset(g)
    w = Writer(g)
    for ev in events:
        apply(w, ev)
    return w


def resolve(g, tenant, cv, symptom):
    """Deterministic decision: the reusable procedures for this situation, or UNKNOWN."""
    ok = applicable(g, tenant, cv, symptom)
    return {"status": "ANSWER", "procedures": ok} if ok else {"status": "UNKNOWN", "procedures": []}
