"""Paired agent eval on held-out cases: graph memory tools vs the same facts as a
JSON memory log. Same model, prompt, turn budget and checker. Hard request cap.
Run: RUN_TAG=_run1 python agent_eval.py   (writes results/agent_eval<RUN_TAG>.json)"""
import json, os
from pathlib import Path

import agent, eval as ev

CAP, HERE = 240, Path(__file__).parent
fx = ev.heldout_fixture(json.loads((HERE / "fixture.json").read_text()), json.loads((HERE / "heldout.json").read_text()))
used, out = 0, []
for c in fx["cases"]:
    for mode in ("graph", "json"):
        if used + agent.MAX_TURNS + 2 > CAP:
            break
        r = agent.run(c, fx["base"] + c["events"], mode)
        used += r["usage"]["requests"]
        a = r["answer"] or {"status": "NONE"}
        got = {"status": "ANSWER" if a["status"] == "REUSE" else "UNKNOWN" if a["status"] == "UNKNOWN" else "NONE",
               "procedures": [a["procedure"]] if a.get("procedure") and a["status"] == "REUSE" else []}
        gr = ev.grade(got, c["expect"])
        out.append({"case": c["id"], "mode": mode, "answer": a, "grade": gr, "usage": r["usage"]})
        print(f"{c['id']} {mode:5} correct={gr['correct']} stale={gr['stale_reuse']} got={got['procedures'] or a['status']} "
              f"expect={c['expect']['procedures'] or 'UNKNOWN'} req={r['usage']['requests']}", flush=True)
tot = {k: sum(r["usage"][k] for r in out) for k in ("requests", "in", "out")}
for m in ("graph", "json"):
    rs = [r for r in out if r["mode"] == m]
    print(m, f"{sum(r['grade']['correct'] for r in rs)}/{len(rs)} correct, stale reuse {sum(r['grade']['stale_reuse'] for r in rs)}, "
             f"unwarranted refusal {sum(r['grade']['unwarranted_refusal'] for r in rs)}")
print("total usage", tot)
(HERE / "results").mkdir(exist_ok=True)
(HERE / "results" / f"agent_eval{os.environ.get('RUN_TAG', '')}.json").write_text(json.dumps({"total_usage": tot, "runs": out}, indent=1, default=str))
