"""音色试听工具。

    python voices.py                      列出所有中文音色
    python voices.py zh-CN-YunxiNeural    试听指定音色
    python voices.py --demo               依次试听全部（每条播放一遍）
    python voices.py --demo -1            反复试听当前 config 里的音色（调语速时用）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import config
import tts

DEMO_TEXT = "你好，我是你的语音助手。今天天气不错，有什么可以帮你的吗？"

RECOMMEND = {
    "zh-CN-XiaoxiaoNeural": "标准女声，最自然（当前默认）",
    "zh-CN-XiaoyiNeural": "年轻女声，活泼一点",
    "zh-CN-YunxiNeural": "年轻男声，温和",
    "zh-CN-YunjianNeural": "成熟男声，解说风",
    "zh-CN-YunyangNeural": "新闻播报男声，偏正式",
    "zh-CN-YunxiaNeural": "少年音",
    "zh-CN-liaoning-XiaobeiNeural": "东北话",
    "zh-CN-shaanxi-XiaoniNeural": "陕西话",
}

LOCALE_NAME = {
    "zh-CN": "普通话",
    "zh-CN-liaoning": "东北话",
    "zh-CN-shaanxi": "陕西话",
    "zh-HK": "粤语·中国香港",
    "zh-TW": "国语·中国台湾",
}


def fetch_voices() -> list[dict]:
    import edge_tts

    voices = asyncio.run(edge_tts.list_voices())
    return [v for v in voices if v["Locale"].startswith("zh")]


def show(voices: list[dict]) -> None:
    print(f"\n共 {len(voices)} 个中文音色，当前使用：{config.TTS_VOICE}\n")
    current = config.TTS_VOICE
    for v in sorted(voices, key=lambda x: (x["Locale"], x["ShortName"])):
        mark = "→" if v["ShortName"] == current else " "
        note = RECOMMEND.get(v["ShortName"], "")
        gender = "女" if v["Gender"] == "Female" else "男"
        locale = LOCALE_NAME.get(v["Locale"], v["Locale"])
        print(f"{mark} {v['ShortName']:32} {gender}  {locale:14} {note}")
    print("\n试听：python voices.py <音色名>")
    print("选好后把 config.py 里的 TTS_VOICE 改成它。\n")


def play_voice(player, voice: str, text: str = DEMO_TEXT) -> None:
    path = tts.synthesize(text, voice=voice)
    try:
        print(f"  ▶ {voice}")
        player.play_file(Path(path))
    finally:
        player.unload()
        Path(path).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("voice", nargs="?", help="音色短名，如 zh-CN-YunxiNeural")
    parser.add_argument("--demo", action="store_true", help="依次试听全部中文音色")
    args = parser.parse_args()

    from audio_io import Player

    if args.voice == "-1":
        play_voice(Player(), config.TTS_VOICE)
        return

    if args.voice:
        play_voice(Player(), args.voice)
        return

    voices = fetch_voices()
    if not args.demo:
        show(voices)
        return

    player = Player()
    for v in sorted(voices, key=lambda x: (x["Locale"], x["ShortName"])):
        try:
            play_voice(player, v["ShortName"])
        except KeyboardInterrupt:
            print("\n已中断")
            sys.exit(0)


if __name__ == "__main__":
    main()
