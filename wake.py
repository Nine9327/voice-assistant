"""唤醒词：常开监听 → 断句 → 识别 → 命中提示词就唤醒助手。

为什么不用 sherpa / porcupine 这类专用唤醒引擎：中文自定义唤醒词基本要付费
SDK 或者自己训练；而本机 GPU 上 whisper small 识别 3 秒音频只要 150ms，
「每说完一句就转写一次」的开销完全可以接受（rtf≈0.05，也就是占用 5% 的
GPU 时间），还顺带拿到了整句话的内容。

命中规则（`match`）：
* 先做精确子串匹配，命中即唤醒；
* 不行再做编辑距离匹配，允许 1~2 个字的偏差 —— whisper 经常把「小蓝」听成
  「小兰」「晓岚」，严格相等会导致唤醒率极低；
* 提示词后面的内容会被当作这次的指令一起送进大模型，
  所以「小蓝小蓝，今天天气怎么样」一句说完即可，不用等唤醒再重复一遍。
"""
from __future__ import annotations

import re
import threading
import time

import numpy as np

import config
import vad

# 保留中文、英文、数字；标点、空格、表情全部丢掉再比对
_NOISE = re.compile(r"[^\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]+")

# 语音片段的能量门槛：95 分位数幅度低于它就当没说话。
# 跟着灵敏度反向走 —— 灵敏度拉满时放宽，宁可多转一次也别漏掉唤醒。
# 定这两个数的依据：安静房间里 whisper 编造整句话的片段峰值约 0.002~0.003，
# 而小声说话（含扬声器外放的合成音）也有 0.006 以上，门槛落在中间。
_PEAK_AT_MIN = 0.008
_PEAK_AT_MAX = 0.0015


def normalize(text: str) -> str:
    return _NOISE.sub("", text or "").lower()


