"""语音合成：edge-tts 逐句合成 + 串行播放，支持随时打断。"""
from __future__ import annotations

import asyncio
import os
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


# 单句在线合成的上限。网络抖动时 edge-tts 的 websocket 可能一直挂着不返回，
# 而播报队列是串行的 —— 一句卡住，整个对话都跟着假死（表现为「打断之后没反应」）。
# 实测正常 1.5~2.2 秒，偶发抖动会到 10 秒以上，所以给到 15 秒再判死刑。
SYNTH_TIMEOUT = 15.0


def _synthesize_edge(text: str, voice: str, rate: str, pitch: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".mp3", prefix="va_")
    import os

    os.close(fd)
    try:
        asyncio.run(
            asyncio.wait_for(
                _edge_save(text, path, voice, rate, config.TTS_VOLUME, pitch),
                timeout=SYNTH_TIMEOUT,
            )
        )
    except asyncio.TimeoutError:
        Path(path).unlink(missing_ok=True)
        raise TTSError(f"在线合成超时（{SYNTH_TIMEOUT:.0f}s，检查网络）") from None
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


def _rate_to_speed(rate: str | None) -> float:
    """把 edge 那种「+12%」换算成 OpenAI 兼容接口的倍率（1.12）。"""
    text = str(rate if rate is not None else config.TTS_RATE or "").strip()
    if text.endswith("%"):
        text = text[:-1]
    try:
        value = 1.0 + float(text) / 100.0
    except (TypeError, ValueError):
        return 1.0
    return max(0.25, min(4.0, value))


def _synthesize_custom(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    opts: dict | None = None,
) -> str:
    """调自建的 OpenAI 兼容合成服务（/audio/speech）。

    这样 IndexTTS2 / GPT-SoVITS / CosyVoice 这些本机大模型都能接进来，
    而不用把它们的依赖塞进本项目 —— 服务跑在哪儿、用什么显卡，与本程序无关。
    """
    import httpx

    opts = opts or {}
    base = str(opts.get("base") or getattr(config, "TTS_CUSTOM_BASE", "") or "").rstrip("/")
    if not base:
        raise TTSError("还没填自建合成服务的地址")
    if not base.lower().startswith(("http://", "https://")):
        raise TTSError("自建服务地址要以 http:// 或 https:// 开头")

    url = base + "/audio/speech"
    fmt = str(opts.get("format") or getattr(config, "TTS_CUSTOM_FORMAT", "wav") or "wav").lower()
    payload = {
        "model": str(opts.get("model") or getattr(config, "TTS_CUSTOM_MODEL", "") or "tts-1"),
        "input": text,
        "voice": str(voice or opts.get("voice") or getattr(config, "TTS_CUSTOM_VOICE", "") or "alloy"),
        "speed": _rate_to_speed(rate),
        "response_format": "mp3" if fmt == "mp3" else "wav",
    }
    headers = {"Content-Type": "application/json", "Accept": "audio/*"}
    key = str(opts.get("key") or getattr(config, "TTS_CUSTOM_KEY", "") or "")
    if key:
        headers["Authorization"] = "Bearer " + key

    # 本地大模型合成动辄几十秒，不能用在线引擎那套 20 秒超时，
    # 否则每句都超时、全部兜底成机械音，还以为服务坏了
    timeout = float(getattr(config, "TTS_CUSTOM_TIMEOUT", 0)
                    or getattr(config, "TTS_TIMEOUT", 20.0)
                    or 180.0)
    # 本机地址不走系统代理：很多人开着 Clash/公司代理，
    # 不关掉的话 127.0.0.1 的请求会被转发出去，然后失败
    local = (httpx.URL(url).host or "") in ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout,
                          trust_env=not local)
    except httpx.HTTPError as exc:
        raise TTSError(f"连不上 {url}：{exc}") from exc
    if resp.status_code >= 400:
        raise TTSError(f"合成服务返回 {resp.status_code}：{resp.text[:160]}")
    data = resp.content
    if not data:
        raise TTSError("合成服务返回了空音频")

    suffix = ".mp3" if payload["response_format"] == "mp3" else ".wav"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="va_")
    os.close(fd)
    Path(path).write_bytes(data)
    return path


