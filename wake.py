"""唤醒词：常开监听 → 断句 → 识别 → 命中提示词就唤醒助手。

为什么不用 sherpa / porcupine 这类专用唤醒引擎：中文自定义唤醒词基本要付费
SDK 或者自己训练；而本机 GPU 上 whisper small 识别 3 秒音频只要 150ms，
「每说完一句就转写一次」的开销完全可以接受（rtf≈0.05，也就是占用 5% 的
GPU 时间），还顺带拿到了整句话的内容。

命中规则（`match`）—— 先按「字」比，比不过再按「音」比：
* 第一道：原文精确子串，命中即唤醒（最省事，也保证提示词后面的剩余文本干净）；
* 第二道：**拼音比对**。把每个字转成不带声调的拼音再滑动窗口对齐，
  这样「小蓝 / 小兰 / 晓岚 / 小懒」都算命中 —— whisper 认字错、但读音通常是对的；
  再把易混的声母韵母归一（zh/z、ch/c、sh/s、n/l、ang/an、eng/en、ing/in），
  于是「小男小男」这种把 l 听成 n 的情况也能唤醒（实测中最常见的一种错法）；
* 提示词后面的内容会被当作这次的指令一起送进大模型，
  所以「小蓝小蓝，今天天气怎么样」一句说完即可，不用等唤醒再重复一遍。

为什么不做成「允许错 1~2 个字」的编辑距离：字错和音错是两回事。
「小蓝」被听成「校篮」是同一个音，该唤醒；被听成「小绿」只错一个字，
但读音完全不同，不该唤醒 —— 编辑距离分不清这两种，拼音可以。
"""
from __future__ import annotations

import functools
import re
import threading
import time

import numpy as np

import config
import vad

try:  # 拼音是同音唤醒的基础；没装上就退化成逐字比对，功能不中断
    from pypinyin import lazy_pinyin as _lazy_pinyin

    HAS_PINYIN = True
except Exception:  # noqa: BLE001
    _lazy_pinyin = None
    HAS_PINYIN = False

# 保留中文、英文、数字；标点、空格、表情全部丢掉再比对
_NOISE = re.compile(r"[^\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]+")

# 语音片段的能量门槛：95 分位数幅度低于它就当没说话。
# 跟着灵敏度反向走 —— 灵敏度拉满时放宽，宁可多转一次也别漏掉唤醒。
# 定这两个数的依据：安静房间里 whisper 编造整句话的片段峰值约 0.002~0.003，
# 而小声说话（含扬声器外放的合成音）也有 0.006 以上，门槛落在中间。
_PEAK_AT_MIN = 0.008
_PEAK_AT_MAX = 0.0015

# 「值不值得送去识别」的绝对下限，录音回答这一侧用它（不看灵敏度）。
# 断句阈值是自适应的，安静房间里会掉到 0.0015 附近，随便一点噪声就能断出一句；
# 真正的幻觉防线是识别端的 silero VAD（asr.py 里 vad_filter=True），
# 这里只做一道便宜的预筛：明显是静音就直接跳过，省下 150ms 显卡。
# 别定太高 —— 实测外放合成音只有 0.0026，定到 0.0035 就会把正常语音挡在外面。
ANSWER_PEAK = 0.003

# 易混声母 / 韵母归一。目标不是「标准拼音」，而是「听起来像」：
# 中文语音里 zh/z、ch/c、sh/s、n/l 本来就分不清，前后鼻音在快速口语里也常常合并。
_INITIALS = (("zh", "z"), ("ch", "c"), ("sh", "s"))
_FINALS = (
    ("iang", "ian"), ("uang", "uan"),
    ("ang", "an"), ("eng", "en"), ("ing", "in"), ("ong", "on"),
)


def normalize(text: str) -> str:
    return _NOISE.sub("", text or "").lower()


def peak_of(audio) -> float:
    """片段的能量代表值：95 分位数幅度（片段尾部总带静音，均值会被稀释）。"""
    if getattr(audio, "size", 0) == 0:
        return 0.0
    return float(np.percentile(np.abs(audio), 95))


def min_peak(sensitivity: float) -> float:
    """唤醒监听用的能量门槛，跟着灵敏度反向走。"""
    k = min(1.0, max(0.0, float(sensitivity)))
    return _PEAK_AT_MIN + k * (_PEAK_AT_MAX - _PEAK_AT_MIN)


