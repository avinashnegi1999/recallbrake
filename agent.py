"""Agent B: a restarted support agent that inherits the team's memory and must
choose which remembered procedure to reuse, or abstain.

mode=graph: FalkorDB memory tools.   mode=json: the same facts as a persisted JSON log.
Both modes share the model, prompt, turn budget and answer checker.
Usage: python agent.py <case_id> [graph|json] [--heldout]    (needs OPENAI_API_KEY)
"""
import json, os, sys
from pathlib import Path

import openai

import baseline, recallbrake as rb

MODEL = os.environ.get("AGENT_MODEL", "gpt-5.4-mini")
MAX_TURNS = 6
HERE = Path(__file__).parent


def _tool(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


S = {"type": "string"}
CHOOSE = _tool("choose_procedure",
               "Submit the decision. status REUSE needs procedure (a procedure id such as P1) that is safe to reuse now; "
               "status UNKNOWN when no remembered procedure is safe for this situation.",
               {"status": {"type": "string", "enum": ["REUSE", "UNKNOWN"]},
                "procedure": {"type": "string", "description": "procedure id, e.g. P1"}, "explanation": S},
               ["status", "explanation"])
GRAPH_TOOLS = [
    _tool("read_case_memory", "Past cases for this team and symptom: what was tried, by which agent, and the result.", {"symptom": S}, ["symptom"]),
    _tool("find_applicable_procedures", "Remembered procedures for a version and symptom, each marked applicable or not, "
          "with the assumptions it needs and their latest evidence.", {"version": S, "symptom": S}, ["version", "symptom"]),
    _tool("inspect_support", "Why a procedure was trusted: its versions and the full revision history of each assumption.", {"procedure": S}, ["procedure"]),
    CHOOSE]
JSON_TOOLS = [
    _tool("read_memory_log", "The team's persisted memory: every recorded event in order (assumptions and their evidence "
          "revisions, attempts with results, corrections).", {}, []),
    CHOOSE]
SYSTEM = """You are a support agent who just restarted and inherited your team's memory. A new case has arrived.
Choose a remembered procedure that is safe to reuse now, or answer UNKNOWN. A procedure is only reusable if it
actually worked before, it was used on this component version, and every assumption it depends on still holds
according to the latest evidence. Do not reuse a fix just because it is the most recent one. Look things up with
the tools first, then submit with choose_procedure."""


class Session:
    def __init__(self, events, case, mode, graph=None):
        """events=None means: use the memory already persisted in `graph` (a restarted agent)."""
        self.events, self.c, self.mode = events, case, mode
        self.g = rb.connect(graph or f"rb_agent_{mode}")
        if events is not None:
            rb.load(self.g, events)
        self.answer, self.looked = None, False

    def known(self):
        if self.events is None:
            return {r["procedure"] for r in rb.rows(self.g, "MATCH (:Tenant {id:$t})-[:HAS_PROCEDURE]->(p) RETURN p.id AS procedure",
                                                       {"t": self.c["tenant"]})}
        return {e["procedure"] for e in baseline.memory_log(self.events, self.c["tenant"]) if "procedure" in e}

    def validate(self, a):
        errs = []
        if a.get("status") == "REUSE":
            known = self.known()
            if a.get("procedure") not in known:
                errs.append("procedure must be a procedure id from this team's memory, such as P1")
        elif a.get("status") != "UNKNOWN":
            errs.append("status must be REUSE or UNKNOWN")
        if not self.looked:
            errs.append("read the lookup results before choosing; choose in a later turn")
        return errs

    def call(self, name, args):
        t = self.c["tenant"]
        if name == "choose_procedure":
            errs = self.validate(args)
            if errs:
                return {"accepted": False, "errors": errs}
            self.answer = args
            return {"accepted": True}
        if self.mode == "graph":
            if name == "read_case_memory":
                return rb.case_memory(self.g, t, args["symptom"])
            if name == "find_applicable_procedures":
                return rb.procedures(self.g, t, args["version"], args["symptom"])
            if name == "inspect_support":
                return rb.support(self.g, t, args["procedure"])
        if self.mode == "json" and name == "read_memory_log":
            return baseline.memory_log(self.events, t)
        return {"error": f"unknown tool {name}"}


def run(case, events, mode, emit=lambda e: None, graph=None):
    s = Session(events, case, mode, graph)
    client = openai.OpenAI()
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Team: {case['tenant']}. Component version: {case['version']}. "
                                        f"New case symptom: {case['symptom']}."}]
    usage = {"requests": 0, "in": 0, "out": 0}
    for turn in range(MAX_TURNS + 2):
        force = turn >= MAX_TURNS - 1
        r = client.chat.completions.create(model=MODEL, messages=msgs, tools=GRAPH_TOOLS if mode == "graph" else JSON_TOOLS,
                                           tool_choice={"type": "function", "function": {"name": "choose_procedure"}} if force else "auto",
                                           max_completion_tokens=900)
        usage["requests"] += 1
        usage["in"] += r.usage.prompt_tokens
        usage["out"] += r.usage.completion_tokens
        msg = r.choices[0].message
        msgs.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            msgs.append({"role": "user", "content": "Use the tools, then submit with choose_procedure."})
            continue
        for tc in msg.tool_calls:
            try:
                out = s.call(tc.function.name, json.loads(tc.function.arguments or "{}"))
            except (KeyError, ValueError, TypeError) as e:
                out = {"error": f"bad arguments: {e}"}
            emit({"tool": tc.function.name, "args": tc.function.arguments, "out": out})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, default=str)})
        lookups = any(tc.function.name != "choose_procedure" for tc in msg.tool_calls)
        s.looked = s.looked or lookups
        if s.answer and not lookups:
            break
    return {"answer": s.answer, "usage": usage}


if __name__ == "__main__":
    if "--live" in sys.argv:   # agent B on the persisted memory: --live graph tenant version symptom
        g, t, v, sym = sys.argv[sys.argv.index("--live") + 1:][:4]
        out = run({"tenant": t, "version": v, "symptom": sym}, None, "graph", graph=g)
        print(json.dumps(out))
        sys.exit()
    import eval as ev
    fx = json.loads((HERE / "fixture.json").read_text())
    if "--heldout" in sys.argv:
        fx = ev.heldout_fixture(fx, json.loads((HERE / "heldout.json").read_text()))
    case = next(c for c in fx["cases"] if c["id"] == sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("graph", "json") else "graph"
    out = run(case, fx["base"] + case["events"], mode, emit=lambda e: print(json.dumps(e, default=str)[:300]))
    print(json.dumps(out, indent=1))
