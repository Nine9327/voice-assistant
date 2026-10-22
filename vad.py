"""能量断句（轻量 VAD）。

不引入 silero / webrtcvad：麦克风已经每 60ms 送一块音频，算个 RMS 就够了。
这里只做一件事 —— 把连续的电平样本流切成一「句」一「句」，供唤醒词检测和
免按键录音共用。

噪声底是**自适应**的：安静房间和开着风扇的房间差一个数量级，写死阈值必然一边
（误触发）或另一边（听不见）失效。空闲时用指数滑动平均跟着环境走，
阈值 = 噪声底 × 增益，增益由灵敏度旋钮控制。
"""
from __future__ import annotations

import threading

# 灵敏度 0~1 → 阈值增益。越大越灵敏（越容易判定为说话）
_GAIN_AT_MIN = 4.5
_GAIN_AT_MAX = 1.6

MIN_THRESHOLD = 0.0030   # 再低就会被麦克风本底噪声触发
INITIAL_FLOOR = 0.0035


class Segmenter:
    """电平流 → 语音段。线程安全（唤醒线程和主线程共用一份）。"""

    def __init__(
        self,
        sensitivity: float = 0.6,
        silence: float = 0.8,
        min_speech: float = 0.3,
        max_speech: float = 20.0,
        start_blocks: int = 2,
        block: float = 0.06,
    ) -> None:
        self.sensitivity = float(sensitivity)
        self.silence = float(silence)
        self.min_speech = float(min_speech)
        self.max_speech = float(max_speech)
        self.start_blocks = start_blocks
        self.block = block
        self.floor = INITIAL_FLOOR
        self._lock = threading.Lock()
        self._speaking = False
        self._loud = 0
        self._started_at = 0.0
        self._last_voice = 0.0
        self.last_level = 0.0

    # ---------------- 参数 ----------------
    def configure(self, sensitivity: float | None = None, silence: float | None = None) -> None:
        """配置页改了灵敏度/静音时长，下一次 feed 就按新值判断。"""
        with self._lock:
            if sensitivity is not None:
                self.sensitivity = min(1.0, max(0.0, float(sensitivity)))
            if silence is not None:
                self.silence = float(silence)

    @property
    def threshold(self) -> float:
        gain = _GAIN_AT_MAX + (1.0 - self.sensitivity) * (_GAIN_AT_MIN - _GAIN_AT_MAX)
        return max(self.floor * gain, MIN_THRESHOLD)

    def reset(self) -> None:
        with self._lock:
            self._speaking = False
            self._loud = 0

    # ---------------- 主逻辑 ----------------
    def feed(self, t: float, level: float) -> tuple[str, float, float] | None:
        """喂一个电平样本。

        返回 None 或：
            ("start", 起始时刻, 0)          检测到开始说话
            ("end",   起始时刻, 结束时刻)    一句话说完了
        """
        with self._lock:
            self.last_level = level
            th = self.threshold

            if not self._speaking:
                # 空闲时缓慢跟踪噪声底；用下限保护，避免安静房间里阈值掉到 0
                if level < th:
                    self.floor = self.floor * 0.98 + level * 0.02
                    self._loud = 0
                    return None
                self._loud += 1
                if self._loud < self.start_blocks:
                    return None
                self._speaking = True
                self._started_at = t - self.start_blocks * self.block
                self._last_voice = t
                return ("start", self._started_at, 0.0)

            # 说话中：用 0.8 倍阈值做迟滞，防止句子中间的气口把一句话切碎
            if level >= th * 0.8:
                self._last_voice = t

            if t - self._started_at >= self.max_speech:
                return self._finish()
            if t - self._last_voice >= self.silence:
                return self._finish()
            return None

    def _finish(self) -> tuple[str, float, float] | None:
        started, ended = self._started_at, self._last_voice
        self._speaking = False
        self._loud = 0
        if ended - started < self.min_speech:
            return None  # 咳嗽、敲键盘之类的短促声，丢掉
        return ("end", started, ended)
