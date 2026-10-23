"""语音打断：助手正在说话时，用户一开口就让它闭嘴。

难点
----
播报期间麦克风听到的是「自己的声音 + 用户」，没法用一个固定阈值区分。
这里的做法是拿**回声自己的峰值**当基线：

    基线 = 最近 2 秒里、但不含最近 0.45 秒的最大电平
    （被排除的 0.45 秒正是候选的人声所在）
    触发 = 当前电平 ≥ 基线 × 倍数，并且持续够久

于是：
* 播报再响也是在跟自己的峰值比，不会自己打断自己；
* 人凑近说话时直达声通常比外放回声高好几倍，能稳定触发；
* 咳嗽、键盘声这种短脉冲撑不满「持续时间」，被挡掉。

只用电平，不额外开一路识别：打断要的是快，等识别完再停就来不及了。
"""
from __future__ import annotations

import collections
import threading
import time

import numpy as np

import config

BASE_WINDOW = 2.0      # 基线回看多久
BASE_GUARD = 0.45      # 基线里排除最近多久（候选语音所在）
DIP_TOLERANCE = 0.15   # 说话中间允许的最长间隙，超过就重新计时
GRACE = 0.6            # 刚开始播的一小段先不判：基线窗口要攒够 BASE_GUARD 那么长，
                       # 否则基线取不到值，第一个响块就能把自己打断
PRE_ROLL = 1.0         # 触发时回捞多久的音频（用户的头几个字在播报里说的）
MIN_FLOOR = 0.004      # 绝对下限：安静房间里的一声轻响不算（正常说话远高于它）


def join(pre: np.ndarray, fresh: np.ndarray) -> np.ndarray:
    """把打断前录到的音频接到新录的前面：用户的头几个字往往是在播报里说的。"""
    if pre is None or len(pre) == 0:
        return fresh
    if fresh is None or len(fresh) == 0:
        return pre
    return np.concatenate([pre, fresh])


