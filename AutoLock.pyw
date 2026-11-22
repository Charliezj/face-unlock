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
import ctypes
import threading
import traceback
import importlib.util
from ctypes import wintypes

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

WM_HOTKEY = 0x0312
HOTKEY_ID = 0xA71
HOTKEY_CANDIDATES = ("ctrl+alt+l", "ctrl+alt+shift+l", "win+alt+l", "ctrl+alt+shift+f10")
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
NAMED_LABEL = {
    "space": "Space", "enter": "Enter", "return": "Enter", "tab": "Tab",
    "esc": "Esc", "escape": "Esc", "backspace": "Backspace",
    "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right",
}


def parse_hotkey(text):
    """把 "ctrl+alt+l" 解析成 (修饰键掩码, 虚拟键码, 显示名)。"""
    parts = [p.strip().lower() for p in str(text).split("+") if p.strip()]
    if not parts:
        raise ValueError("快捷键是空的")
    mods = 0
    vk = None
    labels = []
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
            labels.append("Ctrl")
        elif p == "alt":
            mods |= MOD_ALT
            labels.append("Alt")
        elif p == "shift":
            mods |= MOD_SHIFT
            labels.append("Shift")
        elif p in ("win", "windows", "cmd", "meta"):
            mods |= MOD_WIN
            labels.append("Win")
        elif p in NAMED_KEYS:
            vk = NAMED_KEYS[p]
            labels.append(NAMED_LABEL[p])
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
            labels.append(p.upper())
        elif p[0] == "f" and p[1:].isdigit() and 1 <= int(p[1:]) <= 12:
            vk = 0x6F + int(p[1:])
            labels.append("F" + p[1:])
        else:
            raise ValueError("不认识的按键：%s" % p)
    if vk is None:
        raise ValueError("缺少主键，要写成 ctrl+alt+l 这样")
    return mods, vk, "+".join(labels)


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
    default = {"idle_sec": 45, "min_face_px": 100, "hotkey": "ctrl+alt+l"}
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


class Hotkey:
    """后台线程注册系统级热键，按下就把锁屏拉起来（走主线程队列）。"""

    def __init__(self, guard, text):
        self.guard = guard
        self.text = (text or "ctrl+alt+l").strip()
        self.state = "pending"
        self.error = ""

    def start(self):
        t = threading.Thread(target=self._run, name="hotkey", daemon=True)
        t.start()
        return t

    def _run(self):
        try:
            mods, vk, pretty = parse_hotkey(self.text)
        except Exception as e:
            self.state = "fail"
            self.error = str(e)
            log("hotkey config invalid: %s" % e)
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        tries = [(mods, vk, pretty)]
        for fb in HOTKEY_CANDIDATES:
            if fb == self.text.lower():
                continue
            try:
                fm, fk, fp = parse_hotkey(fb)
            except Exception:
                continue
            tries.append((fm, fk, fp))

        chosen = None
        for m, k, p in tries:
            if user32.RegisterHotKey(None, HOTKEY_ID, m | MOD_NOREPEAT, k):
                chosen = p
                break
            log("hotkey %s unavailable, err=%d" % (p, ctypes.get_last_error()))
        if chosen is None:
            self.state = "fail"
            self.error = "候选快捷键都被占用"
            log("hotkey: no candidate available")
            return
        self.text = chosen
        self.state = "ok"
        if chosen == pretty:
            log("hotkey registered: %s" % chosen)
        else:
            log("hotkey registered: %s（首选 %s 被占用，已回退）" % (chosen, pretty))

        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_HOTKEY:
                log("hotkey pressed")
                self.guard.q.put(self.guard.lock_now)


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

    hotkey = Hotkey(guard, conf.get("hotkey", "ctrl+alt+l"))
    if not selftest:
        hotkey.start()

    def on_quit(icon, item):
        guard.quit(icon)

    def lock_label(item=None):
        if hotkey.state == "ok":
            return "立即锁定（%s）" % hotkey.text
        if hotkey.state == "fail":
            return "立即锁定（快捷键不可用）"
        return "立即锁定"

    menu = pystray.Menu(
        pystray.MenuItem(guard.status_text, None, enabled=False),
        pystray.MenuItem(lock_label, guard.lock_now),
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
