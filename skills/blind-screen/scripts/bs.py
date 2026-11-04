#!/usr/bin/env python3
"""blind-screen: blinded human + Claude dual screening for systematic reviews.

Commands (run from the review's project folder, or pass --project):
  prepare    --stage ta|ft --records FILE --criteria FILE [--pdf-dir DIR] [--allow-missing]
  batches    --stage ta|ft [--size 50]        split records for Claude sub-agents
  check      --stage ta|ft                    how many records Claude has staged (no decisions shown)
  seal       --stage ta|ft --model NAME       seal Claude's decisions (SHA-256)
  serve      --stage ta|ft [--adjudicate] [--port N] [--no-open]
  status     --stage ta|ft
  reconcile  --stage ta|ft                    lock your log, unseal Claude, kappa, round1/round2 TSVs
  finalize   --stage ta|ft                    round3 TSV, PRISMA counts, Methods paragraph
"""

import argparse
import json
import mimetypes
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bs_core as core  # noqa: E402

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "page", "index.html")


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

def make_handler(project, stage, mode, token):
    cfg = core.load_config(project, stage)
    records = core.load_stage_records(project, stage)
    by_id = {r["id"]: r for r in records}

    def state():
        base = {"mode": mode, "stage": stage, "criteria": cfg["criteria"], "codes": cfg["codes"],
                "has_pdf": stage == "ft"}
        if mode == "screen":
            human = core.latest(core.read_jsonl(core.human_path(project, stage)))
            base["records"] = [dict(r, mine=human.get(r["id"])) for r in records]
            base["locked"] = core.is_locked(project, stage)
            return base
        # adjudicate: only now may Claude's decisions be read
        claude = core.load_claude(project, stage)
        human = core.latest(core.read_jsonl(core.human_path(project, stage)))
        adj = core.latest(core.read_jsonl(core.adjudication_path(project, stage)))
        base["records"] = [dict(by_id[i], human=human[i], claude=claude[i], mine=adj.get(i))
                           for i in core.adjudication_queue(project, stage)]
        base["locked"] = os.path.exists(os.path.join(core.stage_dir(project, stage), "final.json"))
        return base

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, q):
            return q.get("t", [""])[0] == token

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                return self._send(403, {"error": "Open the link printed by bs.py serve."})
            if u.path == "/":
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                try:
                    return self._send(200, state())
                except core.BlindScreenError as e:
                    return self._send(409, {"error": str(e)})
            if u.path.startswith("/pdf/") and stage == "ft":
                rid = unquote(u.path[5:])
                rec = by_id.get(rid)
                if not rec or not rec.get("pdf"):
                    return self._send(404, {"error": "no pdf"})
                path = os.path.join(cfg["pdf_dir"], rec["pdf"])
                with open(path, "rb") as f:
                    return self._send(200, f.read(), mimetypes.guess_type(path)[0] or "application/pdf")
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q) or u.path != "/api/decide":
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                fn = core.record_human if mode == "screen" else core.record_adjudication
                row = fn(project, stage, body["id"], body["decision"], body.get("code", ""), body.get("note", ""))
                return self._send(200, {"ok": True, "saved": row})
            except (core.BlindScreenError, KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})

    return Handler


