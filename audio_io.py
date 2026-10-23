"""音频底层：麦克风录音 + 音频播放。"""
from __future__ import annotations

import os

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import collections
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

import config


class Recorder:
    """常开的输入流，按住时累积音频。

    常开而不是每次开关流，是为了避开 Windows 上反复初始化设备带来的 100~300ms 启动延迟。

    除了「按住录音」，它还负责给唤醒词检测供货：麦克风吹来的每一小块音频都会算一个
    RMS 电平存进 `levels`，需要时按时间窗口回收进 `_ring`。这样唤醒检测和正式录音
    共用同一个流 —— 同一个麦克风开两个流在 Windows 上会互相抢设备。
    """

    BLOCK = 0.06          # 每块 60ms
    RING_SECONDS = 24     # 环形缓冲保留的音频长度

    def __init__(self, sample_rate: int = config.SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._block = int(sample_rate * self.BLOCK)
        self._chunks: list[np.ndarray] = []
        self._active = False
        self._monitor = False
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: collections.deque[tuple[int, float, np.ndarray]] = collections.deque(
            maxlen=int(self.RING_SECONDS / self.BLOCK)
        )
        self._levels: collections.deque[tuple[int, float, float]] = collections.deque(
            maxlen=int(self.RING_SECONDS / self.BLOCK)
        )
        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self._block,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        block = indata[:, 0].copy()
        level = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        now = time.monotonic()
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._levels.append((seq, now, level))
            if self._monitor:
                self._ring.append((seq, now, block))
            if self._active:
                self._chunks.append(block)

    # ---------------- 录音 ----------------
    def start(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._active = True

    def stop(self) -> np.ndarray:
        with self._lock:
            self._active = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    # ---------------- 监听（给唤醒词用） ----------------
    def set_monitor(self, on: bool, keep: float | None = None) -> None:
        """开关环形缓冲。关闭时默认清空，避免把上一轮的音频当成新说话。"""
        with self._lock:
            self._monitor = on
            if not on and keep is None:
                self._ring.clear()

    def clear_ring(self) -> None:
        with self._lock:
            self._ring.clear()

    def latest_seq(self) -> int:
        """当前电平样本序号。用它当起点，可以避开「一开录就把历史电平重放一遍」。"""
        with self._lock:
            return self._seq

    def levels_since(self, seq: int) -> list[tuple[int, float, float]]:
        """取序号大于 seq 的电平样本，调用方自己记住最后一条的序号。"""
        with self._lock:
            if not self._levels:
                return []
            if self._levels[-1][0] <= seq:
                return []
            return [item for item in self._levels if item[0] > seq]

    def latest_level(self, window: float = 0.3) -> float:
        with self._lock:
            if not self._levels:
                return 0.0
            cut = self._levels[-1][1] - window
            return max((lv for _, t, lv in self._levels if t >= cut), default=0.0)

    def read_recent(self, seconds: float) -> np.ndarray:
        """取最近 seconds 秒的音频（需要 monitor 处于开启状态）。"""
        with self._lock:
            if not self._ring:
                return np.zeros(0, dtype=np.float32)
            cut = self._ring[-1][1] - seconds
            blocks = [b for _, t, b in self._ring if t >= cut]
        if not blocks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(blocks)

    def close(self) -> None:
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass



class Player:
    """串行的 mp3 播放器，支持随时打断。"""

    def __init__(self) -> None:
        import pygame

        pygame.mixer.pre_init(frequency=24000, size=-16, channels=1, buffer=512)
        pygame.mixer.init()
        self._pygame = pygame
        self._stop = threading.Event()

    def play_file(self, path: Path) -> bool:
        """播放一个音频文件，返回是否正常播完（被打断返回 False）。"""
        music = self._pygame.mixer.music
        self._stop.clear()
        music.load(str(path))
        music.play()
        while music.get_busy():
            if self._stop.is_set():
                music.stop()
                return False
            time.sleep(0.03)
        return True

    def stop(self) -> None:
        self._stop.set()
        try:
            self._pygame.mixer.music.stop()
        except Exception:
            pass

    def unload(self) -> None:
        """释放文件句柄，否则 Windows 上删不掉刚播过的 mp3。"""
        try:
            self._pygame.mixer.music.unload()
        except Exception:
            pass


def rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio))))
