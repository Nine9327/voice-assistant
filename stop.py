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
import time
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

    扫描只认「命令行里带本项目根目录路径」的进程：虚拟环境在项目的 .venv\\ 里
    （跳板解释器路径必含项目根），启动脚本又都传绝对脚本路径（真身命令行也含）。
    机器上别的 Python 程序 —— 哪怕脚本也叫 assistant.py / pet.py，或者 TTS、
    Ollama 这类外部服务 —— 一概不碰。

    uv 虚拟环境里的 Scripts\\python(w).exe 是「跳板」：它拉起真实解释器当子进程
    再等它退出。于是每个助手 / 形象实际占两个 PID，扫描会把跳板和真身都列进来
    （真身死后跳板会跟着退出，两个都在名单上反而稳妥）。但 stop.py 自己也是经
    项目环境启动的 —— 只按 os.getpid() 排除会漏掉自己的跳板（命令行同样带项目
    根路径），而杀掉跳板会连坐整个 Job 里的子进程，也就是把 stop.py 自己当场
    杀掉，名单上剩下的进程就一个都轮不到了。实测助手就这样逃过了一次 stop-all，
    继续占着麦克风和端口。所以：命令行带 stop.py 的（自己或自己的跳板）一律跳过。
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

    root = str(PROJECT).lower()
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine.ToLower().Contains('{root}') }} | "
        "ForEach-Object { [string]$_.ProcessId + '|' + $_.CommandLine }"
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
        # 自己的跳板：命令行里带 stop.py 的只可能是它和真身，见 docstring
        if "stop.py" in cmdline.lower():
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
    """taskkill /F /T：连同子进程一起结束（MCP 的 stdio 服务器就是助手的子进程）。"""
    result = subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def wait_dead(pid: int, tries: int = 4, interval: float = 0.8) -> bool:
    """确认进程真的从进程表里退场了。

    taskkill 报成功不代表瞬间消失（跳板还要等子进程的退出码才能退），
    只看返回码就宣布「已结束」，等于把「杀没杀干净」这个问题留给下次启动。
    """
    for _ in range(tries):
        time.sleep(interval)
        if not _process_name(pid):
            return True
    return False


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
    parser.add_argument("--all", action="store_true", help="关掉助手、形象和配置页（不动 Ollama）")
    parser.add_argument("--with-ollama", action="store_true",
                        help="连本地模型一起关，彻底释放显存")
    parser.add_argument("--keep-ollama", action="store_true", help="（旧参数，现在默认就不动 Ollama）")
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

        dead = 0
        stuck: list[tuple[int, str]] = []
        for pid, cmdline in assistants:
            # 不看 taskkill 的返回码：跳板先被杀时真身会随 Job 连坐死掉，
            # 轮到它时 taskkill 报「找不到进程」——那其实是成功。进程表说了算。
            kill(pid)
            if wait_dead(pid):
                dead += 1
                continue
            kill(pid)  # 偶发失败给一次重试
            if wait_dead(pid, tries=2) or not _process_name(pid):
                dead += 1
                continue
            stuck.append((pid, cmdline))
        console.print(f"[green]已结束 {dead} 个进程（助手 / 桌面形象 / 配置页）[/green]")
        for pid, cmdline in stuck:
            if "scripts\\python" in cmdline.lower():
                # 跳板残壳：真身已由它自己的 PID 单独负责杀掉并验证过，
                # 杀不掉的这个不占麦克风也不占端口，重启后自然消失
                console.print(f"[dim]PID {pid}（虚拟环境跳板）没能结束，无害，可忽略[/dim]")
            else:
                console.print(f"[red]PID {pid} 没能结束，还在占用麦克风或端口：[/red]")
                console.print(f"  [yellow]{cmdline[:90]}{'…' if len(cmdline) > 90 else ''}[/yellow]")
                console.print("[red]请在任务管理器里手动结束它，再重新启动助手[/red]")
        removed = clear_runtime()
        if removed:
            console.print(f"[dim]已清理：{', '.join(removed)}[/dim]")

    if args.dry:
        return

    # ---------- Ollama ----------
    # 本地模型是用户自己的服务：默认不碰它，要释放显存得显式说 --with-ollama
    if args.with_ollama:
        ollamas = find_ollama_processes()
        if ollamas:
            for pid, _ in ollamas:
                kill(pid)
            console.print(f"[green]已关闭 Ollama（{len(ollamas)} 个进程）[/green]")
        else:
            console.print("[dim]Ollama 本来就没在运行[/dim]")
    else:
        ollamas = find_ollama_processes()
        if ollamas:
            console.print(
                "[dim]保留了 Ollama（本地模型由你自己管；要一起关加 --with-ollama）[/dim]"
            )

    console.print(f"\n[dim]显卡显存：{gpu_memory()}[/dim]")
    console.print("[bold]已退出[/bold]")


if __name__ == "__main__":
    main()
