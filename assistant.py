"""本地语音助手 —— 三种唤醒方式共用一条链路。

用法：
    python assistant.py              正常语音交互（同时开配置页 + 唤醒监听）
    python assistant.py --config     只开浏览器配置页，不加载模型、不占麦克风
    python assistant.py --text "你好" 跳过录音，直接测大模型 + 播报
    python assistant.py --no-audio    只打字，不播报
    python assistant.py --no-web      不启动配置页
    python assistant.py --no-wake     不监听唤醒词（只保留按住空格）

三种触发来源最终都进同一条队列：
    按住空格 / 说唤醒词 / 点一下桌面上的形象
"""
from __future__ import annotations

import argparse
import atexit
import gc
import os
import queue
import sys
import threading
import time
from pathlib import Path

_PKGS = Path(__file__).parent / "packages"
if _PKGS.is_dir():
    sys.path.insert(0, str(_PKGS))

ROOT = Path(__file__).parent
RUNTIME_DIR = ROOT / ".runtime"
LOG_FILE = RUNTIME_DIR / "assistant.log"
PID_FILE = RUNTIME_DIR / "assistant.pid"


def _ensure_streams() -> None:
    """pythonw 启动时 stdout/stderr 是 None，任何 print 都会抛异常。

    这里直接把它们接到日志文件上，既不报错，又能在出问题时翻记录。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        RUNTIME_DIR.mkdir(exist_ok=True)
        stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_ensure_streams()

import config  # noqa: E402
import llm  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

console = Console()

# 退出信号（Ctrl+C / 配置页 / 悬浮窗菜单都会置位）与「打断当前回答」
STOP = threading.Event()
INTERRUPT = threading.Event()


class SpaceHotkey:
    """全局监听空格键的按下 / 松开。"""

    def __init__(self) -> None:
        from pynput import keyboard

        self._kb = keyboard
        self.pressed = threading.Event()
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )

    def _on_press(self, key) -> None:
        if key == self._kb.Key.space:
            self.pressed.set()

    def _on_release(self, key) -> None:
        if key == self._kb.Key.space:
            self.pressed.clear()

    def wait_press(self, timeout: float | None = None) -> bool:
        """阻塞直到空格被按下。

        用短超时轮询而不是无限 wait，这样主线程才能及时响应 Ctrl+C。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.pressed.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self.pressed.wait(0.1)
        return True

    def wait_release(self, timeout: float | None = None) -> bool:
        """阻塞直到空格松开（录音超时截断后要用它等用户松手）。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.pressed.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.03)
        return True

    def start(self) -> None:
        self._listener.start()

    def stop(self) -> None:
        try:
            self._listener.stop()
        except Exception:
            pass


class TriggerBus:
    """三种触发来源的汇合点。

    PTT   —— 按住空格，松手结束（最可控）
    LISTEN —— 免按键录一句，靠静音自动断句（唤醒词命中、点悬浮窗）
    QUERY —— 已经有文字了，直接进大模型（唤醒词后面跟了内容）
    """

    PTT = "ptt"
    LISTEN = "listen"
    QUERY = "query"

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, str, str]] = queue.Queue()

    def push(self, kind: str, text: str = "", heard: str = "") -> None:
        self._q.put((kind, text, heard))

    def get(self) -> tuple[str, str, str] | None:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def clear(self) -> None:
        while self.get() is not None:
            pass


class LiveState:
    """主循环对外暴露的状态。配置页和桌面悬浮窗都只读这里。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "idle"      # idle / listening / thinking / speaking
        self.heard = ""
        self.reply = ""
        self.asr_ms = 0.0
        self.bubble = ""

    def update(self, status: str | None = None, heard: str | None = None,
               reply: str | None = None, asr_ms: float | None = None) -> None:
        with self._lock:
            if status is not None:
                self.status = status
            if heard is not None:
                self.heard = heard
            if reply is not None:
                self.reply = reply
            if asr_ms is not None:
                self.asr_ms = asr_ms
            self.bubble = self._compose_bubble()

    def _compose_bubble(self) -> str:
        if self.status == "listening":
            return "我在听…"
        if self.status == "thinking":
            return "在想…"
        if self.status == "speaking":
            return self.reply.strip()[:64] or "…"
        return ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "heard": self.heard,
                "reply": self.reply,
                "bubble": self.bubble,
                "asr_ms": round(self.asr_ms),
            }


