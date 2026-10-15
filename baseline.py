"""Reference baseline: chronological JSON memory with the common reuse rule,
"reuse the most recent fix that worked for this symptom on this version".
It sees the same events (including assumption notes) but does not trace a
procedure's assumptions. This is the typical memory behaviour RecallBrake
is meant to improve on; the paired agent eval gives a model the full log."""


def memory_log(events, tenant):
    """The persisted per-tenant JSON memory a flat agent would read."""
    return [e for e in events if e.get("tenant") == tenant]


def answer(events, tenant, version, symptom):
    worked, corrected = [], {}
    for e in memory_log(events, tenant):
        if e["op"] == "success" and e["symptom"] == symptom and version in e["versions"]:
            worked.append((e["attempt"], e["procedure"]))
        if e["op"] == "correct":
            corrected[e["attempt"]] = e["result"]
    worked = [p for a, p in worked if corrected.get(a, "success") == "success"]
    return {"status": "ANSWER", "procedures": [worked[-1]]} if worked else {"status": "UNKNOWN", "procedures": []}
