"""
CMS Auto — Web Interface for fast_runner.py
Flask backend: handles session login, prosecutor list, input data, and script execution.
"""

import os, json, threading, time, queue, tempfile, traceback
from flask import Flask, request, jsonify, render_template, Response
from fast_runner import (
    ApiSession, parse_case, process_case, DECISION_MAPPING, BASE_URL
)
import requests, urllib.parse

app = Flask(__name__)

# ── In-memory state ────────────────────────────────────────
_state = {
    "session": None,           # ApiSession instance
    "log_queue": queue.Queue(),
    "running": False,
    "stats": {"total": 0, "success": 0, "skip": 0, "invalid": 0},
}

# ── Logging bridge ─────────────────────────────────────────
import logging

class QueueHandler(logging.Handler):
    def emit(self, record):
        _state["log_queue"].put(self.format(record))

_queue_handler = QueueHandler()
_queue_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(_queue_handler)


# ── Routes ─────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    """
    Accepts: { username, password, prosecutor_name }
    Does: browser-style login to cfms.prosecution.punjab.gov.pk,
          captures XSRF + session cookies, stores in ApiSession.
    """
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"ok": False, "msg": "Username aur password dono chahiye"})

    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/143 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "X-Requested-With": "XMLHttpRequest",
        })
        # Step 1: get landing page to capture initial XSRF
        r0 = s.get(BASE_URL + "/", timeout=15)
        xsrf_raw = ""
        for c in s.cookies:
            if c.name == "XSRF-TOKEN":
                xsrf_raw = urllib.parse.unquote(c.value)
                break
        s.headers["X-XSRF-TOKEN"] = xsrf_raw

        # Step 2: login POST
        r = s.post(BASE_URL + "/login", json={
            "email": username,
            "password": password,
        }, timeout=15, allow_redirects=True)

        if r.status_code not in [200, 201]:
            return jsonify({"ok": False, "msg": f"Login fail — HTTP {r.status_code}. Credentials check karo."})

        resp_data = {}
        try:
            resp_data = r.json()
        except Exception:
            pass

        # Some systems return error in JSON body
        if isinstance(resp_data, dict) and resp_data.get("message", "").lower() in ["unauthenticated.", "invalid credentials"]:
            return jsonify({"ok": False, "msg": "Invalid credentials — username/password galat hain"})

        # Refresh XSRF after login
        for c in s.cookies:
            if c.name == "XSRF-TOKEN":
                xsrf_raw = urllib.parse.unquote(c.value)
                break
        s.headers["X-XSRF-TOKEN"] = xsrf_raw

        # Step 3: verify session
        rv = s.get(BASE_URL + "/get-dashboard-stats", timeout=15)
        if rv.status_code != 200:
            return jsonify({"ok": False, "msg": f"Login hua lekin session verify nahi hua (HTTP {rv.status_code}). Site down ho sakti hai."})

        # Build ApiSession using this requests.Session
        api = ApiSession()
        api.session = s
        api.load_master_data()
        _state["session"] = api

        return jsonify({"ok": True, "msg": "✅ Login successful! Session active hai."})

    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error: {str(e)}"})


@app.route("/api/session-manual", methods=["POST"])
def api_session_manual():
    """
    Accepts raw XSRF-TOKEN + session cookie (copy-paste from browser DevTools).
    """
    data = request.json or {}
    xsrf = data.get("xsrf", "").strip()
    session_cookie = data.get("session_cookie", "").strip()

    if not xsrf or not session_cookie:
        return jsonify({"ok": False, "msg": "XSRF-TOKEN aur session cookie dono chahiye"})

    try:
        session_data = {
            "cookies": {
                "XSRF-TOKEN": xsrf,
                "prosecution_department_of_punjab_session": session_cookie,
            },
            "xsrf_token": xsrf,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        # Write temp session file and load it
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(session_data, tmp)
        tmp.close()

        api = ApiSession()
        # Monkey-patch SESSION_FILE
        import fast_runner as fr
        orig = fr.SESSION_FILE
        fr.SESSION_FILE = tmp.name
        ok = api.load_session()
        fr.SESSION_FILE = orig
        os.unlink(tmp.name)

        if not ok:
            return jsonify({"ok": False, "msg": "Session load nahi hua"})

        api.refresh_xsrf()
        rv = api.session.get(BASE_URL + "/get-dashboard-stats", timeout=15)
        if rv.status_code != 200:
            return jsonify({"ok": False, "msg": f"Session invalid — HTTP {rv.status_code}. Naye cookies copy karo."})

        api.load_master_data()
        _state["session"] = api
        return jsonify({"ok": True, "msg": "✅ Manual session load hua! Active hai."})

    except Exception as e:
        return jsonify({"ok": False, "msg": f"Error: {str(e)}"})


@app.route("/api/get-prosecutors", methods=["GET"])
def api_get_prosecutors():
    """
    Hits the prosecutors list API and returns name+id pairs.
    """
    api = _state.get("session")
    if not api:
        return jsonify({"ok": False, "msg": "Pehle login karo"})
    try:
        r = api.session.get(BASE_URL + "/get-prosecutors", timeout=15)
        if r.status_code != 200:
            return jsonify({"ok": False, "msg": f"HTTP {r.status_code}"})
        items = r.json()
        if isinstance(items, list):
            result = [{"id": x.get("id"), "name": x.get("text") or x.get("name") or str(x.get("id"))} for x in items]
        elif isinstance(items, dict):
            result = [{"id": k, "name": v} for k, v in items.items()]
        else:
            result = []
        return jsonify({"ok": True, "prosecutors": result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/set-prosecutor", methods=["POST"])
def api_set_prosecutor():
    data = request.json or {}
    pid = data.get("prosecutor_id")
    api = _state.get("session")
    if not api:
        return jsonify({"ok": False, "msg": "Pehle login karo"})
    try:
        api.prosecutor_id = int(pid)
        return jsonify({"ok": True, "msg": f"Prosecutor ID set: {pid}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/run", methods=["POST"])
def api_run():
    """
    Accepts: { input_text: "tab-separated cases" }
    Runs the script line by line and streams logs via SSE.
    """
    if _state["running"]:
        return jsonify({"ok": False, "msg": "Script abhi bhi chal rahi hai. Ruko."})

    api = _state.get("session")
    if not api:
        return jsonify({"ok": False, "msg": "Pehle login karo"})

    data = request.json or {}
    input_text = data.get("input_text", "").strip()
    if not input_text:
        return jsonify({"ok": False, "msg": "Input data empty hai"})

    lines = [l for l in input_text.splitlines() if l.strip()]
    if not lines:
        return jsonify({"ok": False, "msg": "Koi valid line nahi mili"})

    _state["stats"] = {"total": 0, "success": 0, "skip": 0, "invalid": 0}
    _state["running"] = True

    def runner():
        try:
            api.refresh_xsrf()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                _state["stats"]["total"] += 1
                status = process_case(api, line)
                if status == "COMPLETE":
                    _state["stats"]["success"] += 1
                elif status == "SKIP":
                    _state["stats"]["skip"] += 1
                elif status == "INVALID":
                    _state["stats"]["invalid"] += 1
                time.sleep(0.5)
            _state["log_queue"].put("__DONE__")
        except Exception as e:
            _state["log_queue"].put(f"ERROR: {traceback.format_exc()}")
            _state["log_queue"].put("__DONE__")
        finally:
            _state["running"] = False

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Script shuru ho gayi!"})


@app.route("/api/stream-logs")
def stream_logs():
    """Server-Sent Events stream for real-time logs."""
    def generate():
        yield "data: {\"type\":\"connected\"}\n\n"
        while True:
            try:
                msg = _state["log_queue"].get(timeout=30)
                if msg == "__DONE__":
                    stats = _state["stats"]
                    yield f"data: {{\"type\":\"done\", \"stats\": {json.dumps(stats)}}}\n\n"
                    break
                yield f"data: {json.dumps({'type':'log','msg': msg})}\n\n"
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/status")
def api_status():
    return jsonify({
        "logged_in": _state["session"] is not None,
        "running": _state["running"],
        "stats": _state["stats"],
        "prosecutor_id": _state["session"].prosecutor_id if _state["session"] else None,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