class BargeIn(threading.Thread):
    """播报期间盯麦克风电平，听到「有人在说话」就回调一次。

    只在 `speaker.busy` 为真时才可能触发 —— 没在说话的时候谈不上打断。
    """

    POLL = 0.03

    def __init__(self, recorder, speaker, on_hit) -> None:
        super().__init__(daemon=True, name="barge-in")
        self.rec = recorder
        self.speaker = speaker
        self.on_hit = on_hit
        self.hits = 0
        self.last_ms = 0.0
        self.last_level = 0.0
        self._armed = threading.Event()
        self._stop = threading.Event()
        self._hit = threading.Event()
        self._lock = threading.Lock()
        self._pre: np.ndarray | None = None
        self._spoken = ""
        self._ambient = 0.0
        self._cursor = 0
        self._need = 0.0

    # ---------------- 对外 ----------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(config, "BARGE_IN_ENABLED", True))

    @property
    def ratio(self) -> float:
        """灵敏度 → 倍数。0.1 要 3.7 倍（很难打断），1.0 只要 1.4 倍（很灵敏）。"""
        try:
            s = float(getattr(config, "BARGE_IN_SENSITIVITY", 0.6))
        except (TypeError, ValueError):
            s = 0.6
        return 4.0 - 2.6 * max(0.0, min(1.0, s))

    @property
    def min_seconds(self) -> float:
        try:
            ms = float(getattr(config, "BARGE_IN_MIN_MS", 350))
        except (TypeError, ValueError):
            ms = 350.0
        return max(0.05, ms / 1000.0)

    def arm(self) -> None:
        """一轮对话开始前调用：清掉上一轮的残音，开始盯电平。"""
        with self._lock:
            self._hit.clear()
            self._pre = None
            self._spoken = ""
            self._cursor = self.rec.latest_seq()
        self.rec.clear_ring()
        self.rec.set_monitor(True)   # 打开环形缓冲，触发时才能回捞音频
        self._armed.set()

    def disarm(self) -> None:
        """一轮结束。环形缓冲交还给唤醒线程。"""
        self._armed.clear()
        self.rec.set_monitor(False)

    def take(self) -> tuple[np.ndarray, str]:
        """取走一次打断。返回（打断前录到的音频，当时正在说的话）。

        没打断时返回空数组；取走后本次打断就消费掉了。
        """
        if not self._hit.is_set():
            return np.zeros(0, dtype=np.float32), ""
        with self._lock:
            pre = self._pre
            spoken = self._spoken
            self._pre = None
        self._hit.clear()
        if pre is None:
            pre = np.zeros(0, dtype=np.float32)
        return pre, spoken

    def stop(self) -> None:
        self._stop.set()
        self._armed.clear()

    def info(self) -> dict:
        """给配置页看：当前电平、判定阈值、命中次数。"""
        with self._lock:
            ambient = self._ambient
        return {
            "enabled": self.enabled,
            "armed": self._armed.is_set(),
            "speaking": bool(self.speaker.busy) if self.speaker is not None else False,
            "hits": self.hits,
            "last_ms": round(self.last_ms),
            "level": round(float(self.rec.latest_level(0.2)), 4),
            "need": round(self._need or max(ambient * self.ratio, MIN_FLOOR), 4),
            "ambient": round(ambient, 4),
            "ratio": round(self.ratio, 2),
            "min_ms": round(self.min_seconds * 1000),
        }

    # ---------------- 内部 ----------------
    def run(self) -> None:
        hist: collections.deque[tuple[float, float]] = collections.deque(maxlen=256)
        loud_since: float | None = None
        last_loud = 0.0
        play_since: float | None = None

        while not self._stop.is_set():
            time.sleep(self.POLL)
            armed = self._armed.is_set() and self.enabled
            busy = bool(self.speaker.busy) if self.speaker is not None else False

            if not armed or not busy:
                # 空闲时只维护环境噪声，顺手把游标推到最新，避免积压
                self._cursor = self.rec.latest_seq()
                if not busy:
                    level = float(self.rec.latest_level(0.2))
                    if level > self._ambient:
                        self._ambient = level
                    else:
                        self._ambient = max(self._ambient * 0.98, level)
                hist.clear()
                loud_since, last_loud, play_since = None, 0.0, None
                self._need = max(self._ambient * self.ratio, MIN_FLOOR)
                continue

            now = time.monotonic()
            if play_since is None:
                play_since = now
            if now - play_since < GRACE:
                continue

            ratio = self.ratio
            need_floor = max(self._ambient * 3.0, MIN_FLOOR)
            need_last = need_floor
            for _seq, t, level in self.rec.levels_since(self._cursor):
                self._cursor = _seq
                hist.append((t, level))
                base = max(
                    (lv for tt, lv in hist if t - BASE_WINDOW <= tt <= t - BASE_GUARD),
                    default=self._ambient,
                )
                need_last = max(base * ratio, need_floor)
                if level >= need_last:
                    if loud_since is None:
                        loud_since = t
                    last_loud = t
                elif last_loud and t - last_loud > DIP_TOLERANCE:
                    loud_since = None
                self._need = need_last

            if loud_since is not None and now - loud_since >= self.min_seconds:
                self._fire(loud_since)
                loud_since = None

    def _fire(self, since: float) -> None:
        with self._lock:
            if self._hit.is_set():
                return
            self._hit.set()
            self._pre = self.rec.read_recent(PRE_ROLL)
            self._spoken = self.speaker.spoken_text if self.speaker is not None else ""
            self.hits += 1
            self.last_ms = (time.monotonic() - since) * 1000
            self.last_level = float(self.rec.latest_level(0.2))
        try:
            self.on_hit()
        except Exception as exc:  # noqa: BLE001 - 回调出错也不能让监听线程死掉
            print(f"[打断] 回调异常：{exc}")
