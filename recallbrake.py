"""RecallBrake core: procedural memory in FalkorDB that survives restarts,
is shared between agents, and stops being reused when an assumption it
depends on no longer holds. Work claims go through one serialized writer.

Memory model (per tenant):
  (Tenant)-[:HAS_CASE]->(Case)-[:TRIED]->(Attempt {agent, result})-[:PRODUCED]->(Procedure)
  (Tenant)-[:HAS_PROCEDURE]->(Procedure)-[:APPLIES_TO]->(ComponentVersion)
  (Procedure)-[:REQUIRES]->(Assumption)-[:HAS_REVISION]->(EvidenceRevision {rev, holds, note})
  (Tenant)-[:HAS_TASK]->(Task {owner, gen, snapshot, done})-[:DEPENDS_ON]->(Assumption)
History is never deleted: a changed assumption adds a revision.
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


# A procedure is reusable for (tenant, component version) when a successful
# attempt produced it, it is scoped to that version, and every assumption it
# requires holds in that assumption's latest evidence revision.
APPLICABLE = """
MATCH (t:Tenant {id:$tenant})-[:HAS_PROCEDURE]->(p:Procedure)-[:APPLIES_TO]->(:ComponentVersion {id:$cv})
MATCH (:Attempt {result:'success'})-[:PRODUCED]->(p)
WITH DISTINCT p
OPTIONAL MATCH (p)-[:REQUIRES]->(a:Assumption)-[:HAS_REVISION]->(r:EvidenceRevision)
WITH p, a, collect([r.rev, r.holds]) AS revs
WITH p, a, reduce(best = [0, null], x IN revs | CASE WHEN x[0] > best[0] THEN x ELSE best END) AS latest
WITH p, collect(CASE WHEN a IS NULL THEN NULL ELSE {assumption:a.id, rev:latest[0], holds:latest[1]} END) AS reqs
RETURN p.id AS procedure, p.title AS title, reqs,
       all(x IN reqs WHERE x.holds = true) AS applicable
ORDER BY procedure
"""


def procedures(g, tenant, cv):
    """Every remembered procedure for this tenant and version, with why it does or does not apply."""
    return rows(g, APPLICABLE, {"tenant": tenant, "cv": cv})


def applicable(g, tenant, cv):
    return [r["procedure"] for r in procedures(g, tenant, cv) if r["applicable"]]


def current_rev(g, assumption):
    r = rows(g, "MATCH (:Assumption {id:$a})-[:HAS_REVISION]->(r) RETURN max(r.rev) AS rev", {"a": assumption})
    return r[0]["rev"] if r else None


class Writer:
    """The single serialized writer. Every mutation goes through here; each
    is one Cypher statement guarded by its own preconditions."""

    def __init__(self, g):
        self.g, self.lock = g, threading.Lock()

    def _w(self, q, params):
        with self.lock:
            return rows(self.g, q, params)

    def record_success(self, tenant, case, attempt, agent, procedure, title, cv, requires):
        """Agent records a validated success: case, attempt and the scoped procedure it produced."""
        return self._w("""
            MATCH (t:Tenant {id:$tenant}), (v:ComponentVersion {id:$cv})
            MERGE (t)-[:HAS_CASE]->(c:Case {id:$case})
            MERGE (c)-[:TRIED]->(at:Attempt {id:$attempt})
            SET at.agent = $agent, at.result = 'success'
            MERGE (t)-[:HAS_PROCEDURE]->(p:Procedure {id:$procedure})
            SET p.title = $title
            MERGE (at)-[:PRODUCED]->(p)
            MERGE (p)-[:APPLIES_TO]->(v)
            WITH p UNWIND $requires AS aid
            MATCH (a:Assumption {id:aid}) MERGE (p)-[:REQUIRES]->(a)
            RETURN p.id AS procedure""",
            dict(tenant=tenant, case=case, attempt=attempt, agent=agent, procedure=procedure,
                 title=title, cv=cv, requires=requires))

    def revise(self, assumption, holds, note):
        """Add a new evidence revision. Old revisions stay; the latest decides."""
        return self._w("""
            MATCH (a:Assumption {id:$a})
            OPTIONAL MATCH (a)-[:HAS_REVISION]->(r)
            WITH a, coalesce(max(r.rev), 0) + 1 AS nxt
            CREATE (a)-[:HAS_REVISION]->(:EvidenceRevision {rev:nxt, holds:$holds, note:$note})
            RETURN nxt AS rev""", {"a": assumption, "holds": holds, "note": note})[0]["rev"]

    def claim(self, tenant, task, agent):
        """Claim an unowned task. Binds the claim to a generation and the evidence snapshot."""
        out = self._w("""
            MATCH (:Tenant {id:$tenant})-[:HAS_TASK]->(t:Task {id:$task})-[:DEPENDS_ON]->(a:Assumption)-[:HAS_REVISION]->(r)
            WITH t, max(r.rev) AS snap
            WHERE t.owner IS NULL AND t.done IS NULL
            SET t.owner = $agent, t.gen = coalesce(t.gen, 0) + 1, t.snapshot = snap
            RETURN t.gen AS gen, t.snapshot AS snapshot""", {"tenant": tenant, "task": task, "agent": agent})
        return {"ok": True, **out[0]} if out else {"ok": False, "reason": "already claimed or done"}

    def complete(self, tenant, task, agent, gen, snapshot):
        """Complete only as the current owner, same claim generation, and unchanged evidence."""
        out = self._w("""
            MATCH (:Tenant {id:$tenant})-[:HAS_TASK]->(t:Task {id:$task})-[:DEPENDS_ON]->(a:Assumption)-[:HAS_REVISION]->(r)
            WITH t, max(r.rev) AS cur
            WHERE t.owner = $agent AND t.gen = $gen AND t.snapshot = $snapshot AND cur = $snapshot AND t.done IS NULL
            SET t.done = true
            RETURN t.id AS task""", {"tenant": tenant, "task": task, "agent": agent, "gen": gen, "snapshot": snapshot})
        if out:
            return {"ok": True}
        t = rows(self.g, """MATCH (:Tenant {id:$tenant})-[:HAS_TASK]->(t:Task {id:$task})-[:DEPENDS_ON]->(a)-[:HAS_REVISION]->(r)
                            RETURN t.owner AS owner, t.gen AS gen, t.snapshot AS snapshot, max(r.rev) AS cur, t.done AS done""",
                 {"tenant": tenant, "task": task})
        return {"ok": False, "state": t[0] if t else None}
