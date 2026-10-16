"""Two-agent handoff, each step in a fresh process (paid, about 10 model calls):
  1. agent A solves a case on an empty team memory; results are saved in FalkorDB
  2. agent B restarts and must reuse what A proved
  3. an irrelevant change: B must still reuse it
  4. a relevant change: B must refuse it
Run: python handoff.py      (writes results/handoff.json)"""
import json, os, subprocess, sys
from pathlib import Path

import recallbrake as rb

HERE, G = Path(__file__).parent, "rb_handoff"
hf = json.loads((HERE / "handoff.json").read_text())
c, t = hf["case"], hf["tenant"]


def proc(*args):
    p = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=HERE)
    if p.returncode:
        raise RuntimeError(p.stderr[-2000:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def agent_b():
    return proc("agent.py", "--live", G, t, c["version"], c["symptom"])


g = rb.connect(G)
rb.load(g, hf["setup"])
steps, ok = [], True
a = proc("agent_a.py", G, str(HERE / "handoff.json"))
proven = [x["procedure"] for x in a["tries"] if x["result"] == "success"]
steps.append({"step": "agent A solves the case", "tries": a["tries"], "pass": len(proven) == 1})

b1 = agent_b()
pick = (b1["answer"] or {}).get("procedure")
steps.append({"step": "agent B restarts and reuses A's fix", "answer": b1["answer"],
              "reusable": rb.applicable(g, t, c["version"], c["symptom"]), "pass": bool(proven) and pick == proven[0]})

w = rb.Writer(g)
rb.apply(w, hf["irrelevant_change"])
b2 = agent_b()
steps.append({"step": "irrelevant change: B still reuses it", "answer": b2["answer"],
              "pass": bool(proven) and (b2["answer"] or {}).get("procedure") == proven[0]})

rb.apply(w, hf["relevant_change"])
b3 = agent_b()
want = rb.applicable(g, t, c["version"], c["symptom"])
got = b3["answer"] or {}
steps.append({"step": "relevant change: B refuses the stale fix", "answer": got, "reusable": want,
              "pass": (got.get("status") == "UNKNOWN" and not want) or (got.get("status") == "REUSE" and got.get("procedure") in want)})

usage = {k: sum(x["usage"][k] for x in (a, b1, b2, b3)) for k in ("requests", "in", "out")}
for s in steps:
    print(("PASS " if s["pass"] else "FAIL ") + s["step"], "|", json.dumps({k: v for k, v in s.items() if k not in ("step", "pass")}, default=str)[:220])
print("usage", usage)
os.makedirs(HERE / "results", exist_ok=True)
(HERE / "results" / "handoff.json").write_text(json.dumps({"steps": steps, "usage": usage}, indent=1, default=str))
