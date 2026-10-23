"""MCP（Model Context Protocol）客户端 —— 把外部工具接进语音助手。

为什么自己写协议层，不用官方 SDK：
官方 `mcp` 包会额外拖进 pydantic、anyio、sse-starlette 一整串依赖，
而语音场景真正需要的只有三个方法：initialize / tools/list / tools/call。
这里用标准库 + 已有的 httpx 实现，两种传输都覆盖：
    stdio  本地子进程，npx / uvx / python 脚本都行（最常见）
    http   远程流式 HTTP（旧的 sse 传输已并入这一种）

安全边界：
* 配置文件 mcp.json 里可能有 token，已在 .gitignore 中排除；
* 只执行用户在配置页里明确写下的命令，绝不按语音指令动态生成命令；
* 工具返回值截断后再进模型，防止一次返回把 4K 上下文撑爆。

线程模型：每个服务器一条 stdout 读线程；工具调用在调用方线程同步等待。
"""

from __future__ import annotations

import collections
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

import config

ROOT = Path(__file__).parent

# 依次尝试的协议版本：新的优先，老服务器不认就退回
PROTOCOLS = ("2025-06-18", "2024-11-05")
CONNECT_TIMEOUT = 25.0     # 拉起子进程 + 列工具，冷启动时 npx 下载包可能慢
RESULT_LIMIT = 1600        # 单次工具结果最多塞给模型多少字
CLIENT_INFO = {"name": "voice-assistant", "version": "1.0"}


class McpError(RuntimeError):
    """MCP 交互失败（连接、协议、超时）。"""


# --------------------------------------------------------------------------
# 配置文件
# --------------------------------------------------------------------------
def config_path() -> Path:
    raw = str(getattr(config, "MCP_CONFIG_FILE", "mcp.json") or "mcp.json")
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _str_map(value: Any) -> dict[str, str]:
    """把 {"K": "V"} 或 ["K=V"] 或 "K=V\\nK2=V2" 统一成字典。"""
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if str(k).strip()}
    items: list[str] = []
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    out: dict[str, str] = {}
    for line in items:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("=", ":"):
            if sep in line:
                key, _, val = line.partition(sep)
                if key.strip():
                    out[key.strip()] = val.strip()
                break
    return out


def _arg_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        # 命令行参数一般不含空格引号嵌套，按空白切已经够用
        return [a for a in value.split() if a]
    return [str(v) for v in value]


def normalize(name: str, raw: dict) -> dict:
    """把页面/文件里的条目补全成内部结构。缺字段不报错，留给连接时报。"""
    kind = str(raw.get("type") or raw.get("transport") or "").strip().lower()
    if kind in ("sse", "streamable-http", "streamable_http", "http-sse", "https"):
        kind = "http"
    if kind not in ("stdio", "http"):
        # 没写或写错就按有没有 command 猜
        kind = "stdio" if str(raw.get("command") or "").strip() else "http"

    out: dict[str, Any] = {
        "name": name,
        "type": kind,
        "enabled": bool(raw.get("enabled", True)),
    }
    if kind == "stdio":
        out["command"] = str(raw.get("command") or "").strip()
        out["args"] = _arg_list(raw.get("args"))
        out["env"] = _str_map(raw.get("env"))
        out["cwd"] = str(raw.get("cwd") or "").strip()
    else:
        out["url"] = str(raw.get("url") or "").strip()
        out["headers"] = _str_map(raw.get("headers"))
    return out


def load_servers() -> list[dict]:
    """读 mcp.json。文件不存在返回空列表 —— 没配 MCP 是常态。"""
    path = config_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpError(f"读不了 {path.name}：{exc}") from exc
    raw = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return []
    out: list[dict] = []
    for name, item in raw.items():
        if not isinstance(item, dict):
            continue
        out.append(normalize(str(name), item))
    return out


