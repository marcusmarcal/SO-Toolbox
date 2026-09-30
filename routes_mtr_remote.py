"""MTR remote results: receives automatic MTR reports pushed by remote servers
and exposes them to the MTR tool (Remote tab)."""
import os
import re
import json
import time
import hmac
import datetime
import threading
from flask import Blueprint, request, jsonify

from routes_auth import require_auth

mtr_remote_bp = Blueprint("mtr_remote", __name__)

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = os.path.join(BASE_DIR, "store", "mtr-remote")
os.makedirs(REMOTE_DIR, exist_ok=True)

LABELS_FILE  = os.path.join(BASE_DIR, "store", "mtr-remote-labels.json")
_labels_lock = threading.Lock()

MAX_BODY_BYTES         = 512 * 1024
DEFAULT_RETENTION_DAYS = 15
_last_cleanup          = 0.0
_item_cache            = {}   # 'host/file' -> list item (reports are immutable, so safe to cache)

_HOP_RE = re.compile(
    r'(\d+)\.\s*[|`!\-]+\s+(\S+(?:\s+\([^)]+\))?)\s+'
    r'([\d.]+)%?\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+([\d.]+))?')
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


def _client_ip():
    """Real client IP. Behind the local nginx reverse proxy remote_addr is 127.0.0.1, so trust
    X-Real-IP (set by nginx from $remote_addr) only when the request comes from loopback."""
    addr = request.remote_addr or ""
    if addr in ("127.0.0.1", "::1"):
        real = (request.headers.get("X-Real-IP") or "").strip()
        if real:
            return real
        fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if fwd:
            return fwd
    return addr


