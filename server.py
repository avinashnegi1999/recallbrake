"""Demo UI: python server.py, then open http://localhost:8003
Every answer comes from FalkorDB through recallbrake.py; no model calls.

GET  /api/state?tenant=&version=&symptom=   procedures (reusable or blocked, with evidence) and case memory
POST /api/step   {"step": id}               apply a scripted change through the single writer
POST /api/reset                              reload the base memory
POST /api/race                               two agents claim one task, then a stale completion is attempted
"""
import json, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import recallbrake as rb

HERE = Path(__file__).parent
BASE = json.loads((HERE / "fixture.json").read_text())["base"]
G = rb.connect("rb_ui")
W = rb.load(G, BASE)
STEPS = {
    "redis7": {"label": "Cache upgraded to Redis 7", "op": "revise", "tenant": "acme", "id": "S1", "holds": False,
               "note": "Cache upgraded to Redis 7; a worker restart no longer restores sessions"},
    "redis6": {"label": "Roll the cache back to Redis 6", "op": "revise", "tenant": "acme", "id": "S1", "holds": True,
               "note": "Rolled back to Redis 6"},
    "theme": {"label": "Irrelevant change: dashboard theme", "op": "revise", "tenant": "acme", "id": "S9", "holds": False,
              "note": "Dashboard switched to dark theme"},
    "vault": {"label": "Signing keys leave the vault", "op": "revise", "tenant": "acme", "id": "S4", "holds": False,
              "note": "Keys moved into app config"},
}
LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE / "static"), **kw)

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("localhost", "127.0.0.1") and self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/state":
            t, v, s = q.get("tenant", "acme"), q.get("version", "checkout@1"), q.get("symptom", "sessions-lost")
            return self._json({"tenant": t, "version": v, "symptom": s, "procedures": rb.procedures(G, t, v, s),
                               "cases": rb.case_memory(G, t, s), "steps": {k: x["label"] for k, x in STEPS.items()}})
        return super().do_GET()

    def do_POST(self):
        global W
        if not self._local():
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        with LOCK:
            if self.path == "/api/step" and body.get("step") in STEPS:
                ev = {k: v for k, v in STEPS[body["step"]].items() if k != "label"}
                return self._json({"rev": rb.apply(W, ev)})
            if self.path == "/api/reset":
                W = rb.load(G, BASE)
                return self._json({"ok": True})
            if self.path == "/api/race":
                W = rb.load(G, BASE)
                got, bar = {}, threading.Barrier(2)

                def go(a):
                    bar.wait()
                    got[a] = W.claim("acme", "T1", a)
                ts = [threading.Thread(target=go, args=(a,)) for a in ("agent-a", "agent-b")]
                [t.start() for t in ts]
                [t.join() for t in ts]
                win = next(a for a, r in got.items() if r["ok"])
                log = [{"who": a, "did": "claim T1", "ok": r["ok"]} for a, r in got.items()]
                rev = W.revise("acme", "S2", True, "Queue re-verified after a config change")
                log.append({"who": "ops", "did": f"evidence for T1 changes (revision {rev})", "ok": True})
                stale = W.complete("acme", "T1", win, got[win]["gen"], got[win]["snapshot"])
                log.append({"who": win, "did": "complete T1 with the old snapshot", "ok": stale["ok"]})
                W.release("acme", "T1", win)
                c = W.claim("acme", "T1", win)
                log.append({"who": win, "did": "re-claim with fresh evidence", "ok": c["ok"]})
                log.append({"who": win, "did": "complete T1", "ok": W.complete("acme", "T1", win, c["gen"], c["snapshot"])["ok"]})
                W = rb.load(G, BASE)
                return self._json({"log": log})
        return self._json({"error": "unknown"}, 404)


if __name__ == "__main__":
    print("RecallBrake demo on http://localhost:8003")
    ThreadingHTTPServer(("127.0.0.1", 8003), Handler).serve_forever()
