"""统一工作区文件的换行符。

这个项目已经因为换行符踩过两次坑：
* `.bat` 存成 LF，cmd.exe 解析时把跨行命令粘成一团，报 `'dp0"' 不是内部或外部命令`；
* 把已经是 CRLF 的文件再转一次，变成 `\\r\\r\\n`，git 直接判定成二进制。

所以单独留一个工具，一次性把工作区修正到目标状态，而不是每次手写脚本。

规则：
* `.bat` / `.cmd` / `.vbs` → CRLF，且必须是纯 ASCII（cmd 在 chcp 生效前就按旧代码页解析字节）
* 其余文本文件 → LF
* 二进制（png/jpg/...）→ 不碰

用法：
    python tools/normalize_eol.py            # 修正
    python tools/normalize_eol.py --check    # 只报告，不改
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CRLF_SUFFIXES = {".bat", ".cmd", ".vbs"}
LF_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".html", ".css", ".js", ".sh", ".yml",
    ".yaml", ".toml", ".ini", ".cfg", ".gitignore", ".gitattributes",
}
SKIP_DIRS = {".git", ".runtime", "models", "__pycache__", ".venv", "packages", ".workbuddy"}


def _wanted(path: Path) -> str | None:
    name = path.name
    if name in (".gitignore", ".gitattributes", ".gitmodules"):
        return "lf"
    suffix = path.suffix.lower()
    if suffix in CRLF_SUFFIXES:
        return "crlf"
    if suffix in LF_SUFFIXES:
        return "lf"
    if path.parent.name == ".githooks":  # 钩子必须是 LF，否则 sh 直接跳过
        return "lf"
    return None


def normalize(path: Path, check: bool) -> str | None:
    want = _wanted(path)
    if want is None:
        return None
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    # 先把所有 \r\n / \r\r\n / 裸 \r 统一成 \n，再按目标写回
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if want == "crlf":
        if not raw.isascii():
            # 换成 CRLF 前先拒绝：非 ASCII 的 bat 在 cmd 里必然乱码
            return f"{path.relative_to(ROOT)}  含非 ASCII 字符，必须改成英文"
        data = text.replace("\n", "\r\n").encode("ascii")
        expected = lambda b: b.count(b"\r\r") == 0 and b.replace(b"\r\n", b"").count(b"\n") == 0  # noqa: E731
    else:
        data = text.encode("utf-8")
        expected = lambda b: b.count(b"\r") == 0  # noqa: E731

    if expected(raw):
        return None
    if not check:
        path.write_bytes(data)
    return f"{path.relative_to(ROOT)}  -> {want.upper()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查不修改")
    args = parser.parse_args()

    changed: list[str] = []
    problems: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts[:-1]):
            continue
        result = normalize(path, args.check)
        if result is None:
            continue
        if "非 ASCII" in result:
            problems.append(result)
        else:
            changed.append(result)

    for line in changed:
        print(("需要修：" if args.check else "已修正：") + line)
    for line in problems:
        print("[错误] " + line)

    if not changed and not problems:
        print("换行符全部符合规范")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
