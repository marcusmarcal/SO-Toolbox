"""
routes_live_probe.py — Live SRT Probe Blueprint
SO-Toolbox v2.34.0

Real-time post-SRT transport monitor for GOP Analyser tests whose source is
a live SRT stream (never shown for uploaded file tests). Reuses the same
host/port already stored on the test result. This is fully independent of
any external probe appliance — capture and analysis both happen inside
SO-Toolbox.

Why this cannot read the same as a raw-multicast probe (e.g. Bridge
Technologies watching the LAN multicast feed directly):
SRT itself performs packet-level ARQ (retransmission) and re-ordering
before delivering data to the receiving application. By the time
`srt-live-transmit` hands us bytes, SRT has already recovered most
transient network loss/jitter within its latency window — we are looking
at the multiplex *after* SRT's own error correction, not the raw network
path. A multicast probe watching the LAN feed directly sees the network
before any such correction. The two are legitimately different
measurement points, not the same signal disagreeing.

Why srt-live-transmit instead of ffmpeg:
ffmpeg's SRT input demuxes the transport stream into elementary streams and
then re-multiplexes it (even with `-c copy`), which regenerates MPEG-TS
continuity counters and can shuffle PIDs. That would hide genuine loss
remaining after SRT's own ARQ. `srt-live-transmit` (Haivision srt-tools)
does no such transformation — piping to `file://con` writes the exact bytes
delivered by the SRT socket to stdout, so continuity counters reflect
exactly what SRT handed off to the application layer.

What is actually measured (labelled precisely in the UI, not as generic
"IAT"/"MLR", since those already have a specific different meaning on a
multicast probe):
  PCR interval (ms) — wall-clock interval between successive arrivals of a
        PCR (Program Clock Reference) field on the stream's PCR_PID,
        discovered by parsing PAT -> PMT. MPEG-TS requires a PCR at least
        every 100ms, so a healthy post-SRT feed reads close to 100ms.
        PCR_INTERVAL_WARN_MS/CRIT_MS below classify each sample.
  TS CC loss (packets) — MPEG-TS continuity-counter discontinuities per PID,
        counted per 1-second window, on the stream as delivered by SRT
        (i.e. after SRT's own retransmission). Null packets (PID 0x1FFF)
        and adaptation-field-only packets (no payload, CC does not
        increment) are excluded. A single repeated CC is a legal retransmit
        duplicate, not a loss. On a healthy SRT session this should read 0
        even if the underlying network had real loss, precisely because
        SRT already recovered it — a nonzero value here means loss that
        exceeded SRT's own recovery window.
        v2.33.0 fix: packet-boundary tracking now carries any partial TS
        packet left at the end of a stdout read over to the next read
        instead of discarding it, which had been desyncing 188-byte
        alignment on almost every read and producing constant false loss.

Stall handling (v2.34.0):
  Reads are polled with a 1-second select() timeout instead of a plain
  blocking read, so a network stall is detected even if srt-live-transmit's
  process stays alive and simply stops delivering bytes (a plain blocking
  read would hang silently and the UI would just stop updating with no
  indication anything was wrong). Once STALL_TIMEOUT_S passes with no data,
  every 1-second window still emits a sample with bitrate_kbps=0 and
  state="stalled" instead of going quiet, so the chart keeps populating at
  zero and the readouts don't just freeze on the last good value. If the
  stall continues past HARD_STALL_S, the subprocess is killed and a fresh
  connection attempt is made.

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

import select
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

CHUNK_SIZE          = 4096         # small reads -> finer-grained PCR arrival timestamps
SELECT_POLL_S        = 1.0         # select() timeout per read poll
STALL_TIMEOUT_S     = 5            # no data for this long -> state="stalled"
HARD_STALL_S        = 15           # no data for this long -> kill & reconnect
STARTUP_GRACE_S     = 2.0          # suppress samples for this long after (re)connecting
PCR_INTERVAL_WARN_MS = 130.0       # orange warning threshold (matches broadcast probe convention)
PCR_INTERVAL_CRIT_MS = 150.0       # red critical threshold
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
#  MPEG-TS analyzer: continuity-counter loss + PCR inter-arrival time
# ════════════════════════════════════════════════════════════════════════════

class _TsAnalyzer:
    """Parses raw TS bytes across successive reads to produce two metrics:

    MLR — per-PID continuity-counter discontinuities (packets lost), summed
    across the whole multiplex. Null packets (PID 0x1FFF) and adaptation-
    field-only packets (no payload, CC does not increment) are excluded. A
    single repeated CC is a legal retransmit duplicate, not a loss.
    Note: CC is only 4 bits wide, so a burst loss of 16+ consecutive packets
    on the same PID is indistinguishable from zero loss — an inherent limit
    of CC-based detection, not a bug.

    IAT — wall-clock interval between successive PCR fields on the stream's
    PCR_PID (found by parsing PAT -> PMT), matching how broadcast probes
    define IAT/MLR (ETSI TR 101 290 "PCR repetition" check).

    Any partial 188-byte packet left at the end of a read is carried over
    to the next feed() call rather than discarded, since stdout reads
    rarely land on a clean packet boundary.
    """

    def __init__(self):
        self._last_cc = {}       # pid -> last continuity_counter seen (0-15)
        self._carry = b""        # partial TS packet left over between reads
        self._pmt_pid = None
        self._pcr_pid = None
        self._psi_buf = {}       # pid -> bytearray (partial PAT/PMT section)
        self._last_pcr_arrival = None  # wall time of last PCR field seen

    # -- PSI (PAT/PMT) parsing, just enough to find PCR_PID -----------------
    def _feed_psi(self, pid, pkt, pusi):
        afc = (pkt[3] >> 4) & 0x3
        if afc in (0x0, 0x2):
            return  # no payload in this packet
        off = 4
        if afc == 0x3:
            adapt_len = pkt[4]
            off = 5 + adapt_len
        payload = pkt[off:]
        if not payload:
            return

        if pusi:
            pointer = payload[0]
            payload = payload[1 + pointer:]
            self._psi_buf[pid] = bytearray(payload)
        else:
            if pid not in self._psi_buf:
                return  # haven't seen the section start yet
            self._psi_buf[pid].extend(payload)

        buf = self._psi_buf.get(pid)
        if not buf or len(buf) < 3:
            return
        section_length = ((buf[1] & 0x0F) << 8) | buf[2]
        total_len = 3 + section_length
        if len(buf) < total_len:
            return  # wait for the continuation packet

        section = bytes(buf[:total_len])
        del self._psi_buf[pid]

        if pid == 0:
            self._parse_pat(section)
        elif pid == self._pmt_pid:
            self._parse_pmt(section)

    def _parse_pat(self, section):
        pos = 8
        end = len(section) - 4  # exclude trailing CRC32
        while pos + 4 <= end:
            program_number = (section[pos] << 8) | section[pos + 1]
            pid = ((section[pos + 2] & 0x1F) << 8) | section[pos + 3]
            pos += 4
            if program_number != 0 and self._pmt_pid is None:
                self._pmt_pid = pid  # first service is enough for a single-program feed
                break

    def _parse_pmt(self, section):
        if len(section) < 12:
            return
        self._pcr_pid = ((section[8] & 0x1F) << 8) | section[9]

    # -- main entry -----------------------------------------------------
    def feed(self, buf: bytes, now: float):
        """Scan a chunk of raw TS bytes received at wall-clock time `now`.
        Returns (lost_packets, packets_seen, pcr_iat_samples_ms)."""
        data = self._carry + buf
        lost = 0
        seen = 0
        pcr_iat_samples = []
        i = 0
        n = len(data)

        while i + 188 <= n:
            if data[i] != 0x47:
                nxt = data.find(b'\x47', i + 1)
                if nxt == -1:
                    i = n
                    break
                i = nxt
                continue

            pkt = data[i:i + 188]
            seen += 1
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            pusi = bool(pkt[1] & 0x40)
            afc = (pkt[3] >> 4) & 0x3

            if pid != 0x1FFF and afc not in (0x0, 0x2):
                cc = pkt[3] & 0x0F
                prev = self._last_cc.get(pid)
                if prev is not None:
                    expected = (prev + 1) % 16
                    if cc != expected and cc != prev:
                        lost += (cc - expected) % 16
                self._last_cc[pid] = cc

            if pid == 0 or (self._pmt_pid is not None and pid == self._pmt_pid):
                self._feed_psi(pid, pkt, pusi)

            if self._pcr_pid is not None and pid == self._pcr_pid and afc in (0x2, 0x3):
                adapt_len = pkt[4]
                if adapt_len > 0 and (pkt[5] & 0x10):  # PCR_flag
                    if self._last_pcr_arrival is not None:
                        pcr_iat_samples.append((now - self._last_pcr_arrival) * 1000.0)
                    self._last_pcr_arrival = now

            i += 188

        self._carry = data[i:]
        return lost, seen, pcr_iat_samples


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
        analyzer = _TsAnalyzer()
        window_lost = 0
        window_bytes = 0
        window_iat = []
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
            conn_start_t = None       # set on first byte received this connection
            last_data_t = time.time()
            window_start = time.time()
            window_lost = 0
            window_bytes = 0
            window_iat = []
            reported_stalled = False

            try:
                while not self.stop_flag.is_set():
                    # Poll with a timeout instead of a plain blocking read, so a
                    # network stall is detected even while the subprocess stays
                    # alive and simply stops delivering bytes.
                    ready, _, _ = select.select([self.proc.stdout], [], [], SELECT_POLL_S)
                    now = time.time()

                    if ready:
                        buf = self.proc.stdout.read(CHUNK_SIZE)
                        if not buf:
                            break  # process ended / stream closed
                        last_data_t = now

                        if reported_stalled:
                            # Data resumed after a stall — start a clean window
                            # instead of mixing pre/post-stall bytes together.
                            reported_stalled = False
                            window_start = now
                            window_lost = 0
                            window_bytes = 0
                            window_iat = []

                        if not connected_once:
                            connected_once = True
                            conn_start_t = now
                            window_start = now
                            window_lost = 0
                            window_bytes = 0
                            window_iat = []

                        lost, _seen, pcr_iat = analyzer.feed(buf, now)
                        window_lost += lost
                        window_bytes += len(buf)
                        window_iat.extend(pcr_iat)

                    stalled_now = connected_once and (now - last_data_t) >= STALL_TIMEOUT_S
                    elapsed = now - window_start

                    if elapsed >= 1.0:
                        if stalled_now:
                            # Keep populating the chart at zero instead of
                            # just going quiet — a frozen UI looks identical
                            # to "everything is fine and unchanged".
                            self._emit({
                                "type": "sample",
                                "t": now,
                                "pcr_interval_avg_ms": None,
                                "pcr_interval_max_ms": None,
                                "ts_cc_loss": 0,
                                "bitrate_kbps": 0.0,
                                "state": "stalled",
                            })
                            reported_stalled = True
                        elif connected_once:
                            in_grace = (window_start - conn_start_t) < STARTUP_GRACE_S
                            if not in_grace:
                                kbps = (window_bytes * 8 / 1000.0) / elapsed if elapsed > 0 else 0.0
                                if window_iat:
                                    avg_iat = round(sum(window_iat) / len(window_iat), 1)
                                    max_iat = round(max(window_iat), 1)
                                else:
                                    # PCR_PID not discovered yet (or no PCR seen
                                    # this window) — report loss/bitrate anyway,
                                    # but don't fabricate a PCR interval reading.
                                    avg_iat = None
                                    max_iat = None
                                self._emit({
                                    "type": "sample",
                                    "t": now,
                                    "pcr_interval_avg_ms": avg_iat,
                                    "pcr_interval_max_ms": max_iat,
                                    "ts_cc_loss": window_lost,
                                    "bitrate_kbps": round(kbps, 1),
                                    "state": "running",
                                })
                        window_lost = 0
                        window_bytes = 0
                        window_iat = []
                        window_start = now

                    if connected_once and (now - last_data_t) >= HARD_STALL_S:
                        break  # SRT itself never noticed — force a fresh attempt
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
