"""一键关闭语音助手。

    python stop.py              关掉助手进程（会问要不要顺便关 Ollama）
    python stop.py --all        助手 + 桌面形象 + Ollama 全关，彻底释放显存
    python stop.py --keep-ollama  只关助手，不动 Ollama，不问
    python stop.py --dry        只列出会被关掉的进程，不动手

只结束命令行里带本项目特征的 python / pythonw 进程，不会误伤你别的 Python 程序。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console

PROJECT = Path(__file__).resolve().parent
console = Console()

# 两个进程都自己写了 pid 文件，这是最准的线索
PID_FILES = {
    "assistant.pid": "助手",
    "pet.pid": "桌面形象",
}
# 关完之后这些都得清掉，否则下次启动会以为旧进程还在
RUNTIME_FILES = ("assistant.pid", "pet.pid", "server.json")


def _powershell(script: str) -> str:
    """跑一段 PowerShell，返回标准输出。统一 UTF-8，避免中文乱码。"""
    wrapper = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapper],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]调用 PowerShell 失败：{exc}[/red]")
        return ""
    return result.stdout.strip()


def _process_name(pid: int) -> str:
    script = f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | ForEach-Object {{ $_.ProcessName }}"
    return _powershell(script).strip()


def find_project_processes() -> list[tuple[int, str]]:
    """先读 pid 文件，再扫命令行兜底。

    两个进程的启动命令都是相对路径（assistant.py / pet.py），扫不到项目绝对路径，
    所以按脚本名匹配；它们又都跑在本项目的 conda 环境里，命令行里通常还带
    voice-assistant 字样，两个条件取并集，漏杀和误杀的几率都低。
    """
    found: dict[int, str] = {}

    for name, label in PID_FILES.items():
        pid_file = PROJECT / ".runtime" / name
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            continue
        if pid and pid != os.getpid() and _process_name(pid).lower().startswith("python"):
            found[pid] = f"（{label}，来自 .runtime/{name}）"

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -and ("
        "$_.CommandLine.ToLower().Contains('voice-assistant') -or "
        "$_.CommandLine.ToLower().Contains('assistant.py') -or "
        "$_.CommandLine.ToLower().Contains('pet.py')"
        ") } | ForEach-Object { [string]$_.ProcessId + '|' + $_.CommandLine }"
    )
    for line in _powershell(script).splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_text, _, cmdline = line.partition("|")
        try:
            pid = int(pid_text.strip())
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        found.setdefault(pid, cmdline.strip())

    return sorted(found.items())


def clear_runtime() -> list[str]:
    """删掉 .runtime 里的 pid 与端口记录，避免下次启动被旧状态绊住。"""
    removed: list[str] = []
    for name in RUNTIME_FILES:
        path = PROJECT / ".runtime" / name
        try:
            if path.exists():
                path.unlink()
                removed.append(name)
        except OSError:
            continue
    return removed


def find_ollama_processes() -> list[tuple[int, str]]:
    script = (
        "Get-Process | Where-Object { $_.ProcessName -like '*ollama*' } | "
        "ForEach-Object { [string]$_.Id + '|' + $_.ProcessName }"
    )
    out = _powershell(script)
    found: list[tuple[int, str]] = []
    for line in out.splitlines():
        pid_text, _, name = line.strip().partition("|")
        try:
            found.append((int(pid_text.strip()), name or "ollama"))
        except ValueError:
            continue
    return found


def kill(pid: int) -> bool:
    result = subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def gpu_memory() -> str:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        used, total = result.stdout.strip().split(",")
        return f"{int(used)} / {int(total)} MiB"
    except Exception:  # noqa: BLE001
        return "未知"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="连 Ollama 一起关，彻底释放显存")
    parser.add_argument("--keep-ollama", action="store_true", help="只关助手，不动 Ollama")
    parser.add_argument("--dry", action="store_true", help="只列出，不实际关闭")
    args = parser.parse_args()

    assistants = find_project_processes()
    if not assistants:
        console.print("[dim]没有正在运行的助手进程[/dim]")
        if not args.dry:
            clear_runtime()
    else:
        console.print(f"找到 [bold]{len(assistants)}[/bold] 个进程：")
        for pid, cmdline in assistants:
            short = cmdline[:90] + ("…" if len(cmdline) > 90 else "")
            console.print(f"  [yellow]PID {pid}[/yellow]  {short}")

        if args.dry:
            console.print("\n[dim]--dry 模式，不执行关闭[/dim]")
            return

        ok = sum(1 for pid, _ in assistants if kill(pid))
        console.print(f"[green]已结束 {ok} 个进程（助手 / 桌面形象 / 配置页）[/green]")
        removed = clear_runtime()
        if removed:
            console.print(f"[dim]已清理：{', '.join(removed)}[/dim]")

    if args.dry:
        return

    # ---------- Ollama ----------
    if not args.keep_ollama:
        ollamas = find_ollama_processes()
        if ollamas:
            stopped = False
            if args.all:
                stopped = True
            elif sys.stdin.isatty():
                answer = input(
                    f"\n检测到 Ollama 在运行（{len(ollamas)} 个进程，占着显存约 5G）。\n"
                    "要一起关掉吗？关掉能立刻释放显存，下次启动助手需重新载入模型。[y/N] "
                ).strip().lower()
                stopped = answer in ("y", "yes", "是")
            else:
                console.print("[dim]保留了 Ollama（非交互模式）[/dim]")

            if stopped:
                for pid, _ in ollamas:
                    kill(pid)
                console.print(f"[green]已关闭 Ollama（{len(ollamas)} 个进程）[/green]")

    console.print(f"\n[dim]显卡显存：{gpu_memory()}[/dim]")
    console.print("[bold]已退出[/bold]")


if __name__ == "__main__":
    main()
