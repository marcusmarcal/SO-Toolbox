"""
SRT Ingest Router Blueprint
Handles SRT ingest routes: single destination and multi-destination (port range).
Streams ffmpeg stderr stats (bitrate, speed, time) via SSE per job.

Jobs are persistent: unless the user explicitly stops a job, the reader
thread keeps relaunching ffmpeg after a short delay whenever the process
exits (connection refused, network drop, etc). Each job can also be
stopped/restarted individually, and error output from the last failed
attempt is kept so it can be inspected from the Bitrate Monitor.

Also handles SRT Push Control routes: monitoring and controlling the
srt-push systemd service (Xvfb + Chromium + ffmpeg screen-to-SRT pipeline).
"""

import re
import subprocess
import threading
import os
import signal
import time
import json
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Generator
from flask import Blueprint, request, jsonify, render_template, Response, stream_with_context, send_file

srt_bp = Blueprint("srt_bp", __name__, url_prefix="/srt")

# Track running ffmpeg processes: { job_id: { process, config, stats_buf, ... } }
_running_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_counter = 0

# CBR defaults (Mbps) — overridable per request
CBR_DEFAULT_MBPS = 8.0
CBR_BUFSIZE_FACTOR = 2  # bufsize = bitrate * factor
TS_SOURCE_DIR = "/opt/web/store/gop-results"

# Delay before an unattended job auto-reconnects after ffmpeg exits.
RETRY_DELAY_SECONDS = 3

# Job statuses:
#   starting     - initial launch in progress
#   running      - ffmpeg is up and streaming
#   reconnecting - ffmpeg exited unexpectedly, waiting to relaunch automatically
#   stopping     - SIGTERM sent, waiting for ffmpeg to exit
#   stopped      - user explicitly stopped the job (no auto-retry)
#   error        - ffmpeg could not even be launched (e.g. missing binary/file)


def _next_job_id() -> int:
    global _job_counter
    _job_counter += 1
    return _job_counter


def _build_ffmpeg_cmd(
    input_file: str,
    host: str,
    port: int,
    passphrase: str,
    bitrate_mbps: float = CBR_DEFAULT_MBPS,
    passthrough: bool = False,
) -> list:
    """Build ffmpeg command for a single SRT destination.

    passthrough=True: copy streams without re-encoding (for .ts sources).
    passthrough=False: full CBR transcode with libx264/aac.
    """
    srt_url = f"srt://{host}:{port}?passphrase={passphrase}" if passphrase else f"srt://{host}:{port}"

    if passthrough:
        return [
            "ffmpeg", "-stream_loop", "-1",
            "-fflags", "+genpts+discardcorrupt",
            "-re",
            "-i", input_file,
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-c:v", "copy",
            "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            "-f", "mpegts",
            "-muxdelay", "0",
            "-muxpreload", "0",
            srt_url,
        ]

    vbr = f"{bitrate_mbps}M"
    bufsize = f"{bitrate_mbps * CBR_BUFSIZE_FACTOR}M"
    return [
        "ffmpeg", "-stream_loop", "-1", "-re",
        "-i", input_file,
        "-map", "0:v:0",
        "-map", "0:a:0",
        # Video — strict CBR
        "-c:v", "libx264",
        "-x264-params", "force-cfr=1:pic-struct=1",
        "-bf", "0",
        "-flags", "+cgop",
        "-r", "25",
        "-g", "50",
        "-keyint_min", "50",
        "-sc_threshold", "0",
        "-b:v", vbr,
        "-minrate", vbr,
        "-maxrate", vbr,
        "-bufsize", bufsize,
        # Audio
        "-c:a", "aac",
        "-ar", "48000",
        "-ac", "2",
        # Scale + container
        "-vf", "scale=1920:1080",
        "-f", "mpegts",
        "-muxdelay", "0",
        "-muxpreload", "0",
        srt_url,
    ]


