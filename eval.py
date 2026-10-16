"""Deterministic eval: graph memory vs the reuse-latest-fix baseline, graded by an
authored ledger that must agree with the independent oracle; plus coordination
invariants. No model calls.  Run: python eval.py   (writes results/eval.json)"""
import copy, json, os, subprocess, sys
from pathlib import Path

import baseline, recallbrake as rb
from oracle import oracle

HERE = Path(__file__).parent


def heldout_fixture(dev, spec):
    fx = copy.deepcopy(dev)
    fx["base"] += spec.get("base_events", [])
    fx["cases"] = spec["cases"]
    return fx


def grade(got, exp):
    """A single pick is right if it is in the applicable set; UNKNOWN is right only when the set is empty."""
    picks = got["procedures"]
    if exp["status"] == "UNKNOWN":
        return {"correct": got["status"] == "UNKNOWN", "stale_reuse": bool(picks), "unwarranted_refusal": False}
    stale = any(p not in exp["procedures"] for p in picks)
    return {"correct": got["status"] == "ANSWER" and bool(picks) and not stale,
            "stale_reuse": stale, "unwarranted_refusal": got["status"] == "UNKNOWN"}


def run_split(name, fx):
    g = rb.connect(f"rb_eval_{name}")
    out = []
    for c in fx["cases"]:
        events = fx["base"] + c["events"]
        rb.load(g, events)
        orc = oracle(events, c["tenant"], c["version"], c["symptom"])
        graph = rb.resolve(g, c["tenant"], c["version"], c["symptom"])
        flat = baseline.answer(events, c["tenant"], c["version"], c["symptom"])
        out.append({"id": c["id"], "tenant": c["tenant"], "version": c["version"], "symptom": c["symptom"], "expect": c["expect"],
                    "ledger_agrees_with_oracle": orc == c["expect"], "graph_exact": graph == c["expect"],
                    "graph": graph, "baseline": flat, "graph_grade": grade(graph, c["expect"]), "baseline_grade": grade(flat, c["expect"])})
    return out


def summary(rows, who):
    g = [r[f"{who}_grade"] for r in rows]
    unk = [x for x, r in zip(g, rows) if r["expect"]["status"] == "UNKNOWN"]
    s = {"correct_choice": f"{sum(x['correct'] for x in g)}/{len(g)}",
         "stale_reuse": sum(x["stale_reuse"] for x in g),
         "unwarranted_refusal": sum(x["unwarranted_refusal"] for x in g),
         "correct_unknown": f"{sum(x['correct'] for x in unk)}/{len(unk)}"}
    if who == "graph":
        s["exact_applicable_set"] = f"{sum(r['graph_exact'] for r in rows)}/{len(rows)}"
    return s