def _to_file(item: dict) -> dict:
    """内部结构 → 磁盘格式。保持与常见 MCP 客户端一致，方便直接抄别人的配置。"""
    out: dict[str, Any] = {"type": item["type"], "enabled": item["enabled"]}
    if item["type"] == "stdio":
        out["command"] = item["command"]
        out["args"] = item["args"]
        if item.get("env"):
            out["env"] = item["env"]
        if item.get("cwd"):
            out["cwd"] = item["cwd"]
    else:
        out["url"] = item["url"]
        if item.get("headers"):
            out["headers"] = item["headers"]
    return out


def save_servers(items: list[dict]) -> list[dict]:
    """整份覆盖写入。返回落盘后的规范结构，抛 McpError 表示校验不通过。"""
    clean: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise McpError("服务器条目格式不对")
        name = str(item.get("name") or "").strip()
        if not name:
            raise McpError("服务器名称不能为空")
        if name in seen:
            raise McpError(f"服务器名称重复：{name}")
        seen.add(name)
        entry = normalize(name, item)
        if entry["type"] == "stdio":
            if not entry["command"]:
                raise McpError(f"「{name}」缺少启动命令")
        elif not entry["url"]:
            raise McpError(f"「{name}」缺少地址")
        clean.append(entry)

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mcpServers": {s["name"]: _to_file(s) for s in clean}}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # 原子替换，写一半断电不会留下坏文件
    return clean


