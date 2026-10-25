"""桌面悬浮窗：把角色形象放到桌面上，右键出菜单。

设计要点：

* **独立进程**。界面跑 Qt 事件循环，语音链路跑自己的主循环，两者用本机 HTTP 通信。
  这样界面崩了不影响语音，语音卡住也不影响拖动窗口；而且省掉把语音主循环塞进
  Qt 线程里那一堆锁。
* **真透明**。无边框 + WA_TranslucentBackground + 预先抠好 alpha 的 PNG，
  不是用「白底配白窗」的假透明，深色壁纸上也不会出现白边。
* **点击说话 / 拖动移动**。按下不动 = 点一下说话；按住移动 = 拖走；
  右键 = 菜单（开始说话 / 打开配置页 / 退出程序）。
* 轮询状态放在后台线程，用信号回主线程刷新，避免慢请求卡住拖动。

用法：
    pythonw pet.py            # 正常启动（无控制台）
    python  pet.py --debug    # 带日志窗口，排查用
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import single_instance

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
RUNTIME = ROOT / ".runtime"
LOCK_FILE = RUNTIME / "pet.pid"
POS_FILE = RUNTIME / "pet_pos.json"
LOG_FILE = RUNTIME / "pet.log"

PET_WIDTH = 280          # 窗口显示宽度（逻辑像素）
POLL_MS = 700

STATUS_COLOR = {
    "sleeping": (92, 116, 168),
    "idle": (110, 160, 235),
    "listening": (235, 90, 90),
    "thinking": (238, 180, 80),
    "speaking": (90, 200, 140),
    "offline": (130, 130, 140),
    "booting": (238, 180, 80),
}

# 启动进度条的高度：有补充说明时多一行
BOOT_H = 34
BOOT_H_DETAIL = 46


def log(message: str) -> None:
    try:
        RUNTIME.mkdir(exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# 与助手进程通信
# --------------------------------------------------------------------------
# 本机地址一律不走系统代理：这台机器上 HTTP_PROXY 有值而 NO_PROXY 为空，
# 走代理访问 127.0.0.1 会被拒（和 llm.py 里踩过的是同一个坑）。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _call(url: str, method: str = "GET", payload: dict | None = None, timeout: float = 1.2):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("X-Voice-Assistant", "1")  # 服务端的 CSRF 校验头
    with _opener.open(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_server() -> str | None:
    """找助手进程的配置页地址。优先读 server.json，找不到就扫端口。"""
    try:
        info = json.loads((RUNTIME / "server.json").read_text(encoding="utf-8"))
        url = info.get("url")
        if url:
            _call(f"{url}/api/state", timeout=0.8)
            return url
    except Exception:  # noqa: BLE001
        pass
    for port in range(8765, 8776):
        try:
            _call(f"http://127.0.0.1:{port}/api/state", timeout=0.4)
            return f"http://127.0.0.1:{port}"
        except Exception:  # noqa: BLE001
            continue
    return None


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------
def _qt():
    from PySide6.QtCore import Qt, QTimer, Signal  # noqa: F401
    from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QWidget

    return {
        "Qt": Qt, "QTimer": QTimer, "Signal": Signal, "QAction": QAction,
        "QColor": QColor, "QFont": QFont, "QPainter": QPainter,
        "QPainterPath": QPainterPath, "QPixmap": QPixmap,
        "QApplication": QApplication, "QMenu": QMenu, "QWidget": QWidget,
    }


def build_window(qt, server_ref: dict, on_quit):
    Qt = qt["Qt"]
    QWidget = qt["QWidget"]
    Signal = qt["Signal"]

    class PetWindow(QWidget):
        state_received = Signal(dict)

        def __init__(self) -> None:
            super().__init__()
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setWindowTitle("小蓝")
            self.setCursor(Qt.CursorShape.OpenHandCursor)

            pixmap = qt["QPixmap"](str(ASSETS / "pet.png"))
            if pixmap.isNull():
                raise SystemExit("缺少 assets/pet.png，先跑 python pet_asset.py")
            screen_dpr = self.screen().devicePixelRatio() if hasattr(self, "screen") else 1.0
            target_w = int(PET_WIDTH * max(1.0, screen_dpr))
            self._image = pixmap.scaledToWidth(
                target_w, Qt.TransformationMode.SmoothTransformation
            )
            self._image.setDevicePixelRatio(max(1.0, screen_dpr))
            self._img_w = PET_WIDTH
            self._img_h = int(self._image.height() / max(1.0, screen_dpr))

            self._bubble = ""
            self._bubble_h = 0
            self._boot = None         # 助手启动到哪一步了；None 表示已就绪
            self._boot_h = 0
            self._status = "offline"
            self._offline_hits = 0
            self._phase = 0.0
            self._press_pos = None
            self._drag_offset = None
            self._dragging = False
            self._always_on_top = True

            self.setFixedSize(self._img_w, self._img_h)
            self._move_to_start()
            self._start_timers()
            self.state_received.connect(self._apply_state)

        # ---------------- 位置 ----------------
        def _move_to_start(self) -> None:
            pos = None
            try:
                data = json.loads(POS_FILE.read_text(encoding="utf-8"))
                pos = (int(data["x"]), int(data["y"]))
            except Exception:  # noqa: BLE001
                pos = None
            if pos is None:
                area = self.screen().availableGeometry()
                pos = (area.right() - self._img_w - 40, area.bottom() - self._img_h - 20)
            self.move(*pos)

        def _save_pos(self) -> None:
            try:
                RUNTIME.mkdir(exist_ok=True)
                POS_FILE.write_text(
                    json.dumps({"x": self.x(), "y": self.y()}), encoding="utf-8"
                )
            except OSError:
                pass

        # ---------------- 定时器 ----------------
        def _start_timers(self) -> None:
            self._poll_timer = qt["QTimer"](self)
            self._poll_timer.timeout.connect(self._poll_async)
            self._poll_timer.start(POLL_MS)
            self._poll_async()

            self._anim = qt["QTimer"](self)
            self._anim.timeout.connect(self._tick)
            self._anim.start(50)

        def _tick(self) -> None:
            if self._status in ("listening", "thinking", "speaking", "booting"):
                self._phase = (self._phase + 0.12) % 6.283
                self.update()

        def _poll_async(self) -> None:
            threading.Thread(target=self._poll_once, daemon=True).start()

        def _poll_once(self) -> None:
            url = server_ref.get("url") or discover_server()
            if not url:
                self.state_received.emit({"status": "offline"})
                return
            server_ref["url"] = url
            try:
                # 模型加载那十几秒里，主线程把着 GIL，接口偶尔会两三秒才回；
                # 这里放宽超时，免得把「正在加载」误判成「没连上」
                payload = _call(f"{url}/api/state", timeout=3.0)
            except Exception:  # noqa: BLE001
                server_ref["url"] = None
                self.state_received.emit({"status": "offline"})
                return
            runtime = payload.get("runtime") or {}
            wake = runtime.get("wake") or {}
            self.state_received.emit(
                {
                    "status": runtime.get("status") or "idle",
                    "bubble": runtime.get("bubble") or "",
                    "boot": runtime.get("boot") or {},
                    "wake": wake,
                }
            )

        def _apply_state(self, data: dict) -> None:
            self._status = data.get("status") or "idle"

            # 启动进度条：服务起来了但模型还在加载时，这是唯一能看见的反馈
            boot = data.get("boot") or {}
            self._boot = boot if boot.get("stage") else None
            new_h = 0
            if self._boot:
                new_h = BOOT_H_DETAIL if self._boot.get("detail") else BOOT_H
            if new_h != self._boot_h:
                self._boot_h = new_h
                self._relayout()

            bubble = (data.get("bubble") or "").strip()
            if self._boot:
                bubble = ""          # 有进度条就不用气泡再说一遍
            if data.get("status") == "offline":
                # 悬浮窗常常比助手先起来（光加载模型就要十几秒），
                # 一上来就说「没连上」会让人以为出错。分档给文案：
                # 先是「启动中」，久一点改成中性描述，再久才提示去看日志。
                self._offline_hits += 1
                if self._offline_hits <= 24:          # 约 17 秒
                    bubble = "启动中…"
                elif self._offline_hits <= 90:        # 约 1 分钟
                    bubble = "还在等助手进程…"
                else:
                    bubble = "助手进程没响应，看看 .runtime/assistant.log"
            else:
                self._offline_hits = 0
            self._set_bubble(bubble)
            self.update()

        def _set_bubble(self, text: str) -> None:
            text = text[:64]
            if text == self._bubble:
                return
            self._bubble = text
            self._bubble_h = 0 if not text else self._measure_bubble()
            self._relayout()

        def _relayout(self) -> None:
            """窗口自上而下是：启动进度条 → 气泡 → 角色。

            高度变化一律往上长，角色脚下那一行保持不动，
            否则每来一条状态窗口就往下跳一次。
            """
            height = self._boot_h + self._bubble_h + self._img_h
            if height == self.height():
                self.update()
                return
            delta = height - self.height()
            self.setFixedSize(self._img_w, height)
            self.move(self.x(), self.y() - delta)
            self.update()

        def _measure_bubble(self) -> int:
            from PySide6.QtGui import QFontMetrics

            metrics = QFontMetrics(self._bubble_font())
            rect = metrics.boundingRect(
                0, 0, self._img_w - 36, 200,
                int(qt["Qt"].TextFlag.TextWordWrap), self._bubble,
            )
            return min(96, rect.height() + 20)

        @staticmethod
        def _bubble_font():
            font = qt["QFont"]("Microsoft YaHei UI", 9)
            return font

        # ---------------- 绘制 ----------------
        def paintEvent(self, event) -> None:  # noqa: N802
            painter = qt["QPainter"](self)
            painter.setRenderHint(qt["QPainter"].RenderHint.Antialiasing, True)
            painter.setRenderHint(qt["QPainter"].RenderHint.TextAntialiasing, True)

            if self._boot_h:
                self._paint_boot(painter)
            if self._bubble_h:
                painter.save()
                painter.translate(0, self._boot_h)
                self._paint_bubble(painter)
                painter.restore()
            painter.drawPixmap(0, self._boot_h + self._bubble_h, self._image)
            self._paint_status(painter)
            painter.end()

        def _paint_boot(self, painter) -> None:
            """顶部的启动进度条：阶段名 + 第几步 + 进度 + 补充说明。

            双击 start.vbs 启动时没有任何终端窗口，模型加载那十几秒里，
            这块条子是唯一告诉用户「它在干活、还要多久」的东西。
            """
            Qt = qt["Qt"]
            QColor = qt["QColor"]
            box_w = float(self._img_w - 12)
            box_h = float(self._boot_h - 8)

            path = qt["QPainterPath"]()
            path.addRoundedRect(6.0, 0.0, box_w, box_h, 10.0, 10.0)
            painter.fillPath(path, QColor(20, 24, 34, 240))
            painter.setPen(QColor(238, 180, 80, 170))
            painter.drawPath(path)

            step = int(self._boot.get("step") or 0)
            total = int(self._boot.get("total") or 0)

            head = qt["QFont"]("Microsoft YaHei UI", 9)
            head.setBold(True)
            painter.setFont(head)
            painter.setPen(QColor(238, 244, 255))
            painter.drawText(
                16, 6, int(box_w) - 60, 15,
                int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignVCenter),
                self._boot.get("stage") or "启动中…",
            )

            if total:
                painter.setFont(qt["QFont"]("Microsoft YaHei UI", 8))
                painter.setPen(QColor(148, 158, 180))
                painter.drawText(
                    int(box_w) - 44, 6, 40, 15,
                    int(Qt.AlignmentFlag.AlignRight) | int(Qt.AlignmentFlag.AlignVCenter),
                    f"{step}/{total}",
                )

            bar_y = 25.0
            bar_w = box_w - 32.0
            painter.setPen(Qt.PenStyle.NoPen)
            track = qt["QPainterPath"]()
            track.addRoundedRect(16.0, bar_y, bar_w, 5.0, 2.5, 2.5)
            painter.fillPath(track, QColor(255, 255, 255, 38))
            ratio = 0.08 if not total else max(0.08, min(1.0, step / total))
            fill = qt["QPainterPath"]()
            fill.addRoundedRect(16.0, bar_y, bar_w * ratio, 5.0, 2.5, 2.5)
            painter.fillPath(fill, QColor(238, 180, 80, 235))

            # 加载模型这一步要十几秒，进度条不动会像卡住了 —— 用一道来回扫的
            # 高光表示「还在干活」。只扫已填充的那段，看着像电流在往前推。
            import math

            sweep = (math.sin(self._phase * 0.6) + 1) / 2
            head_w = min(46.0, bar_w * ratio)
            painter.save()
            painter.setClipPath(fill)
            glow = qt["QPainterPath"]()
            glow.addRoundedRect(16.0 + sweep * (bar_w * ratio - head_w), bar_y,
                                head_w, 5.0, 2.5, 2.5)
            painter.fillPath(glow, QColor(255, 255, 255, 130))
            painter.restore()

            detail = self._boot.get("detail") or ""
            if detail:
                painter.setFont(qt["QFont"]("Microsoft YaHei UI", 8))
                painter.setPen(QColor(148, 158, 180))
                painter.drawText(
                    16, int(bar_y) + 8, int(bar_w), 13,
                    int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignVCenter),
                    detail,
                )

        def _paint_bubble(self, painter) -> None:
            QColor = qt["QColor"]
            path = qt["QPainterPath"]()
            rect_h = self._bubble_h - 8
            path.addRoundedRect(6.0, 0.0, float(self._img_w - 12), float(rect_h), 12.0, 12.0)
            painter.fillPath(path, QColor(22, 26, 38, 235))
            painter.setPen(QColor(90, 130, 200, 160))
            painter.drawPath(path)

            painter.setFont(self._bubble_font())
            painter.setPen(QColor(232, 238, 248))
            painter.drawText(
                18, 8, self._img_w - 36, rect_h - 10,
                int(qt["Qt"].TextFlag.TextWordWrap)
                | int(qt["Qt"].AlignmentFlag.AlignLeft)
                | int(qt["Qt"].AlignmentFlag.AlignVCenter),
                self._bubble,
            )

        def _paint_status(self, painter) -> None:
            """右下角的状态点：空闲蓝、听红、想黄、说绿、离线灰。"""
            import math

            QColor = qt["QColor"]
            color = STATUS_COLOR.get(self._status, STATUS_COLOR["offline"])
            cx = self._img_w - 26
            cy = self._img_h + self._bubble_h + self._boot_h - 26
            pulse = 0.0
            if self._status in ("listening", "thinking", "speaking", "booting"):
                pulse = (math.sin(self._phase) + 1) / 2

            if pulse:
                glow = QColor(*color, 90)
                painter.setBrush(glow)
                painter.setPen(qt["Qt"].PenStyle.NoPen)
                radius = 12 + pulse * 5
                painter.drawEllipse(int(cx - radius), int(cy - radius),
                                    int(radius * 2), int(radius * 2))

            painter.setPen(QColor(255, 255, 255, 210))
            painter.setBrush(QColor(*color, 245))
            painter.drawEllipse(cx - 7, cy - 7, 14, 14)

        # ---------------- 交互 ----------------
        def mousePressEvent(self, event) -> None:  # noqa: N802
            if event.button() == qt["Qt"].MouseButton.LeftButton:
                self._press_pos = event.globalPosition().toPoint()
                self._drag_offset = self._press_pos - self.pos()
                self._dragging = False
                self.setCursor(qt["Qt"].CursorShape.ClosedHandCursor)
            elif event.button() == qt["Qt"].MouseButton.RightButton:
                self._show_menu(event.globalPosition().toPoint())

        def mouseMoveEvent(self, event) -> None:  # noqa: N802
            if self._press_pos is None or self._drag_offset is None:
                return
            current = event.globalPosition().toPoint()
            if not self._dragging and (current - self._press_pos).manhattanLength() > 5:
                self._dragging = True
            if self._dragging:
                self.move(current - self._drag_offset)

        def mouseReleaseEvent(self, event) -> None:  # noqa: N802
            if event.button() != qt["Qt"].MouseButton.LeftButton:
                return
            self.setCursor(qt["Qt"].CursorShape.OpenHandCursor)
            was_drag = self._dragging
            self._press_pos = None
            self._drag_offset = None
            self._dragging = False
            if was_drag:
                self._save_pos()
            else:
                self._trigger_listen()

        def _trigger_listen(self) -> None:
            self._set_bubble("我在听…")
            self._status = "listening"
            threading.Thread(target=self._post_trigger, daemon=True).start()

        def _post_trigger(self) -> None:
            url = server_ref.get("url")
            if not url:
                self.state_received.emit({"status": "offline"})
                return
            try:
                _call(f"{url}/api/pet/trigger", "POST", {}, timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                log(f"触发失败：{exc}")
                self.state_received.emit({"status": "offline"})

        def _show_menu(self, global_pos) -> None:
            QMenu = qt["QMenu"]
            menu = QMenu(self)
            title = menu.addAction("小蓝 · 已连接" if server_ref.get("url") else "小蓝 · 未连接")
            title.setEnabled(False)
            menu.addSeparator()

            listen = menu.addAction("开始说话")
            listen.triggered.connect(self._trigger_listen)

            config = menu.addAction("打开配置页")
            config.triggered.connect(self._open_config)

            menu.addSeparator()
            top = menu.addAction("始终置顶")
            top.setCheckable(True)
            top.setChecked(self._always_on_top)
            top.triggered.connect(self._toggle_top)

            menu.addSeparator()
            quit_action = menu.addAction("退出程序")
            quit_action.triggered.connect(on_quit)

            menu.exec(global_pos)

        def _open_config(self) -> None:
            url = server_ref.get("url")
            log(f"打开配置页：url={url!r}")
            if not url:
                self._set_bubble("助手没在运行，先启动它")
                return
            try:
                if self._open_browser_front(url):
                    return
            except Exception as exc:  # noqa: BLE001
                # 槽里的异常默认进 stderr 黑洞，这里必须显式留痕
                log(f"打开配置页异常：{exc!r}")
            self._set_bubble("打不开浏览器，手动访问 " + url)

        def _page_title(self, url: str) -> str:
            """取配置页真实的 <title>，用来在浏览器窗口里认出它。

            不能硬编码标题：页面标题跟着界面改版走（今天就换过），
            而且用户机器上可能有别的窗口标题也带「配置页」字样，模糊匹配
            会把别人的窗口顶到前面（实测踩过）。
            """
            try:
                with _opener.open(url, timeout=2) as resp:
                    head = resp.read(4096).decode("utf-8", "replace")
                return head.split("<title>")[1].split("</title>")[0].strip()
            except Exception:  # noqa: BLE001
                return ""

        def _open_browser_front(self, url: str) -> bool:
            """拉起浏览器，并把配置页窗口真正顶到用户眼前。

            实测坑：webbrowser.open（ShellExecute 分发）在这台机器上对 http
            返回「成功」，但 Chrome 的单例转发把新窗口塞进已有会话、落在
            窗口 Z 序最底层 —— 双屏工作堆里完全看不见，表现为「点了没反应」。
            所以不走 shell 分发：直接以子进程拉起浏览器，再自己找到配置页
            窗口提到前台；已经开着的配置页直接置前，不重复堆窗口。
            """
            import ctypes
            import shutil
            import subprocess
            user32 = ctypes.windll.user32
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.IsIconic.argtypes = [ctypes.c_void_p]
            user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
            user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            user32.EnumWindows.argtypes = [
                ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p),
                ctypes.c_void_p,
            ]

            def chrome_windows() -> list[tuple[int, str]]:
                out: list[tuple[int, str]] = []

                @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                def cb(h, _l):
                    cls = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(h, cls, 64)
                    if cls.value == "Chrome_WidgetWin_1" and user32.IsWindowVisible(h):
                        txt = ctypes.create_unicode_buffer(256)
                        user32.GetWindowTextW(h, txt, 256)
                        out.append((h, txt.value))
                    return True

                user32.EnumWindows(cb, None)
                return out

            def bring_front(h) -> None:
                user32.ShowWindow(h, 9 if user32.IsIconic(h) else 5)  # SW_RESTORE / SW_SHOW
                # ALT 按放一次：绕开「后台进程不许抢前台」的系统限制
                user32.keybd_event(0xA4, 0, 0, 0)
                user32.keybd_event(0xA4, 0, 2, 0)
                user32.SetForegroundWindow(h)

            page_title = self._page_title(url)
            log(f"配置页标题：{page_title!r}")
            # 已开着配置页窗口（活动标签就是它）：直接顶到前面
            if page_title:
                for h, title in chrome_windows():
                    if title.startswith(page_title):
                        bring_front(h)
                        log(f"配置页窗口已存在，置前：{title!r}")
                        return True

            exe = next((c for c in (
                shutil.which("chrome"),
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ) if c and Path(c).is_file()), None)
            if not exe:
                log("找不到 chrome/edge，退回 webbrowser")
                try:
                    return bool(webbrowser.open(url))
                except Exception as exc:  # noqa: BLE001
                    log(f"webbrowser 异常：{exc!r}")
                    return False

            # 用独立 profile 启动，且放在用户目录下 —— 两个实测结论：
            # 1. 共用日常 profile 时走 Chrome 单例转发，多窗口/高频操作下
            #    会「成功但不出窗口」；独立实例的窗口创建是确定的。
            # 2. profile 绝不能放项目目录：这个目录的 ACL 有 DENY 规则，
            #    Chrome 在里面初始化 profile 会无声失败（venv 当年放不进来
            #    也是同一个病根），放 %LOCALAPPDATA% 就正常。
            profile = Path(os.path.expandvars(r"%LOCALAPPDATA%\VoiceAssistantChrome"))
            env = dict(os.environ)
            env["TMP"] = env["TEMP"] = os.path.expandvars(r"%LOCALAPPDATA%\Temp")
            before = {h for h, _ in chrome_windows()}
            subprocess.Popen(
                [exe, f"--user-data-dir={profile}", "--no-first-run", "--new-window",
                 # 独立 profile 没有任何代理设置，系统代理会把 127.0.0.1 吞掉
                 # （这台机器实测：窗口开了、页面永远空白）——强制直连。
                 "--no-proxy-server", url],
                env=env, close_fds=True,
            )
            log(f"已启动 {Path(exe).name}（独立 profile，新实例约 2~10 秒）")
            for _ in range(30):  # 最多等 15 秒：全新 profile 首次启动实测约 10 秒
                time.sleep(0.5)
                for h, title in chrome_windows():
                    if h not in before:
                        # 独立 profile 的实例是我们拉起的，任何新窗口都是配置页
                        bring_front(h)
                        log(f"配置页已打开并置前：{title or '(加载中)'!r}")
                        return True
            log("浏览器已启动但没等到配置页窗口")
            return False

        def _toggle_top(self, checked: bool) -> None:
            self._always_on_top = checked
            self.setWindowFlag(qt["Qt"].WindowType.WindowStaysOnTopHint, checked)
            self.show()  # 改窗口标志后必须重新 show 才生效

    return PetWindow


def main() -> None:
    debug = "--debug" in sys.argv
    RUNTIME.mkdir(exist_ok=True)

    # 单实例：已经有一个悬浮窗就别再开第二个。
    # 判定交给互斥量而不是 pid 文件（pid 会被别的进程复用，详见 single_instance.py），
    # pid 文件照写，stop-all.bat 还要靠它找进程。
    if not single_instance.acquire("VoiceAssistantPet"):
        log("已有悬浮窗在运行，本次退出")
        return
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")

    qt = _qt()
    app = qt["QApplication"]([sys.argv[0]])
    app.setQuitOnLastWindowClosed(True)

    server_ref: dict = {"url": None}
    quitting = {"done": False}

    def quit_app() -> None:
        """退出程序：先让助手进程优雅退出，再关自己的窗口。"""
        if quitting["done"]:
            return
        quitting["done"] = True
        url = server_ref.get("url")
        if url:
            try:
                _call(f"{url}/api/pet/quit", "POST", {}, timeout=2.0)
                for _ in range(20):  # 等它真的关掉，最多 2 秒
                    time.sleep(0.1)
                    try:
                        _call(f"{url}/api/state", timeout=0.4)
                    except Exception:  # noqa: BLE001
                        break
            except Exception as exc:  # noqa: BLE001
                log(f"通知助手退出失败，改用本地结束：{exc}")
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        app.quit()

    Window = build_window(qt, server_ref, quit_app)
    window = Window()

    import signal as _signal

    def _on_sigint(*_args):
        quit_app()

    try:
        _signal.signal(_signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass

    window.show()
    log("悬浮窗已启动")
    code = app.exec()
    if not quitting["done"]:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    sys.exit(code)


def _install_crash_log() -> None:
    """pythonw 没有控制台，未捕获异常会一声不吭地消失。

    表现就是「桌面形象不见了」，日志里连一行错都没有，只能靠猜。
    这里把异常写进 pet.log，下次至少知道它为什么没起来。
    """

    def hook(exc_type, exc, tb) -> None:  # noqa: ANN001
        import traceback

        log("未捕获异常，进程即将退出：")
        for line in "".join(traceback.format_exception(exc_type, exc, tb)).splitlines():
            log(f"    {line}")

    sys.excepthook = hook


if __name__ == "__main__":
    _install_crash_log()
    main()