class AsrService:
    """识别器的可热替换包装。

    Web 配置页改了「识别服务 / 模型 / 设备」后，只要重建实例即可生效，
    不用重启整个程序。重建前必须先释放旧模型：8G 显存里 7B 大模型已占 6.8G，
    新旧两份 whisper 同时驻留很容易 OOM。
    """

    def __init__(self) -> None:
        from asr import create_transcriber

        self._lock = threading.Lock()
        self._impl = create_transcriber()

    @property
    def device(self) -> str:
        return str(getattr(self._impl, "device", "?"))

    @property
    def compute_type(self) -> str:
        return str(getattr(self._impl, "compute_type", "?"))

    def transcribe(self, audio):
        with self._lock:
            return self._impl.transcribe(audio)

    def reload(self) -> dict:
        from asr import create_transcriber

        try:
            new_impl = create_transcriber()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "device": self.device,
                "compute_type": self.compute_type,
            }

        with self._lock:
            old = self._impl
            self._impl = new_impl
        del old
        gc.collect()
        return {
            "ok": True,
            "device": new_impl.device,
            "compute_type": new_impl.compute_type,
        }


class RuntimeBridge:
    """把运行期状态和重载能力交给配置页服务。

    配置页可能在识别模型还没加载完时就被打开，所以这里允许各组件为空。
    """

    def __init__(self) -> None:
        self.web = None
        self.asr: AsrService | None = None
        self.live: LiveState | None = None
        self.waker = None

    def info(self) -> dict:
        out: dict = {}
        if self.asr is None:
            out["asr"] = {"device": "未加载", "compute_type": "-"}
        else:
            out["asr"] = {"device": self.asr.device, "compute_type": self.asr.compute_type}
        if self.live is not None:
            out.update(self.live.snapshot())
        if self.waker is not None:
            out["wake"] = self.waker.info()
        else:
            out["wake"] = {"state": "off", "enabled": False, "phrase": config.WAKE_PHRASE}
        return out

    def reload_asr(self) -> dict:
        if self.asr is None:
            return {"ok": False, "error": "识别模型尚未加载完成，请稍后再试"}
        return self.asr.reload()


def trim_history(history: list[dict]) -> list[dict]:
    keep = config.MAX_HISTORY_TURNS * 2
    return [history[0]] + history[1:][-keep:]


def reply_once(history: list[dict], speaker, hotkey=None, live: LiveState | None = None) -> str:
    """跑一轮大模型，边流式打印边逐句播报。返回完整回复。"""
    splitter = None
    if speaker is not None:
        from tts import SentenceSplitter

        splitter = SentenceSplitter()

    pieces: list[str] = []
    first_token_at: float | None = None
    t0 = time.perf_counter()

    for chunk in llm.stream_reply(history):
        if first_token_at is None:
            first_token_at = time.perf_counter() - t0
        pieces.append(chunk)
        console.print(chunk, end="", style="bright_white")
        if live is not None and len(pieces) % 4 == 0:
            # 直接把已生成的内容喂给悬浮窗气泡，边想边显示
            live.update("speaking" if speaker is not None else "thinking",
                        reply="".join(pieces))
        if splitter is not None:
            for sent in splitter.feed(chunk):
                speaker.say(sent)
        if (hotkey is not None and hotkey.pressed.is_set()) or INTERRUPT.is_set():
            console.print("\n[yellow]（已打断）[/yellow]")
            if speaker is not None:
                speaker.interrupt()
            break

    if splitter is not None:
        for sent in splitter.flush():
            speaker.say(sent)

    reply = "".join(pieces).strip()
    if live is not None:
        live.update(reply=reply)

    console.print()
    if first_token_at is not None:
        console.print(f"[dim]首字 {first_token_at * 1000:.0f} ms[/dim]")
    return reply