# --------------------------------------------------------------------------
# 传输层：stdio
# --------------------------------------------------------------------------
class _StdioTransport:
    """行分隔 JSON-RPC over 子进程 stdin/stdout。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.name = cfg["name"]
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue[dict] = queue.Queue()
        self._id = 0
        self._lock = threading.Lock()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)
        self._exited = False

    # ---- 生命周期 ----
    def start(self) -> None:
        command = self.cfg.get("command") or ""
        if not command:
            raise McpError("没有配置启动命令")
        exe = shutil.which(command) or command
        argv = [exe, *(self.cfg.get("args") or [])]
        # Windows 上 npx 实际是 npx.cmd，CreateProcess 不能直接跑批处理，得经 cmd /c
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]

        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        cwd = self.cfg.get("cwd") or None
        if cwd and not Path(cwd).is_dir():
            cwd = None

        try:
            self._proc = subprocess.Popen(  # noqa: S603 - 命令由用户在本机配置页填写
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"启动失败：{exc}") from exc

        threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 有的服务器会往 stdout 打日志，忽略
                if isinstance(msg, dict) and msg.get("id") is not None and (
                    "result" in msg or "error" in msg
                ):
                    self._q.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._exited = True

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = line.strip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            pass

    def _exit_note(self) -> str:
        code = None
        if self._proc is not None:
            code = self._proc.poll()
        tail = " | ".join(list(self._stderr)[-3:])
        bits = [f"进程已退出（code={code}）"] if code is not None else ["进程没有响应"]
        if tail:
            bits.append(tail)
        return "，".join(bits)

    def _send(self, payload: dict) -> None:
        with self._lock:
            if self._proc is None or self._proc.stdin is None:
                raise McpError(self._exit_note())
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise McpError(f"写入失败：{exc}") from exc

    # ---- 协议 ----
    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise McpError(f"{method} 超时（{timeout:.0f}s）")
            try:
                msg = self._q.get(timeout=min(remain, 0.5))
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    raise McpError(self._exit_note()) from None
                continue
            if msg.get("id") != rid:
                continue  # 迟到的旧响应，丢掉
            err = msg.get("error")
            if err:
                if isinstance(err, dict):
                    raise McpError(str(err.get("message") or err))
                raise McpError(str(err))
            result = msg.get("result")
            return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # EOF，正规服务器会自己退出
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass


# --------------------------------------------------------------------------
# 传输层：流式 HTTP
# --------------------------------------------------------------------------
class _HttpTransport:
    """POST JSON-RPC；响应可能是 JSON，也可能是 SSE 流。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.name = cfg["name"]
        self.url = cfg.get("url") or ""
        self._headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self._session: str | None = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.url:
            raise McpError("没有配置地址")
        if not self.url.lower().startswith(("http://", "https://")):
            raise McpError("地址需要以 http:// 或 https:// 开头")

    def _headers_now(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    @staticmethod
    def _trust_env(url: str) -> bool:
        host = httpx.URL(url).host or ""
        return host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0")

    def _post(self, payload: dict, timeout: float):
        try:
            return httpx.post(
                self.url,
                json=payload,
                headers=self._headers_now(),
                timeout=timeout,
                trust_env=self._trust_env(self.url),
            )
        except httpx.HTTPError as exc:
            raise McpError(f"连不上 {self.url}：{exc}") from exc

    @staticmethod
    def _from_sse(text: str) -> dict | None:
        """从 SSE 文本里挑出 JSON-RPC 响应（跳过 notifications）。"""
        found: dict | None = None
        for block in text.split("\n\n"):
            data_lines = [
                line[5:].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            raw = "\n".join(data_lines)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                found = msg
        return found

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params

        resp = self._post(payload, timeout)
        session = resp.headers.get("Mcp-Session-Id")
        if session:
            self._session = session
        if resp.status_code >= 400:
            raise McpError(f"接口返回 {resp.status_code}：{resp.text[:200]}")

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ctype:
            msg = self._from_sse(resp.text)
            if msg is None:
                raise McpError(f"{method} 没有拿到响应内容")
        else:
            try:
                msg = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise McpError(f"{method} 返回的不是 JSON：{resp.text[:160]}") from exc

        err = msg.get("error")
        if err:
            if isinstance(err, dict):
                raise McpError(str(err.get("message") or err))
            raise McpError(str(err))
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload, 10.0)
        session = resp.headers.get("Mcp-Session-Id")
        if session:
            self._session = session

    def close(self) -> None:
        self._session = None


def _make_transport(cfg: dict):
    return _StdioTransport(cfg) if cfg["type"] == "stdio" else _HttpTransport(cfg)


# --------------------------------------------------------------------------
# 单个服务器的运行状态
# --------------------------------------------------------------------------
def tool_id(server: str, tool: str) -> str:
    """模型只接受 [A-Za-z0-9_-] 的函数名，服务器名里的中文/空格要洗掉。"""
    raw = f"{server}__{tool}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]


class _State:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.name = cfg["name"]
        self.transport = None
        self.tools: list[dict] = []
        self.error = ""
        self.ok = False
        self.ms = 0

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001
                pass
        self.transport = None
        self.ok = False

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "type": self.cfg["type"],
            "ok": self.ok,
            "error": self.error,
            "ms": self.ms,
            "tools": [
                {"name": t["name"], "description": t["description"]} for t in self.tools
            ],
        }


def _connect(state: _State, timeout: float = CONNECT_TIMEOUT) -> None:
    """握手 + 列工具。失败抛 McpError，state.error 由调用方写。"""
    cfg = state.cfg
    if cfg["type"] == "stdio" and not cfg.get("command"):
        raise McpError("没有配置启动命令")
    if cfg["type"] == "http" and not cfg.get("url"):
        raise McpError("没有配置地址")

    transport = _make_transport(cfg)
    transport.start()
    state.transport = transport

    init: dict | None = None
    last: McpError | None = None
    for proto in PROTOCOLS:
        try:
            init = transport.request(
                "initialize",
                {"protocolVersion": proto, "capabilities": {}, "clientInfo": CLIENT_INFO},
                timeout=timeout,
            )
            break
        except McpError as exc:
            last = exc
            if "protocol" not in str(exc).lower() and "version" not in str(exc).lower():
                raise
    if init is None:
        raise last or McpError("初始化失败")

    transport.notify("notifications/initialized")
    data = transport.request("tools/list", {}, timeout=timeout)
    tools: list[dict] = []
    for item in data.get("tools") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        tools.append(
            {
                "name": str(item["name"]),
                "description": str(item.get("description") or item["name"])[:300],
                "schema": schema,
            }
        )
    state.tools = tools


