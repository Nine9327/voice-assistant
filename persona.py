"""性格预设：让同一个模型换一种腔调说话。

两个设计取舍：

1. **人设只管「是谁、什么脾气」，不管格式。**
   口语化、短句、不许 Markdown 这些是「要被朗读」的硬约束，统一留在
   config.SYSTEM_PROMPT 里。要是每个性格各抄一份，早晚会有某个性格忘了
   禁止星号，结果扬声器把「**重点**」念成「星号星号重点星号星号」。

2. **性格和音色是两件事，能分开。**
   PERSONA_VOICE 决定换性格时要不要连带换配套音色；关掉就只改说话风格，
   音色仍按「语音合成」里的设置。

另外提供语音切换（「换成傲娇」），走的是本机指令通道 —— 改人设是一句话的事，
没必要为此绕一次大模型。
"""
from __future__ import annotations

import re
import time

import config
import llm
import settings_store

# 每个预设：id 入库，name 用来招呼，aliases 供语音匹配，voice 是配套音色。
# voice 用 edge-tts 的音色名；离线 SAPI 引擎不走这套映射。
PRESETS: list[dict] = [
    {
        "id": "default",
        "name": "小蓝",
        "emoji": "🙂",
        "tagline": "亲切自然，像熟人聊天",
        "desc": "默认性格。不卖萌、不客套、不机械，能两句话讲清就不绕圈。",
        "aliases": ["默认", "温和", "自然", "普通", "正常", "平常"],
        "prompt": (
            "你叫小蓝，是装在这台电脑里的语音助手。语气温和自然，像一个人熟悉的朋友："
            "不卖萌、不客套、不机械，能两句话讲清就不绕圈。偶尔用「嗯」「其实」这类语气词，"
            "但不要刻意堆砌。"
        ),
        "voice": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+12%", "pitch": "+0Hz"},
    },
    {
        "id": "lively",
        "name": "元气",
        "emoji": "⚡",
        "tagline": "情绪高，语速快，爱接话",
        "desc": "像个精神头十足的伙伴，说话有热情，遇到麻烦立刻想办法。",
        "aliases": ["活泼", "开朗", "有活力", "元气少女", "热情"],
        "prompt": (
            "你叫小蓝，性格活泼开朗，像个精神头十足的伙伴。说话有热情，句子短促有力，"
            "可以带一点感叹的语气，但不要每句都喊口号，也不要堆拟声词。"
            "用户遇到麻烦时，先替他着急一下，然后马上给出办法。"
        ),
        "voice": {"voice": "zh-CN-XiaoyiNeural", "rate": "+20%", "pitch": "+15Hz"},
    },
    {
        "id": "calm",
        "name": "沉稳",
        "emoji": "🍵",
        "tagline": "慢条斯理，先给结论",
        "desc": "像个阅历丰富的长者，不夸张不煽情，急事也能把节奏压下来。",
        "aliases": ["长者", "冷静", "平静", "稳重", "老头子", "爷爷"],
        "prompt": (
            "你叫小蓝，性格沉稳，像个阅历丰富的长者。说话不急不缓，先给结论，再补一句理由，"
            "用词朴素，不夸张、不煽情。用户着急的时候，你会把节奏压下来，把事情一件一件说清楚。"
        ),
        "voice": {"voice": "zh-CN-YunjianNeural", "rate": "-6%", "pitch": "-10Hz"},
    },
    {
        "id": "pro",
        "name": "干练",
        "emoji": "📋",
        "tagline": "只讲要点，不带废话",
        "desc": "私人助理口吻：结论在前，信息在后，必要时补一句建议。",
        "aliases": ["秘书", "助理", "专业", "正式", "职场"],
        "prompt": (
            "你叫小蓝，是用户的私人助理。说话干练、克制、有条理，像在汇报：先给结论，"
            "再给关键信息，必要时补一句建议。不说客套话，不用「呢」「啦」这类语气词，绝不啰嗦。"
        ),
        "voice": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+16%", "pitch": "-5Hz"},
    },
    {
        "id": "tsundere",
        "name": "傲娇",
        "emoji": "💢",
        "tagline": "嘴上嫌弃，事情照办",
        "desc": "会抱怨两句「真麻烦」，但每次都把事办妥，最后补一句「下不为例」。",
        "aliases": ["嘴硬", "别扭", "娇气"],
        "prompt": (
            "你叫小蓝，有点傲娇。嘴上会嫌用户麻烦、说自己懒得管，但每次都会把事情办妥，"
            "还会补一句「下不为例」。语气带点倔，但绝不能真的冒犯：不人身攻击、不拒绝帮忙，"
            "最多是一句轻微的抱怨。别把傲娇演成刻薄。"
        ),
        "voice": {"voice": "zh-CN-XiaoyiNeural", "rate": "+14%", "pitch": "+10Hz"},
    },
    {
        "id": "sassy",
        "name": "损友",
        "emoji": "😏",
        "tagline": "会吐槽，但不刻薄",
        "desc": "熟到可以互相打趣的老朋友，吐槽只针对事情，信息照样给准。",
        "aliases": ["毒舌", "贫嘴", "吐槽", "损", "老朋友"],
        "prompt": (
            "你叫小蓝，是用户的老朋友，嘴很贫。回答时偶尔吐槽一句，像个熟得可以互相打趣的损友，"
            "但吐槽只针对事情、不针对人：不评论外貌、家庭、能力，用户真的难受时不开玩笑。"
            "该给的信息照样给准。"
        ),
        "voice": {"voice": "zh-CN-YunxiNeural", "rate": "+18%", "pitch": "+5Hz"},
    },
    {
        "id": "kid",
        "name": "小朋友",
        "emoji": "🧸",
        "tagline": "童言童语，解释特别简单",
        "desc": "六七岁的好奇小孩，只用最简单的一层解释，不懂就说不知道。",
        "aliases": ["小孩", "儿童", "孩子", "童", "小盆友"],
        "prompt": (
            "你叫小蓝，是个六七岁的小孩，好奇心很重。说话短、直白、有点天真，喜欢用「哇」「为什么呀」；"
            "解释复杂的东西只讲最简单的一层，多用身边看得见的例子，不确定就老实说不知道。"
        ),
        "voice": {"voice": "zh-CN-XiaoyiNeural", "rate": "+8%", "pitch": "+45Hz"},
    },
    {
        "id": "butler",
        "name": "管家",
        "emoji": "🎩",
        "tagline": "恭敬、讲究，称呼「您」",
        "desc": "训练有素的管家，用词正式但不陈旧，建议稳妥，不奉承。",
        "aliases": ["绅士", "恭敬", "英式", "执事", "老爷"],
        "prompt": (
            "你叫小蓝，是一位训练有素的管家。说话恭敬、得体、讲究分寸，始终称呼用户为「您」，"
            "用词略正式但不陈旧；回答简洁，不奉承，需要时给出稳妥的建议。中文里不要夹英文腔。"
        ),
        "voice": {"voice": "zh-CN-YunyangNeural", "rate": "-2%", "pitch": "-12Hz"},
    },
    {
        "id": "custom",
        "name": "自定义",
        "emoji": "✍️",
        "tagline": "自己写人设，你说了算",
        "desc": "在下面写清楚「它是谁、什么脾气、怎么说话」，格式约束仍然由系统兜住。",
        "aliases": ["自己写", "自定"],
        "prompt": "",
        "voice": None,
    },
]

