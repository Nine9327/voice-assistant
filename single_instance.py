"""单实例保护：同一时间只让一个助手 / 一个桌面形象在跑。

为什么不用「pid 文件 + 进程是否还活着」：
    进程号会被复用。pet 被强杀时来不及删掉 pet.pid，那个号码后来分给了
    assistant.py，于是 pet 每次启动都读到「自己还活着」，直接退出 ——
    桌面形象就这么消失了，而且看日志只会看到「已有悬浮窗在运行」。

Windows 上改用命名互斥量：持有者必须是活进程，进程一死（哪怕是被任务管理器
强杀、崩溃、没走任何清理逻辑）内核立刻释放，不存在号码复用的问题。
"""
from __future__ import annotations

import ctypes
import sys

ERROR_ALREADY_EXISTS = 183

# 句柄必须活到进程结束：拿住不放，靠进程退出时内核回收来释放互斥量。
_HELD: list[int] = []


def acquire(name: str) -> bool:
    """抢占单实例位。返回 False 表示已经有一个实例在跑。

    name 建议用「Local\\」前缀（当前登录会话内唯一），这样不同 Windows 账号
    之间不会互相挡住。拿不到互斥量时一律放行 —— 宁可开出两个窗口，
    也不能因为一个系统调用失败就永远起不来。
    """
    if sys.platform != "win32":
        # 项目本身只在 Windows 上跑；别的平台没有互斥量，退化成不限制。
        return True
    return _win_mutex(name)


def _win_mutex(name: str) -> bool:
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):  # pragma: no cover
        return True

    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = k32.CreateMutexW(None, 0, f"Local\\{name}")
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _HELD.append(handle)  # 故意不 CloseHandle
    return True
