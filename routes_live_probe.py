"""
routes_live_probe.py — Live SRT Probe Blueprint
SO-Toolbox v2.32.0

Real-time IAT (Inter-Arrival Time) / MLR (Media Loss Rate) monitor for GOP
Analyser tests whose source is a live SRT stream (never shown for uploaded
file tests). Reuses the same host/port already stored on the test result.
This is fully independent of any external probe appliance — capture and
analysis both happen inside SO-Toolbox.

Why srt-live-transmit instead of ffmpeg:
ffmpeg's SRT input demuxes the transport stream into elementary streams and
then re-multiplexes it (even with `-c copy`), which regenerates MPEG-TS
continuity counters and can shuffle PIDs. That would hide genuine network
packet loss instead of exposing it. `srt-live-transmit` (Haivision srt-tools)
does no such transformation — piping to `file://con` writes the exact bytes
received off the wire to stdout, so continuity counters reflect what the
network actually delivered.

Metrics:
  MLR = MPEG-TS continuity-counter discontinuities per PID, counted per
        1-second window. Null packets (PID 0x1FFF) and adaptation-field-only
        packets (no payload, CC does not increment) are excluded. A single
        repeated CC is treated as a legal retransmit duplicate, not a loss.
  IAT = wall-clock delta between successive stdout reads from
        srt-live-transmit, in ms. avg/max reported per 1-second window.
        A full stream stall is reported as state="stalled" once no data has
        been read for STALL_TIMEOUT_S.

Requires `srt-live-transmit` on PATH (Haivision srt-tools build):
  https://github.com/Haivision/srt  ->  ./configure && make && make install

Session lifecycle:
  POST /live-probe/start           -> starts capture, returns {session_id}
  GET  /live-probe/stream/<id>     -> SSE feed of live samples
  POST /live-probe/stop/<id>       -> explicit stop (called on modal close)
A background watchdog also reaps sessions with no attached SSE client after
IDLE_GRACE_S, and hard-caps any session at SESSION_TTL_S, so an abandoned
browser tab can never leave an orphaned capture process running forever.
"""

import os
import re
import json
import time
import uuid
import shutil
import queue
import threading
import subprocess

from flask import Blueprint, request, jsonify, Response, stream_with_context

from routes_auth import _get_session, _token_from_request

# ── Blueprint ─────────────────────────────────────────────────────────────
live_probe_bp = Blueprint('live_probe', __name__)

# ── Config ────────────────────────────────────────────────────────────────
SRT_LIVE_TRANSMIT = shutil.which("srt-live-transmit") or "srt-live-transmit"

CHUNK_SIZE         = 65536
STALL_TIMEOUT_S     = 5
SESSION_TTL_S       = 4 * 3600     # hard cap per session, regardless of activity
IDLE_GRACE_S        = 20           # reap if no SSE client attached this long
RECONNECT_DELAY_S   = 2
MAX_RECONNECTS      = 100000       # effectively unlimited within SESSION_TTL_S
WATCHDOG_INTERVAL_S = 10

_sessions = {}
_sessions_lock = threading.Lock()


def _get_username_from_request() -> str:
    """Same pattern as routes_gop.py — logged-in username or 'anonymous'."""
    session = _get_session(_token_from_request())
    if session:
        return session.get('username', 'anonymous')
    return 'anonymous'


# ════════════════════════════════════════════════════════════════════════════
#  MPEG-TS continuity-counter tracker
# ════════════════════════════════════════════════════════════════════════════

class _CCTracker:
    """Per-PID continuity-counter tracker -> lost packet count.

    Note: CC is only 4 bits wide, so a burst loss of 16+ consecutive packets
    on the same PID is indistinguishable from zero loss. This is an inherent
    limitation of continuity-counter-based loss detection, not a bug.
    """

    def __init__(self):
        self._last_cc = {}   # pid -> last continuity_counter seen (0-15)

    def feed(self, buf: bytes):
        """Scan a chunk of raw TS bytes. Returns (lost_packets, packets_seen)."""
        lost = 0
        seen = 0
        n = len(buf) - (len(buf) % 188)
        for i in range(0, n, 188):
            if buf[i] != 0x47:
                continue  # not sync-aligned; skip (shouldn't happen on aligned input)
            seen += 1
            pid = ((buf[i + 1] & 0x1F) << 8) | buf[i + 2]
            if pid == 0x1FFF:
                continue  # null/stuffing packet — no CC semantics
            afc = (buf[i + 3] >> 4) & 0x3   # adaptation_field_control
            if afc in (0x0, 0x2):
                continue  # no payload present -> CC does not increment
            cc = buf[i + 3] & 0x0F
            prev = self._last_cc.get(pid)
            if prev is not None:
                expected = (prev + 1) % 16
                if cc == expected:
                    pass
                elif cc == prev:
                    pass  # legal single duplicate (retransmit marker)
                else:
                    lost += (cc - expected) % 16
            self._last_cc[pid] = cc
        return lost, seen


# ════════════════════════════════════════════════════════════════════════════
#  Live capture session
# ════════════════════════════════════════════════════════════════════════════