def record_utterance(rec, segmenter, no_speech_timeout: float = 8.0,
                     max_seconds: float = 20.0) -> tuple[object, bool]:
    """免按键录一句：等到静音就收工。

    返回 (音频, 是否听到人声)。没人说话时不必干等到 max_seconds，
    no_speech_timeout 到点就退出来，避免「点了悬浮窗却半天没反应」。
    """
    segmenter.reset()
    rec.start()
    cursor = 0
    spoke = False
    t0 = time.perf_counter()
    while not STOP.is_set() and not INTERRUPT.is_set():
        for seq, t, level in rec.levels_since(cursor):
            cursor = seq
            event = segmenter.feed(t, level)
            if event is None:
                continue
            if event[0] == "start":
                spoke = True
            elif event[0] == "end":
                return rec.stop(), True
        elapsed = time.perf_counter() - t0
        if not spoke and elapsed > no_speech_timeout:
            break
        if elapsed > max_seconds:
            break
        time.sleep(0.03)
    return rec.stop(), spoke


def preflight() -> None:
    if llm.ensure_running():
        console.print("[dim]已自动启动 Ollama[/dim]")
    llm.ensure_model()
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="input")
        console.print(f"[dim]麦克风：{dev['name']}[/dim]")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]麦克风不可用：{exc}[/red]")
        raise SystemExit(1)


def write_pid() -> None:
    """记下自己的进程号，方便 stop.py 精确找到本程序。"""
    try:
        PID_FILE.parent.mkdir(exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def clear_pid() -> None:
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def already_running() -> bool:
    """已经有一个助手进程在跑就直接退出，避免两个进程抢麦克风。

    用 pythonw 启动时看不到控制台，所以顺手把已有的配置页打开，
    让用户知道程序其实在运行。
    """
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return False
    if pid == os.getpid() or not _pid_alive(pid):
        return False
    console.print(f"[yellow]助手已经在运行（PID {pid}），本次退出[/yellow]")
    try:
        import json

        info = json.loads((RUNTIME_DIR / "server.json").read_text(encoding="utf-8"))
        url = info.get("url")
        if url:
            import webbrowser

            console.print(f"[dim]配置页：{url}[/dim]")
            webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    return True


def _service_summary() -> str:
    """启动横幅里的一行服务摘要。"""
    if config.LLM_PROVIDER == "openai":
        model = f"网络 {config.OPENAI_MODEL}"
    else:
        model = f"本地 {config.LLM_MODEL}"
    tts_name = "离线系统语音" if config.TTS_ENGINE == "sapi" else "在线 edge-tts"
    asr_name = "网络识别" if config.ASR_PROVIDER == "openai" else f"whisper {config.WHISPER_MODEL}"
    return f"模型 {model} · 识别 {asr_name} · 合成 {tts_name}"


def start_web(bridge: RuntimeBridge, port: int | None, on_trigger=None, on_quit=None):
    """起配置页服务。失败不致命 —— 语音功能照常可用。"""
    try:
        from webserver import ConfigServer
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]配置页模块加载失败：{exc}[/red]")
        return None

    server = ConfigServer(
        port=port,
        on_asr_reload=bridge.reload_asr,
        get_runtime=bridge.info,
        on_trigger=on_trigger,
        on_quit=on_quit,
    )
    try:
        server.start()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]配置页启动失败：{exc}[/red]")
        return None
    return server


def wait_forever(web) -> None:
    try:
        while not STOP.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        console.print("\n[dim]再见[/dim]")
    finally:
        if web is not None:
            web.stop()


