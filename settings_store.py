"""运行时配置的持久化层。

设计核心：**config 模块是唯一的「活配置」**。
各业务模块（llm / tts / asr）都是「调用时才读 config.XXX」，所以在运行期
直接 setattr 到 config 模块，改动立刻生效，不需要重启。

本模块只做三件事：
1. 把 config 里的默认值落盘成 settings.json（保存）
2. 启动时把 settings.json 的值覆盖回 config（加载）
3. 校验与类型强转，挡住 Web 页面传来的脏数据

刻意不在模块顶层 import config —— config.py 会反过来 import 本模块，
放在顶层会形成循环导入。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
PATH = ROOT / "settings.json"

_lock = threading.Lock()

# 可改、可持久化的键。type 决定 web 端控件与校验方式。
# restart=True 表示改动后必须重启进程才生效。
SCHEMA: dict[str, dict[str, Any]] = {
    # ---------- 大模型服务 ----------
    "LLM_PROVIDER": {
        "type": "choice", "choices": ["ollama", "openai"],
        "label": "模型服务", "group": "llm", "restart": False,
    },
    "OLLAMA_HOST": {"type": "str", "label": "Ollama 地址", "group": "llm"},
    "LLM_MODEL": {"type": "str", "label": "本地模型", "group": "llm"},
    "LLM_TEMPERATURE": {
        "type": "float", "min": 0.0, "max": 2.0, "step": 0.1,
        "label": "温度", "group": "llm",
    },
    "LLM_NUM_CTX": {
        "type": "int", "min": 1024, "max": 32768, "step": 512,
        "label": "上下文长度", "group": "llm",
    },
    "MAX_HISTORY_TURNS": {
        "type": "int", "min": 0, "max": 30, "label": "记忆轮数", "group": "llm",
    },
    "OPENAI_BASE": {"type": "str", "label": "接口地址", "group": "openai"},
    "OPENAI_KEY": {"type": "secret", "label": "API Key", "group": "openai"},
    "OPENAI_MODEL": {"type": "str", "label": "模型名称", "group": "openai"},

    # ---------- 语音合成服务 ----------
    "TTS_ENGINE": {
        "type": "choice", "choices": ["edge", "sapi"],
        "label": "合成服务", "group": "tts",
    },
    "TTS_VOICE": {"type": "str", "label": "在线音色", "group": "tts"},
    "TTS_RATE": {"type": "str", "label": "语速", "group": "tts"},
    "TTS_VOLUME": {"type": "str", "label": "音量", "group": "tts"},
    "TTS_PITCH": {"type": "str", "label": "音调", "group": "tts"},
    "SAPI_VOICE": {"type": "str", "label": "离线声音", "group": "tts"},

    # ---------- 语音识别服务 ----------
    "ASR_PROVIDER": {
        "type": "choice", "choices": ["local", "openai"],
        "label": "识别服务", "group": "asr",
    },
    "ASR_REMOTE_MODEL": {"type": "str", "label": "网络识别模型", "group": "asr"},
    "WHISPER_MODEL": {
        "type": "choice", "choices": ["tiny", "base", "small", "medium"],
        "label": "识别模型", "group": "asr",
    },
    "WHISPER_DEVICE": {
        "type": "choice", "choices": ["cuda", "cpu"],
        "label": "运行设备", "group": "asr",
    },
    "ASR_BEAM_SIZE": {
        "type": "int", "min": 1, "max": 5, "label": "解码宽度", "group": "asr",
    },
    "ASR_LANGUAGE": {"type": "str", "label": "识别语言", "group": "asr"},

    # ---------- 交互 ----------
    "MIN_RECORD_SECONDS": {
        "type": "float", "min": 0.1, "max": 3.0, "step": 0.1,
        "label": "最短录音", "group": "misc",
    },
    "MAX_RECORD_SECONDS": {
        "type": "int", "min": 5, "max": 120, "label": "最长录音", "group": "misc",
    },
    "WEB_PORT": {
        "type": "int", "min": 1024, "max": 65535,
        "label": "配置页端口", "group": "misc", "restart": True,
    },

    # ---------- 唤醒词 ----------
    "WAKE_ENABLED": {"type": "bool", "label": "常开唤醒", "group": "wake"},
    "WAKE_PHRASE": {"type": "str", "label": "唤醒提示词", "group": "wake"},
    "WAKE_SENSITIVITY": {
        "type": "float", "min": 0.0, "max": 1.0, "step": 0.05,
        "label": "灵敏度", "group": "wake",
    },
    "WAKE_SILENCE": {
        "type": "float", "min": 0.3, "max": 3.0, "step": 0.1,
        "label": "断句静音", "group": "wake",
    },
    "WAKE_SESSION_SECONDS": {
        "type": "int", "min": 0, "max": 300,
        "label": "会话保持", "group": "wake",
    },

    # ---------- 联网搜索 ----------
    "SEARCH_MODE": {
        "type": "choice", "choices": ["off", "auto", "always"],
        "label": "联网方式", "group": "search",
    },
    "SEARCH_BACKEND": {
        "type": "choice", "choices": ["bing", "tavily", "bocha", "searx"],
        "label": "搜索服务", "group": "search",
    },
    "SEARCH_KEY": {"type": "secret", "label": "API Key", "group": "search"},
    "SEARCH_ENDPOINT": {"type": "str", "label": "SearXNG 地址", "group": "search"},
    "SEARCH_RESULTS": {
        "type": "int", "min": 1, "max": 10, "label": "结果条数", "group": "search",
    },
    "SEARCH_TIMEOUT": {
        "type": "float", "min": 2.0, "max": 30.0, "step": 1.0,
        "label": "搜索超时", "group": "search",
    },

    # ---------- 本机指令 ----------
    "APPS_ENABLED": {"type": "bool", "label": "语音打开应用", "group": "apps"},

    # ---------- MCP 工具 ----------
    "MCP_ENABLED": {"type": "bool", "label": "启用 MCP 工具", "group": "mcp"},
    "MCP_MAX_ROUNDS": {
        "type": "int", "min": 0, "max": 6, "label": "最多调用轮数", "group": "mcp",
    },
    "MCP_TIMEOUT": {
        "type": "float", "min": 3.0, "max": 120.0, "step": 1.0,
        "label": "工具超时", "group": "mcp",
    },
    "MCP_MAX_TOOLS": {
        "type": "int", "min": 1, "max": 64, "label": "工具上限", "group": "mcp",
    },
}

SECRET_KEYS = {k for k, v in SCHEMA.items() if v.get("type") == "secret"}


class ConfigError(ValueError):
    """配置值非法。"""


def coerce(key: str, value: Any) -> Any:
    """按 SCHEMA 校验并强转单个值。非法则抛 ConfigError。"""
    spec = SCHEMA.get(key)
    if spec is None:
        raise ConfigError(f"未知配置项：{key}")

    kind = spec["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "on", "yes"):
            return True
        if text in ("0", "false", "off", "no", ""):
            return False
        raise ConfigError(f"{key} 需要布尔值，收到 {value!r}")
    if kind == "choice":
        text = str(value)
        if text not in spec["choices"]:
            raise ConfigError(f"{key} 只能是 {spec['choices']} 之一，收到 {text!r}")
        return text
    if kind in ("str", "secret"):
        text = "" if value is None else str(value)
        text = text.strip()
        if spec.get("label") and len(text) > 2048:
            raise ConfigError(f"{key} 太长")
        return text
    if kind == "int":
        try:
            num = int(float(value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} 需要整数，收到 {value!r}") from exc
    elif kind == "float":
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key} 需要数字，收到 {value!r}") from exc
    else:  # pragma: no cover
        raise ConfigError(f"{key} 类型未知：{kind}")

    low, high = spec.get("min"), spec.get("max")
    if low is not None and num < low:
        raise ConfigError(f"{key} 不能小于 {low}")
    if high is not None and num > high:
        raise ConfigError(f"{key} 不能大于 {high}")
    return num


def load() -> dict[str, Any]:
    """读 settings.json。文件不存在或损坏都返回空字典（用默认值继续跑）。"""
    if not PATH.exists():
        return {}
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}

    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in SCHEMA:
            continue
        try:
            clean[key] = coerce(key, value)
        except ConfigError:
            continue  # 单项非法不该连累整份配置
    return clean


def save(values: dict[str, Any]) -> dict[str, Any]:
    """把值合并进 settings.json 并落盘，返回最终写入的完整内容。"""
    with _lock:
        current = load()
        current.update(values)
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, PATH)  # 原子替换，避免写一半断电留下坏文件
        return current


def apply(namespace: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """把值注入到目标命名空间（通常是 config 的 globals()），返回真正变化的键。"""
    changed: list[str] = []
    for key, value in values.items():
        if key not in SCHEMA:
            continue
        if namespace.get(key) != value:
            namespace[key] = value
            changed.append(key)
    return changed


def snapshot(config_module) -> dict[str, Any]:
    """读取 config 模块当前值，密钥做掩码。"""
    out: dict[str, Any] = {}
    for key in SCHEMA:
        value = getattr(config_module, key, None)
        if value is None:
            continue
        if key in SECRET_KEYS:
            text = str(value)
            out[key] = "" if not text else "****" + text[-4:]
            out[f"{key}__set"] = bool(text)
            continue
        out[key] = value
    return out


def schema_for_client() -> dict[str, Any]:
    """给前端用的元信息（不含内部实现细节）。"""
    return {k: dict(v) for k, v in SCHEMA.items()}
