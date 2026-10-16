"""Agent A: the responder on duty. It checks the team's memory, then tries runbook
fixes for a new case. A deterministic simulator decides whether each try worked,
and the server records every attempt in FalkorDB. The model never writes a result.

Usage: python agent_a.py <graph> <handoff.json>     (needs OPENAI_API_KEY)
"""
import json, os, sys
from pathlib import Path

import openai

import recallbrake as rb

MODEL = os.environ.get("AGENT_MODEL", "gpt-5.4-mini")
MAX_TURNS = 8


def _tool(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


S = {"type": "string"}
TOOLS = [
    _tool("find_applicable_procedures", "Procedures the team already proved, for a version and symptom, each marked applicable or not.",
          {"version": S, "symptom": S}, ["version", "symptom"]),
    _tool("list_runbook", "Untested fixes from the team runbook for a symptom, with the versions and assumptions each one depends on.",
          {"symptom": S}, ["symptom"]),
    _tool("try_procedure", "Try one fix on the current case. The result (success or failure) is observed and saved to team memory.",
          {"procedure": S}, ["procedure"]),
    _tool("finish", "Close the case with a short summary.", {"summary": S}, ["summary"]),
]
SYSTEM = """You are agent A, on duty for a support team. A new case arrived. First check whether the team already has a
proven procedure that applies. If not, try fixes from the runbook, one at a time, until one works. Prefer fixes whose
stated assumptions look true. Stop and call finish as soon as a fix works."""


def run(graph, hf, emit=lambda e: None):
    g, t, c = rb.connect(graph), hf["tenant"], hf["case"]
    w = rb.Writer(g)
    book = {r["procedure"]: r for r in hf["runbook"]}
    tries, done = [], None

    def latest(aid):
        r = rb.rows(g, """MATCH (:Tenant {id:$t})-[:HAS_ASSUMPTION]->(:Assumption {id:$a})-[:HAS_REVISION]->(r)
                          RETURN r.holds AS holds ORDER BY r.rev DESC LIMIT 1""", {"t": t, "a": aid})
        return bool(r and r[0]["holds"])

    def call(name, a):
        nonlocal done
        if name == "find_applicable_procedures":
            return rb.procedures(g, t, a["version"], a["symptom"])
        if name == "list_runbook":
            return [{k: v for k, v in r.items() if k != "works"} for r in hf["runbook"] if r["symptom"] == a["symptom"]]
        if name == "try_procedure":
            r = book.get(a["procedure"])
            if not r or r["symptom"] != c["symptom"]:
                return {"error": "not a runbook fix for this symptom"}
            ok = r["works"] and c["version"] in r["versions"] and all(latest(x) for x in r["requires"])   # the simulator
            att = f"{c['case']}-try{len(tries) + 1}"
            w.attempt(t, c["case"], c["symptom"], att, "agent-a", r["procedure"], r["title"],
                      "success" if ok else "failure", [c["version"]], r["requires"])   # scoped to the version actually tested
            tries.append({"procedure": r["procedure"], "result": "success" if ok else "failure"})
            return {"result": "success" if ok else "failure", "saved_as": att}
        if name == "finish":
            done = a["summary"]
            return {"ok": True}
        return {"error": f"unknown tool {name}"}

    client = openai.OpenAI()
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Team: {t}. Case {c['case']} on {c['version']}: symptom {c['symptom']}."}]
    usage = {"requests": 0, "in": 0, "out": 0}
    for _ in range(MAX_TURNS):
        r = client.chat.completions.create(model=MODEL, messages=msgs, tools=TOOLS, max_completion_tokens=700)
        usage["requests"] += 1
        usage["in"] += r.usage.prompt_tokens
        usage["out"] += r.usage.completion_tokens
        m = r.choices[0].message
        msgs.append(m.model_dump(exclude_none=True))
        if not m.tool_calls:
            msgs.append({"role": "user", "content": "Use the tools; call finish when a fix works."})
            continue
        for tc in m.tool_calls:
            try:
                out = call(tc.function.name, json.loads(tc.function.arguments or "{}"))
            except (KeyError, ValueError, TypeError) as e:
                out = {"error": f"bad arguments: {e}"}
            emit({"tool": tc.function.name, "args": tc.function.arguments, "out": out})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, default=str)})
        if done:
            break
    return {"tries": tries, "summary": done, "usage": usage}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], json.loads(Path(sys.argv[2]).read_text()))))
