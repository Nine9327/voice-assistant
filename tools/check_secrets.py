"""提交前的敏感信息检查。

为什么需要它：`.gitignore` 只能挡住「已知的」文件，挡不住「把 key 贴进
config.py 里试一下」这种手滑；而这些内容一旦 push 出去，改代码也删不掉
（历史里还在，爬虫几秒就抓到）。所以加一道提交前的硬检查。

两道防线：
1. **文件名**：settings.json / *.bak / .env / 私钥 / .runtime 一律拒绝入库；
2. **文件内容**：各类 key、Token、私钥、密码赋值、以及本机用户名和邮箱。

用法：
    python tools/check_secrets.py             # 检查工作区（tracked + 未被忽略的新文件）
    python tools/check_secrets.py --staged    # 只检查暂存区（pre-commit 钩子用）
    python tools/check_secrets.py --all       # 只检查已跟踪文件（相当于「这次 push 会带出去什么」）

退出码 1 表示发现问题，钩子会阻止提交。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# 文件名规则：直接拒绝
# --------------------------------------------------------------------------
BLOCKED_PATHS = [
    (re.compile(r"(^|/)settings\.json$"), "本地配置文件（含 API Key）"),
    (re.compile(r"(^|/)settings\.json\..+$"), "本地配置的临时/备份文件"),
    (re.compile(r"(^|/)mcp\.json$"), "MCP 服务器清单（可能含 token）"),
    (re.compile(r"\.bak$"), "备份文件"),
    (re.compile(r"(^|/)\.env(\..+)?$"), "环境变量文件"),
    (re.compile(r"(^|/)\.runtime/"), "运行期数据（pid / 日志 / 截图）"),
    (re.compile(r"(^|/)models/"), "模型权重（体积大，不入库）"),
    (re.compile(r"\.(pem|key|pfx|p12|jks)$"), "证书或私钥"),
    (re.compile(r"\.(log|pid)$"), "日志或进程号文件"),
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)"), "SSH 私钥"),
]

# --------------------------------------------------------------------------
# 内容规则：命中即视为泄露
# --------------------------------------------------------------------------
SECRET_RULES = [
    ("OpenAI / DeepSeek 风格密钥", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Anthropic 密钥", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("AWS AccessKey", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("私钥内容", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("带 Bearer 的 Token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "密钥赋值",
        re.compile(
            r"(?i)\b(openai_key|api[_-]?key|apikey|access[_-]?key|secret[_-]?key|"
            r"client[_-]?secret|auth[_-]?token|access[_-]?token)\b\s*[:=]\s*[\"']([^\"'\s]{6,})[\"']"
        ),
    ),
    (
        "可疑的长凭据赋值",
        re.compile(
            r"(?i)\b[a-z0-9_]*(key|token|secret|password|passwd)"
            r"\s*[:=]\s*[\"']([A-Za-z0-9_\-]{16,}\d[A-Za-z0-9_\-]*)[\"']"
        ),
    ),
]

# 明显的占位符，不算泄露
PLACEHOLDERS = {
    "", "xxx", "xxxx", "your-key", "your_api_key", "changeme", "none", "null",
    "redacted", "placeholder", "sk-xxx", "sk-xxxx", "your-key-here",
}

# 内容告警（不阻止提交，但会提醒）
WARN_RULES = [
    ("本机 Windows 用户名", re.compile(r"[A-Za-z]:\\Users\\(?!Public\b|Default\b|username\b)[^\\\s\"']{2,}")),
    (
        "个人邮箱",
        re.compile(r"[A-Za-z0-9._%+-]+@(?!.*users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]
WARN_EMAIL_ALLOW = ("example.com", "example.org", "localhost", "noreply.github.com")

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".html", ".css", ".js", ".bat", ".cmd",
    ".vbs", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".gitignore",
    ".gitattributes", ".gitmodules",
}
MAX_BYTES = 2 * 1024 * 1024


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} 失败")
    return result.stdout


def staged_paths() -> list[str]:
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [line for line in out.splitlines() if line.strip()]


def worktree_paths() -> list[str]:
    """已跟踪 + 新增但未被忽略的文件。"""
    out = git("ls-files", "--cached", "--others", "--exclude-standard")
    return sorted({line for line in out.splitlines() if line.strip()})


def read_staged(path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f":{path}"], cwd=ROOT, capture_output=True
    )
    return result.stdout if result.returncode == 0 else None


def read_worktree(path: str) -> bytes | None:
    try:
        return (ROOT / path).read_bytes()
    except OSError:
        return None


def is_text(data: bytes) -> bool:
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def check_text(path: str, text: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        # 同一行只报一条：一行里的 key 往往会被 2~3 条规则同时命中，重复刷屏没意义
        hit_this_line = False
        for name, pattern in SECRET_RULES:
            if hit_this_line:
                break
            for found in pattern.finditer(line):
                value = found.group(0)
                tail = found.groups()[-1] if found.groups() else value
                if str(tail).strip().lower() in PLACEHOLDERS:
                    continue
                errors.append(f"{path}:{lineno}  {name}：{_mask(value)}")
                hit_this_line = True
                break

        if hit_this_line:
            continue  # 已经确认是密钥的行，不用再提醒个人信息

        for name, pattern in WARN_RULES:
            for found in pattern.finditer(line):
                hit = found.group(0)
                if name == "个人邮箱" and any(d in hit for d in WARN_EMAIL_ALLOW):
                    continue
                warnings.append(f"{path}:{lineno}  {name}：{_mask(hit)}")
                break
    return errors, warnings


def _mask(value: str, keep: int = 6) -> str:
    value = value.strip()
    if len(value) <= keep:
        return value
    return value[:keep] + "…" + value[-2:]


def main() -> int:
    parser = argparse.ArgumentParser(description="敏感信息检查")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="只检查暂存区（提交钩子用）")
    mode.add_argument("--all", action="store_true", help="只检查已跟踪文件")
    args = parser.parse_args()

    if args.staged:
        paths = staged_paths()
        reader = read_staged
        scope = "暂存区"
    elif args.all:
        paths = [p for p in git("ls-files").splitlines() if p.strip()]
        reader = read_worktree
        scope = "已跟踪文件"
    else:
        paths = worktree_paths()
        reader = read_worktree
        scope = "工作区"

    errors: list[str] = []
    warnings: list[str] = []

    for path in paths:
        for pattern, reason in BLOCKED_PATHS:
            if pattern.search(path.replace("\\", "/")):
                errors.append(f"{path}  ← {reason}，不该入库")
                break
        else:
            # 文件名没问题才看内容
            suffix = Path(path).suffix.lower()
            if suffix and suffix not in TEXT_SUFFIXES and Path(path).name not in (
                ".gitignore", ".gitattributes", ".gitmodules"
            ):
                continue
            data = reader(path)
            if not data or len(data) > MAX_BYTES or not is_text(data):
                continue
            file_errors, file_warnings = check_text(path, data.decode("utf-8", "ignore"))
            errors.extend(file_errors)
            warnings.extend(file_warnings)

    if warnings:
        print(f"\n[提醒] {scope}里有 {len(warnings)} 处可能涉及个人信息（不阻止提交）：")
        for item in warnings:
            print(f"  · {item}")

    if errors:
        print(f"\n[拦截] {scope}里发现 {len(errors)} 处敏感信息，已阻止提交：")
        for item in errors:
            print(f"  ✗ {item}")
        print(
            "\n处理办法：\n"
            "  1. 密钥这类东西请写到 settings.json（已被 .gitignore 忽略）或环境变量里；\n"
            "  2. 已经 add 进暂存区的，用 git restore --staged <文件> 撤出来；\n"
            "  3. 确认是误报，可在 tools/check_secrets.py 里把规则加进白名单。\n"
        )
        return 1

    print(f"[通过] {scope}共检查 {len(paths)} 个文件，未发现敏感信息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
