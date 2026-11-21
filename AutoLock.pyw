# -*- coding: utf-8 -*-
"""
AutoLock —— 离座自动人脸锁

常驻托盘，摄像头低频检测画面里有没有人；连续 idle_sec 秒没人，就自动拉起
全屏人脸锁，回来刷脸解开。

配置文件：autolock.json
    idle_sec     多久没人算离座（默认 45 秒）
    min_face_px  脸宽多少像素算“人在”（默认 100，太小的脸视为走远）

用法：
    AutoLock.pyw            正常常驻
    AutoLock.pyw --selftest 自检 8 秒后自动退出（不锁屏）
"""

import os
import sys
import json
import time
import queue
import threading
import traceback
import importlib.util

import tkinter as tk
import cv2
import pystray
from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
CORE_PATH = os.path.join(BASE, "FaceUnlock.pyw")
CONF_PATH = os.path.join(BASE, "autolock.json")
LOG_PATH = os.path.join(BASE, "autolock.log")

ACCENT = (102, 191, 178, 255)
DARK = (11, 13, 16, 255)
_LOG_LOCK = threading.Lock()
_INSTANCE_HANDLE = None


def single_instance():
    global _INSTANCE_HANDLE
    try:
        import ctypes
        k = ctypes.windll.kernel32
        _INSTANCE_HANDLE = k.CreateMutexW(None, False, "FaceUnlock_AutoLock_Instance")
        return k.GetLastError() != 183
    except Exception:
        return True


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S  ") + str(msg) + "\n"
    try:
        with _LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def load_core():
    spec = importlib.util.spec_from_file_location("faceunlock_core", CORE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_conf():
    default = {"idle_sec": 45, "min_face_px": 100}
    try:
        with open(CONF_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in default:
            if k in data:
                default[k] = data[k]
    except Exception:
        pass
    try:
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return default


def make_image(color=ACCENT):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, 58, 58), radius=14, fill=color)
    d.rectangle((24, 28, 40, 48), fill=DARK)
    d.arc((18, 8, 46, 42), start=180, end=360, fill=DARK, width=7)
    return img


class Guard:
    def __init__(self, root, core, conf):
        self.root = root
        self.core = core
        self.conf = conf
        self.idle_sec = float(conf.get("idle_sec", 45))
        self.min_face = float(conf.get("min_face_px", 100))
        self.state = "watching"
        self.last_seen = time.time()
        self.cap = None
        self.stop = False
        self.q = queue.Queue()
        self.lock_top = None
        self.paused = False
        self.frame_count = 0

    # ---------- 主线程任务泵 ----------
    def pump(self):
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log("task failed: " + traceback.format_exc())
        if not self.stop:
            self.root.after(120, self.pump)

    # ---------- 检测线程 ----------
    def watch(self):
        log("watch thread started")
        while not self.stop:
            if self.state != "watching":
                if self.cap is not None:
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                    self.cap = None
                time.sleep(0.3)
                continue

            if self.cap is None:
                self.cap = self.core.open_camera()
                if self.cap is None:
                    log("camera unavailable, retry in 3s")
                    time.sleep(3)
                    continue

            try:
                ok, frame = self.cap.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                time.sleep(0.5)
                continue

            self.frame_count += 1
            try:
                faces = self.core.detect_faces(frame)
                face = self.core.pick_face(faces)
            except Exception:
                face = None

            if face is not None and float(face[2]) >= self.min_face:
                self.last_seen = time.time()

            idle = time.time() - self.last_seen
            if idle >= self.idle_sec:
                log("idle %.0fs, locking now" % idle)
                self.trigger()
                continue

            time.sleep(1.2)

        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
        log("watch thread stopped")

    def trigger(self):
        if self.state != "watching":
            return
        self.state = "locking"
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self.q.put(self.do_lock)

    # ---------- 主线程：拉起锁屏 ----------
    def do_lock(self):
        self.root.after(450, self.create_lock)

    def create_lock(self):
        try:
            self.lock_top = tk.Toplevel(self.root)
            self.lock_top.withdraw()
            self.lock_top.bind("<Destroy>", self.on_lock_closed)
            self.core.LockWindow(self.lock_top, self.meta, self.refs)
        except Exception:
            log("lock window failed: " + traceback.format_exc())
            self.state = "watching"
            self.last_seen = time.time()

    def on_lock_closed(self, event):
        if event.widget is not self.lock_top:
            return
        self.lock_top = None
        if self.state != "locking":
            return
        self.state = "watching"
        self.last_seen = time.time()
        log("unlocked, back to watching")

    # ---------- 对外操作 ----------
    def toggle_pause(self, icon=None, item=None):
        self.paused = not self.paused
        if self.paused:
            self.state = "paused"
            self.q.put(self.release_cap)
        else:
            self.state = "watching"
            self.last_seen = time.time()
        log("paused=%s" % self.paused)
        if icon is not None:
            icon.icon = make_image((120, 126, 132, 255) if self.paused else ACCENT)
            icon.update_menu()

    def release_cap(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def lock_now(self, icon=None, item=None):
        if self.state == "locking":
            return
        self.last_seen = time.time() - self.idle_sec
        self.trigger()

    def quit(self, icon):
        self.stop = True
        self.q.put(self.release_cap)
        try:
            icon.stop()
        except Exception:
            pass
        self.root.after(200, self.root.quit)

    def status_text(self, item=None):
        if self.state == "locking":
            return "状态：已锁定"
        if self.paused:
            return "状态：已暂停"
        left = max(0, int(self.idle_sec - (time.time() - self.last_seen)))
        return "状态：监测中（%d 秒后锁）" % left


def main():
    selftest = "--selftest" in sys.argv[1:]

    if not selftest:
        if not single_instance():
            log("another instance already running, exit")
            return

    core = None
    for attempt in range(4):
        try:
            core = load_core()
            break
        except Exception:
            log("core load failed (try %d): " % (attempt + 1) + traceback.format_exc())
            core = None
            time.sleep(15)
    if core is None:
        log("core load failed after retries, exit")
        return

    conf = load_conf()

    meta, refs = core.load_profile()
    if meta is None:
        import tkinter.messagebox as mb
        r0 = tk.Tk()
        r0.withdraw()
        mb.showwarning("还没录入人脸", "请先运行“录入人脸.bat”完成录入。")
        return

    root = tk.Tk()
    root.withdraw()

    guard = Guard(root, core, conf)
    guard.meta = meta
    guard.refs = refs

    def on_quit(icon, item):
        guard.quit(icon)

    menu = pystray.Menu(
        pystray.MenuItem(guard.status_text, None, enabled=False),
        pystray.MenuItem("立即锁定", guard.lock_now),
        pystray.MenuItem("暂停监测", guard.toggle_pause),
        pystray.MenuItem("退出", on_quit),
    )

    icon = pystray.Icon("AutoLock", make_image(), "人脸自动锁", menu)
    threading.Thread(target=icon.run, daemon=True).start()

    threading.Thread(target=guard.watch, daemon=True).start()
    guard.pump()

    def tick_menu():
        try:
            icon.update_menu()
        except Exception:
            pass
        if not guard.stop:
            root.after(1500, tick_menu)

    root.after(1500, tick_menu)

    log("autolock started, idle_sec=%.0f min_face=%.0f" % (guard.idle_sec, guard.min_face))

    if selftest:
        def stop_test():
            log("selftest done, frames=%d" % guard.frame_count)
            print("selftest ok, frames=%d" % guard.frame_count)
            try:
                icon.stop()
            except Exception:
                pass
            root.quit()

        root.after(8000, stop_test)

    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("fatal: " + traceback.format_exc())
        raise
