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

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
RUNTIME = ROOT / ".runtime"
LOCK_FILE = RUNTIME / "pet.pid"
POS_FILE = RUNTIME / "pet_pos.json"
LOG_FILE = RUNTIME / "pet.log"

PET_WIDTH = 280          # 窗口显示宽度（逻辑像素）
POLL_MS = 700

STATUS_COLOR = {
    "idle": (110, 160, 235),
    "listening": (235, 90, 90),
    "thinking": (238, 180, 80),
    "speaking": (90, 200, 140),
    "offline": (130, 130, 140),
}


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
            self._status = "offline"
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
            if self._status in ("listening", "thinking", "speaking"):
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
                payload = _call(f"{url}/api/state", timeout=1.0)
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
                    "wake": wake,
                }
            )

        def _apply_state(self, data: dict) -> None:
            self._status = data.get("status") or "idle"
            bubble = (data.get("bubble") or "").strip()
            if data.get("status") == "offline":
                bubble = "没有连接到助手进程"
            self._set_bubble(bubble)
            self.update()

        def _set_bubble(self, text: str) -> None:
            text = text[:64]
            if text == self._bubble:
                return
            self._bubble = text
            new_h = 0 if not text else self._measure_bubble()
            if new_h == self._bubble_h:
                self.update()
                return
            delta = new_h - self._bubble_h
            self._bubble_h = new_h
            self.setFixedSize(self._img_w, self._img_h + self._bubble_h)
            self.move(self.x(), self.y() - delta)  # 气泡往上长，角色本身不跳
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

            if self._bubble_h:
                self._paint_bubble(painter)
            painter.drawPixmap(0, self._bubble_h, self._image)
            self._paint_status(painter)
            painter.end()

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
            cy = self._img_h + self._bubble_h - 26
            pulse = 0.0
            if self._status in ("listening", "thinking", "speaking"):
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
            if url:
                webbrowser.open(url)
            else:
                self._set_bubble("助手没在运行，先启动它")

        def _toggle_top(self, checked: bool) -> None:
            self._always_on_top = checked
            self.setWindowFlag(qt["Qt"].WindowType.WindowStaysOnTopHint, checked)
            self.show()  # 改窗口标志后必须重新 show 才生效

    return PetWindow


def main() -> None:
    debug = "--debug" in sys.argv
    RUNTIME.mkdir(exist_ok=True)

    # 单实例：已经有一个悬浮窗就别再开第二个
    if LOCK_FILE.exists():
        try:
            other = int(LOCK_FILE.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            log(f"已有悬浮窗在运行（PID {other}），本次退出")
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


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    main()