def invariants():
    """Coordination checks through the single writer (fresh graph)."""
    dev = json.loads((HERE / "fixture.json").read_text())
    g = rb.connect("rb_eval_claims")
    w = rb.load(g, dev["base"])
    res = {}
    a = w.claim("acme", "T1", "agent-a")
    res["second_claim_rejected"] = not w.claim("acme", "T1", "agent-b")["ok"]
    res["wrong_tenant_claim_rejected"] = not w.claim("globex", "T1", "agent-x")["ok"]
    res["wrong_tenant_complete_rejected"] = not w.complete("globex", "T1", "agent-a", a["gen"], a["snapshot"])["ok"]
    w.release("acme", "T1", "agent-a")
    b = w.claim("acme", "T1", "agent-b")
    res["reassigned_new_generation"] = b["ok"] and b["gen"] == a["gen"] + 1
    res["stale_owner_complete_rejected"] = not w.complete("acme", "T1", "agent-a", a["gen"], a["snapshot"])["ok"]
    w.revise("acme", "S2", True, "queue re-verified")
    res["stale_snapshot_complete_rejected"] = not w.complete("acme", "T1", "agent-b", b["gen"], b["snapshot"])["ok"]
    w.release("acme", "T1", "agent-b")
    c = w.claim("acme", "T1", "agent-b")
    res["fresh_claim_completes"] = w.complete("acme", "T1", "agent-b", c["gen"], c["snapshot"])["ok"]
    res["duplicate_complete_rejected"] = not w.complete("acme", "T1", "agent-b", c["gen"], c["snapshot"])["ok"]
    # memory survives a new connection (a restarted agent)
    res["memory_survives_reconnect"] = rb.applicable(rb.connect("rb_eval_claims"), "acme", "checkout@1", "sessions-lost") == ["P1"]
    # retries: idempotent request ids, out-of-order requests, crash between write and acknowledgment
    w.task("acme", "T2", "Rebuild the order index", "S2")
    res["out_of_order_complete_rejected"] = not w.complete("acme", "T2", "agent-a", 1, 1, rid="early")["ok"]
    c1 = w.claim("acme", "T2", "agent-a", rid="req-1")
    c2 = w.claim("acme", "T2", "agent-a", rid="req-1")
    res["duplicate_claim_replays_same_result"] = c2.get("replayed") and c2["gen"] == c1["gen"]
    d1 = w.complete("acme", "T2", "agent-a", c1["gen"], c1["snapshot"], rid="req-2")
    d2 = w.complete("acme", "T2", "agent-a", c1["gen"], c1["snapshot"], rid="req-2")
    res["duplicate_complete_replays_ack"] = d1["ok"] and d2.get("replayed") and d2["ok"]
    w.task("acme", "T3", "Re-index search", "S2")
    crash = ("import os, recallbrake as rb; w = rb.Writer(rb.connect('rb_eval_claims')); "
             "w.claim('acme', 'T3', 'agent-a', rid='req-crash'); os._exit(3)")   # write lands, process dies before the ack
    p = subprocess.run([sys.executable, "-c", crash], cwd=HERE)
    retry = rb.Writer(rb.connect("rb_eval_claims")).claim("acme", "T3", "agent-a", rid="req-crash")
    gen = rb.rows(g, "MATCH (k:Task {id:'T3', tenant:'acme'}) RETURN k.gen AS gen, k.owner AS owner")[0]
    res["crash_before_ack_retry_is_safe"] = p.returncode == 3 and retry.get("replayed") and gen == {"gen": 1, "owner": "agent-a"}
    res["other_agent_blocked_after_crash"] = not w.claim("acme", "T3", "agent-b", rid="req-b")["ok"]
    return res


if __name__ == "__main__":
    dev = json.loads((HERE / "fixture.json").read_text())
    splits = {"dev": dev}
    if (HERE / "heldout.json").exists():
        splits["heldout"] = heldout_fixture(dev, json.loads((HERE / "heldout.json").read_text()))
    report = {}
    for name, fx in splits.items():
        rows = run_split(name, fx)
        bad = [r["id"] for r in rows if not r["ledger_agrees_with_oracle"]]
        report[name] = {"ledger_vs_oracle_disagreements": bad, "graph": summary(rows, "graph"),
                        "baseline": summary(rows, "baseline"), "cases": rows}
        print(f"== {name}: {len(rows)} cases, ledger/oracle disagreements: {bad or 'none'}")
        print(f"   graph    {report[name]['graph']}")
        print(f"   baseline {report[name]['baseline']}")
        for r in rows:
            if not r["graph_exact"] or not r["baseline_grade"]["correct"]:
                print(f"   {r['id']} {r['tenant']} {r['version']} {r['symptom']} expect={r['expect']['procedures'] or 'UNKNOWN'} "
                      f"graph={r['graph']['procedures'] or 'UNKNOWN'} baseline={r['baseline']['procedures'] or 'UNKNOWN'}")
    inv = invariants()
    report["invariants"] = inv
    print("== invariants:", f"{sum(inv.values())}/{len(inv)} hold", [k for k, v in inv.items() if not v] or "")
    os.makedirs(HERE / "results", exist_ok=True)
    (HERE / "results" / "eval.json").write_text(json.dumps(report, indent=1))
