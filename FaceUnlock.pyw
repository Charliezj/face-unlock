# -*- coding: utf-8 -*-
"""
FaceUnlock —— 本地人脸锁（应用级）

用法：
    FaceUnlock.pyw --setup    录入人脸
    FaceUnlock.pyw            锁屏，识别通过即解锁

引擎：OpenCV YuNet（人脸检测）+ SFace（人脸特征比对），模型在 models/ 下。
定位：运行在已登录桌面上的“应用级”人脸锁，离座挡屏用。
     不是 Windows 登录认证，Win+L 真锁屏后由系统接管，需密码/指纹。
"""

import os
import sys
import json
import time
import ctypes
import shutil
import traceback

import cv2
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import messagebox

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE, "models")
DETECT_ONNX = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
RECOG_ONNX = os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx")
FEAT_PATH = os.path.join(BASE, "features.npy")
META_PATH = os.path.join(BASE, "meta.json")
LOG_PATH = os.path.join(BASE, "run.log")

SAMPLES = 42
DEFAULT_THR = 0.45
BG = "#0b0d10"
FG = "#e8ecef"
DIM = "#7d8790"
ACCENT = "#66bfb2"
WARN = "#e08a78"

_det = None
_rec = None
_resolved = None


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + str(msg) + "\n")
    except Exception:
        pass


# ---------------- 引擎 ----------------
def resolve_models():
    """OpenCV 的 dnn 读不了含中文的路径，先把模型复制到纯英文目录再加载。"""
    global _resolved
    if _resolved is not None:
        return _resolved

    if all(ord(c) < 128 for c in DETECT_ONNX):
        _resolved = (DETECT_ONNX, RECOG_ONNX)
        return _resolved

    cache = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "FaceUnlock", "models")
    try:
        os.makedirs(cache, exist_ok=True)
    except Exception:
        cache = os.path.join(os.path.expanduser("~"), ".faceunlock", "models")
        os.makedirs(cache, exist_ok=True)

    dst_det = os.path.join(cache, "yunet.onnx")
    dst_rec = os.path.join(cache, "sface.onnx")
    for src, dst in ((DETECT_ONNX, dst_det), (RECOG_ONNX, dst_rec)):
        if (not os.path.exists(dst)) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
            log("model cached: " + dst)
    _resolved = (dst_det, dst_rec)
    return _resolved


def get_detector():
    global _det
    if _det is None:
        det_p, _ = resolve_models()
        _det = cv2.FaceDetectorYN.create(det_p, "", (320, 320), 0.75, 0.3, 500)
    return _det


def get_recognizer():
    global _rec
    if _rec is None:
        _, rec_p = resolve_models()
        _rec = cv2.FaceRecognizerSF.create(rec_p, "")
    return _rec


def detect_faces(frame):
    det = get_detector()
    h, w = frame.shape[:2]
    det.setInputSize((int(w), int(h)))
    ok, faces = det.detect(frame)
    if not ok or faces is None:
        return []
    return faces


def pick_face(faces):
    if len(faces) == 0:
        return None
    return max(faces, key=lambda r: float(r[2]) * float(r[3]))


def extract_feature(frame, face):
    rec = get_recognizer()
    aligned = rec.alignCrop(frame, face)
    feat = rec.feature(aligned)
    return np.asarray(feat, dtype=np.float32).reshape(1, -1)


def cosine_max(feat, refs):
    rec = get_recognizer()
    best = -1.0
    for r in refs:
        s = float(rec.match(feat, r.reshape(1, -1)))
        if s > best:
            best = s
    return best


def open_camera():
    for idx, backend in [(0, cv2.CAP_DSHOW), (0, cv2.CAP_MSMF), (1, cv2.CAP_DSHOW), (0, 0)]:
        try:
            cap = cv2.VideoCapture(idx, backend)
        except Exception:
            continue
        if cap is not None and cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            ok, frame = cap.read()
            if ok and frame is not None:
                log("camera ok idx=%s backend=%s %dx%d" % (idx, backend, frame.shape[1], frame.shape[0]))
                return cap
            cap.release()
    return None


def models_ready():
    return os.path.exists(DETECT_ONNX) and os.path.exists(RECOG_ONNX)


# ---------------- 屏幕 ----------------
def list_monitors():
    monitors = []
    try:
        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(RECT), ctypes.c_longlong)

        def cb(hmon, hdc, lprc, data):
            r = lprc.contents
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        user32.EnumDisplayMonitors(0, 0, proc_type(cb), 0)
    except Exception:
        log("enum monitors failed: " + traceback.format_exc())
    if not monitors:
        monitors = [(0, 0, 1920, 1080)]
    return monitors