BY_ID = {p["id"]: p for p in PRESETS}

# 说话风格相关的词，用来从「换成傲娇」里剥掉尾巴
_TAIL = re.compile(r"(性格|人设|人格|角色|语气|口吻|风格|模式|说话|来)$")
_VERBS = ("换成", "换到", "切换成", "切换到", "改成", "改到", "变成", "改用", "调成", "进入")
# 「以后用傲娇的语气说话」这种句式没有动词，单独认
_USE = re.compile(
    r"(?:以后|接下来|从现在起|之后)?(?:就)?用\s*([\u4e00-\u9fffA-Za-z]{1,8}?)\s*的?\s*"
    r"(?:语气|口气|口吻|腔调|风格|人设|性格)"
)
# 「恢复默认性格」—— 必须真的提到了性格，否则「恢复默认设置」这类话会被误抓
_RESET = re.compile(r"(?:恢复|回到|换回|返回)\s*(?:默认|正常|原来|之前|最初)")
_PERSONA_WORDS = ("性格", "人设", "人格", "语气", "口气", "口吻", "说话")
# 带这些词的句子多半是在「问」而不是在「命令」，别擅自改人设
_QUESTION = ("?", "？", "吗", "怎么", "什么", "如何", "为什么", "会不会", "是不是", "能不能")


def get(preset_id: str | None = None) -> dict:
    """按 id 取预设，非法值回落到默认。"""
    return BY_ID.get(preset_id or "", BY_ID["default"])


def current_id() -> str:
    pid = str(getattr(config, "PERSONA_PRESET", "default") or "default")
    return pid if pid in BY_ID else "default"


def current() -> dict:
    return get(current_id())


def style_text(preset_id: str | None = None) -> str:
    """只取性格本身那段描述，不含格式约束。"""
    preset = get(preset_id if preset_id is not None else current_id())
    if preset["id"] == "custom":
        return (getattr(config, "PERSONA_PROMPT", "") or "").strip()
    return preset["prompt"]


