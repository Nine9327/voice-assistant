"""语音识别：本地 faster-whisper 或网络识别接口，按 config.ASR_PROVIDER 切换。

* local  —— faster-whisper，优先 GPU，显存或 CUDA 库不可用时自动回退 CPU
* openai —— 把音频直接 POST 到 OpenAI 兼容的 /audio/transcriptions（复用 OPENAI_BASE / OPENAI_KEY）

两者对外接口一致：device / compute_type / transcribe(audio) -> str。
"""
from __future__ import annotations

import io
import os
import sys
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
        # 模型目录要在实例里算，这样切换模型（Web 配置页）后新建实例就能用上新模型
        from pathlib import Path

        local_dir = Path(config.MODEL_DIR) / f"faster-whisper-{config.WHISPER_MODEL}"
        source = str(local_dir) if (local_dir / "model.bin").exists() else config.WHISPER_MODEL

        self.device = config.WHISPER_DEVICE
        # float16 只有 GPU 能用；CPU 上必须 int8，否则直接抛
        # 「target device do not support efficient float16 computation」
        self.compute_type = config.WHISPER_COMPUTE if self.device == "cuda" else "int8"
        try:
            self.model = WhisperModel(
                source,
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
            self.model = WhisperModel(
                source,
                device="cpu",
                compute_type="int8",
                cpu_threads=config.WHISPER_THREADS,
                download_root=config.MODEL_DIR,
            )

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        segments, _ = self.model.transcribe(
            audio,
            language=config.ASR_LANGUAGE,
            beam_size=config.ASR_BEAM_SIZE,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            # 给个简体中文提示，能明显减少繁体/英文乱出
            initial_prompt="以下是普通话的句子。",
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