def wait_for_trigger(hotkey, bus: TriggerBus):
    """等任意一种触发。返回 (类型, 文本, 听到的原话)，退出时返回 None。"""
    while not STOP.is_set():
        if hotkey is not None and hotkey.pressed.is_set():
            return (TriggerBus.PTT, "", "")
        item = bus.get()
        if item is not None:
            return item
        time.sleep(0.03)
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="跳过录音，直接用它当用户输入")
    parser.add_argument("--no-audio", action="store_true", help="不播报")
    parser.add_argument("--no-web", action="store_true", help="不启动配置页服务")
    parser.add_argument("--port", type=int, help="配置页端口，默认用 config.WEB_PORT")
    parser.add_argument("--no-browser", action="store_true", help="起服务但不自动开浏览器")
    parser.add_argument("--config", action="store_true", help="只开配置页，不加载语音链路")
    parser.add_argument("--no-wake", action="store_true", help="不监听唤醒词，只支持按住空格")
    args = parser.parse_args()

    if already_running():
        return

    write_pid()
    atexit.register(clear_pid)

    console.print(
        Panel.fit(
            "[bold]本地语音助手[/bold]\n" + _service_summary(),
            border_style="cyan",
        )
    )

    live = LiveState()
    bridge = RuntimeBridge()
    bridge.live = live
    bus = TriggerBus()

    def on_pet_trigger() -> dict:
        """桌面悬浮窗被点击：等同于按了一次空格。"""
        INTERRUPT.set()   # 正在说的话先停下
        # 已经在录音时，点击的意思是「说完了」，不要再排一轮
        if live.status != "listening":
            bus.push(TriggerBus.LISTEN)
        return {"ok": True}

    def on_quit() -> dict:
        STOP.set()
        return {"ok": True}

    web = None
    if not args.no_web:
        web = start_web(bridge, args.port, on_pet_trigger, on_quit)
        if web is not None:
            bridge.web = web
            console.print(
                f"[cyan]配置页[/cyan] {web.url}  "
                "[dim]（浏览器里切换服务、试听音色、改唤醒词，改完即刻生效）[/dim]"
            )
            # 纯文本模式通常是被脚本调用的，不打扰用户桌面
            if config.WEB_OPEN_BROWSER and not args.no_browser and not args.text:
                web.open_browser()

    if args.config:
        if web is None:
            console.print("[red]配置页没能启动，无法进入仅配置模式[/red]")
            return
        console.print("[dim]仅配置模式：只提供网页配置，不占麦克风、不加载模型。Ctrl+C 退出[/dim]")
        wait_forever(web)
        return

    preflight()

    from audio_io import Player, Recorder
    from tts import Speaker

    speaker = None
    if not args.no_audio:
        speaker = Speaker(Player())
        speaker.warmup()
        console.print("[dim]语音合成就绪[/dim]")

    history = [{"role": "system", "content": config.SYSTEM_PROMPT}]

    # 纯文本模式：验证「大模型 + 播报」这一段
    if args.text:
        history.append({"role": "user", "content": args.text})
        console.print(f"[cyan]你（文本）：[/cyan]{args.text}")
        live.update("thinking", heard=args.text)
        reply_once(history, speaker, live=live)
        if speaker is not None:
            speaker.wait_done()
        return

    console.print("[dim]加载识别服务（本地模型首次运行需下载约 500MB）…[/dim]")
    try:
        asr = AsrService()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]识别服务初始化失败：{exc}[/red]")
        if web is not None:
            console.print(f"[dim]可以打开 {web.url} 改配置，或 Ctrl+C 退出[/dim]")
            wait_forever(web)
        return
    bridge.asr = asr
    console.print(f"[dim]识别就绪：{asr.device} / {asr.compute_type}[/dim]")

    rec = Recorder()
    hotkey = SpaceHotkey()
    hotkey.start()

    import vad
    from wake import Waker

    segmenter = vad.Segmenter(config.WAKE_SENSITIVITY, config.WAKE_SILENCE)

    def on_wake(rest: str, heard: str, asr_ms: float) -> None:
        """唤醒词命中。提示词后面如果跟了内容，就当成一句话直接处理。"""
        console.print(f"[magenta]唤醒：[/magenta]{heard}  [dim]({asr_ms:.0f}ms)[/dim]")
        if len(rest) >= 2:
            bus.push(TriggerBus.QUERY, text=rest, heard=heard)
        else:
            bus.push(TriggerBus.LISTEN, heard=heard)

    waker = None
    if config.WAKE_ENABLED and not args.no_wake:
        waker = Waker(rec, asr, segmenter, on_wake)
        bridge.waker = waker
        waker.start()
        console.print(f"[dim]唤醒词已就绪：说「{config.WAKE_PHRASE}」[/dim]")
    else:
        config.WAKE_ENABLED = False

    tips = "按住 [bold]空格[/bold] 或说[bold]唤醒词[/bold]，也可以点桌面上的形象"
    console.print(f"\n[bold green]就绪[/bold green]  {tips}，[bold]Ctrl+C[/bold] 退出\n")

    try:
        while not STOP.is_set():
            live.update("idle", heard="", reply="")
            if waker is not None:
                waker.enable()
            item = wait_for_trigger(hotkey, bus)
            if item is None:
                break
            if waker is not None:
                waker.disable()
            INTERRUPT.clear()

            kind, text, heard = item
            if speaker is not None:
                speaker.interrupt()

            # ---------- 拿到用户说的话 ----------
            asr_ms = 0.0
            if kind == TriggerBus.QUERY:
                user_text = text
                if heard:
                    console.print(f"[cyan]你：[/cyan]{user_text}  [dim](唤醒词带出)[/dim]")
            else:
                live.update("listening")
                if kind == TriggerBus.PTT:
                    rec.start()
                    console.print("[red]● 录音中…[/red]", end="\r")
                    t_start = time.perf_counter()
                    while hotkey.pressed.is_set() and time.perf_counter() - t_start < config.MAX_RECORD_SECONDS:
                        time.sleep(0.02)
                    audio = rec.stop()
                    # 超过最长时长被截断时用户手可能还按着，先等松手，否则下一轮会立刻又触发
                    if hotkey.pressed.is_set():
                        hotkey.wait_release()
                    spoke = True
                else:
                    console.print("[red]● 录音中…[/red]", end="\r")
                    audio, spoke = record_utterance(
                        rec, segmenter,
                        no_speech_timeout=8.0,
                        max_seconds=min(config.MAX_RECORD_SECONDS, 25),
                    )
                duration = len(audio) / config.SAMPLE_RATE
                console.print(" " * 20, end="\r")

                if not spoke or duration < config.MIN_RECORD_SECONDS:
                    console.print("[dim]没听到声音[/dim]")
                    continue

                t_asr = time.perf_counter()
                user_text = asr.transcribe(audio)
                asr_ms = (time.perf_counter() - t_asr) * 1000
                if not user_text:
                    console.print("[dim]没听清，再说一次[/dim]")
                    continue
                console.print(f"[cyan]你：[/cyan]{user_text}  [dim]({asr_ms:.0f}ms)[/dim]")

            # ---------- 回答 ----------
            live.update("thinking", heard=user_text, reply="", asr_ms=asr_ms)
            console.print("[green]助手：[/green]", end="")

            INTERRUPT.clear()  # 录音期间点的「停下」不该把回答也掐掉
            history.append({"role": "user", "content": user_text})
            reply = reply_once(history, speaker, hotkey, live)
            history.append({"role": "assistant", "content": reply})
            history = trim_history(history)

            if speaker is not None:
                speaker.wait_done()
            live.update("idle", heard="", reply="")
    except KeyboardInterrupt:
        console.print("\n[dim]再见[/dim]")
    finally:
        STOP.set()
        if waker is not None:
            waker.stop()
        hotkey.stop()
        rec.close()
        if speaker is not None:
            speaker.interrupt()
        if web is not None:
            web.stop()


if __name__ == "__main__":
    main()
