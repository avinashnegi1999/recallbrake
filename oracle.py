"""Independent state reducer: plain Python over fixture events.
Shares no code or queries with recallbrake.py; used to grade it."""


def oracle(events, tenant, version, symptom):
    revs, attempts, procs = {}, {}, {}
    for e in events:
        op = e["op"]
        if e.get("tenant") != tenant and op not in ("tenant", "version"):
            continue
        if op == "revise":
            revs.setdefault(e["id"], []).append(e["holds"])
        elif op in ("success", "failure"):
            attempts[e["attempt"]] = (e["procedure"], op)
            p = procs.setdefault(e["procedure"], {"symptom": e["symptom"], "versions": set(), "requires": set()})
            p["versions"] |= set(e["versions"])
            p["requires"] |= set(e.get("requires", []))
        elif op == "correct":
            proc, _ = attempts[e["attempt"]]
            attempts[e["attempt"]] = (proc, e["result"])
    ok = []
    for pid, p in procs.items():
        proven = any(proc == pid and res == "success" for proc, res in attempts.values())
        holds = all(revs.get(a) and revs[a][-1] for a in p["requires"])
        if proven and holds and p["symptom"] == symptom and version in p["versions"]:
            ok.append(pid)
    ok.sort()
    return {"status": "ANSWER" if ok else "UNKNOWN", "procedures": ok}