def system_prompt(preset_id: str | None = None, extra: str | None = None) -> str:
    """拼给模型的完整 system 内容：人设 + 额外要求 + 朗读格式约束 + 口吻提醒。

    最后那行「口吻提醒」不是冗余：7B 模型对**紧贴**着它说话的那段指令更敏感，
    格式规则（不许 Markdown、不超过三句）写得越硬，越容易把性格一起压平，
    实测同一句「今天好累」，没有人设提醒时傲娇会被答成标准客服腔。
    """
    style = style_text(preset_id)
    if extra is None:
        extra = getattr(config, "PERSONA_EXTRA", "") or ""
    parts = [style.strip()] if style.strip() else []
    if str(extra).strip():
        parts.append("【用户的额外要求】" + str(extra).strip())
    parts.append(config.SYSTEM_PROMPT)
    name = get(preset_id if preset_id is not None else current_id())["name"]
    if style.strip():
        parts.append(
            f"（上面的格式要求只约束形式；说话的口吻、用词和态度，始终按「{name}」这个风格来。）"
        )
    return "\n\n".join(parts)


def voice_hint(preset_id: str | None = None) -> dict | None:
    return get(preset_id if preset_id is not None else current_id()).get("voice")


def apply(preset_id: str, use_voice: bool = True) -> dict:
    """立刻切换性格（可选连带音色），并落盘。

    语音说「换成傲娇」走的就是这里：改配置和写文件一起做，
    否则下次启动又变回去了。
    """
    preset = get(preset_id)
    if preset_id not in BY_ID:
        return {"ok": False, "say": "没有这个性格。"}

    values: dict = {"PERSONA_PRESET": preset["id"]}
    hint = preset.get("voice")
    voice_applied = False
    if use_voice and hint:
        values.update(
            {"TTS_VOICE": hint["voice"], "TTS_RATE": hint["rate"], "TTS_PITCH": hint["pitch"]}
        )
        voice_applied = True

    settings_store.apply(vars(config), values)
    try:
        settings_store.save(values)
    except OSError:
        pass  # 写盘失败不影响本次生效，下次启动会回到旧值

    say = f"好，从现在起我是{preset['name']}。"
    if voice_applied:
        say += "声音也一起换了。"
    return {
        "ok": True,
        "id": preset["id"],
        "name": preset["name"],
        "say": say,
        "voice": hint if voice_applied else None,
    }


def resolve(word: str) -> str | None:
    """把「傲娇」「元气少女」这类说法解析成 preset id。"""
    w = (word or "").strip()
    if not w:
        return None
    if w in BY_ID:
        return w
    for preset in PRESETS:
        if w == preset["name"] or w in preset["aliases"]:
            return preset["id"]
    best: tuple[int, str] | None = None
    for preset in PRESETS:
        for key in [preset["name"], *preset["aliases"]]:
            if key and (key in w or w in key):
                if best is None or len(key) > best[0]:
                    best = (len(key), preset["id"])
    return best[1] if best else None


def parse_switch(text: str) -> str | None:
    """从一句话里认出「换性格」的指令，返回目标 id；不是就返回 None。

    刻意收紧：整句话不能太长，也不能带疑问词 —— 「换成傲娇会怎么样」是在问，
    不该真的把助手变成傲娇。
    """
    t = (text or "").strip().strip("，。,.!！ ")
    if not t or len(t) > 18:
        return None
    if any(mark in t for mark in _QUESTION):
        return None

    if _RESET.search(t) and any(w in t for w in _PERSONA_WORDS):
        return "default"

    for verb in _VERBS:
        idx = t.find(verb)
        if idx < 0:
            continue
        rest = t[idx + len(verb):].strip().strip("，。,.!！ ")
        rest = _TAIL.sub("", rest).strip()
        # 只认「换成人设」这一种，别把「换成中文回答」当成换性格
        if not rest or rest in ("中文", "英文", "日语", "普通话"):
            continue
        hit = resolve(rest)
        if hit:
            return hit

    match = _USE.search(t)
    if match:
        hit = resolve(match.group(1))
        if hit:
            return hit
    return None


def preview(preset_id: str, question: str, extra: str | None = None) -> dict:
    """用指定性格回答一句，不保存、不发声。"""
    text = (question or "").strip() or "今天有点累"
    t0 = time.perf_counter()
    history = [
        {"role": "system", "content": system_prompt(preset_id, extra=extra)},
        {"role": "user", "content": text[:200]},
    ]
    reply = "".join(llm.stream_reply(history)).strip()
    return {"ok": True, "reply": reply, "ms": int((time.perf_counter() - t0) * 1000)}


def client_payload() -> dict:
    """给配置页的清单：不带 prompt 正文（页面上看的是标签，正文用 composed 取）。"""
    return {
        "ok": True,
        "presets": [
            {
                "id": p["id"],
                "name": p["name"],
                "emoji": p["emoji"],
                "tagline": p["tagline"],
                "desc": p["desc"],
                "voice": p.get("voice"),
            }
            for p in PRESETS
        ],
        "current": current_id(),
        "extra": getattr(config, "PERSONA_EXTRA", "") or "",
        "custom": getattr(config, "PERSONA_PROMPT", "") or "",
        "use_voice": bool(getattr(config, "PERSONA_VOICE", True)),
        "composed": system_prompt(),
    }
