"""端到端自检：用 TTS 合成一句「用户语音」，跑通 识别 → 大模型 → 播报 全链路。

不需要麦克风，用来验证环境和性能：
    python selftest.py
    python selftest.py "帮我定个七点的闹钟" --no-audio
"""
from __future__ import annotations

import argparse
import time

import numpy as np

import config


def decode_to_16k(path: str) -> np.ndarray:
    import av

    container = av.open(path)
    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="flt", layout="mono", rate=config.SAMPLE_RATE)
    chunks = []
    for frame in container.decode(stream):
        for resampled in resampler.resample(frame):
            chunks.append(resampled.to_ndarray().reshape(-1))
    container.close()
    return np.concatenate(chunks).astype(np.float32)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default="今天天气怎么样，适合出门散步吗")
    parser.add_argument("--no-audio", action="store_true")
    args = parser.parse_args()

    import tts
    from assistant import reply_once

    print(f"[1/4] 合成用户语音：{args.text}")
    t0 = time.perf_counter()
    audio_path = tts.synthesize(args.text)
    audio = decode_to_16k(audio_path)
    print(f"      {len(audio) / config.SAMPLE_RATE:.1f}s 音频，用时 {time.perf_counter() - t0:.2f}s")

    print("[2/4] 加载识别模型")
    from asr import Transcriber

    transcriber = Transcriber()
    print(f"      device={transcriber.device} compute={transcriber.compute_type}")

    t0 = time.perf_counter()
    heard = transcriber.transcribe(audio)
    asr_cost = time.perf_counter() - t0
    print(f"[3/4] 识别（{asr_cost * 1000:.0f}ms, rtf={asr_cost / (len(audio) / config.SAMPLE_RATE):.2f}）")
    print(f"      → {heard}")
    if not heard:
        raise SystemExit("识别为空，检查音频或模型")

    speaker = None
    if not args.no_audio:
        from audio_io import Player
        from tts import Speaker

        speaker = Speaker(Player())
        speaker.warmup()

    history = [
        {"role": "system", "content": config.SYSTEM_PROMPT},
        {"role": "user", "content": heard},
    ]
    print("[4/4] 大模型回答")
    t0 = time.perf_counter()
    reply = reply_once(history, speaker)
    print(f"      生成用时 {time.perf_counter() - t0:.2f}s → {reply}")

    if speaker is not None:
        speaker.wait_done()
        print("播报完成")

    import os

    os.unlink(audio_path)
    print("\n全链路 OK")


if __name__ == "__main__":
    main()