def test_server(cfg: dict) -> dict:
    """配置页的「测试」：连一下、列出工具、然后立刻关掉进程。"""
    name = str(cfg.get("name") or "测试").strip() or "测试"
    item = normalize(name, cfg)
    state = _State(item)
    t0 = time.perf_counter()
    try:
        _connect(state)
        state.ok = True
    except McpError as exc:
        state.error = str(exc)
    except Exception as exc:  # noqa: BLE001
        state.error = f"{type(exc).__name__}: {exc}"
    finally:
        state.ms = round((time.perf_counter() - t0) * 1000)
        out = state.snapshot()
        state.close()
    return {"ok": out["ok"] or False, "error": out["error"], "tools": out["tools"], "ms": out["ms"]}


# --------------------------------------------------------------------------
# 多服务器管理器
# --------------------------------------------------------------------------
class McpHub:
    """全局唯一的 MCP 管理器。

    网页、主循环、命令行都通过 hub() 拿同一个实例，
    所以配置页里点了「重连」，语音端下一轮就能看到新工具。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self._states: dict[str, _State] = {}
        self._index: dict[str, tuple[_State, str]] = {}
        self._started = False
        self._connecting = False
        self._last_error = ""

    # ---- 生命周期 ----
    def ensure_started(self) -> None:
        """启动时调一次。后台线程连接，绝不阻塞开机。"""
        if not getattr(config, "MCP_ENABLED", True):
            return
        with self._lock:
            if self._started or self._connecting:
                return
            self._connecting = True
        threading.Thread(target=self._boot, name="mcp-boot", daemon=True).start()

    def _boot(self) -> None:
        try:
            self.reload()
        finally:
            with self._lock:
                self._connecting = False
                self._started = True

    def reload(self) -> None:
        """按磁盘上的配置全部重连。配置页保存后调用。

        同一时刻只允许一个重连在进行：页面上连点两下「重连」会把每个服务器
        启动两遍，多出来的进程没人收。
        """
        if not self._reload_lock.acquire(blocking=False):
            return
        try:
            self._reload()
        finally:
            self._reload_lock.release()

    def _reload(self) -> None:
        self.stop_all()
        if not getattr(config, "MCP_ENABLED", True):
            return
        try:
            servers = load_servers()
        except McpError as exc:
            with self._lock:
                self._last_error = str(exc)
            return

        for cfg in servers:
            if not cfg.get("enabled", True):
                continue
            state = _State(cfg)
            t0 = time.perf_counter()
            try:
                _connect(state)
                state.ok = True
            except McpError as exc:
                state.error = str(exc)
            except Exception as exc:  # noqa: BLE001
                state.error = f"{type(exc).__name__}: {exc}"
            state.ms = round((time.perf_counter() - t0) * 1000)
            if not state.ok:
                state.close()
            with self._lock:
                self._states[state.name] = state
                self._rebuild_index()

    def stop_all(self) -> None:
        with self._lock:
            states = list(self._states.values())
            self._states.clear()
            self._index.clear()
        for state in states:
            state.close()

    def _rebuild_index(self) -> None:
        """把「工具 id → (服务器, 原始工具名)」重建一遍。调用方需持锁。"""
        index: dict[str, tuple[_State, str]] = {}
        budget = max(1, int(getattr(config, "MCP_MAX_TOOLS", 24) or 24))
        for state in self._states.values():
            if not state.ok:
                continue
            for tool in state.tools:
                if len(index) >= budget:
                    return
                index[tool_id(state.name, tool["name"])] = (state, tool["name"])
        self._index = index

    # ---- 查询 ----
    def enabled(self) -> bool:
        return bool(getattr(config, "MCP_ENABLED", True))

    def ready(self) -> bool:
        with self._lock:
            return bool(self._index)

    def tool_specs(self) -> list[dict]:
        """转成两家的工具声明格式（Ollama 与 OpenAI 这里一致）。"""
        with self._lock:
            states = [s for s in self._states.values() if s.ok]
        specs: list[dict] = []
        budget = max(1, int(getattr(config, "MCP_MAX_TOOLS", 24) or 24))
        for state in states:
            for tool in state.tools:
                if len(specs) >= budget:
                    return specs
                specs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool_id(state.name, tool["name"]),
                            "description": tool["description"],
                            "parameters": tool["schema"],
                        },
                    }
                )
        return specs

    def status(self) -> list[dict]:
        with self._lock:
            return [s.snapshot() for s in self._states.values()]

    def summary(self) -> dict:
        """给配置页和状态条用的一行摘要。"""
        with self._lock:
            states = list(self._states.values())
            err = self._last_error
            # 用索引长度而不是原始工具数：这才是真正塞进提示词的数量
            usable = len(self._index)
        ok = [s for s in states if s.ok]
        return {
            "enabled": self.enabled(),
            "servers": len(states),
            "connected": len(ok),
            "tools": usable,
            "tool_cap": int(getattr(config, "MCP_MAX_TOOLS", 24) or 24),
            "error": err,
        }

    # ---- 调用 ----
    def call(self, name: str, arguments: dict | None = None) -> str:
        """执行一个工具，返回给模型看的纯文本结果。"""
        with self._lock:
            entry = self._index.get(name)
            timeout = float(getattr(config, "MCP_TIMEOUT", 20) or 20)
        if entry is None:
            return f"没有名为 {name} 的工具，请换一个，或者直接回答用户"
        state, tool_name = entry
        if state.transport is None:
            return f"{state.name} 已经断开，请提示用户重新连接"
        try:
            result = state.transport.request(
                "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
                timeout=timeout,
            )
        except McpError as exc:
            return f"调用 {tool_name} 失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            return f"调用 {tool_name} 出错：{type(exc).__name__}: {exc}"
        return _flatten(result)


def _flatten(result: dict) -> str:
    """MCP 的 content 是分块数组，取文本；图片在语音场景没意义，只留占位。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(str(res.get("text") or res.get("uri") or "[资源]"))
        elif kind == "image":
            parts.append("[图片，语音场景不展示]")
        else:
            parts.append(f"[{kind}]")
    text = "\n".join(p.strip() for p in parts if p.strip()).strip()
    if not text:
        text = "（工具没有返回内容）"
    if result.get("isError"):
        text = "工具执行报错：" + text
    if len(text) > RESULT_LIMIT:
        text = text[:RESULT_LIMIT] + "…（已截断）"
    return text


# --------------------------------------------------------------------------
# 单例
# --------------------------------------------------------------------------
_HUB: McpHub | None = None
_HUB_LOCK = threading.Lock()


def hub() -> McpHub:
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = McpHub()
        return _HUB


def presets() -> list[dict]:
    """配置页的常见服务器示例。填的是官方/社区包名，不预置任何密钥。"""
    return [
        {
            "name": "filesystem",
            "type": "stdio",
            "enabled": True,
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\workspace"],
            "env": {},
        },
        {
            "name": "fetch",
            "type": "stdio",
            "enabled": True,
            "command": "uvx",
            "args": ["mcp-server-fetch"],
            "env": {},
        },
        {
            "name": "memory",
            "type": "stdio",
            "enabled": False,
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
            "env": {},
        },
        {
            "name": "remote",
            "type": "http",
            "enabled": False,
            "url": "http://127.0.0.1:8000/mcp",
            "headers": {},
        },
    ]
