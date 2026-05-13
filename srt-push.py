#!/usr/bin/env python3

import os
import signal
import subprocess
import sys
import time
import shutil

# ============================================================
# CONFIG
# ============================================================

HTML_URL = "https://10.11.203.239/id3as-DC-Monitor.html?view=nodes&dc=ix&sort=nW&dir=-1&inuse=1"

SRT_URL = "srt://10.11.203.1:3292?mode=caller&latency=1000&passphrase=rQ6zgFnfz1WgmJ0AgzI4Zs7Own54K0dU"

DISPLAY = ":99"
WIDTH = 1920
HEIGHT = 1080
FPS = 25

VIDEO_BITRATE = "3000k"

XVFB_PATH = "/usr/bin/Xvfb"
FFMPEG_PATH = "/usr/bin/ffmpeg"

CHROMIUM_PATH = (
    shutil.which("chromium-browser")
    or shutil.which("chromium")
    or "/usr/bin/chromium-browser"
)

processes = []

# ============================================================
# HELPERS
# ============================================================

def run(cmd, env=None):
    print("[RUN]", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=open("/var/log/srt-push.log", "a"),
        text=True,
        env=env
    )

def kill_existing():
    print("[INFO] cleaning old processes...")
    for p in ["Xvfb", "chromium", "chromium-browser", "ffmpeg"]:
        subprocess.run(["pkill", "-9", p],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

# ============================================================
# Xvfb
# ============================================================

def start_xvfb():
    print("[INFO] starting Xvfb...")
    p = run([
        XVFB_PATH,
        DISPLAY,
        "-screen", "0",
        f"{WIDTH}x{HEIGHT}x24"
    ])
    processes.append(p)
    time.sleep(2)

# ============================================================
# CHROMIUM
# ============================================================

def start_chromium():
    print("[INFO] starting chromium...")
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY

    p = run([
        CHROMIUM_PATH,
            "--window-position=0,0",
            "--window-size=1920,1080",
            "--kiosk",
            "--start-fullscreen",
            "--disable-infobars",
            "--noerrdialogs",
            "--disable-session-crashed-bubble",
            "--disable-features=TranslateUI",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-background-networking",
            "--disable-extensions",
            "--autoplay-policy=no-user-gesture-required",
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
            "--unsafely-treat-insecure-origin-as-secure=https://10.11.203.239",
        HTML_URL
    ], env=env)

    processes.append(p)
    time.sleep(10)

# ============================================================
# FFMPEG (SRT PUSH)
# ============================================================

def start_ffmpeg():
    print("[INFO] starting ffmpeg...")

    cmd = [
        FFMPEG_PATH,
        "-f", "x11grab",
        "-video_size", f"{WIDTH}x{HEIGHT}",
        "-framerate", str(FPS),
        "-i", f"{DISPLAY}+0,0",
        "-draw_mouse", "0",

        "-vf", "format=yuv420p",

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_BITRATE,
        "-bufsize", str(int(int(VIDEO_BITRATE.replace('k','')))*2) + "k",

        "-f", "mpegts",
        SRT_URL
    ]

    return run(cmd)

# ============================================================
# WATCHDOG (NUNCA PARA)
# ============================================================

def watchdog_loop():
    ffmpeg_proc = start_ffmpeg()

    while True:
        time.sleep(3)

        # Xvfb morreu -> reinicia tudo
        if processes[0].poll() is not None:
            print("[WATCHDOG] Xvfb died -> full restart...")
            kill_existing()
            time.sleep(1)
            start_xvfb()
            start_chromium()
            ffmpeg_proc = start_ffmpeg()
            continue

        # Chromium morreu -> reinicia chromium + ffmpeg
        if processes[1].poll() is not None:
            print("[WATCHDOG] Chromium died -> restarting chromium + ffmpeg...")
            try:
                ffmpeg_proc.kill()
            except:
                pass
            subprocess.run(["pkill", "-9", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "chromium-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.pop()  # remove chromium da lista
            time.sleep(2)
            start_chromium()
            ffmpeg_proc = start_ffmpeg()
            continue

        # FFmpeg morreu
        if ffmpeg_proc.poll() is not None:
            print("[WATCHDOG] FFmpeg died -> restarting...")
            time.sleep(2)
            ffmpeg_proc = start_ffmpeg()

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
    sys.exit(0)

# ============================================================
# MAIN
# ============================================================

def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    kill_existing()
    start_xvfb()
    start_chromium()

    # loop infinito - nunca deixa o SRT parar
    watchdog_loop()


if __name__ == "__main__":
    main()