def synthesize(
    text: str,
    voice: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
    engine: str | None = None,
    opts: dict | None = None,
) -> str:
    """合成一句语音，返回临时音频路径。不传参数就用 config 里的当前配置。

    engine 显式传入时按它合成（用于配置页「试听」而不改动正式配置）。
    opts 只给自建服务用：让「还没保存」的地址/Key 也能先试听。
    返回 mp3（edge）或 wav（sapi / 自建服务），调用方按后缀判断即可。
    """
    target = engine or config.TTS_ENGINE
    if target == "sapi":
        return _synthesize_sapi(text, voice or config.SAPI_VOICE, rate, None)

    if target == "custom":
        # 地址没填属于「配置没做完」，不该偷偷退回机械音让人以为配好了
        base = str((opts or {}).get("base") or getattr(config, "TTS_CUSTOM_BASE", "") or "").strip()
        if not base:
            raise TTSError("还没填自建合成服务的地址（「语音合成」页选「自建服务」后填）")
        try:
            return _synthesize_custom(text, voice, rate, opts=opts)
        except Exception as exc:  # noqa: BLE001
            if not getattr(config, "TTS_FALLBACK_SAPI", True):
                raise
            path = _synthesize_sapi(text, config.SAPI_VOICE, rate, None)
            print(f"[TTS] 自建服务不可用（{exc}），已改用离线语音")
            return path

    try:
        return _synthesize_edge(
            text,
            voice or config.TTS_VOICE,
            rate or config.TTS_RATE,
            pitch or config.TTS_PITCH,
        )
    except Exception as exc:  # noqa: BLE001
        # 在线合成挂了（断网、限流、抖动）不能就此变哑巴：退回离线系统语音。
        # 音质机械一点，但话能说完 —— 比一片安静的「没反应」强得多。
        if not getattr(config, "TTS_FALLBACK_SAPI", True):
            raise
        try:
            path = _synthesize_sapi(text, config.SAPI_VOICE, rate, None)
        except Exception:  # noqa: BLE001
            raise exc from None
        print(f"[TTS] 在线合成失败（{exc}），已改用离线语音")
        return path



class _SynthJob:
    """一句语音的预合成任务。worker 取到它时，多半已经合成好了。"""

    __slots__ = ("done", "path", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.path: str | None = None
        self.error: BaseException | None = None


class Speaker:
    """后台串行播报队列：主线程只管往里塞句子，绝不阻塞大模型流。

    合成是**流水线**的：`say()` 一进来就在后台排队合成，worker 播上一句时
    下一句已经合成完 —— 否则每个句号后面都要空等一次合成（1.5~2 秒），
    听起来就是「标点之间的间隔特别长」。
    """

    PREPARE = 2   # 最多同时预合成几句（够覆盖「正在播的 + 下一句」）

    def __init__(self, player) -> None:
        self._player = player
        self._q: queue.Queue[tuple[int, _SynthJob, str]] = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        # 正在处理的任务属于哪一代（None = 空闲）。用代号而不是布尔量，
        # 这样「打断后那句作废的合成」不会把 wait_done / busy 一起拖住。
        self._playing_gen: int | None = None
        self._slots = threading.Semaphore(self.PREPARE)
        self._spoken: list[str] = []
        threading.Thread(target=self._worker, daemon=True).start()

    @property
    def spoken_text(self) -> str:
        return "".join(self._spoken)

    def say(self, text: str) -> None:
        text = sanitize(text)
        if not text:
            return
        job = _SynthJob()
        with self._lock:
            gen = self._gen
            self._spoken.append(text)
            self._q.put((gen, job, text))
        # 立刻开始合成，不等 worker 来取
        threading.Thread(
            target=self._prepare, args=(gen, text, job), daemon=True
        ).start()

    def _prepare(self, gen: int, text: str, job: _SynthJob) -> None:
        """后台合成。结果给 worker 用，被打断就丢掉并删临时文件。"""
        with self._slots:
            if gen != self._gen:      # 排队等槽位时就已经被打断了
                job.done.set()
                return
            try:
                path = synthesize(text)
            except BaseException as exc:  # noqa: BLE001
                job.error = exc
                job.done.set()
                return
            if gen != self._gen:      # 合成期间被打断，这句不会播了
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                job.path = path
            job.done.set()

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

    def wait_done(self, timeout: float = 15.0) -> bool:
        """等到队列清空且最后一句播放结束。返回 True 表示播完（含被打断作废）。

        `_playing_gen` 从「拿到任务」就算占位（不是在真正播放时），
        否则队列被取走、合成还没开始的那几百毫秒里，wait_done 会以为说完了 ——
        连续对话模式下直接开麦，就会把自己的播报当成用户输入，形成自问自答。

        但「属于被打断那一代」的合并不算在内：用户都已经开口了，
        再去等那句作废的话合成完毫无意义 —— edge-tts 网络一抖就能白等十几秒，
        表现就是「打断之后要等很久」。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                gen = self._gen
                playing = self._playing_gen
            if self._q.empty() and playing != gen:
                return True
            time.sleep(0.04)
        return False

    @property
    def busy(self) -> bool:
        with self._lock:
            gen = self._gen
            playing = self._playing_gen
        return bool((playing is not None and playing == gen) or not self._q.empty())

    def warmup(self) -> None:
        try:
            path = synthesize("嗯。")
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    def _worker(self) -> None:
        while True:
            gen, job, text = self._q.get()
            if gen != self._gen:
                job.done.set()
                continue
            path = None
            with self._lock:
                self._playing_gen = gen   # 从这里就算「还在说话」
            try:
                job.done.wait()           # 预合成；通常到这里已经好了
                if gen != self._gen:      # 等待期间被打断
                    # 合成线程可能已经把文件写在 job.path 上，worker 不播就没人删它
                    orphan = getattr(job, "path", None)
                    if orphan:
                        try:
                            Path(orphan).unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                if job.error is not None:
                    raise job.error
                path = job.path
                if not path:
                    continue
                self._player.play_file(Path(path))
            except Exception as exc:  # noqa: BLE001
                print(f"[TTS] 合成/播放失败：{exc}")
            finally:
                with self._lock:
                    if self._playing_gen == gen:
                        self._playing_gen = None
                if path is not None:
                    self._player.unload()
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