def _build_ffmpeg_cmd_shared(input_file: str, destinations: list, passphrase: str) -> list:
    """Build a SINGLE ffmpeg command that reads input_file once and pushes an
    unmodified copy (-c copy) to every destination in `destinations`.

    This is the fix for CPU spiking to 100% when fanning out to many SRT
    targets: the previous approach spawned one whole ffmpeg process (one
    decode, and for transcode mode one libx264 encode) PER destination. With
    10 destinations in transcode mode that is 10 simultaneous libx264
    encodes of the same source. Here there is one demux and zero re-encode
    (stream copy), shared across all destinations, in a single process.

    Passthrough/copy only — sharing a single re-encode across destinations
    would need the ffmpeg 'tee' muxer, which is not implemented here.
    """
    cmd = ["ffmpeg", "-stream_loop", "-1", "-fflags", "+genpts+discardcorrupt", "-re", "-i", input_file]
    for dest in destinations:
        srt_url = (
            f"srt://{dest['host']}:{dest['port']}?passphrase={passphrase}"
            if passphrase else f"srt://{dest['host']}:{dest['port']}"
        )
        cmd += [
            "-map", "0:v:0",
            "-map", "0:a?",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-f", "mpegts",
            "-muxdelay", "0",
            "-muxpreload", "0",
            srt_url,
        ]
    return cmd


# Regex to parse ffmpeg progress lines:
# frame=  120 fps= 25 q=28.0 size=    1536kB time=00:00:04.80 bitrate=2621.4kbits/s speed=   1x
_FFMPEG_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?size=\s*([\d.]+\w+).*?"
    r"time=([\d:.]+).*?bitrate=\s*([\d.]+\w+/s).*?speed=\s*([\d.]+)x",
    re.S,
)


def _parse_ffmpeg_line(line: str) -> Optional[dict]:
    """Extract stats from a ffmpeg progress stderr line."""
    m = _FFMPEG_RE.search(line)
    if not m:
        return None
    return {
        "frame": int(m.group(1)),
        "fps": float(m.group(2)),
        "size": m.group(3),
        "time": m.group(4),
        "bitrate": m.group(5),
        "speed": m.group(6),
        "utc": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + " UTC",
        "ts": time.time(),
    }


def _launch_process(job: dict) -> None:
    """Start (or relaunch) the ffmpeg subprocess for an existing job dict,
    using its stored configuration. Mutates the job dict in place.
    Raises OSError if the process cannot be spawned at all.
    """
    if job.get("type") == "shared":
        cmd = _build_ffmpeg_cmd_shared(job["input_file"], job["destinations"], job["passphrase"])
    else:
        cmd = _build_ffmpeg_cmd(
            job["input_file"], job["host"], job["port"], job["passphrase"],
            job["bitrate_mbps"], job["passthrough"],
        )
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    job["process"] = process
    job["pid"] = process.pid
    job["cmd"] = " ".join(cmd)
    job["status"] = "running"
    job["stats_buf"].clear()
    job["last_stat"] = None


def _job_reader_thread(job_id: int) -> None:
    """Owns the full lifecycle of a job's ffmpeg process(es).

    Reads stderr into stats_buf/error_log while the process is alive. When
    the process exits, it auto-relaunches after RETRY_DELAY_SECONDS unless
    the job has been explicitly stopped (stop_requested). This is what keeps
    a job trying to connect indefinitely until the user stops it.
    """
    while True:
        with _jobs_lock:
            job = _running_jobs.get(job_id)
            proc = job["process"] if job else None

        if job is None:
            return

        if proc is None:
            # No live process (previous launch attempt failed) — wait, then retry.
            time.sleep(RETRY_DELAY_SECONDS)
            with _jobs_lock:
                job = _running_jobs.get(job_id)
                if job is None or job.get("stop_requested"):
                    return
                try:
                    _launch_process(job)
                except OSError as e:
                    job["status"] = "error"
                    job["last_error"] = f"Failed to launch ffmpeg: {e}"
                    job["retry_count"] = job.get("retry_count", 0) + 1
            continue

        buf = ""
        for chunk in iter(lambda: proc.stderr.read(256), ""):
            buf += chunk
            while "\r" in buf or "\n" in buf:
                sep = "\r" if "\r" in buf else "\n"
                line, buf = buf.split(sep, 1)
                stat = _parse_ffmpeg_line(line)
                with _jobs_lock:
                    j = _running_jobs.get(job_id)
                    if j is None:
                        return
                    if stat:
                        j["stats_buf"].append(stat)
                        j["last_stat"] = stat
                    elif line.strip():
                        j["error_log"].append(line.strip())
        proc.wait()

        with _jobs_lock:
            job = _running_jobs.get(job_id)
            if job is None:
                return
            if job.get("stop_requested"):
                job["status"] = "stopped"
                job["process"] = None
                return
            job["last_error"] = (
                job["error_log"][-1] if job["error_log"]
                else f"ffmpeg exited with code {proc.returncode}"
            )
            job["status"] = "reconnecting"
            job["retry_count"] = job.get("retry_count", 0) + 1
            job["process"] = None

        time.sleep(RETRY_DELAY_SECONDS)


