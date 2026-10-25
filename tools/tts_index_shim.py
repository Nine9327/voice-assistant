"""把 IndexTTS-2.5 / 2.0 包成 OpenAI 兼容的 /v1/audio/speech。

**用部署 IndexTTS 的那个环境运行**（不是语音助手的虚拟环境）：

    # 在 index-tts 仓库根目录下
    uv run python D:/project/voice-assistant/tools/tts_index_shim.py

然后语音助手 →「语音合成」→ 选「自建服务」→ 地址填 http://127.0.0.1:9880/v1

参考音色：把 wav 放进 voices/ 目录，文件名就是音色名。
助手那边「音色」填这个名（默认找 voices/default.wav）。

为什么要这一层：官方只提供 Gradio 界面和 Python API，没有 HTTP 服务；
而语音助手需要的只是「给文本、还音频」这一件事。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- 模型

_tts = None
_is_25 = False
_load_lock = threading.Lock()
_infer_lock = threading.Lock()   # 模型不是线程安全的，一次只跑一句
ARGS: argparse.Namespace


def load_model():
    """懒加载。第一次请求才载入，避免开机就把显存占了。"""
    global _tts, _is_25
    if _tts is not None:
        return _tts
    with _load_lock:
        if _tts is not None:
            return _tts
        model_dir = str(Path(ARGS.model_dir).resolve())
        cfg = str(Path(ARGS.cfg).resolve()) if ARGS.cfg else str(Path(model_dir) / "config.yaml")
        try:
            from indextts.infer_v2_5 import IndexTTS2   # 2.5
            _is_25 = True
            kwargs = {"cfg_path": cfg, "model_dir": model_dir, "use_bf16": not ARGS.no_half}
        except ImportError:
            from indextts.infer_v2 import IndexTTS2     # 2.0
            _is_25 = False
            kwargs = {"cfg_path": cfg, "model_dir": model_dir, "use_fp16": not ARGS.no_half}
        if ARGS.compile:
            # s2mel 是耗时大头（扩散 25 步），torch.compile 能明显提速。
            # Windows 上要先装 triton-windows；没装或编译失败会自动退回去，不影响使用。
            kwargs["use_torch_compile"] = True
        print(f"[shim] 载入 IndexTTS-{'2.5' if _is_25 else '2.0'}"
              f"（{model_dir}{'，torch.compile' if ARGS.compile else ''}）…", flush=True)
        t0 = time.perf_counter()
        _tts = IndexTTS2(**kwargs)
        print(f"[shim] 模型就绪，用了 {time.perf_counter() - t0:.1f}s", flush=True)
        return _tts


def voice_path(name: str) -> Path:
    name = (name or ARGS.default_voice or "default").strip()
    cand = Path(ARGS.voices) / (name if name.lower().endswith(".wav") else name + ".wav")
    if cand.is_file():
        return cand
    fallback = Path(ARGS.voices) / f"{ARGS.default_voice}.wav"
    if fallback.is_file():
        print(f"[shim] 没找到音色 {name}，改用 {fallback.name}", flush=True)
        return fallback
    raise FileNotFoundError(f"{cand} 不存在，也没有 {fallback.name}")


def synth(text: str, voice: str, speed: float) -> bytes:
    with _infer_lock:
        tts = load_model()
        ref = voice_path(voice)
        fd, out = tempfile.mkstemp(suffix=".wav", prefix="ix_")
        import os
        os.close(fd)
        kwargs = {
            "spk_audio_prompt": str(ref),
            "text": text,
            "output_path": out,
        }
        if _is_25:
            kwargs["lang"] = ARGS.lang
        if speed and abs(speed - 1.0) > 0.02:
            # OpenAI 的 speed >1 是更快；IndexTTS 的 duration_factor >1 是更慢
            kwargs["duration_factor"] = max(0.5, min(2.0, 1.0 / speed))
        t0 = time.perf_counter()
        tts.infer(**kwargs)
        data = Path(out).read_bytes()
        Path(out).unlink(missing_ok=True)
        dur = len(data) / (2 * 22050) if data else 0
        print(f"[shim] {time.perf_counter() - t0:.2f}s 合成 {len(text)} 字"
              f"（约 {dur:.1f}s 音频，RTF {((time.perf_counter() - t0) / dur) if dur else 0:.2f}）",
              flush=True)
        return data


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "IndexTTSShim/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [
                {"id": "index-tts", "object": "model", "owned_by": "local"}]})
        elif self.path.rstrip("/") in ("", "/", "/health"):
            self._json(200, {"ok": True, "engine": "IndexTTS-2.5" if _is_25 else "IndexTTS",
                             "loaded": _tts is not None,
                             "voices": sorted(p.stem for p in Path(ARGS.voices).glob("*.wav"))})
        else:
            self._json(404, {"error": {"message": f"没有这个路径：{self.path}"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/audio/speech"):
            self._json(404, {"error": {"message": f"没有这个路径：{self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": {"message": f"请求体不是合法 JSON：{exc}"}})
            return

        text = str(payload.get("input") or "").strip()
        if not text:
            self._json(400, {"error": {"message": "input 不能为空"}})
            return
        # 助手是一句一句发过来的，太长的句子反而更慢且容易爆显存
        if len(text) > ARGS.max_chars:
            text = text[: ARGS.max_chars]

        try:
            audio = synth(text, str(payload.get("voice") or ""), float(payload.get("speed") or 1.0))
        except FileNotFoundError as exc:
            self._json(400, {"error": {"message": str(exc)}})
            return
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": {"message": f"{type(exc).__name__}: {exc}"}})
            return

        ctype = "audio/wav"
        if str(payload.get("response_format") or "wav").lower() == "mp3":
            ctype = "audio/wav"   # 直接给 wav 也能播，省一次转码
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def main() -> None:
    global ARGS
    parser = argparse.ArgumentParser(description="IndexTTS → OpenAI 兼容 /v1/audio/speech")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-dir", default="checkpoints", help="IndexTTS 的模型目录")
    parser.add_argument("--cfg", default="", help="默认 <model-dir>/config.yaml")
    parser.add_argument("--voices", default="voices", help="参考音频目录，文件名即音色名")
    parser.add_argument("--default-voice", default="default", help="voices/ 下的默认音色")
    parser.add_argument("--lang", default="ZH", help="2.5 专有：ZH/EN/JA/ES/AR")
    parser.add_argument("--max-chars", type=int, default=120, help="单句最长字数")
    parser.add_argument("--no-half", action="store_true", help="关掉 fp16/bf16（更慢更占显存）")
    parser.add_argument("--compile", action="store_true",
                        help="开 torch.compile（实测：2.5 上会让 GPT 自回归阶段报错，别开）")
    parser.add_argument("--preload", action="store_true", help="启动就加载模型（默认懒加载）")
    ARGS = parser.parse_args()

    voices = Path(ARGS.voices)
    if not voices.is_dir():
        voices.mkdir(parents=True, exist_ok=True)
        print(f"[shim] 已创建 {voices}，把参考音频（10 秒左右、干净无杂音）放进去，"
              f"文件名就是音色名，例如 {voices / 'default.wav'}", flush=True)

    if ARGS.preload:
        try:
            load_model()
        except Exception as exc:  # noqa: BLE001
            print(f"[shim] 预加载失败：{exc}", flush=True)
            raise SystemExit(1)

    server = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print(f"[shim] 监听 http://{ARGS.host}:{ARGS.port}/v1/audio/speech", flush=True)
    print(f"[shim] 语音助手配置页填：http://{ARGS.host}:{ARGS.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[shim] 退出", flush=True)


if __name__ == "__main__":
    sys.exit(main())
