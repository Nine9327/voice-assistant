"""大模型服务：本地 Ollama 或任意 OpenAI 兼容接口，统一成流式输出。

服务切换靠 config.LLM_PROVIDER：
    ollama  本地自建，数据不出机器，需要显卡
    openai  OpenAI / DeepSeek / 硅基流动 / vLLM 等任意兼容端点，走网络

两个分支共用同一个 stream_reply()，上层（assistant.py）完全无感。
所有参数都在调用时读 config，所以 Web 配置页改完立刻生效，不用重启。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx

import config

# 语音场景不需要长回答，读超时给足即可；连接超时要短，快速暴露配置错误
TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


class LLMError(RuntimeError):
    pass


def _is_loopback(url: str) -> bool:
    host = httpx.URL(url).host or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def trust_env_for(url: str) -> bool:
    """本机地址一律绕开 HTTP_PROXY。

    开着代理（尤其国内常见的全局代理）时，若不绕开，请求 127.0.0.1:11434
    会被送到代理服务器，直接 502 —— 表现为「Ollama 明明开着却连不上」。
    云端接口则要保留代理设置，否则可能根本出不去。
    """
    return not _is_loopback(url)


def _get(url: str, timeout: float, headers: dict | None = None) -> httpx.Response:
    return httpx.get(url, timeout=timeout, headers=headers, trust_env=trust_env_for(url))


def _stream(method: str, url: str, **kwargs):
    return httpx.stream(method, url, trust_env=trust_env_for(url), **kwargs)


# --------------------------------------------------------------------------
# 模型列表（供 Web 配置页下拉框使用）
# --------------------------------------------------------------------------
def list_ollama_models(host: str | None = None, timeout: float = 3.0) -> list[str]:
    base = (host or config.OLLAMA_HOST).rstrip("/")
    try:
        resp = _get(f"{base}/api/tags", timeout)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"连不上 Ollama（{base}），请确认服务已启动：{exc}") from exc
    return sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))


def list_openai_models(
    base: str | None = None, key: str | None = None, timeout: float = 8.0
) -> list[str]:
    url = (base or config.OPENAI_BASE).rstrip("/") + "/models"
    headers = auth_header(key)
    try:
        resp = _get(url, timeout, headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LLMError(f"接口返回 {exc.response.status_code}：{exc.response.text[:200]}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"连不上 {url}：{exc}") from exc
    data = resp.json().get("data")
    if not isinstance(data, list):
        raise LLMError("接口返回格式不是 OpenAI 兼容结构")
    return sorted(str(m.get("id", "")) for m in data if m.get("id"))


def list_models(provider: str | None = None) -> list[str]:
    """按当前（或指定）服务列出可用模型。"""
    if (provider or config.LLM_PROVIDER) == "openai":
        return list_openai_models()
    return list_ollama_models()


def models_for(values: dict) -> list[str]:
    """用「还没保存」的一份配置探测模型列表，方便页面上先看再切。"""
    provider = values.get("LLM_PROVIDER", config.LLM_PROVIDER)
    if provider == "openai":
        return list_openai_models(
            base=values.get("OPENAI_BASE") or config.OPENAI_BASE,
            key=values.get("OPENAI_KEY") or config.OPENAI_KEY,
        )
    return list_ollama_models(host=values.get("OLLAMA_HOST") or config.OLLAMA_HOST)


def auth_header(key: str | None = None) -> dict[str, str]:
    token = key if key is not None else config.OPENAI_KEY
    return {"Authorization": f"Bearer {token}"} if token else {}


# --------------------------------------------------------------------------
# Ollama 生命周期
# --------------------------------------------------------------------------
def _find_ollama_exe() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.exists():
            return str(candidate)
    return None


def _find_ollama_app() -> str | None:
    """桌面版托盘程序。必须优先用它启动。"""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None
    candidate = Path(local) / "Programs" / "Ollama" / "ollama app.exe"
    return str(candidate) if candidate.exists() else None


def _models_dir_from_env() -> str | None:
    """OLLAMA_MODELS 可能只写在注册表里，当前进程看不到，这里补读一次。"""
    value = os.environ.get("OLLAMA_MODELS")
    if value:
        return value
    if sys.platform != "win32":
        return None
    try:
        import winreg

        locations = [
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ),
        ]
        for root, path in locations:
            try:
                with winreg.OpenKey(root, path) as key:
                    found, _ = winreg.QueryValueEx(key, "OLLAMA_MODELS")
                    if found:
                        return str(found)
            except OSError:
                continue
    except ImportError:
        return None
    return None


def ensure_running(timeout: float = 30.0) -> bool:
    """Ollama 没起来就拉起来。返回是否本次由我们启动。仅 ollama 服务下有意义。

    stop.py --all 会关掉 Ollama 释放显存，下次启动助手时靠这个自动恢复。

    优先拉起桌面端的 `ollama app.exe` 而不是 `ollama serve`：
    桌面版会带上用户在图形界面里配置的模型目录（本机就是 D:\\AI\\ollamamodels）。
    裸跑 serve 只认默认的 ~/.ollama/models，那里往往是空的，
    结果就是「服务起来了但一个模型都没有」——很难排查。
    """
    if config.LLM_PROVIDER != "ollama":
        return False
    try:
        list_ollama_models(timeout=2.0)
        return False
    except LLMError:
        pass

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    app = _find_ollama_app()
    if app:
        # 桌面应用自己管理后台进程，不弹控制台窗口
        subprocess.Popen([app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        exe = _find_ollama_exe()
        if not exe:
            raise LLMError("找不到 ollama.exe，请确认 Ollama 已安装并在 PATH 中")
        env = dict(os.environ)
        models_dir = _models_dir_from_env()
        if models_dir:
            env["OLLAMA_MODELS"] = models_dir
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            env=env,
        )

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            list_ollama_models(timeout=1.5)
            return True
        except LLMError:
            time.sleep(0.8)
    raise LLMError(f"已尝试启动 Ollama，但 {timeout:.0f} 秒内没起来")


def ensure_model() -> None:
    """启动前确认模型可用，给出能直接照抄的修复命令。"""
    if config.LLM_PROVIDER == "openai":
        if not config.OPENAI_KEY:
            raise LLMError("已选择 OpenAI 兼容服务，但还没填 API Key。请在配置页填写。")
        try:
            models = list_openai_models(timeout=8.0)
        except LLMError:
            return  # 有的自建端点不开放 /models，不因此拦住启动
        if models and config.OPENAI_MODEL not in models:
            raise LLMError(
                f"接口上没有模型 {config.OPENAI_MODEL}。可用：{models[:12]}"
            )
        return

    models = list_ollama_models()
    if config.LLM_MODEL in models:
        return
    base = config.LLM_MODEL.split(":")[0]
    close = [m for m in models if m.startswith(base)]
    raise LLMError(
        f"没找到模型 {config.LLM_MODEL}。已安装：{models or '无'}。\n"
        f"请执行：ollama pull {config.LLM_MODEL}"
        + (f"\n（检测到相近模型：{close}，可在配置页切换）" if close else "")
    )


# --------------------------------------------------------------------------
# 对话
# --------------------------------------------------------------------------
_WEEK = "一二三四五六日"


def now_note() -> str:
    """把真实时间告诉模型。

    7B 模型不知道今天是几号，问「现在几点」「今天周几」它会拿训练数据里的日期瞎猜。
    本地能答的事就别去联网，所以这条一直在。

    刻意放在消息列表**末尾**而不是第一条 system 里：Ollama 的 KV cache 按前缀复用，
    时间每轮都变，塞在开头等于每轮都让缓存失效，首字延迟直接从 0.1 秒涨到 0.5 秒以上。
    """
    stamp = time.localtime()
    return (
        f"（当前真实时间：{stamp.tm_year}年{stamp.tm_mon}月{stamp.tm_mday}日 "
        f"星期{_WEEK[stamp.tm_wday]} {stamp.tm_hour:02d}:{stamp.tm_min:02d}。"
        "问到时间、日期、星期几，直接按这个回答。）"
    )


def _compose(history: list[dict], extra_context: str = "") -> list[dict]:
    """在最后一条 user 之前插入时间说明和（可选的）联网资料。

    不写回 history：搜索结果是「这一轮用过的证据」，留在历史里会让模型
    下一轮继续引用已经不相关的旧网页。
    """
    msgs = list(history)
    extras: list[dict] = [{"role": "system", "content": now_note()}]
    if extra_context:
        extras.append({"role": "system", "content": extra_context})
    if msgs and msgs[-1].get("role") == "user":
        return msgs[:-1] + extras + [msgs[-1]]
    return msgs + extras


def stream_reply(history: list[dict], extra_context: str = "") -> Iterator[str]:
    """逐 token 产出模型输出。按当前服务自动分发。"""
    messages = _compose(history, extra_context)
    if config.LLM_PROVIDER == "openai":
        yield from _stream_openai(messages)
    else:
        yield from _stream_ollama(messages)


def _stream_ollama(history: list[dict]) -> Iterator[str]:
    base = config.OLLAMA_HOST.rstrip("/")
    payload = {
        "model": config.LLM_MODEL,
        "messages": history,
        "stream": True,
        "options": {
            "temperature": config.LLM_TEMPERATURE,
            "num_ctx": config.LLM_NUM_CTX,
        },
    }
    try:
        with _stream("POST", f"{base}/api/chat", json=payload, timeout=TIMEOUT) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "ignore")[:300]
                raise LLMError(f"Ollama 返回 {resp.status_code}：{body}")
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("error"):
                    raise LLMError(str(data["error"]))
                content = (data.get("message") or {}).get("content") or ""
                if content:
                    yield content
                if data.get("done"):
                    break
    except httpx.HTTPError as exc:
        raise LLMError(f"请求大模型失败：{exc}") from exc


def _stream_openai(history: list[dict]) -> Iterator[str]:
    url = config.OPENAI_BASE.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.OPENAI_MODEL,
        "messages": history,
        "stream": True,
        "temperature": config.LLM_TEMPERATURE,
    }
    try:
        with _stream(
            "POST", url, json=payload, headers=auth_header(), timeout=TIMEOUT
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "ignore")[:300]
                raise LLMError(f"接口返回 {resp.status_code}：{body}")
            for line in resp.iter_lines():
                # SSE 格式：空行分隔、以 data: 开头、以 [DONE] 结束
                if not line or not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if data_text == "[DONE]":
                    break
                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data.get("error"), dict):
                    raise LLMError(str(data["error"].get("message") or data["error"]))
                choices = data.get("choices") or []
                if not choices:
                    continue
                content = (choices[0].get("delta") or {}).get("content") or ""
                if content:
                    yield content
    except httpx.HTTPError as exc:
        raise LLMError(f"请求大模型失败：{exc}") from exc


# --------------------------------------------------------------------------
# 连通性自检（配置页的「测试」按钮）
# --------------------------------------------------------------------------
def test_connection(values: dict | None = None) -> dict:
    """发一句最短的请求，返回 {ok, detail, ms}。不改动任何 config。"""
    values = values or {}
    provider = values.get("LLM_PROVIDER", config.LLM_PROVIDER)
    t0 = time.perf_counter()
    try:
        if provider == "openai":
            models = list_openai_models(
                base=values.get("OPENAI_BASE") or config.OPENAI_BASE,
                key=values.get("OPENAI_KEY") or config.OPENAI_KEY,
            )
            detail = f"连接正常，可见 {len(models)} 个模型"
        else:
            models = list_ollama_models(host=values.get("OLLAMA_HOST") or config.OLLAMA_HOST)
            detail = f"连接正常，已装 {len(models)} 个模型"
        return {
            "ok": True,
            "detail": detail,
            "models": models,
            "ms": round((time.perf_counter() - t0) * 1000),
        }
    except LLMError as exc:
        return {"ok": False, "detail": str(exc), "ms": round((time.perf_counter() - t0) * 1000)}