def serve(project, stage, adjudicate, port, open_browser):
    mode = "adjudicate" if adjudicate else "screen"
    if mode == "screen":
        if not core.is_sealed(project, stage):
            raise core.BlindScreenError("Claude's screening is not sealed yet. Run Claude's pass and "
                                        "`bs.py seal` first so the two reviews are provably independent.")
        if core.is_locked(project, stage):
            raise core.BlindScreenError("Your screening is locked. Use --adjudicate for the disagreements.")
    else:
        core.adjudication_queue(project, stage)  # raises if not reconciled
    token = secrets.token_urlsafe(16)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(project, stage, mode, token))
    url = "http://127.0.0.1:%d/?t=%s" % (httpd.server_address[1], token)
    print("Screening page (%s, %s): %s" % (stage, mode, url), flush=True)
    print("Leave this running; every keypress is saved. Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["prepare", "batches", "check", "seal", "serve", "status",
                                        "reconcile", "finalize"])
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--stage", default="ta", choices=["ta", "ft"])
    ap.add_argument("--records")
    ap.add_argument("--criteria")
    ap.add_argument("--pdf-dir")
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--size", type=int, default=50)
    ap.add_argument("--model")
    ap.add_argument("--adjudicate", action="store_true")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)
    p, s = os.path.abspath(a.project), a.stage

    try:
        if a.command == "prepare":
            if not a.records or not a.criteria:
                ap.error("prepare needs --records and --criteria")
            cfg = core.prepare(p, s, a.records, a.criteria, a.pdf_dir, a.allow_missing)
            print("Prepared %d records for %s screening in %s" % (cfg["n_records"], s, core.stage_dir(p, s)))
            print("Exclusion codes: " + "; ".join("%s %s" % kv for kv in sorted(cfg["codes"].items())))
            if cfg["not_retrieved"]:
                print("Reports not retrieved (PRISMA): %d" % len(cfg["not_retrieved"]))
        elif a.command == "batches":
            names = core.make_batches(p, s, a.size)
            print("%d batch file(s) in %s" % (len(names), os.path.join(core.stage_dir(p, s), "batches")))
            for n in names:
                print("  " + n)
        elif a.command == "check":
            got, missing, problems = core.staging_status(p, s)
            print("Claude has staged %d of %d records." % (len(got), len(got) + len(missing)))
            for pr in problems:
                print("  PROBLEM: " + pr)
            if missing:
                print("  Not yet screened: %s" % ", ".join(missing[:30]) + (" ..." if len(missing) > 30 else ""))
        elif a.command == "seal":
            if not a.model:
                ap.error("seal needs --model (e.g. claude-opus-5-5)")
            info = core.seal(p, s, a.model)
            print("Sealed %d Claude decisions at %s\nSHA-256 %s" % (info["n"], info["sealed_at"], info["sha256"]))
        elif a.command == "serve":
            serve(p, s, a.adjudicate, a.port, not a.no_open)
        elif a.command == "status":
            cfg = core.load_config(p, s)
            done, total = core.progress(p, s)
            print("Stage %s: %d records" % (s, total))
            print("  Claude sealed : %s" % ("yes" if core.is_sealed(p, s) else "no"))
            print("  You screened  : %d / %d" % (done, total))
            print("  Reconciled    : %s" % ("yes" if core.is_locked(p, s) else "no"))
            if core.is_locked(p, s):
                q = core.adjudication_queue(p, s)
                adj = core.latest(core.read_jsonl(core.adjudication_path(p, s)))
                print("  Disagreements : %d decided / %d" % (len([i for i in q if i in adj]), len(q)))
            print("  Finalised     : %s" % ("yes" if os.path.exists(os.path.join(core.stage_dir(p, s), "final.json")) else "no"))
            if cfg.get("not_retrieved"):
                print("  Not retrieved : %d" % len(cfg["not_retrieved"]))
        elif a.command == "reconcile":
            stats, lock = core.reconcile(p, s)
            k = stats["kappa_binary"]
            print("Seal verified. Your log is locked.")
            print("Agreement %.1f%%, Cohen's kappa %s (include vs exclude)" %
                  (stats["percent_agreement_binary"], "n/a" if k is None else "%.2f" % k))
            print("Both include %d | both exclude %d | only you include %d | only Claude includes %d | unsure %d"
                  % (stats["both_include"], stats["both_exclude"], stats["human_only_include"],
                     stats["claude_only_include"], stats["either_unsure"]))
            print("Disagreements to decide: %d" % stats["maybe"])
            print("Wrote %s and %s" % (lock["round1_file"], lock["round2_file"]))
        elif a.command == "finalize":
            final, prisma, mpath = core.finalize(p, s)
            print(json.dumps(prisma, indent=2))
            print("Wrote %s\nMethods paragraph: %s" % (final["round3_file"], os.path.relpath(mpath, p)))
    except core.BlindScreenError as e:
        print("blind-screen: " + str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
