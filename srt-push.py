#!/usr/bin/env python3

import os
import re
import signal
import subprocess
import sys
import time
import shutil
import json
import threading
from datetime import datetime, timezone

# ============================================================
# CONFIG
# ============================================================
# Runtime configuration is loaded from CONFIG_FILE (written by the
# SO-Toolbox web UI). The file holds a LIST of services under the
# "services" key; each service is an independent source (an HTML page
# captured through Xvfb + Chromium, or a static JPEG/PNG image) pushed
# to its own SRT destination. Any key missing from a service falls back
# to DEFAULT_SERVICE below. Config changes only take effect on the next
# process start (the web UI restarts the service via systemctl after
# saving).
#
# Legacy support: if CONFIG_FILE still contains the old flat single-service
# object (no "services" key), it is automatically treated as a single
# service on load, so existing deployments keep working unmodified.

STORE_DIR = "/opt/web/store"
CONFIG_FILE = os.path.join(STORE_DIR, "srt-push-config.json")
STATS_FILE = os.path.join(STORE_DIR, "srt-push-stats.json")
LOG_DIR = "/var/log/srt-push"
LOG_MAX_BYTES = 100 * 1024 * 1024  # rotate once a log crosses this size
LOG_RETENTION_DAYS = 7  # rotated backups older than this are deleted
BASE_DISPLAY_NUM = 99  # first Xvfb display; each html-source service gets its own, incrementing from here

