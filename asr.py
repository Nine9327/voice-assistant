"""语音识别：本地 faster-whisper 或网络识别接口，按 config.ASR_PROVIDER 切换。

* local  —— faster-whisper，优先 GPU，显存或 CUDA 库不可用时自动回退 CPU
* openai —— 把音频直接 POST 到 OpenAI 兼容的 /audio/transcriptions（复用 OPENAI_BASE / OPENAI_KEY）

两者对外接口一致：device / compute_type / transcribe(audio) -> str。
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
import wave

# HuggingFace 官方源在国内经常连不上，默认走镜像；用户自己设了 HF_ENDPOINT 则以用户为准
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 新版 huggingface_hub 默认走 Xet 存储协议，镜像站不支持会 401，强制回退传统 HTTP 下载
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np

import config
import llm


class ASRError(RuntimeError):
    pass


def pick_model(device: str) -> str:
    """把 auto 解析成真实模型名：有 GPU 就用 medium，只剩 CPU 就退回 small。

    medium 在 CPU 上一句话要算十几秒，语音场景直接用不了，所以回退时必须跟着降档。
    """
    want = config.WHISPER_MODEL
    if want != "auto":
        return want
    return "medium" if device == "cuda" else "small"


def pick_compute(device: str, model_size: str) -> str:
    """medium 及以上自动降成 int8_float16，省一半显存（省下的要留给 7B 模型）。"""
    base = config.WHISPER_COMPUTE if device == "cuda" else "int8"
    if device == "cuda" and model_size in ("medium", "large-v1", "large-v2", "large-v3"):
        return "int8_float16"
    return base


# --------------------------------------------------------------------------
# 领域词提示：把「这个词很可能存在」提前告诉 whisper
#
# 实测（12 句指令 × 远场/噪声/混响三种退化）：small 模型开提示词，字错率从 8.7~13.7%
# 降到 5.1~9.4%，主要修掉的正是「记事本→计识本」「命令提示符→命令提示服」这类
# 本项目天天要用的说法；但 medium 自己够强，再给提示反而会被带偏（同一批测试上一处
# 场景反而变差 4 个点），所以默认只在小模型上用。
# --------------------------------------------------------------------------
_BUILTIN_WORDS = (
    "记事本", "计算器", "画图", "任务管理器", "命令提示符", "控制面板", "资源管理器",
    "回收站", "截图工具", "浏览器", "微信", "QQ", "钉钉", "腾讯会议", "网易云音乐",
    "打开文件", "显存", "进程", "端口",
)
_prompt_cache: dict[str, tuple[float, str]] = {}
_PROMPT_TTL = 300.0


def _split_words(text: str) -> list[str]:
    return [w.strip() for w in re.split(r"[\s、,，;；/|]+", text) if w.strip()]


def build_initial_prompt(model_size: str) -> str:
    mode = config.ASR_PROMPT_MODE
    if mode == "off" or (mode == "auto" and model_size not in ("tiny", "base", "small")):
        return "以下是普通话的句子。"
    cached = _prompt_cache.get(mode)
    now = time.monotonic()
    if cached and now - cached[0] < _PROMPT_TTL:
        return cached[1]

    words: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        if word and word not in seen and len(words) < 40:
            seen.add(word)
            words.append(word)

    phrase = (config.WAKE_PHRASE or "").strip()
    if phrase:
        add(phrase)
    for word in _split_words(config.ASR_HOTWORDS or ""):
        add(word)
    # 本机装着的应用名，正好是「打开 XX」里最容易听错的部分。
    # 中文叫法比英文名更重要 —— 用户说的是「谷歌浏览器」，软件叫 Google Chrome。
    try:
        import apps

        for label in apps.labels(limit=40, with_aliases=True):
            add(label)
    except Exception:  # noqa: BLE001
        pass
    for word in _BUILTIN_WORDS:
        add(word)

    text = "以下是普通话的短句。"
    if words:
        text += "常见词：" + "、".join(words) + "。"
    text += "请用简体中文输出。"
    _prompt_cache[mode] = (now, text)
    return text


def prepare_audio(audio: np.ndarray) -> np.ndarray:
    """识别前把音量拉到接近满刻度，并去掉直流偏移。

    麦克风增益偏低、或者人离得远的时候，整段音频峰值可能只有几百个量化单位；
    whisper 内部的 mel 归一化救不回这么小的动态范围。实测远场 ×0.008 的场景
    能少错几个字，单个来说不惊人，但这项几乎不花时间。
    """
    if not config.ASR_NORMALIZE or audio.size == 0:
        return audio
    audio = audio - float(np.mean(audio))  # 去掉直流，避免有效动态范围被吃掉一半
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-4:
        return audio
    return (audio * (0.95 / peak)).astype(np.float32)


def _register_cuda_dlls() -> None:
    """pip 装的 nvidia-cublas/cudnn 把 DLL 放在 site-packages/nvidia/*/bin，Windows 不会自动找。

    add_dll_directory 对 ctranslate2 的加载器不生效，必须同时写进 PATH。
    """
    if sys.platform != "win32":
        return
    import site
    from pathlib import Path

    roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    bin_dirs: list[str] = []
    for root in roots:
        base = Path(root) / "nvidia"
        if base.is_dir():
            bin_dirs.extend(str(p) for p in base.glob("*/bin"))
    if not bin_dirs:
        return
    os.environ["PATH"] = os.pathsep.join(bin_dirs + [os.environ.get("PATH", "")])
    for path in bin_dirs:
        try:
            os.add_dll_directory(path)
        except OSError:
            pass


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    """numpy float32 [-1,1] -> 16bit 单声道 wav 字节，供网络接口上传。"""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return buf.getvalue()