def _distance(a: str, b: str) -> int:
    """莱文斯坦距离（字符串都很短，直接写，不引依赖）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _repeat_unit(target: str) -> str | None:
    """「小蓝小蓝」这种前后重复的提示词，返回重复单元「小蓝」。"""
    n = len(target)
    if n < 4 or n % 2:
        return None
    unit = target[: n // 2]
    return unit if unit * 2 == target else None


def _match_repeat(heard: str, unit: str) -> int | None:
    """连续两次模糊命中重复单元，返回命中结束位置。

    专门处理 whisper 把「小蓝小蓝」听成「小兰小兰」的情况：整串比是错 2 个字，
    按整串算会被判定为不匹配，但按单元比每个单元只错 1 个字，属于正常识别漂移。
    """
    size = len(unit)
    for i in range(0, max(1, len(heard) - size + 1)):
        first = heard[i : i + size]
        if _distance(first, unit) > 1:
            continue
        for skip in (0, 1):  # 允许中间夹一个重复字，如「小蓝小蓝蓝」
            j = i + size + skip
            second = heard[j : j + size]
            # 两个单元必须听起来是同一个词：ASR 把「小蓝」稳定听成「小兰」时会
            # 得到「小兰小兰」，而「小明小红」这种两个词各错一个字的不算唤醒词
            if len(second) == size and second == first:
                return j + size
    return None


def match(text: str, phrase: str) -> tuple[bool, str]:
    """判断 text 里有没有唤醒词。返回 (是否命中, 提示词之后的剩余文本)。"""
    target = normalize(phrase)
    heard = normalize(text)
    if not target or not heard:
        return False, ""

    pos = heard.find(target)
    if pos >= 0:
        return True, heard[pos + len(target) :]

    # 两个字的提示词只认精确匹配：允许错一个字的话，「小红」也会唤醒「小蓝」
    if len(target) <= 2:
        return False, ""

    # 允许少量听错字：窗口长度限制在 ±1 个字，避免只说了半个提示词就唤醒
    allow = 1 if len(target) <= 5 else 2
    min_size = len(target) - 1 if len(target) >= 4 else len(target)
    best_pos, best_dist, best_size = -1, 99, len(target)
    for size in (min_size, len(target), len(target) + 1):
        if size < 2:
            continue
        for i in range(0, max(1, len(heard) - size + 1)):
            dist = _distance(heard[i : i + size], target)
            if dist < best_dist:
                best_pos, best_dist, best_size = i, dist, size
        if best_dist == 0:
            break
    if best_dist <= allow and best_pos >= 0:
        return True, heard[best_pos + best_size :]

    unit = _repeat_unit(target)
    if unit:
        end = _match_repeat(heard, unit)
        if end is not None:
            return True, heard[end:]
    return False, ""


class Waker(threading.Thread):
    """后台常驻的唤醒监听线程。

    生命周期由主循环通过 `enable()` / `disable()` 控制：
    助手说话时必须关掉（否则自己的声音会把话筒塞满），录完再打开。
    """

    def __init__(self, recorder, asr, segmenter: vad.Segmenter, on_wake) -> None:
        super().__init__(daemon=True, name="waker")
        self.recorder = recorder
        self.asr = asr
        self.segmenter = segmenter
        self.on_wake = on_wake

        self._enabled = threading.Event()
        self._run = threading.Event()
        self._run.set()
        self._cursor = 0
        self._paused_until = 0.0

        self.last_heard = ""
        self.last_at = 0.0
        self.last_peak = 0.0
        self.hits = 0
        self.state = "off"          # off / listening / error
        self.last_error = ""

    # ---------------- 控制 ----------------
    def enable(self) -> None:
        if not config.WAKE_ENABLED:
            return
        # 清空缓冲：否则刚播完的那段音频会被当成新的一句话重放
        self.recorder.clear_ring()
        self.segmenter.reset()
        self.recorder.set_monitor(True)
        self._cursor = 0
        self.state = "listening"
        self._enabled.set()

    def disable(self) -> None:
        self._enabled.clear()
        self.recorder.set_monitor(False)
        self.state = "off"

    def stop(self) -> None:
        self.disable()
        self._run.clear()

    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    def info(self) -> dict:
        return {
            "state": self.state,
            "enabled": config.WAKE_ENABLED,
            "phrase": config.WAKE_PHRASE,
            "last_heard": self.last_heard,
            "hits": self.hits,
            "error": self.last_error,
            "threshold": round(self.segmenter.threshold, 5),
            "peak": round(self.last_peak, 4),
            "min_peak": round(self.min_peak, 4),
        }

    @property
    def min_peak(self) -> float:
        """当前灵敏度下的语音能量门槛。"""
        k = min(1.0, max(0.0, self.segmenter.sensitivity))
        return _PEAK_AT_MIN + k * (_PEAK_AT_MAX - _PEAK_AT_MIN)

    # ---------------- 线程主体 ----------------
    def run(self) -> None:
        while self._run.is_set():
            if not self._enabled.is_set():
                time.sleep(0.05)
                continue
            try:
                self._pump()
            except Exception as exc:  # noqa: BLE001
                # 识别服务被热替换、麦克风被拔掉都可能抛，不能让线程死掉
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.state = "error"
                self._enabled.clear()
                time.sleep(0.5)
            time.sleep(0.03)

    def _pump(self) -> None:
        if time.monotonic() < self._paused_until:
            time.sleep(0.05)
            return

        # 配置页改了灵敏度/静音时长，这里每轮同步一次，不用重启
        self.segmenter.configure(config.WAKE_SENSITIVITY, config.WAKE_SILENCE)
        if not config.WAKE_ENABLED or not config.WAKE_PHRASE.strip():
            self.disable()
            return

        for seq, t, level in self.recorder.levels_since(self._cursor):
            self._cursor = seq
            event = self.segmenter.feed(t, level)
            if event is None or event[0] != "end":
                continue
            _, start_t, end_t = event
            # 多截一点：断句可能起步偏晚（麦克风增益低时前几个字压不过阈值），
            # 而 read_recent 是从末尾往回取，多给的时长正好补在句首
            duration = min(10.0, max(1.2, end_t - start_t + 0.9))
            audio = self.recorder.read_recent(duration)
            if audio.size < config.SAMPLE_RATE * 0.4:
                continue
            # 近乎静音的片段直接扔掉：whisper 在静音上会凭空编出一整句中文，
            # 白白烧掉 150ms 显卡，还会让配置页的「最近听到」变成乱码现场。
            # 用 95 分位数而不是均值 —— 片段尾部总带着静音，均值会被稀释。
            self.last_peak = float(np.percentile(np.abs(audio), 95))
            if self.last_peak < self.min_peak:
                continue
            self._check(audio)

    def _check(self, audio) -> None:
        t0 = time.perf_counter()
        text = self.asr.transcribe(audio)
        self.last_heard = text or ""
        self.last_at = time.time()
        if not text or not self._enabled.is_set():
            # 主循环可能在识别期间就把监听关掉了（用户按了空格 / 点了悬浮窗），
            # 这时再回调会多出一轮对话
            return
        hit, rest = match(text, config.WAKE_PHRASE)
        if not hit:
            return
        self.hits += 1
        self._paused_until = time.monotonic() + 1.2  # 别让同一句话连着触发两次
        self.state = "triggered"
        try:
            self.on_wake(rest.strip(), self.last_heard, (time.perf_counter() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"唤醒回调失败：{exc}"
