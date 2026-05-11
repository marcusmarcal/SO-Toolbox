#!/usr/bin/env python3
# ============================================================
# HTML -> Xvfb -> Chromium -> FFmpeg -> SRT PUSH
# ORACLE LINUX 9.7 - PRODUCTION SERVICE (AUTO RECONNECT SRT)
# ============================================================

import os
import signal
import subprocess
import sys
import time
import shutil

# ============================================================
# CONFIG
# ============================================================

HTML_URL = "https://10.11.203.239/id3as-DC-Monitor.html?sort=nW&dir=-1&view=nodes&inuse=1"
#HTML_URL = "https://www.statsperform.com/"
# ?view=nodes
# ?view=nodes&inuse=1
# ?view=channels&warn=1
# ?view=running&dc=eq

SRT_URL = "srt://10.11.203.1:3292?mode=caller&passphrase=rQ6zgFnfz1WgmJ0AgzI4Zs7Own54K0dU&latency=1000"


BRIGHTNESS = 0.1      # -1.0 (dark) to +1.0 (bright)
CONTRAST   = 1.0      # 1.0 = normal
SATURATION = 1.0      # 1.0 = normal
GAMMA      = 1.0      # optional fine tuning


DISPLAY = ":99"

WIDTH = 1920
HEIGHT = 1080
FPS = 25

VIDEO_BITRATE = "3000k"
VIDEO_CODEC = "libx264"

XVFB_PATH = "/usr/bin/Xvfb"
FFMPEG_PATH = "/usr/bin/ffmpeg"

CHROMIUM_PATH = (
    shutil.which("chromium-browser")
    or shutil.which("chromium")
    or "/usr/bin/chromium-browser"
)

vf_filter = (
    f"eq="
    f"brightness={BRIGHTNESS}:"
    f"contrast={CONTRAST}:"
    f"saturation={SATURATION}:"
    f"gamma={GAMMA}"
)

processes = []

# ============================================================
# CHROMIUM FLAGS
# ============================================================

CHROMIUM_FLAGS = [
    "--kiosk",
    "--start-fullscreen",

    "--no-sandbox",
    "--disable-setuid-sandbox",

    "--ignore-certificate-errors",
    "--allow-insecure-localhost",

    "--disable-gpu",
    "--disable-infobars",
    "--autoplay-policy=no-user-gesture-required",
    "--noerrdialogs",
    "--disable-session-crashed-bubble",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-notifications",
    "--disable-extensions",

    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-frame-rate-limit",
]

# ============================================================
# HELPERS
# ============================================================

def run(cmd, env=None):
    print("\n[RUN]", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env
    )


def kill_existing():
    print("[INFO] cleaning old processes...")
    subprocess.run(["pkill", "-9", "Xvfb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "chromium-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["pkill", "-9", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ============================================================
# Xvfb
# ============================================================

def start_xvfb():
    print("[INFO] starting Xvfb...")

    proc = run([
        XVFB_PATH,
        DISPLAY,
        "-screen",
        "0",
        f"{WIDTH}x{HEIGHT}x24",
        "-nocursor"
    ])

    processes.append(proc)
    time.sleep(2)

# ============================================================
# CHROMIUM
# ============================================================

def start_chromium():
    print("[INFO] starting chromium...")

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    cmd = [
        CHROMIUM_PATH,
        f"--window-size={WIDTH},{HEIGHT}",
    ] + CHROMIUM_FLAGS + [HTML_URL]

    proc = run(cmd, env=env)

    processes.append(proc)
    time.sleep(6)

# ============================================================
# FFMPEG (SRT WITH AUTO RECONNECT)
# ============================================================

def start_ffmpeg():
    print("[INFO] starting ffmpeg (SRT auto-reconnect enabled)...")

    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    # IMPORTANT:
    # reconnect_streamed = 1 -> auto reconnect SRT
    srt_url = SRT_URL + "&reconnect_streamed=1&reconnect_delay_max=5"

    cmd = [
        FFMPEG_PATH,

        "-thread_queue_size", "512",

        "-f", "x11grab",
        "-video_size", f"{WIDTH}x{HEIGHT}",
        "-framerate", str(FPS),
        "-i", DISPLAY,

         # 🔥 BRIGHTNESS FIX
        "-vf", vf_filter,

        "-c:v", VIDEO_CODEC,
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",

        "-g", str(FPS * 2),

        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE,
        "-bufsize", "6M",

        "-f", "mpegts",
        srt_url
    ]

    proc = run(cmd, env=env)
    processes.append(proc)

# ============================================================
# SRT WATCHDOG (RECONNECT LOGIC)
# ============================================================

def restart_srt():
    print("[SRT] reconnecting stream...")
    subprocess.run(["pkill", "-9", "ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    start_ffmpeg()

# ============================================================
# CLEANUP
# ============================================================

def cleanup(sig=None, frame=None):
    print("\n[INFO] shutting down...")

    for p in processes:
        try:
            p.kill()
        except:
            pass

    kill_existing()
    sys.exit(0)

# ============================================================
# MAIN LOOP (WATCHDOG)
# ============================================================

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    kill_existing()

    start_xvfb()
    start_chromium()
    start_ffmpeg()

    print("\n[OK] SRT PUSH SERVICE STARTED (AUTO RECONNECT ENABLED)")
    print("[INFO] running...\n")

    ffmpeg_fail_counter = 0

    while True:
        time.sleep(5)

        for p in processes:
            if p.poll() is not None:
                print("[ERROR] process died")

                # only restart ffmpeg if it fails
                if ffmpeg_fail_counter < 5:
                    ffmpeg_fail_counter += 1
                    restart_srt()
                else:
                    print("[FATAL] too many failures -> restarting ffmpeg cleanly")

                    cleanup()
                    time.sleep(2)

                    ffmpeg_fail_counter = 0

                    start_xvfb()
                    start_chromium()
                    start_ffmpeg()

if __name__ == "__main__":
    main()