class LocalTranscriber:
    """本地 faster-whisper。"""

    def __init__(self) -> None:
        from faster_whisper import WhisperModel

        _register_cuda_dlls()
        # 模型和精度都在下面现算：auto 模式要按设备决定，不能写死在模块加载时

        self.device = config.WHISPER_DEVICE
        self.model_size = pick_model(self.device)
        if self.device == "cuda":
            # 测试的 Samba 结果显示 medium 在 float16 下要多占 1G 显存，那部分留给模型更划算
            self.compute_type = pick_compute(self.device, self.model_size)
        else:
            # float16 只有 GPU 能用；CPU 上必须 int8，否则直接抛
            # 「target device do not support efficient float16 computation」
            self.compute_type = "int8"
        try:
            self.model = WhisperModel(
                self._source(self.model_size),
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=config.WHISPER_THREADS,
                download_root=config.MODEL_DIR,
            )
            if self.device == "cuda":
                # CUDA 的坑是「加载成功但推理时找不到 DLL」，必须真跑一次才算验证
                list(
                    self.model.transcribe(
                        np.zeros(16000, dtype=np.float32), language=config.ASR_LANGUAGE
                    )[0]
                )
        except Exception as exc:  # noqa: BLE001
            if self.device != "cuda":
                raise
            print(f"[识别] GPU 不可用（{type(exc).__name__}: {str(exc)[:80]}），回退 CPU")
            self.device, self.compute_type = "cpu", "int8"
            # medium 在 CPU 上一句要算十几秒，回退时跟着降档，别让人对着它干等
            self.model_size = pick_model("cpu")
            self.model = WhisperModel(
                self._source(self.model_size),
                device="cpu",
                compute_type="int8",
                cpu_threads=config.WHISPER_THREADS,
                download_root=config.MODEL_DIR,
            )

    @staticmethod
    def _source(model_size: str) -> str:
        """本地有就用本地那份，否则交给 faster-whisper 自己去下。"""
        from pathlib import Path

        local_dir = Path(config.MODEL_DIR) / f"faster-whisper-{model_size}"
        return str(local_dir) if (local_dir / "model.bin").exists() else model_size

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        segments, _ = self.model.transcribe(
            prepare_audio(audio),
            language=config.ASR_LANGUAGE,
            beam_size=config.ASR_BEAM_SIZE,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            # 先用 silero VAD 把非语音段切掉再解码。不开的话，环境噪声会被 whisper
            # 当成「有人说话」，输出它训练集里的字幕套话（实测「请不吝点赞 订阅 转发 打赏」
            # 反复出现），连续对话模式下就变成对着空气自说自话。
            vad_filter=True,
            # threshold 保持 silero 默认的 0.5：往下调（试过 0.35）确实能放行更小的声音，
            # 但也把空调声、键盘声放进来，whisper 会把这些噪声转成整句「字幕套话」，
            # 连续对话模式就变成对着空气自说自话 —— 这个失败模式比「小声没听清」糟得多。
            vad_parameters={
                "min_silence_duration_ms": 400,
                "speech_pad_ms": 150,
            },
            # 给个简体中文提示，能明显减少繁体/英文乱出。小模型还会额外带上
            # 本机应用名等常见词，把「打开计识本」这类错字拉回来（见 build_initial_prompt）
            initial_prompt=build_initial_prompt(self.model_size),
        )
        return "".join(seg.text for seg in segments).strip()


class RemoteTranscriber:
    """网络识别：OpenAI 兼容的 /audio/transcriptions。显卡不够或想省本地资源时用。"""

    device = "remote"

    def __init__(self) -> None:
        if not config.OPENAI_KEY:
            raise ASRError(
                "已选择网络识别，但还没填 API Key。请在配置页「大模型服务」里填写后重试。"
            )
        self.compute_type = config.ASR_REMOTE_MODEL

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        import httpx

        url = config.OPENAI_BASE.rstrip("/") + "/audio/transcriptions"
        files = {
            "file": ("speech.wav", _wav_bytes(audio, config.SAMPLE_RATE), "audio/wav"),
        }
        # 有的服务商不认 language 参数，这里只在配置了语言时才带
        data = {"model": config.ASR_REMOTE_MODEL}
        if config.ASR_LANGUAGE:
            data["language"] = config.ASR_LANGUAGE.split("-")[0]
        try:
            resp = httpx.post(
                url,
                headers=llm.auth_header(),
                files=files,
                data=data,
                timeout=httpx.Timeout(connect=8.0, read=90.0, write=30.0, pool=8.0),
                trust_env=llm.trust_env_for(url),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:200]
            raise ASRError(f"识别接口返回 {exc.response.status_code}：{body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"请求识别接口失败：{exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ASRError("识别接口返回的不是 JSON") from exc
        text = payload.get("text") or payload.get("result") or ""
        return str(text).strip()


# 兼容旧调用名
Transcriber = LocalTranscriber


def create_transcriber():
    """按当前配置造一个识别器。Web 配置页改完服务后，主程序用它重建实例。"""
    if config.ASR_PROVIDER == "openai":
        return RemoteTranscriber()
    return LocalTranscriber()
