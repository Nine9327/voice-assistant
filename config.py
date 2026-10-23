"""全局配置。改这里就能调优，不用动业务代码。"""
from pathlib import Path

ROOT = Path(__file__).parent

# ---------------- 大模型 ----------------
LLM_PROVIDER = "ollama"           # ollama=本地自建 / openai=任意 OpenAI 兼容接口
OLLAMA_HOST = "http://127.0.0.1:11434"
LLM_MODEL = "qwen2.5:7b"          # 中文口语强，4.7G，4060 上约 40 tok/s
LLM_TEMPERATURE = 0.7
LLM_NUM_CTX = 4096
MAX_HISTORY_TURNS = 6             # 保留最近几轮对话

# 选 openai 时用这三项（可指向 OpenAI、DeepSeek、硅基流动、vLLM 等任意兼容端点）
OPENAI_BASE = "https://api.openai.com/v1"
OPENAI_KEY = ""
OPENAI_MODEL = "gpt-4o-mini"

# 只写「格式约束」，不写性格 —— 性格由 persona.py 的预设拼在前面。
# 这样换性格不会把朗读规则换掉（漏一条就会把「**」念出来）。
SYSTEM_PROMPT = """【说话方式，任何情况下都要遵守】
1. 用口语化、简短的中文回答，一般不超过三句话；
2. 禁止使用 Markdown、列表、代码块、表情符号、括号注释、链接；
3. 不要复述用户的问题，直接给答案；
4. 长数字、单位、英文缩写要用中文口语习惯表达；
5. 不确定的事情直接说不确定，不要编造。"""

# ---------------- 性格 ----------------
PERSONA_PRESET = "default"        # 见 persona.py：default/lively/calm/pro/tsundere/sassy/kid/butler/custom
PERSONA_PROMPT = ""               # 预设选 custom 时用这段人设
PERSONA_EXTRA = ""                # 叠加在任何性格上的额外要求（如「别叫我主人」）
PERSONA_VOICE = True              # 换性格时连带换配套音色与语速

# ---------------- 语音识别 ----------------
ASR_PROVIDER = "local"            # local=本地 faster-whisper / openai=网络识别接口
ASR_REMOTE_MODEL = "whisper-1"    # 网络识别用的模型；复用上面的 OPENAI_BASE / OPENAI_KEY
WHISPER_MODEL = "small"           # tiny / base / small / medium
WHISPER_DEVICE = "cuda"           # "cuda" 更快；显存不足或 CUDA 库缺失会自动回退 cpu
WHISPER_COMPUTE = "float16"       # cuda 用 float16，回退时自动改 int8
WHISPER_THREADS = 8
ASR_BEAM_SIZE = 1                 # 1=贪心解码，比 beam5 快三倍，中文短句准确率几乎无差别
ASR_LANGUAGE = "zh"
MODEL_DIR = str(ROOT / "models")

# ---------------- 录音 ----------------
SAMPLE_RATE = 16000
MIN_RECORD_SECONDS = 0.4          # 太短的误触直接忽略
MAX_RECORD_SECONDS = 30           # 超时自动截止

# ---------------- 语音合成 ----------------
TTS_ENGINE = "edge"               # edge=在线音质好 / sapi=Windows 离线（断网可用）
TTS_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_RATE = "+12%"                 # 语速略快一点更像对话
TTS_VOLUME = "+0%"
TTS_PITCH = "+0Hz"                # 音调，负值更低沉（如 -20Hz），正值更清亮
SAPI_VOICE = ""                   # 空=用系统默认中文声音，如 Microsoft Huihui Desktop

# ---------------- 交互 ----------------
HOTKEY = "space"                  # 按住说话

# ---------------- 唤醒词 ----------------
WAKE_ENABLED = True               # 常开监听，说提示词即可唤醒（占用少量 GPU 做识别）
WAKE_PHRASE = "小蓝小蓝"           # 唤醒提示词；后面的内容会被当成这次的指令
WAKE_SENSITIVITY = 0.6            # 0~1，越大越容易判定为说话（嘈杂环境调低）
WAKE_SILENCE = 0.8                # 说完后静音多久算一句话结束（秒）
WAKE_SESSION_SECONDS = 20         # 唤醒后保持对话的时长：这段时间里直接说就行，不用再喊提示词；
                                  # 超过这么久没说话就转回休眠。0 = 关闭连续对话

# ---------------- 本机指令 ----------------
APPS_ENABLED = True               # 说「打开微信」直接执行，不绕大模型

# ---------------- MCP 工具 ----------------
MCP_ENABLED = True                # 允许模型调用 MCP 服务器暴露的工具
MCP_CONFIG_FILE = "mcp.json"      # 服务器清单；含 token，已在 .gitignore 排除
MCP_MAX_ROUNDS = 3                # 一轮对话里最多「调工具→再想」几轮，防死循环
MCP_TIMEOUT = 20                  # 单次工具调用超时（秒）
MCP_MAX_TOOLS = 24                # 最多把几个工具塞进提示词：7B 模型上下文有限

# ---------------- 联网搜索 ----------------
SEARCH_MODE = "auto"              # off=从不联网 / auto=按问题判断（推荐）/ always=每次都查
SEARCH_BACKEND = "bing"           # bing=必应（免 key，国内直连）/ tavily / bocha / searx
SEARCH_KEY = ""                   # tavily、bocha 需要；必应留空即可
SEARCH_ENDPOINT = ""              # searx 必填，如 http://127.0.0.1:8888
SEARCH_RESULTS = 5                # 取前几条结果喂给模型
SEARCH_TIMEOUT = 6                # 单次搜索超时（秒）

# ---------------- Web 配置页 ----------------
WEB_HOST = "127.0.0.1"            # 只监听本机，不对外暴露
WEB_PORT = 8765
WEB_OPEN_BROWSER = True           # 启动时自动打开配置页

# ---------------- 用户覆盖 ----------------
# settings.json 由 Web 配置页写入。放在最后执行，保证它覆盖上面的默认值。
# 配置文件损坏不应该让整个程序起不来，所以异常只提示不抛出。
try:
    import settings_store as _store

    _store.apply(globals(), _store.load())
except Exception as _exc:  # noqa: BLE001
    print(f"[配置] settings.json 读取失败，改用默认值：{_exc}")