def make_fullscreen(win, x, y, w, h):
    win.overrideredirect(True)
    win.geometry("%dx%d+%d+%d" % (w, h, x, y))
    win.attributes("-topmost", True)
    win.protocol("WM_DELETE_WINDOW", lambda: None)


def fit_frame(frame, max_w, max_h):
    h, w = frame.shape[:2]
    scale = min(max_w / float(w), max_h / float(h), 1.0)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    return frame


def to_photo(frame):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return ImageTk.PhotoImage(image=Image.fromarray(rgb))


# ---------------- 录入 ----------------
class SetupWindow:
    def __init__(self, root):
        self.root = root
        self.cap = None
        self.feats = []
        self.stage = 0
        self.stages = [(14, "请正对摄像头"), (14, "头稍微向左转一点"), (14, "头稍微向右转一点")]
        self.last_grab = 0.0
        self.done = False
        self.photo = None

        self.w, self.h = 760, 720
        root.title("录入人脸")
        root.configure(bg=BG)
        root.geometry("%dx%d" % (self.w, self.h))
        root.deiconify()

        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(28, 26, anchor="w", text="录入人脸", fill=FG,
                                font=("Microsoft YaHei UI", 17, "bold"))
        self.canvas.create_text(28, 56, anchor="w",
                                text="采集 %d 帧特征，保存在这台电脑本地" % SAMPLES,
                                fill=DIM, font=("Microsoft YaHei UI", 10))

        self.cam_box = (28, 92, self.w - 28, 92 + 420)
        self.canvas.create_rectangle(*self.cam_box, outline="#1e2429", width=1)
        self.cam_item = self.canvas.create_image(self.w // 2, 92 + 210, anchor="center", tags="cam")

        self.tip_item = self.canvas.create_text(self.w // 2, 560, text="正在启动摄像头…",
                                                fill=FG, font=("Microsoft YaHei UI", 13))
        self.sub_item = self.canvas.create_text(self.w // 2, 588, text="",
                                                fill=DIM, font=("Microsoft YaHei UI", 10))
        self.canvas.create_rectangle(28, 620, self.w - 28, 632, fill="#1a1f24", width=0)
        self.bar_fg = self.canvas.create_rectangle(28, 620, 28, 632, fill=ACCENT, width=0)

        btn = tk.Button(root, text="关闭", command=self.close, bg="#1a1f24", fg=FG,
                        activebackground="#232a30", activeforeground=FG, bd=0,
                        font=("Microsoft YaHei UI", 10), padx=18, pady=6)
        btn.place(relx=1.0, x=-28, y=640, anchor="ne")
        root.protocol("WM_DELETE_WINDOW", self.close)

        if not models_ready():
            self.set_tip("缺少模型文件", WARN)
            self.set_sub("models 目录下需要 YuNet 与 SFace 两个 onnx")
            return

        self.cap = open_camera()
        if self.cap is None:
            self.set_tip("没有找到可用的摄像头", WARN)
            self.set_sub("检查摄像头是否被其他程序占用")
            return
        self.tick()

    def close(self):
        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def set_tip(self, text, color=FG):
        self.canvas.itemconfig(self.tip_item, text=text, fill=color)

    def set_sub(self, text):
        self.canvas.itemconfig(self.sub_item, text=text)

    def set_progress(self, frac):
        x1 = 28 + (self.w - 56) * max(0.0, min(1.0, frac))
        self.canvas.coords(self.bar_fg, 28, 620, x1, 632)

    def tick(self):
        if self.done:
            return
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.set_tip("读取摄像头失败，稍后重试", WARN)
            self.root.after(500, self.tick)
            return

        faces = detect_faces(frame)
        face = pick_face(faces)

        view = frame.copy()
        if face is not None:
            x, y, w, h = [int(v) for v in face[:4]]
            cv2.rectangle(view, (x, y), (x + w, y + h), (102, 191, 178), 1)

        view = fit_frame(view, self.cam_box[2] - self.cam_box[0] - 2, 418)
        self.photo = to_photo(view)
        self.canvas.itemconfig(self.cam_item, image=self.photo)
        self.canvas.coords(self.cam_item, (self.cam_box[0] + self.cam_box[2]) // 2,
                           (self.cam_box[1] + self.cam_box[3]) // 2)

        if face is None:
            self.set_tip("把整张脸放进画面里", WARN)
            self.set_sub("光线要够，脸要正对镜头")
            self.root.after(60, self.tick)
            return

        fw, fh = float(face[2]), float(face[3])
        if fw < 140 or fh < 140:
            self.set_tip("再靠近一点", WARN)
            self.set_sub("脸现在只有 %d 像素宽，最好 160 以上" % int(fw))
            self.root.after(60, self.tick)
            return

        bright = float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())

        now = time.time()
        if now - self.last_grab > 0.18 and len(self.feats) < SAMPLES:
            try:
                feat = extract_feature(frame, face)
                self.feats.append(feat)
                self.last_grab = now
            except Exception:
                log("feature failed: " + traceback.format_exc())

        total = len(self.feats)
        self.set_progress(total / float(SAMPLES))
        need, text = self.stages[min(self.stage, len(self.stages) - 1)]
        if total >= need and self.stage < len(self.stages) - 1:
            self.stage += 1
        if total < SAMPLES:
            self.set_tip(text, ACCENT)
            sub = "已采集 %d / %d" % (total, SAMPLES)
            if bright < 40:
                sub += " · 光线偏暗，开灯会更稳"
            self.set_sub(sub)
        if total >= SAMPLES:
            self.finish()
            return

        self.root.after(60, self.tick)

    def finish(self):
        self.done = True
        try:
            arr = np.concatenate(self.feats, axis=0).astype(np.float32)

            rec = get_recognizer()
            sims = []
            idxs = list(range(min(len(arr), 18)))
            for i in idxs:
                for j in idxs:
                    if i == j:
                        continue
                    sims.append(float(rec.match(arr[i:i + 1], arr[j:j + 1])))
            self_sim = float(np.mean(sims)) if sims else 0.6

            if self_sim < 0.45:
                self.set_tip("这次采集不合格", WARN)
                self.set_sub("同人相似度只有 %.2f。开灯、把脸靠近到画面一半，再来一次" % self_sim)
                log("enroll rejected: self=%.3f" % self_sim)
                self.feats = []
                self.stage = 0
                self.done = False
                self.set_progress(0.0)
                self.root.after(900, self.tick)
                return

            thr = max(0.46, min(0.62, self_sim - 0.40))
            np.save(FEAT_PATH, arr)

            with open(META_PATH, "w", encoding="utf-8") as f:
                json.dump({"thr": round(thr, 3), "self": round(self_sim, 3),
                           "count": int(arr.shape[0]), "name": "主人",
                           "stamp": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=2)

            self.set_progress(1.0)
            self.set_tip("录入完成", ACCENT)
            self.set_sub("样本 %d 帧，同人相似度 %.2f，判定阈值 %.2f" % (arr.shape[0], self_sim, thr))
            log("enrolled %d feats, self=%.3f thr=%.3f" % (arr.shape[0], self_sim, thr))
            if self.cap:
                self.cap.release()
                self.cap = None
        except Exception:
            log("enroll failed: " + traceback.format_exc())
            self.set_tip("录入失败，详见 run.log", WARN)
            self.set_sub(traceback.format_exc().splitlines()[-1][:90])


# ---------------- 锁屏 ----------------
class LockWindow:
    def __init__(self, root, meta, refs):
        self.root = root
        self.meta = meta
        self.refs = refs
        self.name = meta.get("name", "主人")
        self.thr = float(meta.get("thr", DEFAULT_THR))
        self.cap = None
        self.window = []
        self.feat_buf = []
        self.pos_buf = []
        self.fails = 0
        self.cooldown_until = 0.0
        self.unlocked = False
        self.photo = None
        self.esc_presses = []
        self.esc_hold_from = None
        self.blackouts = []

        self.monitors = list_monitors()
        self.primary = self.monitors[0]
        for m in self.monitors[1:]:
            if m[2] * m[3] > self.primary[2] * self.primary[3]:
                self.primary = m
        self.px, self.py, self.pw, self.ph = self.primary

        make_fullscreen(root, self.px, self.py, self.pw, self.ph)
        root.configure(bg=BG)

        for m in self.monitors:
            if m == self.primary:
                continue
            top = tk.Toplevel(root, bg=BG)
            make_fullscreen(top, m[0], m[1], m[2], m[3])
            tk.Label(top, text="屏幕已锁定", bg=BG, fg="#2a3035",
                     font=("Microsoft YaHei UI", 16)).place(relx=0.5, rely=0.5, anchor="center")
            self.blackouts.append(top)

        self.canvas = tk.Canvas(root, width=self.pw, height=self.ph, bg=BG,
                                highlightthickness=0, cursor="none")
        self.canvas.pack(fill="both", expand=True)

        cx, cy = self.pw // 2, int(self.ph * 0.44)
        r = 132
        self.ring_center = (cx, cy)
        self.spin = 0
        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#2a3238", width=2)
        self.arc = self.canvas.create_arc(cx - r, cy - r, cx + r, cy + r, start=0, extent=88,
                                         style="arc", outline=ACCENT, width=4)
        self.canvas.create_oval(cx - 46, cy - 58, cx + 46, cy + 36, outline="#3f4a54", width=2)
        self.canvas.create_arc(cx - 24, cy - 24, cx - 6, cy - 8, start=0, extent=180,
                               style="arc", outline="#3f4a54", width=2)
        self.canvas.create_arc(cx + 6, cy - 24, cx + 24, cy - 8, start=0, extent=180,
                               style="arc", outline="#3f4a54", width=2)
        self.canvas.create_arc(cx - 18, cy + 8, cx + 18, cy + 30, start=180, extent=180,
                               style="arc", outline="#3f4a54", width=2)

        self.tip_item = self.canvas.create_text(cx, cy + r + 58, text="正在启动摄像头…",
                                                fill=FG, font=("Microsoft YaHei UI", 17))
        self.sub_item = self.canvas.create_text(cx, cy + r + 92, text="",
                                                fill=DIM, font=("Microsoft YaHei UI", 11))
        root.bind("<Escape>", self.on_esc_down)
        root.bind("<KeyRelease-Escape>", self.on_esc_up)
        root.bind("<Alt-F4>", lambda e: "break")
        root.bind("<Button-3>", lambda e: "break")
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.focus_force()
        self.poll_esc()

        self.cap = open_camera()
        if self.cap is None:
            self.set_tip("摄像头没有响应", WARN)
            self.set_sub("检查摄像头是否被占用")
        else:
            self.set_tip("请看摄像头", FG)
            self.set_sub("识别到本人后自动解锁")
            self.tick()

    def on_esc_down(self, event=None):
        if self.esc_hold_from is None:
            self.esc_hold_from = time.time()
        now = time.time()
        self.esc_presses = [t for t in self.esc_presses if now - t < 5.0]
        self.esc_presses.append(now)
        if len(self.esc_presses) >= 5:
            self.unlock(quiet=True)

    def on_esc_up(self, event=None):
        if self.esc_hold_from is not None and time.time() - self.esc_hold_from >= 4.0:
            self.unlock(quiet=True)
        self.esc_hold_from = None

    def poll_esc(self):
        if self.unlocked:
            return
        if self.esc_hold_from is not None and time.time() - self.esc_hold_from >= 4.0:
            self.unlock(quiet=True)
            return
        self.root.after(200, self.poll_esc)

    def set_tip(self, text, color=FG):
        self.canvas.itemconfig(self.tip_item, text=text, fill=color)

    def set_sub(self, text):
        self.canvas.itemconfig(self.sub_item, text=text)

    def show_ring(self, score):
        if score is None:
            self.canvas.itemconfig(self.arc, outline="#4b5761", width=3)
        elif score >= self.thr:
            self.canvas.itemconfig(self.arc, outline=ACCENT, width=5)
        else:
            self.canvas.itemconfig(self.arc, outline="#6b7783", width=3)

    def tick(self):
        if self.unlocked:
            return

        now = time.time()
        if now < self.cooldown_until:
            self.set_tip("试得太频繁，等 %d 秒" % int(self.cooldown_until - now + 1), WARN)
            self.set_sub("连续多次不像本人")
            self.root.after(300, self.tick)
            return

        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.set_tip("读取摄像头失败", WARN)
            self.root.after(400, self.tick)
            return

        faces = detect_faces(frame)
        face = pick_face(faces)
        score = None

        if face is None:
            self.window = []
            self.feat_buf = []
            self.pos_buf = []
            self.set_tip("请看摄像头", FG)
            self.set_sub("把整张脸放进画面里")
        else:
            x, y, w, h = [int(v) for v in face[:4]]

            if w < 110:
                self.window = []
                self.feat_buf = []
                self.set_tip("再靠近一点", WARN)
                self.set_sub("脸只有 %d 像素宽，太小容易认错" % w)
            else:
                # 活体：记录人脸位置，照片不会动
                self.pos_buf.append((float(x + w / 2.0), float(y + h / 2.0), float(w)))
                if len(self.pos_buf) > 8:
                    self.pos_buf.pop(0)

                try:
                    self.feat_buf.append(extract_feature(frame, face))
                except Exception:
                    log("feature failed: " + traceback.format_exc())
                if len(self.feat_buf) > 5:
                    self.feat_buf.pop(0)

                if len(self.feat_buf) >= 3:
                    mean_feat = np.mean(np.vstack(self.feat_buf), axis=0).reshape(1, -1).astype(np.float32)
                    try:
                        score = cosine_max(mean_feat, self.refs)
                    except Exception:
                        log("match failed: " + traceback.format_exc())
                        score = None

                passed = score is not None and score >= self.thr
                self.window.append(1 if passed else 0)
                if len(self.window) > 8:
                    self.window.pop(0)
                hits = sum(self.window)

                alive = True
                if len(self.pos_buf) >= 6:
                    spread = float(np.std(np.array(self.pos_buf, dtype=np.float32), axis=0).max())
                    alive = spread >= 1.2

                if passed:
                    self.set_tip("识别中 %d/5" % min(hits, 5), ACCENT)
                    sub = "相似度 %.2f，阈值 %.2f" % (score, self.thr)
                    if not alive:
                        sub += " · 动一下再试（防照片）"
                    self.set_sub(sub)
                else:
                    self.fails += 1
                    self.set_tip("未识别到本人", WARN)
                    if score is not None:
                        if score < 0.25:
                            self.set_sub("相似度 %.2f，不是已录入的脸" % score)
                        else:
                            self.set_sub("相似度 %.2f 偏低，靠近或面向光" % score)
                    if self.fails >= 12:
                        self.fails = 0
                        self.window = []
                        self.feat_buf = []
                        self.pos_buf = []
                        self.cooldown_until = time.time() + 15

                if hits >= 5 and len(self.window) >= 5 and alive:
                    self.unlock()
                    return

        self.show_ring(score)
        self.spin = (self.spin + 9) % 360
        self.canvas.itemconfig(self.arc, start=self.spin)

        self.root.after(55, self.tick)

    def unlock(self, quiet=False):
        if self.unlocked:
            return
        self.unlocked = True
        log("unlock quiet=%s" % quiet)
        try:
            if not quiet:
                self.set_tip("欢迎回来，%s" % self.name, ACCENT)
                self.set_sub("正在解锁…")
                self.root.update_idletasks()
                time.sleep(0.4)
        except Exception:
            pass
        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass
        for w in self.blackouts:
            try:
                w.destroy()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------- 入口 ----------------
def load_profile():
    if not (os.path.exists(FEAT_PATH) and os.path.exists(META_PATH)):
        return None, None
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            meta = json.load(f)
        refs = np.load(FEAT_PATH).astype(np.float32)
        if refs.ndim != 2:
            return None, None
        return meta, refs
    except Exception:
        log("load profile failed: " + traceback.format_exc())
        return None, None


def main():
    mode = "lock"
    for a in sys.argv[1:]:
        if a in ("--setup", "-s", "setup"):
            mode = "setup"

    root = tk.Tk()
    root.withdraw()

    if not models_ready():
        messagebox.showerror(
            "模型缺失",
            "models 目录里需要这两个文件：\n"
            "face_detection_yunet_2023mar.onnx\n"
            "face_recognition_sface_2021dec.onnx")
        return

    if mode == "setup":
        SetupWindow(root)
        root.mainloop()
        return

    meta, refs = load_profile()
    if meta is None:
        messagebox.showwarning(
            "还没有录入人脸",
            "请先双击运行“录入人脸.bat”，然后再启动人脸锁。")
        return

    if float(meta.get("self", 1.0)) < 0.45:
        messagebox.showwarning(
            "上次录入不合格",
            "同人相似度只有 %.2f，这样识别会不稳定。\n"
            "请重新录入：开灯、把脸靠近到画面一半大小、正对镜头。"
            % float(meta.get("self", 0.0)))
        return

    LockWindow(root, meta, refs)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        detail = traceback.format_exc()
        log(detail)
        try:
            messagebox.showerror("人脸锁启动失败", detail[-500:])
        except Exception:
            pass