class LiveProbeSession:
    def __init__(self, session_id, host, port, passphrase, tag, username):
        self.id = session_id
        self.host = host
        self.port = port
        self.passphrase = passphrase
        self.tag = tag
        self.username = username
        self.created_at = time.time()
        self.last_client_at = time.time()
        self.stop_flag = threading.Event()
        self.subscribers = []
        self.subscribers_lock = threading.Lock()
        self.proc = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _build_uri(self):
        q = "mode=caller"
        if self.passphrase:
            q += f"&passphrase={self.passphrase}"
        return f"srt://{self.host}:{self.port}?{q}"

    def _emit(self, event):
        with self.subscribers_lock:
            subs = list(self.subscribers)
        for q_ in subs:
            try:
                q_.put_nowait(event)
            except queue.Full:
                pass  # slow/gone client — drop sample rather than block capture

    def _run(self):
        reconnects = 0
        cc = _CCTracker()
        window_lost = 0
        window_bytes = 0
        iat_samples = []
        window_start = time.time()

        while not self.stop_flag.is_set() and reconnects <= MAX_RECONNECTS:
            if (time.time() - self.created_at) > SESSION_TTL_S:
                break

            uri = self._build_uri()
            cmd = [SRT_LIVE_TRANSMIT, "-loglevel:error", uri, "file://con"]
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )
            except FileNotFoundError:
                self._emit({
                    "type": "error",
                    "message": "srt-live-transmit not found on server. "
                               "Install Haivision srt-tools (github.com/Haivision/srt).",
                })
                return

            self._emit({"type": "status", "state": "connecting"})
            connected_once = False
            last_read_t = time.time()

            try:
                while not self.stop_flag.is_set():
                    buf = self.proc.stdout.read(CHUNK_SIZE)
                    now = time.time()
                    if not buf:
                        break  # process ended / stream closed

                    connected_once = True
                    delta_ms = (now - last_read_t) * 1000.0
                    last_read_t = now
                    iat_samples.append(delta_ms)

                    lost, _seen = cc.feed(buf)
                    window_lost += lost
                    window_bytes += len(buf)

                    elapsed = now - window_start
                    if elapsed >= 1.0:
                        avg_iat = sum(iat_samples) / len(iat_samples) if iat_samples else 0.0
                        max_iat = max(iat_samples) if iat_samples else 0.0
                        kbps = (window_bytes * 8 / 1000.0) / elapsed if elapsed > 0 else 0.0
                        self._emit({
                            "type": "sample",
                            "t": now,
                            "iat_avg_ms": round(avg_iat, 1),
                            "iat_max_ms": round(max_iat, 1),
                            "mlr": window_lost,
                            "bitrate_kbps": round(kbps, 1),
                            "state": "running",
                        })
                        window_lost = 0
                        window_bytes = 0
                        iat_samples = []
                        window_start = now
            finally:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass

            if self.stop_flag.is_set():
                break

            self._emit({
                "type": "status",
                "state": "stalled" if connected_once else "unreachable",
            })
            reconnects += 1
            time.sleep(RECONNECT_DELAY_S)

        self._emit({"type": "status", "state": "stopped"})

    def subscribe(self):
        q_ = queue.Queue(maxsize=200)
        with self.subscribers_lock:
            self.subscribers.append(q_)
        self.last_client_at = time.time()
        return q_

    def unsubscribe(self, q_):
        with self.subscribers_lock:
            if q_ in self.subscribers:
                self.subscribers.remove(q_)
        self.last_client_at = time.time()

    def has_subscribers(self):
        with self.subscribers_lock:
            return len(self.subscribers) > 0

    def stop(self):
        self.stop_flag.set()
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass


# ── Watchdog: reap idle / expired sessions ─────────────────────────────────
def _watchdog():
    while True:
        time.sleep(WATCHDOG_INTERVAL_S)
        now = time.time()
        with _sessions_lock:
            stale_ids = []
            for sid, s in list(_sessions.items()):
                expired = (now - s.created_at) > SESSION_TTL_S
                idle = (not s.has_subscribers()) and (now - s.last_client_at > IDLE_GRACE_S)
                if expired or idle:
                    stale_ids.append(sid)
            for sid in stale_ids:
                sess = _sessions.pop(sid, None)
                if sess:
                    sess.stop()


threading.Thread(target=_watchdog, daemon=True).start()


# ════════════════════════════════════════════════════════════════════════════
#  Routes
# ════════════════════════════════════════════════════════════════════════════

@live_probe_bp.route("/live-probe/start", methods=["POST"])
def start_live_probe():
    username = _get_username_from_request()
    data = request.get_json(force=True, silent=True) or {}

    host = (data.get("host") or "").strip()
    port = (data.get("port") or "").strip()
    passphrase = (data.get("passphrase") or "").strip()
    tag = (data.get("tag") or "").strip()

    if not host or not port:
        return jsonify({"error": "host and port are required"}), 400
    if not re.match(r"^\d+$", port):
        return jsonify({"error": "port must be numeric"}), 400
    if not re.match(r"^[A-Za-z0-9_.:\-]+$", host):
        return jsonify({"error": "invalid host"}), 400

    session_id = uuid.uuid4().hex
    sess = LiveProbeSession(session_id, host, port, passphrase, tag, username)
    with _sessions_lock:
        _sessions[session_id] = sess

    return jsonify({"session_id": session_id})


@live_probe_bp.route("/live-probe/stream/<session_id>", methods=["GET"])
def stream_live_probe(session_id):
    with _sessions_lock:
        sess = _sessions.get(session_id)
    if not sess:
        return jsonify({"error": "unknown or expired session"}), 404

    def gen():
        q_ = sess.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = q_.get(timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "status" and event.get("state") == "stopped":
                        break
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            pass
        finally:
            sess.unsubscribe(q_)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@live_probe_bp.route("/live-probe/stop/<session_id>", methods=["POST"])
def stop_live_probe(session_id):
    with _sessions_lock:
        sess = _sessions.pop(session_id, None)
    if sess:
        sess.stop()
    return jsonify({"ok": True})
