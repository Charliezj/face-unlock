# 人脸锁 FaceLock

一个跑在 Windows 已登录桌面上的**应用级人脸锁**：离开座位时自动盖一层全屏遮罩，回来对着摄像头刷脸就自动解开。全程本机离线运行，不联网、不上传任何图像。

> 它不是 Windows Hello 的替代品——真正的系统登录解锁需要红外摄像头，这里做的是桌面遮罩。

## 特性

- **离座自动锁**：托盘常驻，摄像头每 1.2 秒看一眼；连续 45 秒（可配）画面里没有脸，自动拉起全屏锁
- **静默识别**：锁屏界面不显示摄像头画面，只有旋转的扫描环和状态文字；影像只在内存里逐帧比对，不渲染、不落盘
- **五项防误识**：阈值 0.505、最近 5 帧特征平均、活体微动检测、脸宽 ≥110 像素、连续失败冷却
- **全局热键**：`Ctrl+Alt+Shift+L` 随时锁定，前台任意程序下都生效；首选组合被占用时按候选列表自动回退
- **单实例保护**：多入口同时启动只会保留一个进程
- **纯本地**：OpenCV YuNet + SFace，模型 ONNX 文件放本地，无网络请求

## 原理

| 环节 | 实现 |
| --- | --- |
| 人脸检测 | `cv2.FaceDetectorYN`（YuNet，232 KB） |
| 特征提取 | `cv2.FaceRecognizerSF`（SFace，38 MB，128 维向量） |
| 比对 | 余弦相似度取最大值；多帧平均后与录入的参考集比对 |
| 判定 | 最近 8 帧里至少 5 帧达标，且人脸位置有微动（防静止照片） |
| 锁屏 | Tk 全屏置顶窗口 + `Esc` 长按/连按后路 |

录入时采样 42 帧，人工质量门槛：脸宽 ≥140 像素才采样；参考集内部相似度 <0.45 判为不合格并清空重录。

## 目录

```
FaceUnlock.pyw      主程序：--setup 录入 / 无参锁屏
AutoLock.pyw        托盘常驻离座锁，--selftest 自检
录入人脸.bat         录入入口
启动人脸锁.bat       手动锁屏入口
启动自动锁.bat       托盘常驻入口
使用说明.md          面向使用者的中文说明
autolock.json       自动锁配置（idle_sec / min_face_px / hotkey）
models/             模型目录（仓库不含，见下）
```

## 安装

需要 Python 3.10+ 与一个可用的摄像头。

```powershell
pip install opencv-contrib-python numpy pillow pystray
```

`opencv-contrib-python` 是必须的——普通版 `opencv-python` 不含 `cv2.face`。

模型文件不在仓库里（38 MB），需自行下载后放进 `models/`：

- `face_detection_yunet_2023mar.onnx` — <https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx>
- `face_recognition_sface_2021dec.onnx` — <https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx>

首次启动会把模型复制到 `%LOCALAPPDATA%\FaceUnlock\models`，因为 OpenCV 的 dnn 读不了含中文的路径。

## 使用

1. 双击 `录入人脸.bat`，正对镜头完成录入（会生成 `features.npy` 与 `meta.json`）
2. 双击 `启动自动锁.bat` 常驻托盘；或 `启动人脸锁.bat` 手动锁一次
3. 托盘右键：立即锁定 / 暂停监测 / 退出

开机自启：把 `启动自动锁.bat`（或 `pythonw.exe AutoLock.pyw`）的快捷方式放进
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`。

## 配置

`autolock.json`

| 键 | 含义 | 默认 |
| --- | --- | --- |
| `idle_sec` | 多久没人算离座 | 45 |
| `min_face_px` | 脸宽多少像素算“在座” | 100 |
| `hotkey` | 全局锁定快捷键，写法如 `ctrl+alt+shift+l` | `ctrl+alt+l` |

`meta.json` 里的 `thr` 是判定阈值。本人解不开就降到 0.46；要更严防认错人可提到 0.56～0.60。

## 隐私

- 摄像头画面只在内存中逐帧处理，**不渲染到锁屏界面，也不写盘**
- `features.npy` 存的是 128 维特征向量，不是照片，无法还原成人脸图像
- 全部计算在本地，没有任何网络请求
- 仓库不含任何录入数据、日志或模型文件

## 已知限制

- 应用层遮罩，不是系统登录认证；`Ctrl+Alt+Del` → 任务管理器可以直接结束进程
- 需要一台普通 RGB 摄像头；无红外即无法使用 Windows Hello 人脸
- 摄像头被本程序独占，开会/录屏前需在托盘暂停
- 低头看手机超过 45 秒会被判定离座
- 桌面缩放/多显示器场景下遮罩只覆盖主屏

## 许可

MIT
