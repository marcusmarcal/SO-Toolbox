"""
SRT Ingest Router Blueprint
Handles SRT ingest routes: single destination and multi-destination (port range).
"""

import subprocess
import threading
import os
import signal
from flask import Blueprint, request, jsonify, render_template

srt_bp = Blueprint("srt_bp", __name__, url_prefix="/srt")

# Track running ffmpeg processes: { job_id: { process, config } }
_running_jobs: dict = {}
_jobs_lock = threading.Lock()
_job_counter = 0


def _next_job_id() -> int:
    global _job_counter
    _job_counter += 1
    return _job_counter


def _build_ffmpeg_cmd(
    input_file: str,
    host: str,
    port: int,
    passphrase: str,
) -> list[str]:
    """Build the ffmpeg command for a single SRT destination."""
    srt_url = f"srt://{host}:{port}?passphrase={passphrase}"
    return [
        "ffmpeg", "-re",
        "-i", input_file,
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c:v", "libx264",
        "-x264-params", "force-cfr=1:pic-struct=1",
        "-r", "25",
        "-g", "50",
        "-keyint_min", "50",
        "-c:a", "aac",
        "-ar", "48000",
        "-ac", "2",
        "-vf", "scale=1920:1080",
        "-f", "mpegts",
        "-muxdelay", "0",
        "-muxpreload", "0",
        srt_url,
    ]


def _launch_job(input_file: str, host: str, port: int, passphrase: str) -> dict:
    """Launch a single ffmpeg process and register it."""
    job_id = _next_job_id()
    cmd = _build_ffmpeg_cmd(input_file, host, port, passphrase)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    job = {
        "id": job_id,
        "host": host,
        "port": port,
        "pid": process.pid,
        "status": "running",
        "process": process,
        "cmd": " ".join(cmd),
    }

    with _jobs_lock:
        _running_jobs[job_id] = job

    # Background thread to reap the process and update status
    def _watch(jid: int, proc: subprocess.Popen) -> None:
        proc.wait()
        with _jobs_lock:
            if jid in _running_jobs:
                _running_jobs[jid]["status"] = (
                    "finished" if proc.returncode == 0 else "error"
                )

    threading.Thread(target=_watch, args=(job_id, process), daemon=True).start()
    return job


def _job_info(job: dict) -> dict:
    """Serialisable snapshot of a job (no subprocess object)."""
    return {
        "id": job["id"],
        "host": job["host"],
        "port": job["port"],
        "pid": job["pid"],
        "status": job["status"],
    }


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
    Body JSON: { host, port, passphrase, input_file? }
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port = int(data.get("port", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()

    if not host or not port or not passphrase:
        return jsonify({"error": "host, port and passphrase are required"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    job = _launch_job(input_file, host, port, passphrase)
    return jsonify({"message": "Ingest started", "job": _job_info(job)}), 201


@srt_bp.route("/ingest/multi", methods=["POST"])
def ingest_multi():
    """
    Start ingest to multiple SRT destinations (port range).
    Body JSON: { host, port_start, port_end, passphrase, input_file? }
    """
    data = request.get_json(force=True)
    host = data.get("host", "").strip()
    port_start = int(data.get("port_start", 0))
    port_end = int(data.get("port_end", 0))
    passphrase = data.get("passphrase", "").strip()
    input_file = data.get("input_file", "test.mp4").strip()

    if not host or not port_start or not port_end or not passphrase:
        return jsonify({"error": "host, port_start, port_end and passphrase are required"}), 400
    if port_start > port_end:
        return jsonify({"error": "port_start must be <= port_end"}), 400
    if (port_end - port_start) > 99:
        return jsonify({"error": "Port range limited to 100 destinations"}), 400
    if not os.path.isfile(input_file):
        return jsonify({"error": f"Input file not found: {input_file}"}), 400

    jobs = []
    for port in range(port_start, port_end + 1):
        job = _launch_job(input_file, host, port, passphrase)
        jobs.append(_job_info(job))

    return jsonify({
        "message": f"Ingest started to {len(jobs)} destinations",
        "jobs": jobs,
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
    """Stop (SIGTERM) a running ingest job."""
    with _jobs_lock:
        job = _running_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "running":
        return jsonify({"error": "Job is not running"}), 400

    try:
        os.kill(job["pid"], signal.SIGTERM)
        job["status"] = "stopping"
    except ProcessLookupError:
        job["status"] = "finished"

    return jsonify({"message": "Stop signal sent", "job": _job_info(job)})


@srt_bp.route("/jobs/stop-all", methods=["POST"])
def stop_all_jobs():
    """Stop all running ingest jobs."""
    stopped = []
    with _jobs_lock:
        for job in _running_jobs.values():
            if job["status"] == "running":
                try:
                    os.kill(job["pid"], signal.SIGTERM)
                    job["status"] = "stopping"
                    stopped.append(job["id"])
                except ProcessLookupError:
                    job["status"] = "finished"

    return jsonify({"message": f"Stopped {len(stopped)} jobs", "stopped_ids": stopped})
