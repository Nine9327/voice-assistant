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
GRACE = 0.8            # 刚开始播的一小段先不判：基线窗口要攒够 BASE_GUARD 那么长，
                       # 否则基线取不到值，第一个响块就能把自己打断；调成 0.8 秒
                       # 还顺带压住「第一个字很长、音量一路渐强」的误触发
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
        self._echo_peak = 0.0     # 播报期间回声到达过的最高电平

    # ---------------- 对外 ----------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(config, "BARGE_IN_ENABLED", True))

    @property
    def ratio(self) -> float:
        """灵敏度 → 倍数。

        外放时人声和回声混在同一条麦克风数据里，只能看总电平涨了多少倍。
        实测（.runtime/barge_sim.py）：原来 0.6 对应 2.44 倍，人声得压过回声一倍半
        才停得下来，等于「打不断」。现在 0.6 约 1.64 倍，正常音量说话就够；
        拉到 1.0 是 1.35 倍（很灵敏），降到 0.1 是 2.94 倍（只有贴近说才停）。
        """
        try:
            s = float(getattr(config, "BARGE_IN_SENSITIVITY", 0.6))
        except (TypeError, ValueError):
            s = 0.6
        s = max(0.0, min(1.0, s))
        return max(1.35, 3.2 - 2.6 * s)

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
            self._echo_peak = 0.0
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
            # 两种情况下先不判定：开播头 0.8 秒（攒基线），以及「还没听到任何回声」。
            # 后者是为了音频开头有长静音的情况 —— 那时候基线是空的，
            # 第一声正常回声会被当成用户在说话，把自己的播报打断。
            grace = (now - play_since < GRACE) or (self._echo_peak < MIN_FLOOR)

            ratio = self.ratio
            need_floor = max(self._ambient * 2.0, MIN_FLOOR)
            need_last = need_floor
            for _seq, t, level in self.rec.levels_since(self._cursor):
                self._cursor = _seq
                hist.append((t, level))

                # 基线 = 回声自己有多响。这里的教训是「只用滑动窗口的峰值不够」：
                # 句子之间一安静，窗口里就只剩静音，峰值掉到谷底，
                # 恢复说话时正常音量就被当成打断（仿真实测 1.58s 误触发）。
                # 所以另外记一个 _echo_peak：播报期间回声到达过的最高电平，
                # 只升不降，直到下一轮 arm() 才清零。静音期它不会失忆。
                window = [lv for tt, lv in hist if t - BASE_WINDOW <= tt <= t - BASE_GUARD]
                base = max(
                    max(window) if window else 0.0,
                    self._echo_peak,
                    self._ambient,
                )
                need_last = max(base * ratio, need_floor)

                if grace:
                    # 开播的头 0.8 秒只学回声，不判定
                    self._echo_peak = max(self._echo_peak, level)
                    continue
                if level >= need_last:
                    if loud_since is None:
                        loud_since = t
                    last_loud = t
                else:
                    # 没到阈值就当成回声，抬高参考峰值（渐强因此不会误触发：
                    # 阈值跟着一起涨，永远追不上）
                    self._echo_peak = max(self._echo_peak, level)
                    if last_loud and t - last_loud > DIP_TOLERANCE:
                        loud_since = None
                self._need = need_last

            if not grace and loud_since is not None and now - loud_since >= self.min_seconds:
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
