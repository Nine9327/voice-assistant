"""本地 Web 配置服务。

只做三件事：把当前配置和运行状态吐给页面、接收页面改配置、代页面做试听/连通性测试。
全部基于标准库，不引入 Web 框架 —— 这个项目的依赖已经够重了。

安全取舍：
* 只绑定 127.0.0.1，不对局域网暴露；
* 所有写操作要求自定义请求头 X-Voice-Assistant，
  浏览器跨站请求无法自行添加该头（会触发预检），可以挡掉 CSRF；
* 另外校验 Origin，非本机来源直接拒绝。
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import llm
import settings_store
import tts

WEBUI_DIR = Path(__file__).parent / "webui"
INDEX_FILE = WEBUI_DIR / "index.html"
RUNTIME_DIR = Path(__file__).parent / ".runtime"
SERVER_FILE = RUNTIME_DIR / "server.json"

REQUIRED_HEADER = "X-Voice-Assistant"
MAX_BODY = 64 * 1024

# 这些键变了需要重建识别器
ASR_RELOAD_KEYS = frozenset(
    {
        "ASR_PROVIDER",
        "WHISPER_MODEL",
        "WHISPER_DEVICE",
        "ASR_REMOTE_MODEL",
        "ASR_BEAM_SIZE",
        "ASR_LANGUAGE",
    }
)
# 网络识别复用大模型的地址与 Key，改动它们同样要重载
ASR_SHARED_KEYS = frozenset({"OPENAI_BASE", "OPENAI_KEY"})

# 跨进程共享的轻量缓存，避免每次刷新页面都去打网络或 COM
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, producer):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _edge_voices() -> list[dict]:
    """在线音色列表。网络不通时返回空列表而不是报错，页面照常可用。"""
    try:
        from voices import fetch_voices

        voices = _cached("edge_voices", 300.0, fetch_voices)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "name": v["ShortName"],
            "gender": "女" if v.get("Gender") == "Female" else "男",
            "locale": v.get("Locale", ""),
        }
        for v in sorted(voices, key=lambda x: x["ShortName"])
    ]


def _sapi_voices() -> list[dict]:
    try:
        return _cached("sapi_voices", 600.0, tts.sapi_voices)
    except Exception:  # noqa: BLE001
        return []


def _gpu() -> dict:
    def read() -> dict:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            used, total = (int(x) for x in out.stdout.strip().splitlines()[0].split(","))
            return {"used": used, "total": total}
        except Exception:  # noqa: BLE001
            return {}

    return _cached("gpu", 3.0, read)  # type: ignore[return-value]


def _llm_status() -> dict:
    def read() -> dict:
        try:
            models = llm.list_models()
            return {"ok": True, "models": models, "detail": f"{len(models)} 个模型可用"}
        except llm.LLMError as exc:
            return {"ok": False, "models": [], "detail": str(exc)}

    return _cached("llm_status", 5.0, read)  # type: ignore[return-value]


class _Handler(BaseHTTPRequestHandler):
    server_version = "VoiceAssistantConfig/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础设施 ----------------
    @property
    def app(self) -> "ConfigServer":
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 静音：只在启动横幅里给一条 URL
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400, **extra) -> None:
        self._json({"ok": False, "error": message, **extra}, status)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # 同源导航或非浏览器客户端
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}")

    def _guard_write(self) -> bool:
        """写操作前置校验，返回 False 表示已回错误。"""
        if not self._origin_ok():
            self._error("来源不被允许", 403)
            return False
        if not self.headers.get(REQUIRED_HEADER):
            self._error("缺少校验请求头，拒绝执行", 403)
            return False
        return True

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error("Content-Length 非法")
            return None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            self._error("请求体过大", 413)
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error("请求体不是合法 JSON")
            return None
        if not isinstance(data, dict):
            self._error("请求体必须是对象")
            return None
        return data

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/state":
            self._json(self.app.state())
        elif path == "/api/live":
            # 只回运行状态，不打显卡、不查模型列表 —— 页面可以每两秒轮询
            self._json({"runtime": self.app.live_state()})
        elif path == "/api/models":
            self._handle_models(query)
        elif path == "/api/wake/test":
            self._handle_wake_test(query)
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._error("没有这个地址", 404)

    def _serve_index(self) -> None:
        if not INDEX_FILE.exists():
            self._error("缺少 webui/index.html", 500)
            return
        body = INDEX_FILE.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    def _handle_models(self, query: dict) -> None:
        """按「页面上当前选择但尚未保存」的服务探测模型列表。"""
        values = {
            "LLM_PROVIDER": (query.get("provider") or [config.LLM_PROVIDER])[0],
            "OLLAMA_HOST": (query.get("host") or [config.OLLAMA_HOST])[0],
            "OPENAI_BASE": (query.get("base") or [config.OPENAI_BASE])[0],
            "OPENAI_KEY": (query.get("key") or [""])[0] or None,
        }
        try:
            models = llm.models_for({k: v for k, v in values.items() if v})
            self._json({"ok": True, "models": models})
        except llm.LLMError as exc:
            self._json({"ok": False, "models": [], "error": str(exc)})

    def _handle_wake_test(self, query: dict) -> None:
        """拿一句话去比对提示词，验证模糊匹配到底认不认。

        唤醒率是这个方案最玄的一环（whisper 常把「小蓝」听成「小兰」），
        页面上能直接验证，比反复对着麦克风试快得多。
        """
        text = (query.get("text") or [""])[0]
        phrase = (query.get("phrase") or [config.WAKE_PHRASE])[0] or config.WAKE_PHRASE
        try:
            import wake

            hit, rest = wake.match(text, phrase)
        except Exception as exc:  # noqa: BLE001
            self._error(f"比对失败：{exc}", 500)
            return
        self._json(
            {
                "ok": True,
                "hit": hit,
                "rest": rest.strip(),
                "phrase": phrase,
                "text": text,
            }
        )

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._guard_write():
            return

        if path == "/api/config":
            self._handle_save()
        elif path == "/api/tts/preview":
            self._handle_preview()
        elif path == "/api/llm/test":
            self._handle_llm_test()
        elif path == "/api/asr/reload":
            self._handle_asr_reload()
        elif path == "/api/pet/trigger":
            self._handle_pet_trigger()
        elif path == "/api/pet/quit":
            self._handle_quit()
        else:
            self._error("没有这个地址", 404)

    def _handle_pet_trigger(self) -> None:
        """桌面悬浮窗点一下就开一轮对话。"""
        if self.app.on_trigger is None:
            self._error("当前进程没有加载语音链路（仅配置模式）", 409)
            return
        self._json(self.app.on_trigger() or {"ok": True})

    def _handle_quit(self) -> None:
        """退出整个助手。先把响应发回去，再触发关闭 —— 反向的话对方会拿到连接重置。"""
        if self.app.on_quit is None:
            self._error("当前进程没有注册退出钩子", 409)
            return
        self._json({"ok": True})
        try:
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            pass
        threading.Timer(0.2, self.app.on_quit).start()

    def _handle_save(self) -> None:
        payload = self._read_json()
        if payload is None:
            return

        coerced: dict = {}
        errors: dict[str, str] = {}
        for key, value in payload.items():
            if key.endswith("__set"):
                continue
            # 页面回传的掩码值代表「没改」，不能拿它覆盖真实密钥
            if isinstance(value, str) and value.startswith("****"):
                continue
            try:
                coerced[key] = settings_store.coerce(key, value)
            except settings_store.ConfigError as exc:
                errors[key] = str(exc)
        if errors:
            self._error("配置未保存：" + "；".join(errors.values()), 400, fields=errors)
            return

        # 重载识别服务可能失败，先留一份改动前的识别相关配置用于回退
        asr_snapshot = {
            key: getattr(config, key)
            for key in ASR_RELOAD_KEYS | ASR_SHARED_KEYS
            if hasattr(config, key)
        }

        changed = settings_store.apply(vars(config), coerced)
        try:
            settings_store.save(coerced)
        except OSError as exc:
            self._error(f"已生效但写入 settings.json 失败：{exc}", 500, changed=changed)
            return

        needs_restart = [k for k in changed if settings_store.SCHEMA[k].get("restart")]
        asr_result = self._maybe_reload_asr(changed, asr_snapshot)

        # 唤醒开关 / 提示词改了要立刻生效：主循环正卡在等触发，
        # 不主动叫它一声的话，得等下一轮对话结束才会重新读配置
        wake_result = None
        if self.app.on_config_changed is not None:
            try:
                wake_result = self.app.on_config_changed(changed)
            except Exception as exc:  # noqa: BLE001
                wake_result = {"error": f"{type(exc).__name__}: {exc}"}

        _cache.pop("llm_status", None)  # 服务地址/模型变了，状态必须重查
        self._json(
            {
                "ok": True,
                "changed": changed,
                "needs_restart": needs_restart,
                "asr": asr_result,
                "wake": wake_result,
                "config": settings_store.snapshot(config),
            }
        )

    def _maybe_reload_asr(self, changed: list[str], snapshot: dict) -> dict | None:
        """识别相关配置变了就重建识别器；失败则把配置退回改动前。"""
        if self.app.on_asr_reload is None:
            return None

        touched = set(changed)
        need = bool(touched & ASR_RELOAD_KEYS)
        if not need and config.ASR_PROVIDER == "openai" and touched & ASR_SHARED_KEYS:
            need = True  # 网络识别复用了大模型的地址和 Key
        if not need:
            return None

        result = self.app.on_asr_reload()
        if result and not result.get("ok"):
            settings_store.apply(vars(config), snapshot)
            try:
                settings_store.save(snapshot)
            except OSError:
                pass
            result["reverted"] = True
        return result

    def _handle_preview(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        text = str(payload.get("text") or "你好，我是你的语音助手。").strip()[:200]
        if not text:
            self._error("试听文本为空")
            return
        try:
            path = tts.synthesize(
                text,
                voice=payload.get("voice") or None,
                rate=payload.get("rate") or None,
                engine=payload.get("engine") or None,
            )
        except Exception as exc:  # noqa: BLE001
            self._error(f"合成失败：{exc}", 500)
            return
        try:
            data = Path(path).read_bytes()
            ctype = mimetypes.guess_type(path)[0] or "audio/mpeg"
        finally:
            Path(path).unlink(missing_ok=True)
        self._send(200, data, ctype)

    def _handle_llm_test(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        result = llm.test_connection(payload)
        _cache.pop("llm_status", None)
        self._json(result)

    def _handle_asr_reload(self) -> None:
        if self.app.on_asr_reload is None:
            self._error("当前进程没有加载识别模型", 409)
            return
        self._json(self.app.on_asr_reload())


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, app):
        self.app = app
        super().__init__(addr, handler)


class ConfigServer:
    """配置页服务。start() 后阻塞在后台线程，调用方继续跑语音主循环。"""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        on_asr_reload=None,
        get_runtime=None,
        on_trigger=None,
        on_quit=None,
        on_config_changed=None,
    ) -> None:
        self.host = host or config.WEB_HOST
        self.port = int(port or config.WEB_PORT)
        self.on_asr_reload = on_asr_reload
        self.get_runtime = get_runtime
        self.on_trigger = on_trigger
        self.on_quit = on_quit
        self.on_config_changed = on_config_changed
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self._started_at = time.time()

    # ---------------- 生命周期 ----------------
    def start(self, ports_to_try: int = 10) -> int:
        last_error: Exception | None = None
        for offset in range(ports_to_try):
            port = self.port + offset
            try:
                self._httpd = _Server((self.host, port), _Handler, self)
                self.port = port
                break
            except OSError as exc:  # 端口被占用就顺延
                last_error = exc
                continue
        if self._httpd is None:
            raise RuntimeError(f"{self.port} 起连续 {ports_to_try} 个端口都被占用：{last_error}")

        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._write_server_file()
        return self.port

    def _write_server_file(self) -> None:
        """把实际端口写到 .runtime/server.json —— 端口顺延后悬浮窗靠它找过来。"""
        try:
            RUNTIME_DIR.mkdir(exist_ok=True)
            SERVER_FILE.write_text(
                json.dumps(
                    {"pid": os.getpid(), "port": self.port, "url": self.url},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        try:
            SERVER_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def open_browser(self) -> None:
        try:
            webbrowser.open(self.url)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 状态汇总 ----------------
    def live_state(self) -> dict:
        """轻量运行状态：不查显卡、不查模型列表，页面可以每两秒轮询。"""
        out: dict = {
            "pid": os.getpid(),
            "port": self.port,
            "uptime": round(time.time() - self._started_at),
        }
        if self.get_runtime is not None:
            try:
                out.update(self.get_runtime() or {})
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"运行状态读取失败：{exc}"
        return out

    def state(self) -> dict:
        runtime = {
            "pid": os.getpid(),
            "port": self.port,
            "uptime": round(time.time() - self._started_at),
            "llm": _llm_status(),
            "gpu": _gpu(),
            "settings_file": str(settings_store.PATH),
            "settings_exists": settings_store.PATH.exists(),
        }
        if self.get_runtime is not None:
            try:
                runtime.update(self.get_runtime() or {})
            except Exception as exc:  # noqa: BLE001
                runtime["error"] = f"运行状态读取失败：{exc}"
        return {
            "config": settings_store.snapshot(config),
            "schema": settings_store.schema_for_client(),
            "runtime": runtime,
            "voices": _edge_voices(),
            "sapi_voices": _sapi_voices(),
        }


def main() -> None:
    """单独启动配置页（不开麦克风、不加载模型）。"""
    import argparse

    parser = argparse.ArgumentParser(description="启动 Web 配置页")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ConfigServer(port=args.port)
    port = server.start()
    print(f"配置页已启动：http://{config.WEB_HOST}:{port}")
    if not args.no_browser:
        server.open_browser()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("\n已关闭")


if __name__ == "__main__":
    main()