def _fuzz(syllable: str) -> str:
    """把拼音音节压成「听感等价类」。"""
    s = (syllable or "").strip().lower()
    if not s:
        return ""
    for a, b in _INITIALS:
        if s.startswith(a):
            s = b + s[len(a):]
            break
    if s.startswith("n"):          # 小蓝 → 小男：边音 / 鼻音混淆
        s = "l" + s[1:]
    for a, b in _FINALS:
        if s.endswith(a):
            s = s[: -len(a)] + b
            break
    return s


@functools.lru_cache(maxsize=8192)
def _syllable(ch: str) -> str:
    """单字 → 听感等价的拼音。非中文原样小写返回，保证比对时有东西可比。"""
    if _lazy_pinyin is not None and "\u4e00" <= ch <= "\u9fff":
        try:
            got = _lazy_pinyin(ch)
            if got and got[0]:
                return _fuzz(got[0])
        except Exception:  # noqa: BLE001
            pass
    return _fuzz(ch)


def _syllables(text: str) -> list[str]:
    return [_syllable(ch) for ch in text]


def _repeat_unit(target: str) -> str | None:
    """「小蓝小蓝」这种前后重复的提示词，返回重复单元「小蓝」。"""
    n = len(target)
    if n < 4 or n % 2:
        return None
    unit = target[: n // 2]
    return unit if unit * 2 == target else None


def _match_repeat(heard: list[str], unit: list[str]) -> int | None:
    """连续两次命中重复单元，返回命中结束位置（字节下标，按字计）。

    专门处理「小蓝小蓝」被听成「小兰小蓝」这类一半对一半错的情况：
    整串滑窗差一点，按单元比则两边都命中。
    """
    size = len(unit)
    for i in range(0, max(1, len(heard) - size + 1)):
        if heard[i: i + size] != unit:
            continue
        for skip in (0, 1):  # 允许中间夹一个重复字，如「小蓝小蓝蓝」
            j = i + size + skip
            if heard[j: j + size] == unit:
                return j + size
    return None


def _allowed_typos(n: int) -> int:
    """允许多少个音对不上。两个字必须全对，否则「小红」也会唤醒「小蓝」。"""
    if n <= 2:
        return 0
    return 1 if n <= 5 else 2


def _seq_distance(a: list[str], b: list[str]) -> int:
    """音节序列的编辑距离（增 / 删 / 改各算一步）。

    必须用编辑距离而不是「逐位比」：窗口长度可能是 n-1，逐位比会被 zip 截断成
    只比前 n-1 个音节，于是「小红小红」只错一个音也能命中「小蓝小蓝」。
    """
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match(text: str, phrase: str) -> tuple[bool, str]:
    """判断 text 里有没有唤醒词。返回 (是否命中, 提示词之后的剩余文本)。"""
    target = normalize(phrase)
    heard = normalize(text)
    if not target or not heard:
        return False, ""

    pos = heard.find(target)
    if pos >= 0:
        return True, heard[pos + len(target):]

    t_syl = _syllables(target)
    h_syl = _syllables(heard)
    n = len(t_syl)
    allow = _allowed_typos(n)

    # 窗口长度限制在 ±1 个音节：既容忍多听 / 漏听一个字，又不至于只说了半个提示词就唤醒
    sizes = [n] if n <= 2 else [n - 1, n, n + 1]
    best_cost, best_pos, best_size = 99, -1, n
    for size in sizes:
        if size < 1:
            continue
        for i in range(0, len(h_syl) - size + 1):
            cost = _seq_distance(h_syl[i: i + size], t_syl)
            if cost < best_cost:
                best_cost, best_pos, best_size = cost, i, size
            if best_cost == 0:
                break
        if best_cost == 0:
            break
    if best_pos >= 0 and best_cost <= allow:
        return True, heard[best_pos + best_size:]

    unit = _repeat_unit(target)
    if unit:
        end = _match_repeat(h_syl, _syllables(unit))
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
        self.state = "off"          # off / listening / triggered / error
        self.last_error = ""

    # ---------------- 控制 ----------------
    def enable(self) -> None:
        if not config.WAKE_ENABLED:
            return
        # 清空缓冲：否则刚播完的那段音频会被当成新的一句话重放
        self.recorder.clear_ring()
        self.segmenter.reset()
        self.recorder.set_monitor(True)
        self._cursor = self.recorder.latest_seq()
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
            "match_mode": "同音" if HAS_PINYIN else "逐字",
        }

    @property
    def min_peak(self) -> float:
        """当前灵敏度下的语音能量门槛。"""
        return min_peak(self.segmenter.sensitivity)

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

        # 配置页改了灵敏度 / 静音时长，这里每轮同步一次，不用重启
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
            self.last_peak = peak_of(audio)
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
