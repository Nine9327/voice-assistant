"""语音合成：edge-tts 逐句合成 + 串行播放，支持随时打断。"""
from __future__ import annotations

import asyncio
import queue
import re
import tempfile
import threading
import time
from pathlib import Path

import config

_MARKDOWN = re.compile(r"[*#`_>|~]+")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\u2190-\u21ff]",
)
_LEFTOVER_EN = re.compile(r"([。！？，、])\1+")


def sanitize(text: str) -> str:
    """清掉模型偶尔冒出来的 markdown / emoji / 连续标点。"""
    text = _EMOJI.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = _LEFTOVER_EN.sub(r"\1", text)
    return text.strip()


class SentenceSplitter:
    """把流式 token 攒成「适合念出来」的句子。

    硬标点立即切；句子过长时退到最近的逗号切，避免一口气念 80 个字。
    """

    HARD = "。！？!?；;…\n"
    SOFT = "，,、：:"

    def __init__(self, min_len: int = 6, max_len: int = 42) -> None:
        self.min_len = min_len
        self.max_len = max_len
        self._buf = ""
        self._first = True

    def _find_break(self) -> int | None:
        buf = self._buf
        # 首句放宽门槛：「你好。」这种两个字的问候也立刻发声，缩短首字延迟
        min_len = 2 if self._first else self.min_len
        for i, ch in enumerate(buf):
            if ch in self.HARD and i + 1 >= min_len:
                # 合并紧随其后的同类标点，如「？！」
                j = i + 1
                while j < len(buf) and buf[j] in self.HARD:
                    j += 1
                return j - 1
        if len(buf) > self.max_len:
            cut = -1
            for i, ch in enumerate(buf[: self.max_len]):
                if ch in self.SOFT:
                    cut = i
            if cut >= self.min_len:
                return cut
            return self.max_len - 1
        return None

    def feed(self, chunk: str) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        while True:
            idx = self._find_break()
            if idx is None:
                break
            piece = sanitize(self._buf[: idx + 1])
            self._buf = self._buf[idx + 1 :]
            if piece:
                out.append(piece)
                self._first = False
        return out

    def flush(self) -> list[str]:
        piece = sanitize(self._buf)
        self._buf = ""
        return [piece] if piece else []


async def _edge_save(text: str, path: str, voice: str, rate: str, volume: str, pitch: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
    await communicate.save(path)


def _synthesize_edge(text: str, voice: str, rate: str, pitch: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="va_")
    import os

    os.close(fd)
    try:
        asyncio.run(
            _edge_save(text, path, voice, rate, config.TTS_VOLUME, pitch)
        )
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path


# --------------------------------------------------------------------------
# 离线引擎：Windows SAPI（走 COM，进程内直接合成，不启子进程）
# --------------------------------------------------------------------------
_SAPI_FOR_WRITE = 3  # SpeechStreamFileMode.SSFMCreateForWrite
_sapi_cache: dict[str, object] = {}


class TTSError(RuntimeError):
    pass


def sapi_voices() -> list[dict]:
    """列出系统已安装的离线声音。结果缓存，避免每次开配置页都初始化 COM。"""
    if "voices" in _sapi_cache:
        return _sapi_cache["voices"]  # type: ignore[return-value]
    try:
        import comtypes.client

        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        items = voice.GetVoices()
        voices = [
            {"name": items.Item(i).GetDescription()} for i in range(items.Count)
        ]
    except Exception as exc:  # noqa: BLE001
        raise TTSError(f"读取系统声音失败（{type(exc).__name__}）：{exc}") from exc
    _sapi_cache["voices"] = voices
    return voices


def _percent_to_sapi_rate(rate: str) -> int:
    """把 "+12%" 这类语速映射到 SAPI 的 -10~10 档位。"""
    digits = "".join(ch for ch in rate if ch.isdigit() or ch == "-")
    try:
        pct = int(digits)
    except ValueError:
        return 0
    return max(-10, min(10, round(pct / 10)))


def _percent_to_sapi_volume(volume: str) -> int:
    digits = "".join(ch for ch in volume if ch.isdigit() or ch == "-")
    try:
        pct = int(digits)
    except ValueError:
        return 100
    return max(0, min(100, 100 + pct))


def _synthesize_sapi(
    text: str,
    voice_name: str,
    rate: str | None = None,
    volume: str | None = None,
) -> str:
    import comtypes.client

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="va_")
    import os

    os.close(fd)
    try:
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        if voice_name:
            items = voice.GetVoices()
            for i in range(items.Count):
                if items.Item(i).GetDescription() == voice_name:
                    voice.Voice = items.Item(i)
                    break
        voice.Rate = _percent_to_sapi_rate(rate if rate is not None else config.TTS_RATE)
        voice.Volume = _percent_to_sapi_volume(
            volume if volume is not None else config.TTS_VOLUME
        )

        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        stream.Open(path, _SAPI_FOR_WRITE)
        voice.AudioOutputStream = stream
        voice.Speak(text, 0)  # 0 = 同步朗读
        stream.Close()
    except Exception as exc:  # noqa: BLE001
        Path(path).unlink(missing_ok=True)
        raise TTSError(f"离线合成失败（{type(exc).__name__}）：{exc}") from exc

    if Path(path).stat().st_size <= 44:  # 只有 wav 头，说明没念出来
        Path(path).unlink(missing_ok=True)
        raise TTSError("离线合成返回空音频")
    return path


def synthesize(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
    engine: str | None = None,
) -> str:
    """合成一句语音，返回临时音频路径。不传参数就用 config 里的当前配置。

    engine 显式传入时按它合成（用于配置页「试听」而不改动正式配置）。
    返回 mp3（edge）或 wav（sapi），调用方按后缀判断即可。
    """
    target = engine or config.TTS_ENGINE
    if target == "sapi":
        return _synthesize_sapi(text, voice or config.SAPI_VOICE, rate, None)
    return _synthesize_edge(
        text,
        voice or config.TTS_VOICE,
        rate or config.TTS_RATE,
        pitch or config.TTS_PITCH,
    )



class Speaker:
    """后台串行播报队列：主线程只管往里塞句子，绝不阻塞大模型流。"""

    def __init__(self, player) -> None:
        self._player = player
        self._q: queue.Queue[tuple[int, str]] = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._playing = threading.Event()
        self._spoken: list[str] = []
        threading.Thread(target=self._worker, daemon=True).start()

    @property
    def spoken_text(self) -> str:
        return "".join(self._spoken)

    def say(self, text: str) -> None:
        text = sanitize(text)
        if not text:
            return
        with self._lock:
            self._spoken.append(text)
            self._q.put((self._gen, text))

    def interrupt(self) -> None:
        with self._lock:
            self._gen += 1
            self._spoken.clear()
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
        self._player.stop()

    def wait_done(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty() and not self._playing.is_set():
                return
            time.sleep(0.04)

    def warmup(self) -> None:
        try:
            path = synthesize("嗯。")
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    def _worker(self) -> None:
        while True:
            gen, text = self._q.get()
            if gen != self._gen:
                continue
            path = None
            try:
                path = synthesize(text)
            except Exception as exc:  # noqa: BLE001
                print(f"[TTS] 合成失败：{exc}")
                continue
            self._playing.set()
            try:
                self._player.play_file(Path(path))
            except Exception as exc:  # noqa: BLE001
                print(f"[播放] 失败：{exc}")
            finally:
                self._playing.clear()
                self._player.unload()
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
