"""
MTR — Network Trace routes (Flask Blueprint).

Endpoints
---------
GET    /mtr/stream               Start (or attach to) an MTR job, SSE progress
POST   /mtr/kill/<job_id>        Kill a running job (admin password if set)
DELETE /mtr/delete/<filename>    Delete a saved result (admin password if set)
GET    /mtr/running              List running jobs
POST   /mtr/tag/<filename>       Update the tag of a saved result
GET    /mtr/results              List saved results
GET    /mtr/results/<filename>   Download a saved result (JSON)
GET    /mtr/destinations         Saved destinations from .env (MTR_DEST_<n>=name|host)
"""

import datetime
import json
import os
import re
import subprocess
import threading
import time as _time

from flask import Blueprint, Response, jsonify, request, send_from_directory

mtr_bp = Blueprint("mtr", __name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_BASE_DIR, ".env")
MTR_RESULTS_DIR = os.path.join(_BASE_DIR, "mtr-results")

_DEST_KEY_RE = re.compile(r"^MTR_DEST_(\d+)$")


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════
def _read_env():
    """Parse .env into a dict. Returns {} if the file is missing/unreadable."""
    env = {}
    try:
        with open(_ENV_PATH, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key:
                    env[key] = val.strip()
    except Exception:
        pass
    return env


def _get_admin_password():
    """Read ADMIN_PASSWORD from .env. Returns None if not set."""
    return _read_env().get("ADMIN_PASSWORD") or None


def _check_password(req):
    """Validate X-Admin-Password header against ADMIN_PASSWORD in .env.
    Returns (ok: bool, error_response or None)."""
    required = _get_admin_password()
    if not required:
        return True, None  # No password set — allow all
    provided = req.headers.get("X-Admin-Password", "")
    if provided == required:
        return True, None
    return False, (jsonify({"success": False, "output": "❌ Invalid admin password."}), 403)


def _safe_name(filename):
    """Strip any directory component to prevent path traversal."""
    return os.path.basename(filename or "")


def _parse_mtr_output(lines):
    hops = []
    for line in lines:
        # mtr --report-wide with -b produces lines like:
        #  1.|-- 192.168.1.1 (192.168.1.1)  0.0%  60  1.2  1.5  0.9  3.1  0.5
        # or without -b:
        #  1.|-- 192.168.1.1  0.0%  60  1.2  1.5  0.9  3.1  0.5
        m = re.search(
            r'(\d+)\.\s*[|`!\-]+\s+(\S+(?:\s+\([^)]+\))?)\s+'
            r'([\d.]+)%\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
            line)
        if m:
            hops.append({
                "hop":   int(m.group(1)),
                "host":  m.group(2).strip(),
                "loss":  float(m.group(3)),
                "sent":  int(m.group(4)),
                "last":  float(m.group(5)),
                "avg":   float(m.group(6)),
                "best":  float(m.group(7)),
                "worst": float(m.group(8)),
            })
    return hops


def mtr_running_items():
    """Return list of currently running MTR jobs (from .running.json files).
    Shared by /mtr/running and the proxy activity aggregator."""
    if not os.path.isdir(MTR_RESULTS_DIR):
        return []
    items = []
    for f in sorted(os.listdir(MTR_RESULTS_DIR), reverse=True):
        if not f.endswith(".running.json"):
            continue
        try:
            with open(os.path.join(MTR_RESULTS_DIR, f)) as fh:
                d = json.load(fh)
            elapsed = 0
            try:
                st = datetime.datetime.fromisoformat(d["started_at"].replace("Z", ""))
                elapsed = int((datetime.datetime.utcnow() - st).total_seconds())
            except Exception:
                pass
            remaining = max(0, d.get("total_cycles", 0) - elapsed)
            items.append({
                "job_id":       d.get("job_id"),
                "destination":  d.get("destination"),
                "started_at":   d.get("started_at"),
                "mode":         d.get("mode"),
                "tag":          d.get("tag", ""),
                "source_ip":    d.get("source_ip", ""),
                "public_ip":    d.get("public_ip", ""),
                "duration_s":   d.get("duration_s"),
                "packets":      d.get("packets"),
                "no_dns":       d.get("no_dns", False),
                "proto":        d.get("proto", "icmp"),
                "geo":          d.get("geo", "country"),
                "elapsed":      elapsed,
                "remaining":    remaining,
                "total_cycles": d.get("total_cycles", 0),
            })
        except Exception:
            pass
    return items


# ═══════════════════════════════════════════════════════════
#  Saved destinations (.env → MTR_DEST_<n>=name|host)
# ═══════════════════════════════════════════════════════════
@mtr_bp.route("/mtr/destinations", methods=["GET"])
def mtr_destinations():
    """Return saved MTR destinations defined in .env.
    Format: MTR_DEST_<n>=<name>|<ip or hostname>  (name is optional)."""
    entries = []
    for key, val in _read_env().items():
        m = _DEST_KEY_RE.match(key)
        if not m or not val:
            continue
        if "|" in val:
            name, host = val.split("|", 1)
        else:
            name, host = val, val
        name, host = name.strip(), host.strip()
        if not host:
            continue
        entries.append((int(m.group(1)), {"name": name or host, "ip": host}))
    entries.sort(key=lambda e: e[0])
    return jsonify([e[1] for e in entries])


# ═══════════════════════════════════════════════════════════
#  Run / stream
# ═══════════════════════════════════════════════════════════
@mtr_bp.route("/mtr/stream", methods=["GET"])
def mtr_stream():
    """
    Runs mtr --report in a background thread (result saved to disk) while the
    SSE stream ticks the countdown. Reconnecting attaches to the same job
    without duplicating it.
    """
    host    = (request.args.get("host") or "").strip()
    mode    = request.args.get("mode") or "packets"
    count   = max(1, min(int(request.args.get("count") or 50), 500))
    seconds = max(10, min(int(request.args.get("seconds") or 60), 86400))
    no_dns  = request.args.get("no_dns") == "1"
    proto   = request.args.get("proto") or "icmp"     # "icmp" | "udp53"
    geo     = request.args.get("geo") or "country"    # "country" | "asn"
    src_ip  = request.args.get("src_ip") or "unknown"
    pub_ip  = request.args.get("pub_ip") or "unknown"
    tag     = (request.args.get("tag") or "").strip()

    if not host or host.startswith("-") or re.search(r"\s", host):
        def err():
            yield "data: ERROR: A valid host is required.\n\n"
            yield "data: __DONE__\n\n"
        return Response(err(), content_type="text/event-stream")

    os.makedirs(MTR_RESULTS_DIR, exist_ok=True)

    total_cycles = seconds if mode == "time" else count

    started_at = datetime.datetime.utcnow().isoformat() + "Z"
    ts         = started_at[:19].replace(":", "-").replace("T", "_")
    safe_host  = re.sub(r"[^\w\.\-]", "_", host)
    job_id     = f"{ts}_{safe_host}"
    state_file = os.path.join(MTR_RESULTS_DIR, f"{job_id}.running.json")
    done_file  = os.path.join(MTR_RESULTS_DIR, f"{job_id}.json")

    is_new_job = not os.path.isfile(state_file)

    if is_new_job:
        initial_state = {
            "job_id": job_id, "status": "running",
            "started_at": started_at, "ended_at": None,
            "source_ip": src_ip, "public_ip": pub_ip,
            "destination": host, "mode": mode,
            "packets": count if mode == "packets" else None,
            "duration_s": seconds if mode == "time" else None,
            "no_dns": no_dns, "proto": proto, "geo": geo, "tag": tag,
            "total_cycles": total_cycles,
            "hops": [], "raw": ""
        }
        with open(state_file, "w") as f:
            json.dump(initial_state, f)
    else:
        # Reconnecting — read existing state for metadata
        try:
            with open(state_file) as f:
                initial_state = json.load(f)
            tag          = initial_state.get("tag", tag)
            total_cycles = initial_state.get("total_cycles", total_cycles)
        except Exception:
            initial_state = {}

    def run_background():
        if mode == "time":
            bg_cmd = ["mtr", "--report", "--report-wide", "--interval", "1",
                      "--report-cycles", str(seconds)]
        else:
            bg_cmd = ["mtr", "--report", "--report-wide",
                      "--report-cycles", str(count)]
        # Always show IPs alongside names
        bg_cmd.append("-b")
        # Protocol
        if proto == "udp53":
            bg_cmd += ["-u", "-P", "53"]
        # Geo annotation
        if geo == "asn":
            bg_cmd.append("-z")
        else:
            bg_cmd += ["-y", "2"]
        if no_dns:
            bg_cmd.append("--no-dns")
        bg_cmd.append(host)

        lines = []
        try:
            proc = subprocess.Popen(bg_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, bufsize=0)
            raw_out = proc.communicate()[0]
            lines.extend(raw_out.decode(errors="replace").splitlines())
        except Exception as e:
            lines.append(f"ERROR: {e}")

        ended_at = datetime.datetime.utcnow().isoformat() + "Z"
        result   = dict(initial_state)
        result.update({
            "status": "done", "ended_at": ended_at,
            "hops": _parse_mtr_output(lines), "raw": "\n".join(lines)
        })
        with open(done_file, "w") as f:
            json.dump(result, f, indent=2)
        try:
            os.remove(state_file)
        except Exception:
            pass

    if is_new_job:
        threading.Thread(target=run_background, daemon=True).start()

    def stream_ticks():
        start = _time.time()
        last  = start
        while os.path.isfile(state_file):
            _time.sleep(0.3)
            now = _time.time()
            if now - last >= 1.0:
                elapsed   = int(now - start)
                remaining = max(0, total_cycles - elapsed)
                last = now
                yield f"data: __TICK__ {elapsed} {remaining}\n\n"
        # Job finished — stream the final result
        try:
            with open(done_file) as f:
                d = json.load(f)
            yield "data: \n\n"
            for line in (d.get("raw") or "").splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"
            yield "data: \n\n"
            yield f"data: ✔ Result saved: {job_id}.json\n\n"
        except Exception as e:
            yield f"data: ⚠ Could not read result: {e}\n\n"
        yield "data: __DONE__\n\n"

    return Response(stream_ticks(), content_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ═══════════════════════════════════════════════════════════
#  Job / result management
# ═══════════════════════════════════════════════════════════
@mtr_bp.route("/mtr/kill/<job_id>", methods=["POST"])
def mtr_kill(job_id):
    """Kill a running MTR job. Requires admin password if set."""
    ok, err = _check_password(request)
    if not ok:
        return err

    state_file = os.path.join(MTR_RESULTS_DIR, f"{_safe_name(job_id)}.running.json")
    if not os.path.isfile(state_file):
        return jsonify({"error": "Job not found or already finished"}), 404

    try:
        with open(state_file) as f:
            d = json.load(f)
        dest = d.get("destination", "")
        os.remove(state_file)
        if dest:
            subprocess.run(["pkill", "-f", f"mtr.*{re.escape(dest)}"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return jsonify({"success": True, "output": f"Job {job_id} killed."})
    except Exception as e:
        return jsonify({"success": False, "output": str(e)}), 500


@mtr_bp.route("/mtr/delete/<path:filename>", methods=["DELETE"])
def mtr_delete(filename):
    """Delete a completed MTR result JSON file. Requires admin password if set."""
    ok, err = _check_password(request)
    if not ok:
        return err

    filepath = os.path.join(MTR_RESULTS_DIR, _safe_name(filename))
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    try:
        os.remove(filepath)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@mtr_bp.route("/mtr/running", methods=["GET"])
def mtr_running():
    """Return list of currently running MTR jobs (from .running.json files)."""
    return jsonify(mtr_running_items())


@mtr_bp.route("/mtr/tag/<path:filename>", methods=["POST"])
def mtr_set_tag(filename):
    """Update the tag on a saved MTR result."""
    filepath = os.path.join(MTR_RESULTS_DIR, _safe_name(filename))
    if not os.path.isfile(filepath):
        return jsonify({"error": "File not found"}), 404
    data = request.get_json(silent=True) or {}
    tag  = (data.get("tag") or "").strip()
    try:
        with open(filepath) as f:
            result = json.load(f)
        result["tag"] = tag
        with open(filepath, "w") as f:
            json.dump(result, f, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mtr_bp.route("/mtr/results", methods=["GET"])
def mtr_results():
    """List saved MTR result files (completed only)."""
    if not os.path.isdir(MTR_RESULTS_DIR):
        return jsonify([])
    files = sorted(
        [f for f in os.listdir(MTR_RESULTS_DIR)
         if f.endswith(".json") and not f.endswith(".running.json")],
        reverse=True
    )[:50]
    items = []
    for f in files:
        try:
            with open(os.path.join(MTR_RESULTS_DIR, f)) as fh:
                d = json.load(fh)
            items.append({
                "file":        f,
                "destination": d.get("destination"),
                "started_at":  d.get("started_at"),
                "ended_at":    d.get("ended_at"),
                "mode":        d.get("mode"),
                "packets":     d.get("packets"),
                "duration_s":  d.get("duration_s"),
                "hops":        len(d.get("hops", [])),
                "tag":         d.get("tag", ""),
                "proto":       d.get("proto", "icmp"),
                "geo":         d.get("geo", "country"),
            })
        except Exception:
            pass
    return jsonify(items)


@mtr_bp.route("/mtr/results/<path:filename>", methods=["GET"])
def mtr_result_file(filename):
    """Download a specific MTR result JSON."""
    return send_from_directory(MTR_RESULTS_DIR, filename, as_attachment=True)
