"""MTR remote results: receives automatic MTR reports pushed by remote servers
and exposes them to the MTR tool (Remote tab)."""
import os
import re
import json
import time
import hmac
import datetime
from flask import Blueprint, request, jsonify

mtr_remote_bp = Blueprint("mtr_remote", __name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = os.path.join(BASE_DIR, "store", "mtr-remote")
os.makedirs(REMOTE_DIR, exist_ok=True)

MAX_BODY_BYTES         = 512 * 1024
DEFAULT_RETENTION_DAYS = 30
_last_cleanup          = 0.0

_HOP_RE = re.compile(
    r'(\d+)\.\s*[|`!\-]+\s+(\S+(?:\s+\([^)]+\))?)\s+'
    r'([\d.]+)%\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+([\d.]+))?')
_TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')


def _env(key, default=""):
    """Read a single key from .env (same convention as the rest of the proxy)."""
    env_path = os.path.join(BASE_DIR, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


def _safe(s):
    return re.sub(r"[^\w.\-]", "_", s or "")[:120]


def _parse_hops(raw):
    hops = []
    for line in raw.splitlines():
        m = _HOP_RE.search(line)
        if not m:
            continue
        hops.append({
            "hop":   int(m.group(1)),
            "host":  m.group(2).strip(),
            "loss":  float(m.group(3)),
            "sent":  int(m.group(4)),
            "last":  float(m.group(5)),
            "avg":   float(m.group(6)),
            "best":  float(m.group(7)),
            "worst": float(m.group(8)),
            "stdev": float(m.group(9)) if m.group(9) else None,
        })
    return hops


def _summarize(hops):
    if not hops:
        return {"hops": 0, "final_loss": None, "final_avg": None,
                "final_best": None, "final_worst": None, "max_loss": None}
    last = hops[-1]
    return {
        "hops":        len(hops),
        "final_loss":  last["loss"],
        "final_avg":   last["avg"],
        "final_best":  last["best"],
        "final_worst": last["worst"],
        "max_loss":    max(h["loss"] for h in hops),
    }


def _cleanup():
    """Remove results older than MTR_REMOTE_RETENTION_DAYS (0 = keep forever). Runs at most hourly."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 3600:
        return
    _last_cleanup = now
    try:
        days = int(_env("MTR_REMOTE_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    except ValueError:
        days = DEFAULT_RETENTION_DAYS
    if days <= 0:
        return
    cutoff = now - days * 86400
    for h in os.listdir(REMOTE_DIR):
        hdir = os.path.join(REMOTE_DIR, h)
        if not os.path.isdir(hdir):
            continue
        for f in os.listdir(hdir):
            p = os.path.join(hdir, f)
            try:
                if f.endswith(".json") and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(hdir)  # only succeeds when empty
        except OSError:
            pass


@mtr_remote_bp.route("/mtr/remote/ingest", methods=["POST"])
def mtr_remote_ingest():
    """Receive one raw `mtr -rwb` report. Metadata comes in X-MTR-* headers, body is the raw output."""
    token = _env("MTR_REMOTE_TOKEN")
    if not token:
        return jsonify({"error": "MTR_REMOTE_TOKEN not configured"}), 503
    if not hmac.compare_digest(request.headers.get("X-MTR-Token", ""), token):
        return jsonify({"error": "Invalid token"}), 403

    if (request.content_length or 0) > MAX_BODY_BYTES:
        return jsonify({"error": "Payload too large"}), 413

    raw      = request.get_data(as_text=True) or ""
    hostname = (request.headers.get("X-MTR-Hostname") or "").strip()[:200]
    target   = (request.headers.get("X-MTR-Target") or "").strip()[:200]
    if not hostname or not target or not raw.strip():
        return jsonify({"error": "X-MTR-Hostname, X-MTR-Target and body are required"}), 400

    now_iso    = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    started_at = request.headers.get("X-MTR-Started", "")
    ended_at   = request.headers.get("X-MTR-Ended", "")
    if not _TS_RE.match(started_at):
        started_at = now_iso
    if not _TS_RE.match(ended_at):
        ended_at = now_iso

    hops   = _parse_hops(raw)
    record = {
        "hostname":    hostname,
        "target":      target,
        "started_at":  started_at,
        "ended_at":    ended_at,
        "received_at": now_iso,
        "source_ip":   (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
        "count":       request.headers.get("X-MTR-Count", ""),
        "interval":    request.headers.get("X-MTR-Interval", ""),
        "summary":     _summarize(hops),
        "hops":        hops,
        "raw":         raw,
    }

    host_dir = os.path.join(REMOTE_DIR, _safe(hostname))
    os.makedirs(host_dir, exist_ok=True)
    ts    = started_at[:19].replace(":", "-").replace("T", "_")
    fname = f"{ts}_{_safe(target)}.json"
    path  = os.path.join(host_dir, fname)
    tmp   = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        _cleanup()
    except Exception:
        pass
    return jsonify({"ok": True, "file": f"{_safe(hostname)}/{fname}", "hops": len(hops)})


@mtr_remote_bp.route("/mtr/remote/results", methods=["GET"])
def mtr_remote_results():
    """List remote results (newest first). Filters: host, target, date (YYYY-MM-DD, UTC), latest=1, limit."""
    host_f   = _safe((request.args.get("host") or "").strip())
    target_f = (request.args.get("target") or "").strip().lower()
    date_f   = (request.args.get("date") or "").strip()
    latest   = request.args.get("latest") == "1"
    try:
        limit = max(1, min(int(request.args.get("limit") or 200), 500))
    except ValueError:
        limit = 200

    hosts, entries = [], []
    for h in sorted(os.listdir(REMOTE_DIR)):
        hdir = os.path.join(REMOTE_DIR, h)
        if not os.path.isdir(hdir):
            continue
        hosts.append(h)
        if host_f and h != host_f:
            continue
        for f in os.listdir(hdir):
            if not f.endswith(".json"):
                continue
            parts = f[:-5].split("_", 2)  # date, time, target
            if len(parts) < 3:
                continue
            if date_f and parts[0] != date_f:
                continue
            if target_f and target_f not in parts[2].lower():
                continue
            entries.append((f, h, parts[2]))

    entries.sort(key=lambda e: e[0], reverse=True)

    if latest:
        seen, dedup = set(), []
        for e in entries:
            key = (e[1], e[2])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(e)
        entries = dedup

    items = []
    for f, h, _t in entries[:limit]:
        try:
            with open(os.path.join(REMOTE_DIR, h, f)) as fh:
                d = json.load(fh)
        except Exception:
            continue
        items.append({
            "file":       f"{h}/{f}",
            "host":       d.get("hostname", h),
            "target":     d.get("target", ""),
            "started_at": d.get("started_at"),
            "ended_at":   d.get("ended_at"),
            "source_ip":  d.get("source_ip", ""),
            "summary":    d.get("summary", {}),
        })
    return jsonify({"hosts": hosts, "items": items})


@mtr_remote_bp.route("/mtr/remote/results/<path:relpath>", methods=["GET"])
def mtr_remote_result_file(relpath):
    """Return one remote result (full record incl. raw output)."""
    root = os.path.realpath(REMOTE_DIR)
    full = os.path.realpath(os.path.join(root, relpath))
    if not full.startswith(root + os.sep) or not full.endswith(".json") or not os.path.isfile(full):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(full) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