def _load_labels():
    """Destination labels, keyed by the filesystem-safe target name."""
    try:
        with open(LABELS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_item(h, f):
    """List item for one stored report (without label), cached in memory."""
    key = f"{h}/{f}"
    it = _item_cache.get(key)
    if it is None:
        with open(os.path.join(REMOTE_DIR, h, f)) as fh:
            d = json.load(fh)
        it = {
            "file":       key,
            "host":       (d.get("hostname") or h).upper(),
            "target":     d.get("target", ""),
            "started_at": d.get("started_at"),
            "ended_at":   d.get("ended_at"),
            "source_ip":  d.get("source_ip", ""),
            # Re-parsed from raw so reports stored by an older parser are corrected too
            "summary":    _summarize(_parse_hops(d["raw"])) if d.get("raw") else d.get("summary", {}),
        }
        _item_cache[key] = it
    return it


def _has_loss(summary, mode):
    """mode 'final' = loss at the destination hop; 'any' = loss on any hop.
    Reports with no parsed hops (mtr failed) count as loss."""
    v = summary.get("final_loss") if mode == "final" else summary.get("max_loss")
    return v is None or v > 0


def _range_key(v):
    """Convert an ISO UTC timestamp (YYYY-MM-DDTHH:MM:SSZ) to the filename key (YYYY-MM-DD_HH-MM-SS)."""
    v = (v or "").strip()
    if not _TS_RE.match(v):
        return ""
    return v[:19].replace(":", "-").replace("T", "_")


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
                    _item_cache.pop(f"{h}/{f}", None)
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
    hostname = (request.headers.get("X-MTR-Hostname") or "").strip()[:200].upper()
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
        "source_ip":   _client_ip(),
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
    """List remote results (newest first).
    Filters: host, target (substring), date (YYYY-MM-DD, UTC), from/to (ISO UTC timeframe), latest=1, limit,
    loss=final|any (only reports with loss at the destination hop / on any hop; applied after `latest`).
    `target` matches the IP/host or its label.
    Also returns `hosts` and `targets` (targets already narrowed by host/date/timeframe, not by target)."""
    host_f   = _safe((request.args.get("host") or "").strip()).upper()
    target_f = (request.args.get("target") or "").strip().lower()
    date_f   = (request.args.get("date") or "").strip()
    from_f   = _range_key(request.args.get("from"))
    to_f     = _range_key(request.args.get("to"))
    loss_f   = (request.args.get("loss") or "").strip()
    if loss_f not in ("final", "any"):
        loss_f = ""
    latest   = request.args.get("latest") == "1"
    try:
        limit = max(1, min(int(request.args.get("limit") or 200), 500))
    except ValueError:
        limit = 200

    lbl_map = _load_labels()
    hosts, entries, all_targets = [], [], set()
    for h in sorted(os.listdir(REMOTE_DIR)):
        hdir = os.path.join(REMOTE_DIR, h)
        if not os.path.isdir(hdir):
            continue
        hosts.append(h)
        if host_f and h.upper() != host_f:
            continue
        for f in os.listdir(hdir):
            if not f.endswith(".json"):
                continue
            parts = f[:-5].split("_", 2)  # date, time, target
            if len(parts) < 3:
                continue
            if date_f and parts[0] != date_f:
                continue
            key = f[:19]
            if from_f and key < from_f:
                continue
            if to_f and key > to_f:
                continue
            all_targets.add(parts[2])
            lbl = lbl_map.get(parts[2], "")
            if target_f and target_f not in parts[2].lower() and target_f not in lbl.lower():
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
    for f, h, _t in entries:
        try:
            it = _load_item(h, f)
        except Exception:
            continue
        if loss_f and not _has_loss(it["summary"], loss_f):
            continue
        items.append(dict(it, label=lbl_map.get(_t, "")))
        if len(items) >= limit:
            break
    return jsonify({"hosts": hosts, "targets": [{"value": t, "label": lbl_map.get(t, "")}
                                          for t in sorted(all_targets, key=str.lower)],
                    "items": items})


@mtr_remote_bp.route("/mtr/remote/results/<path:relpath>", methods=["GET"])
def mtr_remote_result_file(relpath):
    """Return one remote result (full record incl. raw output)."""
    root = os.path.realpath(REMOTE_DIR)
    full = os.path.realpath(os.path.join(root, relpath))
    if not full.startswith(root + os.sep) or not full.endswith(".json") or not os.path.isfile(full):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(full) as f:
            d = json.load(f)
        d["label"] = _load_labels().get(_safe(d.get("target", "")), "")
        d["hostname"] = (d.get("hostname") or "").upper()
        if d.get("raw"):  # re-parse so reports stored by an older parser show all hops
            d["hops"] = _parse_hops(d["raw"])
            d["summary"] = _summarize(d["hops"])
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mtr_remote_bp.route("/mtr/remote/labels", methods=["GET"])
def mtr_remote_labels_get():
    """All destination labels (keyed by safe target name)."""
    return jsonify(_load_labels())


@mtr_remote_bp.route("/mtr/remote/labels", methods=["POST"])
@require_auth
def mtr_remote_labels_set():
    """Set or remove (empty label) the label of a destination. Body: {"target": "...", "label": "..."}."""
    data   = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()[:200]
    label  = re.sub(r"[\r\n\t]+", " ", (data.get("label") or "")).strip()[:60]
    if not target:
        return jsonify({"error": "target is required"}), 400

    key = _safe(target)
    with _labels_lock:
        labels = _load_labels()
        if label:
            labels[key] = label
        else:
            labels.pop(key, None)
        tmp = LABELS_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(labels, f, indent=2, sort_keys=True)
            os.replace(tmp, LABELS_FILE)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "target": target, "label": label})


def _migrate_host_dirs():
    """Hostnames are stored upper-case; merge any legacy mixed/lower-case host folders into them."""
    for h in os.listdir(REMOTE_DIR):
        src = os.path.join(REMOTE_DIR, h)
        if not os.path.isdir(src) or h == h.upper():
            continue
        dst = os.path.join(REMOTE_DIR, h.upper())
        os.makedirs(dst, exist_ok=True)
        for f in os.listdir(src):
            try:
                os.replace(os.path.join(src, f), os.path.join(dst, f))
            except OSError:
                pass
        try:
            os.rmdir(src)
        except OSError:
            pass


def _cleanup_loop():
    """Hourly retention cleanup, independent of incoming ingests."""
    while True:
        try:
            _cleanup()
        except Exception:
            pass
        time.sleep(3600)


try:
    _migrate_host_dirs()
except Exception:
    pass
threading.Thread(target=_cleanup_loop, daemon=True).start()
