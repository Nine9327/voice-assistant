"""打开本机应用。

「打开微信」「启动计算器」这种话不该绕一大圈：交给大模型去猜、去生成命令行，
既慢又危险。这里用一张白名单索引直接接住，匹配到就 launch，匹配不到就当没听懂，
原样交还给大模型 —— 所以「开个玩笑」不会被当成启动指令。

三级来源，逐级补充：
    内置表    Windows 自带的那几个（记事本、计算器、任务管理器…），不依赖安装位置
    开始菜单  扫 .lnk，一百多个文件只要 20 毫秒，覆盖微信、QQ、Chrome 这类桌面程序
    应用列表   StartApps 缓存，兜住商店应用（可能在 .lnk 里没有）

匹配按「同音」而不是「同字」：whisper 把「微信」听成「威信」「维信」是常有的事，
所以比较的是拼音音节，且把 zh/z、ch/c、sh/s、n/l、前后鼻音都归成一类。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import config

RUNTIME = Path(__file__).parent / ".runtime"
CACHE_FILE = RUNTIME / "startapps.json"
CACHE_TTL = 7 * 24 * 3600          # 装没装新软件不会天天变，一周刷一次就够

# 内置表：Windows 自带、且启动方式固定的。第二项是启动目标（exe 或协议），
# 第三项是显示名。别名写得宽一点，因为语音识别出来的花样很多。
_BUILTIN: list[tuple[tuple[str, ...], str, str]] = [
    (("记事本", "笔记本", "notepad", "计事本"), "notepad.exe", "记事本"),
    (("计算器", "计算机", "calc", "计算起"), "calc.exe", "计算器"),
    (("画图", "画板", "画笔", "paint", "画兔"), "mspaint.exe", "画图"),
    (("资源管理器", "文件管理器", "我的电脑", "此电脑", "文件夹", "explorer"),
     "explorer.exe", "资源管理器"),
    (("任务管理器", "任务管理", "进程管理器"), "taskmgr.exe", "任务管理器"),
    (("命令提示符", "命令行", "终端", "cmd", "控制台"), "cmd.exe", "命令提示符"),
    (("powershell", "power shell", "ps终端"), "powershell.exe", "PowerShell"),
    (("设置", "系统设置", "windows设置", "设置应用"), "ms-settings:", "设置"),
    (("控制面板", "控制台面板"), "control.exe", "控制面板"),
    (("截图", "截图工具", "截屏"), "ms-screenclip:", "截图工具"),
    (("注册表", "注册表编辑器", "regedit"), "regedit.exe", "注册表编辑器"),
    (("任务计划", "计划任务"), "taskschd.msc", "任务计划程序"),
    (("设备管理器",), "devmgmt.msc", "设备管理器"),
    (("磁盘管理",), "diskmgmt.msc", "磁盘管理"),
    (("服务", "服务管理器"), "services.msc", "服务"),
    (("系统信息", "关于电脑"), "msinfo32.exe", "系统信息"),
    (("网络设置", "wifi设置", "无线设置"), "ms-settings:network-wifi", "网络设置"),
    (("声音设置", "音量设置"), "ms-settings:sound", "声音设置"),
    (("显示设置", "屏幕设置"), "ms-settings:display", "显示设置"),
    (("蓝牙设置", "蓝牙"), "ms-settings:bluetooth", "蓝牙设置"),
    (("应用商店", "微软商店", "商店", "store"), "ms-windows-store:", "应用商店"),
    (("照片", "看图", "图片"), "ms-photos:", "照片"),
    (("闹钟", "闹钟和时钟"), "ms-clock:", "闹钟"),
    (("日历", "日程"), "outlookcal:", "日历"),
    (("相机", "摄像头"), "microsoft.windows.camera:", "相机"),
    (("地图",), "bingmaps:", "地图"),
    (("邮件", "邮箱"), "outlookmail:", "邮件"),
]

# 中文叫法 → 本机英文名。
#
# 这是「说谷歌浏览器、装的是 Google Chrome」这类问题的正解：开始菜单里只会写
# 英文名，而人说的是中文。左边的键在本机软件名里做子串匹配（比较前会 normalize，
# 所以空格和大小写都不影响），命中就把右边的中文叫法加进这个软件的别名。
#
# 只收真正会被用中文念出来的软件；键要够长，避免「code」这种到处都有的词乱点鸳鸯。
_BRAND: list[tuple[str, tuple[str, ...]]] = [
    # 浏览器
    ("chrome", ("谷歌浏览器", "谷歌", "古狗浏览器", "chrome浏览器")),
    ("firefox", ("火狐浏览器", "火狐")),
    ("microsoftedge", ("微软浏览器", "edge浏览器", "微软edge")),
    ("opera", ("欧朋浏览器", "欧朋")),
    ("qqbrowser", ("qq浏览器",)),
    ("360chrome", ("360浏览器", "三六零浏览器")),
    ("sogouexplorer", ("搜狗浏览器",)),
    ("brave", ("brave浏览器",)),
    # 开发
    ("visualstudiocode", ("vscode", "vs code", "vs", "代码编辑器", "写代码")),
    ("intellij", ("idea",)),
    ("androidstudio", ("安卓开发工具",)),
    ("notepad++", ("notepad加加",)),
    ("sublime", ("sublime文本",)),
    ("docker", ("容器工具",)),
    ("navicat", ("数据库工具",)),
    ("apifox", ("接口工具",)),
    ("wsl", ("linux子系统", "子系统")),
    # 办公
    ("winword", ("word", "word文档", "文档")),
    ("excel", ("excel表格", "表格", "电子表格")),
    ("powerpnt", ("ppt", "幻灯片", "演示文稿")),
    ("wps", ("金山办公", "wps办公", "办公软件")),
    ("onenote", ("onenote", "笔记软件")),
    ("outlook", ("邮件客户端",)),
    ("foxmail", ("邮件客户端",)),
    ("dingtalk", ("钉钉",)),
    ("feishu", ("飞书",)),
    ("wemeet", ("腾讯会议", "会议软件")),
    ("zoom", ("会议软件",)),
    # 设计与媒体
    ("photoshop", ("ps", "修图软件", "图片处理")),
    ("illustrator", ("矢量软件",)),
    ("premiere", ("pr", "视频剪辑", "剪辑软件")),
    ("aftereffects", ("ae", "特效软件")),
    ("lightroom", ("调色软件",)),
    ("sketchup", ("草图大师",)),
    ("3dsmax", ("建模软件",)),
    ("autocad", ("cad", "制图软件")),
    ("obs", ("录屏软件", "录屏")),
    ("potplayer", ("播放器", "视频播放器")),
    ("vlc", ("vlc播放器",)),
    ("cloudmusic", ("网易云音乐", "音乐")),
    ("qqmusic", ("qq音乐", "音乐")),
    ("kugou", ("酷狗音乐", "音乐")),
    ("kuwo", ("酷我音乐", "音乐")),
    ("xunlei", ("迅雷", "下载工具")),
    ("thunder", ("迅雷", "下载工具")),
    ("motrix", ("下载器",)),
    # 系统与工具
    ("everything", ("文件搜索",)),
    ("bandizip", ("解压软件", "压缩软件")),
    ("winrar", ("解压软件", "压缩软件")),
    ("7-zip", ("解压软件", "压缩软件")),
    ("teamviewer", ("远程控制",)),
    ("sunlogin", ("向日葵", "远程控制")),
    ("todesk", ("远程控制",)),
    ("steam", ("蒸汽平台", "游戏平台")),
    ("epicgames", ("epic平台", "游戏平台")),
    ("wegame", ("腾讯游戏平台", "游戏平台")),
    ("vmware", ("虚拟机",)),
    ("virtualbox", ("虚拟机",)),
    ("baidunetdisk", ("百度网盘", "网盘")),
    ("telegram", ("电报",)),
]

# 泛称：只说类别不说牌子。装了多个时按这里写的顺序挑，所以顺序即偏好。
_CATEGORY: dict[str, tuple[str, ...]] = {
    "浏览器": ("chrome", "firefox", "microsoftedge", "qqbrowser", "360chrome", "opera"),
    "网页": ("chrome", "microsoftedge"),
    "音乐": ("cloudmusic", "qqmusic", "kugou", "kuwo"),
    "播放器": ("potplayer", "vlc", "windowsmediaplayer"),
    "解压": ("bandizip", "winrar", "7-zip"),
    "压缩": ("bandizip", "winrar", "7-zip"),
    "下载": ("xunlei", "thunder", "motrix"),
    "虚拟机": ("vmware", "virtualbox"),
    "录屏": ("obs",),
    "远程": ("todesk", "sunlogin", "teamviewer"),
}

# 强动词：意图明确，就算找不到应用也该回一句「没找到」，
# 而不是丢给大模型去发挥。
_STRONG = re.compile(
    r"^(?:请|帮我|麻烦|你|给我|能不能|可以)*"
    r"(?:打开|开启|启动|运行|唤起|调出)"
    r"(?:一下|下|一个|个)?\s*"
    r"(?P<name>[^，。,.！!？?；;\s]{1,16}?)"
    r"(?:吧|呗|啊|呀)?[。.！!？?]*$"
)

# 弱动词（单个「开」）。这句可能压根不是指令 —— 「开个玩笑」「开什么玩笑」——
# 所以规矩不一样：只有真在索引里找到一个叫这名字的应用才接管。
_WEAK = re.compile(
    r"^(?:请|帮我|麻烦|你|给我)*"
    r"(?:开一下|开个|开|来一下)"
    r"(?:一下|下|一个|个)?\s*"
    r"(?P<name>[^，。,.！!？?；;\s]{1,16}?)"
    r"(?:吧|呗|啊|呀)?[。.！!？?]*$"
)

# 「把微信打开」这种倒装
_STRONG_REV = re.compile(
    r"^(?:请|帮我|麻烦|你|给我)*(?:把|将)(?P<name>[^，。,.！!？?；;\s]{1,16}?)"
    r"(?:打开|开启|启动|运行|调出来)(?:吧|呗|啊|呀|。|！|!)*$"
)

_NOISE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")

# 声母韵母的听感等价类：识别成「小兰」和「小蓝」是一回事
_FUZZ = [
    ("zh", "z"), ("ch", "c"), ("sh", "s"),
    ("ang", "an"), ("eng", "en"), ("ing", "in"), ("ong", "on"),
]


def normalize(text: str) -> str:
    return _NOISE.sub("", text or "").lower()


def _syllables(text: str) -> list[str]:
    """转成音节序列；没有 pypinyin 就退回逐字。"""
    try:
        from pypinyin import lazy_pinyin

        return [s.lower() for s in lazy_pinyin(text)]
    except Exception:  # noqa: BLE001
        return list(text)


def _fuzz(syl: str) -> str:
    for a, b in _FUZZ:
        if syl.startswith(a):
            syl = b + syl[len(a):]
            break
    if syl.startswith("n"):
        syl = "l" + syl[1:]      # n/l 不分是南方口音的常态
    return syl


def _sound_key(text: str) -> str:
    return "".join(_fuzz(s) for s in _syllables(normalize(text)))


def _user_brand() -> list[tuple[str, tuple[str, ...]]]:
    """用户自己补的别名，格式每行一条：中文叫法 = 软件名里的关键词。

    例：
        谷歌浏览器、谷歌 = chrome
        达梦 = dm
    左边是说的话（可以写多个，用顿号或逗号分开），右边是本机软件名里出现的词。
    内置表不可能覆盖所有人装的东西，这张表就是留给「我这台机器上的软件叫英文名」的。
    """
    raw = str(getattr(config, "APPS_ALIASES", "") or "")
    out: list[tuple[str, tuple[str, ...]]] = []
    for line in raw.replace("：", ":").replace("＝", "=").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = "=" if "=" in line else (":" if ":" in line else "")
        if not sep:
            continue
        left, right = line.split(sep, 1)
        names = tuple(x.strip() for x in re.split(r"[、,，/|]+", left) if x.strip())
        keyword = right.strip()
        if names and keyword:
            out.append((keyword, names))
    return out


def brand_aliases(label: str) -> list[str]:
    """这个软件名能对应上哪些中文叫法。内置表 + 用户表一起查。"""
    key = normalize(label)
    if not key:
        return []
    out: list[str] = []
    for keyword, names in _BRAND + _user_brand():
        probe = normalize(keyword)
        if probe and probe in key:
            out.extend(names)
    return out


def parse_open(text: str) -> tuple[str, bool] | None:
    """从一句话里取应用名。

    返回 (应用名, 是否强意图)；不是打开指令返回 None。
    强意图 = 用了「打开 / 启动 / 运行」这类明确的动词，此时找不到应用也该回一句；
    弱意图只有单个「开」字，风险是「开个玩笑」这类，必须真的查到应用才接管。
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 24:
        return None
    for pattern in (_STRONG, _STRONG_REV):
        m = pattern.search(raw)
        if m and m.group("name").strip():
            return m.group("name").strip(), True
    m = _WEAK.search(raw)
    if m and m.group("name").strip():
        return m.group("name").strip(), False
    return None