def _launch_job(
    input_file: str,
    host: str,
    port: int,
    passphrase: str,
    bitrate_mbps: float = CBR_DEFAULT_MBPS,
    passthrough: bool = False,
) -> dict:
    """Create a job record and launch its ffmpeg process for the first time."""
    job_id = _next_job_id()

    job = {
        "id": job_id,
        "host": host,
        "port": port,
        "passphrase": passphrase,
        "input_file": input_file,
        "bitrate_mbps": bitrate_mbps,
        "passthrough": passthrough,
        "mode": "passthrough" if passthrough else "transcode",
        "status": "starting",
        "process": None,
        "pid": None,
        "cmd": "",
        # Ring buffer: last 300 stat samples (~5 min at 1 sample/s)
        "stats_buf": deque(maxlen=300),
        "last_stat": None,
        # Ring buffer of raw non-progress stderr lines, for diagnostics.
        "error_log": deque(maxlen=40),
        "last_error": None,
        "stop_requested": False,
        "retry_count": 0,
    }

    with _jobs_lock:
        _running_jobs[job_id] = job

    try:
        _launch_process(job)
    except OSError as e:
        job["status"] = "error"
        job["last_error"] = f"Failed to launch ffmpeg: {e}"

    threading.Thread(target=_job_reader_thread, args=(job_id,), daemon=True).start()
    return job


def _launch_shared_job(input_file: str, destinations: list, passphrase: str) -> dict:
    """Create and launch a SHARED job: one ffmpeg process, one decode, fanning
    out an unmodified copy to every destination in `destinations`. Reuses the
    same reader-thread/reconnect/stop/restart machinery as a single job.
    """
    job_id = _next_job_id()

    job = {
        "id": job_id,
        "type": "shared",
        "destinations": destinations,  # [{"host": ..., "port": ...}, ...]
        "host": None,
        "port": None,
        "passphrase": passphrase,
        "input_file": input_file,
        "bitrate_mbps": None,
        "passthrough": True,
        "mode": "passthrough-shared",
        "status": "starting",
        "process": None,
        "pid": None,
        "cmd": "",
        "stats_buf": deque(maxlen=300),
        "last_stat": None,
        "error_log": deque(maxlen=40),
        "last_error": None,
        "stop_requested": False,
        "retry_count": 0,
    }

    with _jobs_lock:
        _running_jobs[job_id] = job

    try:
        _launch_process(job)
    except OSError as e:
        job["status"] = "error"
        job["last_error"] = f"Failed to launch ffmpeg: {e}"

    threading.Thread(target=_job_reader_thread, args=(job_id,), daemon=True).start()
    return job


def _job_info(job: dict) -> dict:
    """Serialisable snapshot of a job (no subprocess object)."""
    info = {
        "id": job["id"],
        "type": job.get("type", "single"),
        "host": job["host"],
        "port": job["port"],
        "pid": job["pid"],
        "bitrate_mbps": job["bitrate_mbps"],
        "passthrough": job["passthrough"],
        "mode": job["mode"],
        "status": job["status"],
        "last_stat": job.get("last_stat"),
        "retry_count": job.get("retry_count", 0),
        "last_error": job.get("last_error"),
    }
    if job.get("type") == "shared":
        info["destinations"] = job["destinations"]
    return info


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@srt_bp.route("/")
def srt_tool():
    """Serve the SRT ingest HTML tool."""
    return render_template("srt_tool.html")