DEFAULT_SERVICE = {
    "id": "srv1",
    "name": "Service 1",
    "enabled": True,
    # "html"  -> capture a web page via Xvfb + Chromium
    # "image" -> loop a static JPEG/PNG file
    "source_type": "html",
    "html_url": "https://127.0.0.1/id3as-DC-Monitor.html?view=nodes&dc=ix&inuse=1&sort=nW&dir=-1",
    "image_path": "",
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


def _normalize_service(raw: dict, index: int) -> dict:
    """Merge a raw service dict from the config file over DEFAULT_SERVICE."""
    svc = dict(DEFAULT_SERVICE)
    svc.update({k: v for k, v in raw.items() if k in DEFAULT_SERVICE})
    if not svc.get("id"):
        svc["id"] = f"srv{index + 1}"
    return svc


def load_config() -> list:
    """Load the service list from CONFIG_FILE, falling back to a single default service."""
    try:
        with open(CONFIG_FILE, "r") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        print(f"[CONFIG] Using defaults ({e})")
        return [dict(DEFAULT_SERVICE)]

    if isinstance(raw, dict) and "services" in raw:
        services_raw = raw["services"]
    elif isinstance(raw, dict):
        # Legacy flat config written by older web UI versions: one service.
        services_raw = [raw]
    elif isinstance(raw, list):
        services_raw = raw
    else:
        services_raw = []

    services = [_normalize_service(s, i) for i, s in enumerate(services_raw) if isinstance(s, dict)]
    services = [s for s in services if s.get("enabled", True)]

    if not services:
        print("[CONFIG] No enabled services found in config, falling back to default")
        services = [dict(DEFAULT_SERVICE)]

    # Guarantee unique ids (used for displays, log files, preview files, stats keys).
    seen = set()
    for i, s in enumerate(services):
        if s["id"] in seen:
            s["id"] = f"{s['id']}-{i + 1}"
        seen.add(s["id"])

    return services


os.makedirs(STORE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
SERVICE_CONFIGS = load_config()


def _resolve_binary(names, fallback):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return fallback


XVFB_PATH = _resolve_binary(["Xvfb"], "/usr/bin/Xvfb")
FFMPEG_PATH = _resolve_binary(["ffmpeg"], "/usr/bin/ffmpeg")
CHROMIUM_PATH = _resolve_binary(
    ["chromium-browser", "chromium", "google-chrome-stable", "google-chrome"],
    "/usr/bin/chromium-browser",
)


def _is_executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def build_srt_url(cfg: dict) -> str:
    return (
        f"srt://{cfg['srt_host']}:{cfg['srt_port']}"
        f"?mode={cfg['srt_mode']}&latency={cfg['srt_latency']}"
        f"&passphrase={cfg['srt_passphrase']}"
    )


# ============================================================
# LIVE STATS (consumed by the SO-Toolbox web UI)
# ============================================================
# STATS_FILE now holds one entry per service under "services", keyed by
# service id, plus a top-level "legacy" mirror of the first service using
# the old flat field names, so a monitor page that hasn't been updated yet
# keeps showing something sensible for the primary service.

_FFMPEG_RE = re.compile(
    r"frame=\s*(\d+).*?fps=\s*([\d.]+).*?size=\s*([\d.]+\w+).*?"
    r"time=([\d:.]+).*?bitrate=\s*([\d.]+\w+/s).*?speed=\s*([\d.]+)x",
    re.S,
)

_stats_lock = threading.Lock()
_all_stats = {}  # service_id -> stats dict


def _write_all_stats() -> None:
    """Atomically write the combined stats snapshot for every service to STATS_FILE."""
    tmp_path = STATS_FILE + ".tmp"
    with _stats_lock:
        services_snapshot = {sid: dict(s) for sid, s in _all_stats.items()}
    legacy = next(iter(services_snapshot.values()), None)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "services": services_snapshot,
        "legacy": legacy,  # backward-compat for monitor pages built for the single-service format
    }
    try:
        with open(tmp_path, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, STATS_FILE)
    except OSError as e:
        print(f"[STATS] Failed to write stats file: {e}")


# ============================================================
# HELPERS
# ============================================================

def run(cmd, log_path, env=None):
    print("[RUN]", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=open(log_path, "a"),
        text=True,
        env=env,
    )


def kill_existing():
    print("[INFO] cleaning old processes...")
    for p in ["Xvfb", "chromium", "chromium-browser", "ffmpeg"]:
        subprocess.run(["pkill", "-9", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ============================================================
# LOG ROTATION
# ============================================================
# Xvfb/Chromium/ffmpeg hold their log file open in append mode for their
# whole lifetime, so a simple rename-based rotation would leave them
# writing to the old (renamed) inode forever. Instead we copy the current
# content to a timestamped backup and truncate the same file in place
# (copytruncate), which is safe for processes that already have it open.
# Every service has its own log file under LOG_DIR, rotated independently.

def _rotate_log_if_needed(log_path: str) -> None:
    try:
        if not os.path.isfile(log_path):
            return
        if os.path.getsize(log_path) < LOG_MAX_BYTES:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = f"{log_path}.{stamp}"
        shutil.copyfile(log_path, backup_path)
        with open(log_path, "r+") as f:
            f.truncate(0)
        print(f"[LOG] Rotated {log_path} -> {backup_path}")
    except OSError as e:
        print(f"[LOG] Rotation failed for {log_path}: {e}")


def _prune_old_logs(log_path: str) -> None:
    log_dir = os.path.dirname(log_path) or "."
    base_name = os.path.basename(log_path)
    cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
    try:
        for entry in os.listdir(log_dir):
            if not entry.startswith(base_name + "."):
                continue
            entry_path = os.path.join(log_dir, entry)
            try:
                if os.path.getmtime(entry_path) < cutoff:
                    os.remove(entry_path)
                    print(f"[LOG] Removed expired backup: {entry_path}")
            except OSError:
                continue
    except OSError as e:
        print(f"[LOG] Prune failed for {log_path}: {e}")


def _log_maintenance_loop(log_paths: list, interval: int = 3600) -> None:
    """Background loop: rotate oversized logs and prune expired backups for every service.

    Runs immediately on start (before the first sleep), so already oversized
    log files are rotated right away on service restart.
    """
    while True:
        for log_path in log_paths:
            _rotate_log_if_needed(log_path)
            _prune_old_logs(log_path)
        time.sleep(interval)

# ============================================================
# SERVICE
# ============================================================
# One Service instance = one independent source -> SRT destination pipeline.
# HTML sources get their own Xvfb display (":99", ":100", ...) and their own
# Chromium instance; image sources skip Xvfb/Chromium entirely and feed the
# static file straight into ffmpeg. Each service is watched by its own
# watchdog thread, so a crash in one service never affects the others.


class Service:
    def __init__(self, cfg: dict, display_num):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name") or cfg["id"]
        self.source_type = cfg.get("source_type", "html")
        self.html_url = cfg.get("html_url", "")
        self.image_path = cfg.get("image_path", "")

        self.width = int(cfg["width"])
        self.height = int(cfg["height"])
        self.fps = int(cfg["fps"])

        self.bitrate_kbps = int(cfg["video_bitrate_kbps"])
        self.bitrate = f"{self.bitrate_kbps}k"
        self.bufsize = f"{self.bitrate_kbps * 2}k"
        self.gop_size = self.fps * 5  # 1 IDR frame every 5 seconds

        # Transport-stream mux rate: kept strictly above the video bitrate so
        # ffmpeg pads the MPEG-TS with null (PID 0x1FFF) packets, making the
        # SRT push a true constant bitrate at the transport level.
        self.mux_bitrate_kbps = int(self.bitrate_kbps * 1.10)  # +10% margin for TS/PSI overhead
        self.mux_bitrate = f"{self.mux_bitrate_kbps}k"

        self.srt_url = build_srt_url(cfg)
        self.display = f":{display_num}" if self.source_type == "html" and display_num is not None else None

        self.log_file = os.path.join(LOG_DIR, f"{self.id}.log")
        self.preview_file = os.path.join(STORE_DIR, f"srt-push-preview-{self.id}.jpg")
        self.preview_tmp_file = os.path.join(STORE_DIR, f"srt-push-preview-{self.id}.tmp.jpg")

        self.xvfb_proc = None
        self.chromium_proc = None
        self.ffmpeg_proc = None

        with _stats_lock:
            _all_stats[self.id] = {
                "id": self.id,
                "name": self.name,
                "source_type": self.source_type,
                "service_status": "starting",
                "ffmpeg_pid": None,
                "started_at": None,
                "updated_at": None,
                "frame": None,
                "fps": None,
                "size": None,
                "time": None,
                "bitrate": None,
                "speed": None,
                "last_error": None,
            }

    # -------------------- stats --------------------

    def update_stats(self, **kwargs) -> None:
        with _stats_lock:
            _all_stats[self.id].update(kwargs)
            _all_stats[self.id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_all_stats()

    # -------------------- dependency check --------------------

    def can_start(self) -> bool:
        missing = []
        if self.source_type == "html":
            if not _is_executable(XVFB_PATH):
                missing.append(f"Xvfb not found/executable at '{XVFB_PATH}'")
            if not _is_executable(CHROMIUM_PATH):
                missing.append(f"Chromium not found/executable at '{CHROMIUM_PATH}'")
            if not self.html_url:
                missing.append("html_url is empty")
        elif self.source_type == "image":
            if not self.image_path or not os.path.isfile(self.image_path):
                missing.append(f"image_path '{self.image_path}' does not exist")
        else:
            missing.append(f"unknown source_type '{self.source_type}' (expected 'html' or 'image')")

        if not _is_executable(FFMPEG_PATH):
            missing.append(f"ffmpeg not found/executable at '{FFMPEG_PATH}'")

        if missing:
            reason = "; ".join(missing)
            print(f"[FATAL][{self.id}] Cannot start: {reason}")
            self.update_stats(service_status="error", last_error=reason)
            return False
        return True

    # -------------------- Xvfb --------------------

    def start_xvfb(self):
        print(f"[INFO][{self.id}] starting Xvfb on {self.display}...")
        self.xvfb_proc = run(
            [XVFB_PATH, self.display, "-screen", "0", f"{self.width}x{self.height}x24", "-nocursor"],
            self.log_file,
        )
        time.sleep(2)

    # -------------------- Chromium --------------------

    def start_chromium(self):
        print(f"[INFO][{self.id}] starting chromium...")
        env = os.environ.copy()
        env["DISPLAY"] = self.display

        self.chromium_proc = run(
            [
                CHROMIUM_PATH,
                "--incognito",
                "--window-position=0,0",
                f"--window-size={self.width},{self.height}",
                "--kiosk",
                "--start-fullscreen",
                "--disable-infobars",
                "--noerrdialogs",
                "--disable-session-crashed-bubble",
                "--disable-features=TranslateUI",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-background-networking",
                "--disable-extensions",
                "--autoplay-policy=no-user-gesture-required",
                "--ignore-certificate-errors",
                "--allow-insecure-localhost",
                "--touch-events=enabled",
                "--disable-gpu-vsync",
                "--disable-smooth-scrolling",
                "--disable-low-end-device-mode",
                "--blink-settings=imagesEnabled=true",
                "--unsafely-treat-insecure-origin-as-secure=https://127.0.0.1",
                self.html_url,
            ],
            self.log_file,
            env=env,
        )
        time.sleep(10)

    # -------------------- preview capture --------------------

    def _capture_preview_loop(self, interval: int = 5):
        """Periodically grab a frame from this service's virtual display for the web preview."""
        while True:
            try:
                subprocess.run(
                    [
                        FFMPEG_PATH, "-y",
                        "-f", "x11grab",
                        "-video_size", f"{self.width}x{self.height}",
                        "-i", f"{self.display}+0,0",
                        "-frames:v", "1",
                        self.preview_tmp_file,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                os.replace(self.preview_tmp_file, self.preview_file)
            except (subprocess.SubprocessError, OSError) as e:
                print(f"[PREVIEW][{self.id}] Capture failed: {e}")
            time.sleep(interval)

    def _refresh_image_preview_loop(self, interval: int = 30):
        """For image-source services, periodically re-copy the source file as the preview,
        in case the underlying static file gets replaced while the service is running."""
        while True:
            try:
                shutil.copyfile(self.image_path, self.preview_tmp_file)
                os.replace(self.preview_tmp_file, self.preview_file)
            except OSError as e:
                print(f"[PREVIEW][{self.id}] Image preview refresh failed: {e}")
            time.sleep(interval)

    def start_preview_loop(self):
        if self.source_type == "html":
            threading.Thread(target=self._capture_preview_loop, daemon=True).start()
        elif self.source_type == "image":
            threading.Thread(target=self._refresh_image_preview_loop, daemon=True).start()

    # -------------------- ffmpeg --------------------

    def _read_ffmpeg_stderr(self, proc: subprocess.Popen):
        """Read ffmpeg stderr, log raw lines, and parse progress into live stats."""
        log_f = open(self.log_file, "a")
        buf = ""
        try:
            for chunk in iter(lambda: proc.stderr.read(256), ""):
                buf += chunk
                while "\r" in buf or "\n" in buf:
                    sep = "\r" if "\r" in buf else "\n"
                    line, buf = buf.split(sep, 1)
                    if line.strip():
                        log_f.write(line + "\n")
                        log_f.flush()
                    m = _FFMPEG_RE.search(line)
                    if m:
                        self.update_stats(
                            service_status="running",
                            frame=int(m.group(1)),
                            fps=float(m.group(2)),
                            size=m.group(3),
                            time=m.group(4),
                            bitrate=m.group(5),
                            speed=m.group(6),
                        )
        finally:
            log_f.close()

    def start_ffmpeg(self):
        print(f"[INFO][{self.id}] starting ffmpeg...")

        if self.source_type == "html":
            input_args = [
                "-f", "x11grab",
                "-draw_mouse", "0",
                "-video_size", f"{self.width}x{self.height}",
                "-framerate", str(self.fps),
                "-i", f"{self.display}+0,0",
            ]
        else:  # "image"
            input_args = [
                "-loop", "1",
                "-framerate", str(self.fps),
                "-i", self.image_path,
            ]

        cmd = [
            FFMPEG_PATH,
            *input_args,

            "-vf", f"format=yuv420p,scale={self.width}:{self.height}",

            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",

            # --- Strict CBR ---
            "-b:v", self.bitrate,
            "-maxrate", self.bitrate,
            "-minrate", self.bitrate,
            "-bufsize", self.bufsize,
            "-nal-hrd", "cbr",

            "-g", str(self.gop_size),
            "-threads", "2",

            "-f", "mpegts",
            "-muxrate", self.mux_bitrate,
            self.srt_url,
        ]

        print(f"[RUN][{self.id}]", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        self.update_stats(
            service_status="running",
            ffmpeg_pid=proc.pid,
            started_at=datetime.now(timezone.utc).isoformat(),
            last_error=None,
        )

        threading.Thread(target=self._read_ffmpeg_stderr, args=(proc,), daemon=True).start()
        self.ffmpeg_proc = proc
        return proc

    # -------------------- lifecycle --------------------

    def start_all(self):
        if self.source_type == "html":
            self.start_xvfb()
            self.start_chromium()
        self.start_ffmpeg()
        self.start_preview_loop()
        threading.Thread(target=self.watchdog_loop, daemon=True).start()

    def watchdog_loop(self):
        """Never stops: restarts whichever piece of this service dies."""
        while True:
            time.sleep(3)

            if self.source_type == "html":
                # Xvfb died -> full restart of this service
                if self.xvfb_proc is not None and self.xvfb_proc.poll() is not None:
                    print(f"[WATCHDOG][{self.id}] Xvfb died -> full restart...")
                    self.update_stats(service_status="starting", ffmpeg_pid=None)
                    self._kill_own_processes()
                    time.sleep(1)
                    self.start_xvfb()
                    self.start_chromium()
                    self.start_ffmpeg()
                    continue

                # Chromium died -> restart chromium + ffmpeg
                if self.chromium_proc is not None and self.chromium_proc.poll() is not None:
                    print(f"[WATCHDOG][{self.id}] Chromium died -> restarting chromium + ffmpeg...")
                    self.update_stats(service_status="starting", ffmpeg_pid=None)
                    try:
                        self.ffmpeg_proc.kill()
                    except Exception:
                        pass
                    time.sleep(2)
                    self.start_chromium()
                    self.start_ffmpeg()
                    continue

            # ffmpeg died
            if self.ffmpeg_proc is not None and self.ffmpeg_proc.poll() is not None:
                print(f"[WATCHDOG][{self.id}] FFmpeg died -> restarting...")
                self.update_stats(service_status="error", ffmpeg_pid=None, last_error="ffmpeg process exited unexpectedly")
                time.sleep(2)
                self.start_ffmpeg()

    def _kill_own_processes(self):
        for p in (self.xvfb_proc, self.chromium_proc, self.ffmpeg_proc):
            try:
                if p is not None:
                    p.kill()
            except Exception:
                pass

    def stop(self):
        self.update_stats(
            service_status="stopped",
            ffmpeg_pid=None,
            frame=None,
            fps=None,
            size=None,
            time=None,
            bitrate=None,
            speed=None,
        )
        self._kill_own_processes()

# ============================================================
# CLEANUP
# ============================================================

SERVICES = []


def cleanup(sig=None, frame=None):
    print("\n[INFO] shutting down...")
    for svc in SERVICES:
        svc.stop()
    sys.exit(0)

# ============================================================
# MAIN
# ============================================================

def main():
    global SERVICES

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    kill_existing()

    display_counter = BASE_DISPLAY_NUM
    for cfg in SERVICE_CONFIGS:
        display_num = None
        if cfg.get("source_type", "html") == "html":
            display_num = display_counter
            display_counter += 1
        SERVICES.append(Service(cfg, display_num))

    startable = [svc for svc in SERVICES if svc.can_start()]
    if not startable:
        print("[FATAL] No service could start (see errors above). Exiting.")
        sys.exit(1)

    if len(startable) < len(SERVICES):
        skipped = [svc.id for svc in SERVICES if svc not in startable]
        print(f"[WARN] Skipping services with unmet dependencies: {', '.join(skipped)}")

    threading.Thread(
        target=_log_maintenance_loop,
        args=([svc.log_file for svc in startable],),
        daemon=True,
    ).start()

    for svc in startable:
        svc.start_all()
        time.sleep(1)  # stagger startup so Xvfb/Chromium/ffmpeg launches don't all spike CPU at once

    # main thread just stays alive; each service is supervised by its own watchdog thread
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