# ---------------------------------------------------------------- 索引
class Entry:
    __slots__ = ("label", "target", "kind", "keys", "sounds")

    def __init__(self, label: str, target: str, kind: str, aliases: list[str]) -> None:
        self.label = label
        self.target = target
        self.kind = kind                      # builtin / lnk / startapp
        self.keys = {normalize(a) for a in aliases if a}
        self.sounds = {_sound_key(a) for a in aliases if a}

    def as_dict(self) -> dict:
        return {"name": self.label, "target": self.target, "kind": self.kind}


_index: list[Entry] | None = None
_index_at = 0.0
_index_sig = ""


def _start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    user = Path(os.environ.get("APPDATA") or (Path.home() / "AppData/Roaming"))
    roots.append(user / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    program_data = Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData")
    roots.append(program_data / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [r for r in roots if r.is_dir()]


def _load_bound() -> list[Entry]:
    entries: list[Entry] = []
    for aliases, target, label in _BUILTIN:
        entries.append(Entry(label, target, "builtin", list(aliases)))
    for root in _start_menu_roots():
        try:
            for lnk in root.rglob("*.lnk"):
                # 显示名就用快捷方式文件名，这是用户在开始菜单里看到的那个。
                # 同时补上中文叫法 —— 装的是 Google Chrome，人说的是「谷歌浏览器」。
                entries.append(
                    Entry(lnk.stem, str(lnk), "lnk", [lnk.stem, *brand_aliases(lnk.stem)])
                )
        except OSError:
            continue
    return entries


def _startapps() -> list[Entry]:
    """商店应用的清单。慢（要拉一次 PowerShell），所以缓存到 .runtime。

    只作为兜底：先按 .lnk 找，找不到才来这里翻。任何异常都当没有，
    应用清单拉不到不该影响「打开记事本」这种基本功能。
    """
    if CACHE_FILE.exists():
        try:
            age = time.time() - CACHE_FILE.stat().st_mtime
            if age < CACHE_TTL:
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8-sig"))
                return [
                    Entry(d.get("name", ""), d.get("appid", ""), "startapp",
                          [d.get("name", ""), *brand_aliases(d.get("name", ""))])
                    for d in data
                    if d.get("name") and d.get("appid")
                ]
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    if sys.platform != "win32":
        return []
    script = (
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$OutputEncoding=[Text.Encoding]::UTF8; " + script],
            capture_output=True, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = proc.stdout.decode("utf-8", "ignore").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = [
        {"name": d.get("Name", ""), "appid": d.get("AppID", "")}
        for d in data
        if isinstance(d, dict)
    ]
    try:
        RUNTIME.mkdir(exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    return [
        Entry(r["name"], r["appid"], "startapp", [r["name"], *brand_aliases(r["name"])])
        for r in rows
        if r["name"] and r["appid"]
    ]


def _get_index(refresh: bool = False) -> list[Entry]:
    global _index, _index_at, _index_sig
    # 别名是写进索引里的，所以自定义别名一改就必须重建，否则改了看不到效果
    sig = str(getattr(config, "APPS_ALIASES", "") or "")
    if (_index is None or refresh or sig != _index_sig
            or time.time() - _index_at > 3600):
        _index = _load_bound()
        _index_at = time.time()
        _index_sig = sig
    return _index


def labels(limit: int = 25, with_aliases: bool = False) -> list[str]:
    """本机认得的程序名，取前若干个。

    给识别模块的「领域词提示」用：这些恰恰是「打开 XX」里最容易听错的字。
    索引本身会缓存一小时，重复调用不扫盘。

    with_aliases=True 时把中文叫法也带上 —— 用户说的是「谷歌浏览器」，
    光把「Google Chrome」塞给识别模型没多大用。
    """
    try:
        entries = _get_index()
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> bool:
        value = (value or "").strip()
        if not value or value.lower() in seen:
            return False
        seen.add(value.lower())
        out.append(value)
        return len(out) >= limit

    for entry in entries:
        if _add(entry.label):
            return out
        if not with_aliases:
            continue
        for alias in sorted(entry.keys):
            # 只收中文别名：英文的跟软件名重复，白占提示词的位置
            if not any("\u4e00" <= ch <= "\u9fff" for ch in alias):
                continue
            if _add(alias):
                return out
    return out


def find(name: str, deep: bool = True) -> Entry | None:
    """在索引里找。返回命中的条目，找不到返回 None。

    匹配顺序刻意这样排：同一关键词先比同音，再比包含。
    「微信」和「威信」同音，但和「信」只是包含关系，前者显然更应该赢。
    """
    key = normalize(name)
    if not key:
        return None
    sound = _sound_key(name)

    # 先查本地索引（快），查不到才去拉商店应用清单（要起一次 PowerShell，
    # 冷启动一两秒，不能每句话都付这个代价）
    entry = _match(name, _get_index())
    if entry is not None or not deep:
        return entry
    entry = _match(name, _startapps())
    if entry is not None:
        return entry
    return find_category(name)          # 「打开浏览器」这种只说了类别的


def _match(name: str, entries: list[Entry]) -> Entry | None:
    key = normalize(name)
    sound = _sound_key(name)
    if not key:
        return None

    # 1) 精确同音
    if sound:
        for entry in entries:
            if sound in entry.sounds:
                return entry
    # 2) 完全一样的字
    for entry in entries:
        if key in entry.keys:
            return entry
    # 3) 包含关系：优先短的，避免「记事本」被「记事本快捷方式」抢走
    #
    # 太短的英文名不走这一步：「ps」会被 PowerShell 抢走，「ai」能套中一堆词。
    # 短词只认上面第 2 步的精确别名 —— 真装了 Photoshop，它的别名里就有「ps」。
    if key.isascii() and len(key) < 3:
        return None
    best: Entry | None = None
    best_len = 999
    for entry in entries:
        for k in entry.keys:
            if len(k) < 2:
                continue
            if (key in k or k in key) and len(k) < best_len:
                best, best_len = entry, len(k)
    return best


def find_category(name: str) -> Entry | None:
    """「打开浏览器」「来个播放器」—— 只说了类别没说牌子。

    按 _CATEGORY 里写的偏好顺序在本机找，找到第一个装了的就算数。
    这是最后一道兜底：只有在按名字怎么都找不到之后才试，避免抢走具体名字的匹配。
    """
    key = normalize(name)
    if not key:
        return None
    for word, keywords in _CATEGORY.items():
        if key != normalize(word):
            continue
        for keyword in keywords:
            probe = normalize(keyword)
            if not probe:
                continue
            for entry in _get_index():
                if probe in normalize(entry.label):
                    return entry
    return None


def suggest(name: str) -> str | None:
    """找不着的时候，本机有没有名字相近的？给语音留一句「是不是想开 XX」。

    只在彻底找不到时才调，可以放心用 difflib 慢慢比。阈值卡得比较紧：
    这句子是要念出来给用户听的，猜错比沉默更糟，宁可不说。
    （实测 0.62 会把「ggogle浏览器」提示成 Edge，0.7 才不会乱点鸳鸯。）
    """
    import difflib

    key = normalize(name)
    if len(key) < 2:
        return None
    best: str | None = None
    best_ratio = 0.0
    for entry in _get_index():
        for k in entry.keys:
            if len(k) < 2:
                continue
            ratio = difflib.SequenceMatcher(None, key, k).ratio()
            if ratio > best_ratio:
                best, best_ratio = entry.label, ratio
    return best if best_ratio >= 0.7 else None


def open_app(name: str, entry: Entry | None = None) -> dict:
    """按名字启动。返回 {ok, app, way, say, error}，say 是给 TTS 念的一句话。"""
    if entry is None:
        entry = find(name)
    if entry is None:
        near = suggest(name)
        say = f"没找到叫{name}的应用。"
        if near is not None:
            say = f"没找到叫{name}的应用，本机有{near}，要打开吗？"
        return {
            "ok": False,
            "app": name,
            "near": near or "",
            "say": say,
            "error": "没有匹配的应用",
        }
    ok, error = _launch(entry)
    if ok:
        return {"ok": True, "app": entry.label, "way": entry.kind,
                "say": f"打开了{entry.label}。", "error": ""}
    return {
        "ok": False,
        "app": entry.label,
        "way": entry.kind,
        "say": f"{entry.label}打不开，可能没装好。",
        "error": error,
    }


def _launch(entry: Entry) -> tuple[bool, str]:
    """统一交给 ShellExecute：exe、快捷方式、协议（ms-settings:）、shell 命名空间都认。"""
    try:
        if entry.kind == "startapp":
            # 商店应用没有可直接执行的 exe，只能走 AppsFolder 命名空间；
            # 少数 startapp 条目给的本来就是完整 exe 路径，那种直接开更稳。
            if Path(entry.target).is_file():
                os.startfile(entry.target)
            else:
                os.startfile(f"shell:AppsFolder\\{entry.target}")
        else:
            os.startfile(entry.target)
        return True, ""
    except OSError as exc:
        # 有些 .lnk 是「卸载」「帮助文档」这类，双击也未必起得来，如实报错即可
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def list_apps(keyword: str = "", limit: int = 40) -> list[dict]:
    """给配置页用的应用清单，顺便让用户知道「它认得哪些名字」。"""
    key = normalize(keyword)
    out: list[dict] = []
    seen: set[str] = set()
    for entry in _get_index():
        if entry.kind == "lnk" and not entry.label:
            continue
        if key and key not in "".join(entry.keys):
            continue
        label = entry.label
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append(entry.as_dict())
        if len(out) >= limit:
            break
    return out