@srt_bp.route("/ingest/single", methods=["POST"])
def ingest_single():
    """
    Start a single SRT ingest.
    Body JSON: { host, port, passphrase, input_file?, bitrate_mbps? }
    The job keeps retrying to connect automatically until stopped.
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port = int(data.get("port", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()
    bitrate_mbps = float(data.get("bitrate_mbps", CBR_DEFAULT_MBPS))
    passthrough = bool(data.get("passthrough", False))

    if not host or not port:
        return jsonify({"error": "host and port are required"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    job = _launch_job(input_file, host, port, passphrase, bitrate_mbps, passthrough)
    return jsonify({"message": "Ingest started", "job": _job_info(job)}), 201


@srt_bp.route("/ingest/multi", methods=["POST"])
def ingest_multi():
    """
    Start ingest to multiple SRT destinations (port range).
    Body JSON: { host, port_start, port_end, passphrase, input_file?, bitrate_mbps? }
    Each destination job keeps retrying to connect automatically until stopped.
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port_start = int(data.get("port_start", 0))
    port_end = int(data.get("port_end", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()
    bitrate_mbps = float(data.get("bitrate_mbps", CBR_DEFAULT_MBPS))
    passthrough = bool(data.get("passthrough", False))

    if not host or not port_start or not port_end:
        return jsonify({"error": "host, port_start and port_end are required"}), 400
    if port_start > port_end:
        return jsonify({"error": "port_start must be <= port_end"}), 400
    if (port_end - port_start) > 99:
        return jsonify({"error": "Port range limited to 100 destinations"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    jobs = []
    for port in range(port_start, port_end + 1):
        job = _launch_job(input_file, host, port, passphrase, bitrate_mbps, passthrough)
        jobs.append(_job_info(job))

    return jsonify({
        "message": f"Ingest started to {len(jobs)} destinations",
        "jobs": jobs,
    }), 201


@srt_bp.route("/ingest/multi-shared", methods=["POST"])
def ingest_multi_shared():
    """
    Start ONE ffmpeg process (passthrough / -c copy only) that reads the
    input file a single time and fans it out, unmodified, to every
    destination in the port range. Use this instead of /ingest/multi when
    pushing to many destinations at once — it avoids one decode (and, in
    transcode mode, one libx264 encode) per destination, which is what
    causes CPU to spike with large fan-outs.
    Body JSON: { host, port_start, port_end, passphrase, input_file? }
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port_start = int(data.get("port_start", 0))
    port_end = int(data.get("port_end", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()

    if not host or not port_start or not port_end:
        return jsonify({"error": "host, port_start and port_end are required"}), 400
    if port_start > port_end:
        return jsonify({"error": "port_start must be <= port_end"}), 400
    if (port_end - port_start) > 99:
        return jsonify({"error": "Port range limited to 100 destinations"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    destinations = [{"host": host, "port": port} for port in range(port_start, port_end + 1)]
    job = _launch_shared_job(input_file, destinations, passphrase)
    return jsonify({
        "message": f"Shared ingest started to {len(destinations)} destinations (1 ffmpeg process)",
        "job": _job_info(job),
    }), 201


@srt_bp.route("/jobs", methods=["GET"])
def list_jobs():
    """List all known ingest jobs and their current status."""
    with _jobs_lock:
        jobs = [_job_info(j) for j in _running_jobs.values()]
    return jsonify({"jobs": jobs})


@srt_bp.route("/jobs/<int:job_id>", methods=["GET"])
def get_job(job_id: int):
    """Get status of a specific job."""
    with _jobs_lock:
        job = _running_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(_job_info(job))


@srt_bp.route("/jobs/<int:job_id>/stop", methods=["POST"])
def stop_job(job_id: int):
    """Stop a job for good (SIGTERM). Disables auto-reconnect for this job."""
    with _jobs_lock:
        job = _running_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        if job["status"] not in ("running", "reconnecting", "starting", "stopping"):
            return jsonify({"error": "Job is not running"}), 400

        job["stop_requested"] = True
        proc = job["process"]
        if proc is not None:
            try:
                os.kill(proc.pid, signal.SIGTERM)
                job["status"] = "stopping"
            except ProcessLookupError:
                job["status"] = "stopped"
        else:
            # Currently waiting between reconnect attempts — nothing to kill.
            job["status"] = "stopped"
        info = _job_info(job)

    return jsonify({"message": "Stop signal sent", "job": info})


@srt_bp.route("/jobs/<int:job_id>/restart", methods=["POST"])
def restart_job(job_id: int):
    """
    Restart a single job using its stored configuration (host, port,
    passphrase, bitrate, source file). Works whether the job is currently
    running (forces a fresh reconnect) or stopped/errored (relaunches it).
    """
    with _jobs_lock:
        job = _running_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        job["stop_requested"] = False
        job["retry_count"] = 0
        job["last_error"] = None
        proc = job["process"]
        currently_alive = proc is not None and job["status"] in (
            "running", "reconnecting", "starting", "stopping"
        )
        pid = proc.pid if proc is not None else None

    if currently_alive:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # The reader thread will pick up the exit and relaunch automatically
        # since stop_requested is now False (same path as auto-reconnect).
        with _jobs_lock:
            info = _job_info(_running_jobs[job_id])
        return jsonify({"message": "Restart requested, reconnecting shortly", "job": info})

    # Job was stopped/errored — its reader thread has already exited, so
    # relaunch here and start a fresh reader thread for it.
    with _jobs_lock:
        job = _running_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        try:
            _launch_process(job)
        except OSError as e:
            job["status"] = "error"
            job["last_error"] = f"Failed to launch ffmpeg: {e}"
            return jsonify({"error": job["last_error"]}), 500
        info = _job_info(job)

    threading.Thread(target=_job_reader_thread, args=(job_id,), daemon=True).start()
    return jsonify({"message": "Job restarted", "job": info}), 200


@srt_bp.route("/jobs/stop-all", methods=["POST"])
def stop_all_jobs():
    """Stop all active jobs (running/reconnecting/starting). Disables auto-reconnect for each."""
    stopped = []
    with _jobs_lock:
        for job in _running_jobs.values():
            if job["status"] in ("running", "reconnecting", "starting", "stopping"):
                job["stop_requested"] = True
                proc = job["process"]
                if proc is not None:
                    try:
                        os.kill(proc.pid, signal.SIGTERM)
                        job["status"] = "stopping"
                    except ProcessLookupError:
                        job["status"] = "stopped"
                else:
                    job["status"] = "stopped"
                stopped.append(job["id"])

    return jsonify({"message": f"Stopped {len(stopped)} jobs", "stopped_ids": stopped})


@srt_bp.route("/jobs/clear", methods=["POST"])
def clear_jobs():
    """
    Remove all finished jobs (stopped/error) from the list. Active jobs
    (running/reconnecting/starting/stopping) are left untouched — stop them
    first so their ffmpeg process is cleaned up properly.
    """
    removed = []
    with _jobs_lock:
        for jid, job in list(_running_jobs.items()):
            if job["status"] in ("stopped", "error"):
                del _running_jobs[jid]
                removed.append(jid)

    return jsonify({"message": f"Cleared {len(removed)} jobs", "removed_ids": removed})


@srt_bp.route("/sources", methods=["GET"])
def list_sources():
    sources = [{"file": "test.mp4", "type": "mp4"}]

    if os.path.isdir(TS_SOURCE_DIR):
        ts_files = sorted(
            [f for f in os.listdir(TS_SOURCE_DIR) if f.lower().endswith(".ts")],
            reverse=True  # mais recente primeiro (baseado no nome)
        )

        for f in ts_files:
            sources.append({
                "file": os.path.join(TS_SOURCE_DIR, f),
                "name": f,
                "type": "ts",
            })

    return jsonify({"sources": sources})


@srt_bp.route("/jobs/<int:job_id>/stats", methods=["GET"])
def job_stats_sse(job_id: int):
    """
    SSE stream: sends a JSON stat event every 0.5s for the given job, plus a
    "status" event whenever the job's status/last_error changes (so the
    client can show reconnect attempts and error details live).
    Clients connect once per job and receive live bitrate/fps/frame data.
    """
    with _jobs_lock:
        job = _running_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    @stream_with_context
    def _generate():
        last_idx = 0
        last_status = None
        last_error = None
        while True:
            with _jobs_lock:
                j = _running_jobs.get(job_id)
            if not j:
                yield "event: done\ndata: {}\n\n"
                break

            if j["status"] != last_status or j.get("last_error") != last_error:
                last_status = j["status"]
                last_error = j.get("last_error")
                yield "event: status\ndata: " + json.dumps({
                    "status": last_status,
                    "last_error": last_error,
                    "retry_count": j.get("retry_count", 0),
                }) + "\n\n"

            buf = list(j["stats_buf"])
            new = buf[last_idx:]
            last_idx = len(buf)

            if new:
                for stat in new:
                    yield f"data: {json.dumps(stat)}\n\n"

            if j["status"] in ("stopped", "error"):
                yield f"event: done\ndata: {json.dumps({'status': j['status']})}\n\n"
                break

            time.sleep(0.5)

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


# ===========================================================================
# SRT Push Control
# ===========================================================================
# Monitoring and control for the srt-push systemd service (Xvfb + Chromium +
# ffmpeg screen-to-SRT pipeline running srt-push.py on this same host).
#
# The service is not managed as a subprocess of Flask: it runs under systemd.
# Communication happens through three files that srt-push.py itself writes:
#   - srt-push-config.json  : desired runtime configuration (read by srt-push.py at start)
#   - srt-push-stats.json   : live ffmpeg progress stats (written by srt-push.py)
#   - srt-push-preview.jpg  : latest single-frame screenshot of the Xvfb display
#
# NOTE: PUSH_DEFAULT_CONFIG below must be kept in sync with DEFAULT_CONFIG in
# srt-push.py — it is only used here as a fallback when no config file exists yet.

PUSH_STORE_DIR = "/opt/web/store"
PUSH_CONFIG_FILE = os.path.join(PUSH_STORE_DIR, "srt-push-config.json")
PUSH_STATS_FILE = os.path.join(PUSH_STORE_DIR, "srt-push-stats.json")
PUSH_PREVIEW_FILE = os.path.join(PUSH_STORE_DIR, "srt-push-preview.jpg")
PUSH_LOG_FILE = "/var/log/srt-push.log"
PUSH_SERVICE_NAME = "srt-push"

PUSH_DEFAULT_CONFIG = {
    "html_url": "https://127.0.0.1/id3as-DC-Monitor.html?view=nodes&dc=ix&inuse=1&sort=nW&dir=-1",
    "srt_host": "10.11.203.1",
    "srt_port": 3292,
    "srt_mode": "caller",
    "srt_latency": 1000,
    "srt_passphrase": "rQ6zgFnfz1WgmJ0AgzI4Zs7Own54K0dU",
    "width": 1920,
    "height": 1080,
    "fps": 5,
    "video_bitrate_kbps": 500,
}

# Type casters used to validate incoming config values per field.
PUSH_CONFIG_FIELDS = {
    "html_url": str,
    "srt_host": str,
    "srt_port": int,
    "srt_mode": str,
    "srt_latency": int,
    "srt_passphrase": str,
    "width": int,
    "height": int,
    "fps": int,
    "video_bitrate_kbps": int,
}


def _load_push_config() -> dict:
    """Read the current srt-push config, falling back to defaults for missing keys."""
    cfg = dict(PUSH_DEFAULT_CONFIG)
    try:
        with open(PUSH_CONFIG_FILE, "r") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in PUSH_DEFAULT_CONFIG})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return cfg


def _save_push_config(cfg: dict) -> None:
    """Atomically persist the srt-push config file."""
    os.makedirs(PUSH_STORE_DIR, exist_ok=True)
    tmp_path = PUSH_CONFIG_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp_path, PUSH_CONFIG_FILE)


def _load_push_stats() -> dict:
    """Read the live stats file written by srt-push.py."""
    try:
        with open(PUSH_STATS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _systemctl(action: str, timeout: int = 15) -> dict:
    """Run a systemctl action for the srt-push unit via sudo and return the result.

    Requires a sudoers NOPASSWD rule for the Flask service account, e.g.:
    <flask_user> ALL=(root) NOPASSWD: /usr/bin/systemctl start srt-push, \
        /usr/bin/systemctl stop srt-push, /usr/bin/systemctl restart srt-push, \
        /usr/bin/systemctl show srt-push *
    """
    try:
        result = subprocess.run(
            ["sudo", "/usr/bin/systemctl", action, PUSH_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": "systemctl call timed out"}
    except OSError as e:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(e)}


def _push_service_state() -> dict:
    """Query systemd for the current state of the srt-push unit."""
    try:
        result = subprocess.run(
            ["sudo", "/usr/bin/systemctl", "show", PUSH_SERVICE_NAME,
             "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"active_state": "unknown", "sub_state": str(e), "main_pid": "0", "started_at": ""}

    state = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            state[k] = v

    return {
        "active_state": state.get("ActiveState", "unknown"),
        "sub_state": state.get("SubState", "unknown"),
        "main_pid": state.get("MainPID", "0"),
        "started_at": state.get("ExecMainStartTimestamp", ""),
    }


@srt_bp.route("/push")
def srt_push_tool():
    """Serve the SRT Push Control HTML tool."""
    return render_template("srt_push_control.html")


@srt_bp.route("/push/status", methods=["GET"])
def push_status():
    """Combined status: systemd state, live ffmpeg stats, current config."""
    return jsonify({
        "service": _push_service_state(),
        "stats": _load_push_stats(),
        "config": _load_push_config(),
    })


@srt_bp.route("/push/config", methods=["GET"])
def push_get_config():
    """Return the current srt-push configuration."""
    return jsonify(_load_push_config())


@srt_bp.route("/push/config", methods=["POST"])
def push_set_config():
    """
    Save a new srt-push configuration and restart the service in background to apply it.
    """
    data = request.get_json(force=True) or {}
    cfg = _load_push_config()

    for key, caster in PUSH_CONFIG_FIELDS.items():
        if key in data:
            try:
                cfg[key] = caster(data[key])
            except (TypeError, ValueError):
                return jsonify({"error": f"Invalid value for '{key}'"}), 400

    _save_push_config(cfg)

    # Em vez de esperar o systemctl síncrono, joga para o background
    try:
        subprocess.Popen(["bash", "-c", f"sleep 1 && systemctl restart {PUSH_SERVICE_NAME}"])
        return jsonify({
            "message": "Config saved and service restart scheduled.",
            "config": cfg
        }), 200
    except Exception as e:
        return jsonify({"error": f"Config saved but failed to schedule restart: {str(e)}"}), 500


@srt_bp.route("/push/service/<action>", methods=["POST"])
def push_service_action(action: str):
    """Control the srt-push systemd service in background. action: start | stop | restart."""
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action, use start/stop/restart"}), 400

    try:
        # Dispara a ação do systemctl em background para o Flask responder na hora
        subprocess.Popen(["bash", "-c", f"sleep 0.5 && systemctl {action} {PUSH_SERVICE_NAME}"])
        return jsonify({
            "ok": True,
            "message": f"Service {action} scheduled successfully."
        }), 200
    except Exception as e:
        return jsonify({
            "ok": False,
            "stderr": str(e)
        }), 500


@srt_bp.route("/push/preview.jpg", methods=["GET"])
def push_preview():
    """Serve the latest Xvfb screenshot captured by srt-push (single overwritten file)."""
    if not os.path.isfile(PUSH_PREVIEW_FILE):
        return jsonify({"error": "Preview not available yet"}), 404
    response = send_file(PUSH_PREVIEW_FILE, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@srt_bp.route("/push/log", methods=["GET"])
def push_log():
    """Return the last N lines of the srt-push log file (default 200)."""
    try:
        lines_count = int(request.args.get("lines", 200))
    except ValueError:
        lines_count = 200
    lines_count = max(1, min(lines_count, 2000))

    if not os.path.isfile(PUSH_LOG_FILE):
        return jsonify({"lines": []})

    try:
        with open(PUSH_LOG_FILE, "r", errors="replace") as f:
            lines = deque(f, maxlen=lines_count)
        return jsonify({"lines": [line.rstrip("\n") for line in lines]})
    except OSError as e:
        return jsonify({"error": str(e)}), 500
