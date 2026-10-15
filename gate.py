"""60-minute feasibility gate (TRACK-2-IDEAS.md). Real FalkorDB, no model calls.
Each phase runs in a fresh Python process, so memory must survive restarts.
Run: python gate.py      -> trace + PASS/FAIL, writes results/gate_trace.json
"""
import json, os, subprocess, sys, threading
from pathlib import Path

import recallbrake as rb

G = "rb_gate"
HERE = Path(__file__).parent


def seed():
    g = rb.connect(G)
    E = [{"op": "tenant", "id": "acme"}, {"op": "tenant", "id": "globex"},
         {"op": "version", "id": "checkout@1"}, {"op": "version", "id": "checkout@2"},
         {"op": "assumption", "tenant": "acme", "id": "S1", "text": "Checkout cache runs Redis 6"},
         {"op": "assumption", "tenant": "acme", "id": "S2", "text": "Order queue delivers at least once"},
         {"op": "assumption", "tenant": "acme", "id": "S3", "text": "Dashboard uses the light theme"}]
    E += [{"op": "revise", "tenant": "acme", "id": a, "holds": True, "note": "initial observation"} for a in ("S1", "S2", "S3")]
    E += [{"op": "success", "tenant": "acme", "case": "C1", "symptom": "sessions-lost", "attempt": "A1", "agent": "agent-a",
           "procedure": "P1", "title": "Restart the cache worker", "versions": ["checkout@1"], "requires": ["S1"]},
          {"op": "success", "tenant": "acme", "case": "C2", "symptom": "orders-stuck", "attempt": "A2", "agent": "agent-a",
           "procedure": "P2", "title": "Drain the queue, then restart", "versions": ["checkout@1"], "requires": ["S2"]},
          {"op": "failure", "tenant": "acme", "case": "C3", "symptom": "sessions-lost", "attempt": "A3", "agent": "agent-a",
           "procedure": "P3", "title": "Clear all browser cookies", "versions": ["checkout@1"]},
          {"op": "task", "tenant": "acme", "id": "T1", "title": "Re-run order sync", "depends_on": "S2"}]
    rb.load(g, E)
    return {"seeded": True}


def look():
    g = rb.connect(G)
    return {"acme@1": rb.applicable(g, "acme", "checkout@1"), "acme@2": rb.applicable(g, "acme", "checkout@2"),
            "globex@1": rb.applicable(g, "globex", "checkout@1"),
            "why_acme@1": rb.procedures(g, "acme", "checkout@1")}


def change():
    w = rb.Writer(rb.connect(G))
    return {"S1": w.revise("acme", "S1", False, "Redis upgraded to 7: restarting the worker no longer clears sessions"),
            "S3": w.revise("acme", "S3", False, "Dashboard switched to dark theme (irrelevant to checkout)")}


def claims():
    g = rb.connect(G)
    w = rb.Writer(g)
    got, barrier = {}, threading.Barrier(2)

    def go(agent):
        barrier.wait()
        got[agent] = w.claim("acme", "T1", agent)
    th = [threading.Thread(target=go, args=(a,)) for a in ("agent-a", "agent-b")]
    [t.start() for t in th]
    [t.join() for t in th]
    winner = next((a for a, r in got.items() if r["ok"]), None)
    loser = next((a for a in got if a != winner), None)
    out = {"claims": got, "owners": sum(r["ok"] for r in got.values())}
    if winner:
        gen, snap = got[winner]["gen"], got[winner]["snapshot"]
        out["wrong_owner_complete"] = w.complete("acme", "T1", loser, gen, snap)
        out["wrong_tenant_claim"] = w.claim("globex", "T1", "agent-x")
        w.revise("acme", "S2", True, "queue re-verified after a config change")  # evidence changes before completion
        out["stale_complete"] = w.complete("acme", "T1", winner, gen, snap)
    return out


PHASES = {"seed": seed, "look": look, "change": change, "claims": claims}


def phase(name):
    """Run one phase in a brand-new Python process and return its JSON result."""
    p = subprocess.run([sys.executable, __file__, "--phase", name], capture_output=True, text=True, cwd=HERE)
    if p.returncode:
        raise RuntimeError(p.stderr)
    return json.loads(p.stdout)


if __name__ == "__main__":
    if "--phase" in sys.argv:
        print(json.dumps(PHASES[sys.argv[sys.argv.index("--phase") + 1]](), default=str))
        sys.exit()
    exp = json.loads((HERE / "gate_expected.json").read_text())
    trace = {"seed": phase("seed"), "before": phase("look"), "change": phase("change"),
             "after": phase("look"), "claims": phase("claims")}
    fails = []
    for k in ("before", "after"):
        for key, want in exp[k].items():
            if trace[k][key] != want:
                fails.append(f"{k} {key}: expected {want} got {trace[k][key]}")
    c = trace["claims"]
    if not trace["before"]["acme@1"]:
        fails.append("KILL: restart lost memory")
    if c["owners"] != 1:
        fails.append(f"KILL: {c['owners']} claimants own the same task")
    if c.get("stale_complete", {}).get("ok"):
        fails.append("KILL: completion with an old evidence snapshot was accepted")
    if c.get("wrong_owner_complete", {}).get("ok"):
        fails.append("wrong owner completed the task")
    if c.get("wrong_tenant_claim", {}).get("ok"):
        fails.append("another tenant claimed the task")
    print(json.dumps({k: v for k, v in trace.items() if k != "seed"}, indent=1, default=str))
    os.makedirs(HERE / "results", exist_ok=True)
    (HERE / "results" / "gate_trace.json").write_text(json.dumps({"query": rb.PROCEDURES, "trace": trace, "failures": fails}, indent=1, default=str))
    print("FAILURES:", fails or "none")
    print("GATE", "PASS" if not fails else "FAIL")
