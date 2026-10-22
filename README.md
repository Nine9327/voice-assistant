# 本地语音助手

按住空格说话，松开后自动识别、思考、开口回答。也可以直接说唤醒词，或者点一下桌面上的形象。
启动后用浏览器打开配置页，可以随时切换每一环用本地还是网络服务。

三种叫醒方式共用同一条链路：

```
按住空格 ─┐
说唤醒词 ─┼→ 录音/取文本 → 识别 → 大模型 → 逐句切分 → 合成 → 扬声器
点桌面形象 ┘
```

双击 `start.vbs` 同时拉起助手和桌面形象，全程没有命令行窗口；要停就双击 `stop-all.bat`。

## 链路

```
麦克风 → 识别 → 大模型 → 逐句切分 → 语音合成 → 扬声器
```

每一环都有两套服务可选，在配置页里点一下就能换：

| 环节 | 本地（默认） | 网络 |
|---|---|---|
| 识别 | faster-whisper small，GPU 0.15s | OpenAI 兼容 `/audio/transcriptions` |
| 大模型 | Ollama `qwen2.5:7b` | 任意 OpenAI 兼容接口（DeepSeek、硅基流动、百炼、智谱…） |
| 合成 | Windows SAPI，稳定 54ms | edge-tts，音质接近真人 |

默认组合是「本地识别 + 本地模型 + 在线合成」，只有语音合成需要联网。断网时把合成切成离线引擎即可完全离线运行。

## 环境

| 项目 | 版本 |
|---|---|
| Python | 3.11（conda 环境 `voice-assistant`） |
| GPU | RTX 4060 8GB（7B Q4 + whisper small 可共存） |
| 大模型 | Ollama + `qwen2.5:7b` |
| 识别 | faster-whisper `small`，float16，CUDA |
| 合成 | edge-tts `zh-CN-XiaoxiaoNeural`（需联网） |

## 安装

```bash
conda create -n voice-assistant python=3.11 -y
conda activate voice-assistant
pip install -r requirements.txt
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   # GPU 识别所需
ollama pull qwen2.5:7b
```

## 使用

### 日常使用（无窗口）

| 双击 | 作用 |
|---|---|
| `start.vbs` | 启动助手 + 桌面形象，**不显示任何命令行窗口**，日志写在 `.runtime/*.log` |
| `stop-all.bat` | 关掉助手、桌面形象和 Ollama，释放全部显存 |

用 `start.vbs` 启动后，配置页不会自动弹出：右键点桌面上的形象 → **打开配置页**。
同一个菜单里还有 **开始说话**、**始终置顶**、**退出程序**。左键点一下形象 = 说一句话，
按住拖动可以换位置（位置会记住）。

### 命令行

```bash
python assistant.py              # 正常语音交互，同时开配置页
python assistant.py --config     # 只开配置页（不占麦克风、不加载模型）
python assistant.py --no-web     # 不启动配置页
python assistant.py --no-wake    # 不监听唤醒词，只留按住空格
python assistant.py --port 9000  # 换端口（默认 8765，占用时自动顺延）
python assistant.py --text "你好" # 跳过录音，测大模型+播报
python assistant.py --no-audio   # 只打字不播报
python pet.py                    # 单独起桌面形象（python pet.py --debug 带日志）
python selftest.py               # 端到端自检（合成语音当输入，不需要麦克风）
python voices.py                 # 音色列表 / 试听
python stop.py                   # 一键关闭
```

只想调配置：双击 `config.bat`（或 `python assistant.py --config`）。
想看到控制台输出、排查问题：双击 `run.bat`。

操作：**按住空格**说话，松开即开始回答；回答过程中再按空格可以打断它。`Ctrl+C` 退出。

## 唤醒词与桌面形象

`assets/pet.png` 是抠好透明通道的形象（由 `python pet_asset.py` 从原图生成，边缘做了羽化，
深色壁纸上也不会出现白边）。它跑在**独立进程**里，通过本机 HTTP 和助手通信：

* 界面崩了不影响语音，语音卡住也不影响拖动窗口；
* 配置页端口顺延（8765 被占）时，形象靠 `.runtime/server.json` 找过来；
* 助手没起来时气泡显示「启动中…」，几秒仍连不上才提示「没有连接到助手进程」。

唤醒词默认是「小蓝小蓝」，在配置页「唤醒与形象」里改，**保存即刻生效**（不用重启）：

| 项 | 说明 |
|---|---|
| 常开唤醒 | 关掉后只能按住空格或点形象。开着时每说完一句会转写一次（rtf≈0.05，约占 5% GPU 时间） |
| 唤醒提示词 | 建议 3~6 个字；叠词（小蓝小蓝）比单词更不易被日常聊天误触发 |
| 灵敏度 | 越大越容易判定为说话。嘈杂环境调低，小声说话调高 |
| 断句静音 | 说完后停顿多久算一句结束 |

提示词后面的内容会被当成这一次的指令：「小蓝小蓝，今天天气怎么样」一句话说完即可，
不用先唤醒再重复一遍。识别有偏差也能命中——whisper 常把「小蓝」听成「小兰」「小男」，
所以匹配是编辑距离模糊匹配（两个字的提示词只认精确匹配，避免「小红」唤醒「小蓝」）。

配置页里有个**验证提示词**的输入框：随便输一句话，直接看它认不认，不用反复对着麦克风试。
它上面那行「监听状态」会实时显示最近听到的内容、峰值电平和当前阈值，调参时看这里最直观。

唤醒判定前有一道**能量门槛**：95 分位数幅度低于门槛的片段直接丢掉。因为 whisper 在近乎
静音的片段上会凭空编出整句话，既白烧显卡，又污染状态显示。门槛跟着灵敏度反向走，
灵敏度拉满时最宽松。

## 配置页

启动后浏览器会自动打开 `http://127.0.0.1:8765`（用 `start.vbs` 启动时不自动开，走形象的右键菜单），
控制台也会打印地址。页面里能做的事：

| 区域 | 能改什么 |
|---|---|
| 大模型服务 | 本地 Ollama ↔ 网络接口（含 DeepSeek、硅基流动、百炼、智谱、Moonshot 预设）；模型、温度、上下文、记忆轮数 |
| 语音合成服务 | 在线 edge-tts ↔ 离线系统语音；音色、语速、音调，可当场试听 |
| 语音识别服务 | 本地模型 ↔ 网络接口；GPU/CPU、模型大小、解码宽度 |
| 唤醒与形象 | 唤醒开关、提示词、灵敏度、断句静音；提示词匹配自测、实时监听状态 |
| 交互与页面 | 最短/最长录音时长、配置页端口 |

「保存并应用」之后：

* 大模型、语音合成、唤醒开关的改动**立即生效**，因为这几个模块都重新读配置；
* 识别相关的改动会**自动重建识别模型**，页面会回显切换后的设备（如 `cuda / float16`）；
* 改动同时写入 `settings.json`，下次启动自动沿用；
* 只有端口这类需要重新监听的项会提示「需重启」。

识别服务重建失败时（比如选了网络识别但没填 Key），**配置会自动回退**，内存和 `settings.json` 一起还原，不会留下一个下次启动必然报错的状态。

页面只监听 `127.0.0.1`，写操作还要求一个自定义请求头，防止别的网页跨站改你的配置。API Key 保存后只显示末四位。

## 关闭

双击 `stop-all.bat`（全关），或：

```bash
python stop.py            # 只关助手和形象，Ollama 会问你要不要一起关
python stop.py --all      # 助手 + 形象 + Ollama 全关，立刻释放约 5G 显存
python stop.py --keep-ollama  # 只关助手，不动 Ollama，不弹询问
python stop.py --dry      # 只列出会被关掉的进程，不动手
```

它会结束命令行里带本项目特征的 Python 进程（`voice-assistant` 路径、`assistant.py`、
`pet.py`），**不会误伤你别的 Python 程序**。进程定位优先读 `.runtime/*.pid`，
pid 文件失效时再扫命令行兜底；关完顺手清掉残留的 pid 和端口记录，下次启动不会被旧状态绊住。

Ollama 被 `--all` 关掉后，下次启动助手会自动把它拉起来，不用手动开。

## 实测性能（RTX 4060 8G）

| 环节 | 实时率 / 耗时 |
|---|---|
| 语音识别（3.1s 音频） | **161ms**，rtf 0.05 |
| 大模型首字 | **0.07 ~ 0.19s**（热态） |
| 大模型整段生成 | 0.39s（约 20 字回答） |
| **松手到开口** | **约 0.6 秒** |
| 语音合成 · 在线 edge-tts | 约 1.9s / 句（网络往返为主） |
| 语音合成 · 离线 SAPI | **54ms / 句**（首次含 COM 初始化 240ms） |
| 首次冷启动 | 约 30s（4.7G 模型载入显存，之后常驻） |

对照：whisper 如果跑 CPU，同段音频要 1.5s，整体延迟翻三倍——所以值得让它挤进 GPU。

## 配置

优先级：`settings.json`（配置页保存的） > `config.py`（默认值）。
`config.py` 里写的是出厂默认，适合设定长期基线；日常调整直接走配置页。
想恢复默认，删掉 `settings.json` 再重启即可。

| 项 | 说明 |
|---|---|
| `LLM_MODEL` | 换模型，如 `qwen2.5:3b`（更快）/ `qwen2.5:14b`（8G 显存跑不动） |
| `WHISPER_MODEL` | `small` 平衡最好；`medium` 更准但 CPU 慢一倍 |
| `TTS_VOICE` | 音色，内置 14 个中文音色，用 `python voices.py` 试听 |
| `TTS_RATE` | 语速，`+12%` 比默认更像真人对话 |
| `TTS_PITCH` | 音调，负值更低沉（`-20Hz`），正值更清亮 |
| `MAX_HISTORY_TURNS` | 记住几轮对话 |
| `WAKE_PHRASE` | 唤醒提示词，建议 3~6 个字、叠词 |
| `WAKE_SENSITIVITY` | 0~1，越大越容易判定为说话；环境嘈杂时调低 |
| `SYSTEM_PROMPT` | 人设与说话风格，直接影响朗读效果 |

## 设计上的几个关键点

1. **显存预算算清楚**：7B Q4 占 4.7G，whisper small float16 只需约 500MB，两者能共存。加载失败会自动回退 CPU，不会卡死。
2. **逐句播报**：大模型流式输出，遇到句末标点就切一句送去合成，播放线程串行消费，所以「边说边想」，不等全文生成完。
3. **打断即闭麦**：按空格立刻停播并清空待播队列，避免麦克风把扬声器的声音录进去造成自问自答。唤醒监听同理——播报期间关掉，一轮对话结束再打开。
4. **形象独立成进程**：Qt 事件循环和语音主循环互不干扰，两者只用本机 HTTP 说话。界面卡住不会拖慢识别，语音卡住也能照常拖动窗口。
5. **配置是活对象**：`config` 模块就是唯一真相，业务模块每次调用都重新读它，所以网页改完即刻生效；`settings_store` 只负责校验和落盘。

## 换音色

```bash
python voices.py                    # 列出全部 14 个中文音色
python voices.py zh-CN-YunxiNeural  # 试听某一个
python voices.py --demo             # 依次试听全部（每条约 5 秒）
python voices.py --demo -1          # 反复听当前配置，调语速/音调时用
```

挑好后把 `config.py` 的 `TTS_VOICE` 改成它，重启生效。

| 音色 | 感觉 |
|---|---|
| `zh-CN-XiaoxiaoNeural` | 标准女声，最自然（默认） |
| `zh-CN-XiaoyiNeural` | 年轻女声，活泼 |
| `zh-CN-YunxiNeural` | 年轻男声，温和 |
| `zh-CN-YunjianNeural` | 成熟男声，解说风 |
| `zh-CN-YunyangNeural` | 新闻播报，偏正式 |
| `zh-CN-YunxiaNeural` | 少年音 |

另有东北话 `zh-CN-liaoning-XiaobeiNeural`、陕西话 `zh-CN-shaanxi-XiaoniNeural`，
以及粤语（中国香港）`zh-HK-*`、国语（中国台湾）`zh-TW-*` 各三个。

除了换音色，还能调 `TTS_RATE`（语速）和 `TTS_PITCH`（音调，如 `-20Hz` 更低沉），
三个参数组合能明显改变角色气质。

## 常见问题

**双击 `run.bat` 报「'1' 不是内部或外部命令」「'dp0"' 不是内部或外部命令」**
批处理文件被存成了 LF 换行（或含非 ASCII 字符）。cmd.exe 只认 CRLF，换行不对会把多行命令拼成一团，`%~dp0` 被切碎成 `dp0"`、`if errorlevel 1` 被切出一个孤立的 `1`。
修复：`python tools/normalize_eol.py`（会把 `.bat` / `.vbs` 统一成 CRLF + 纯 ASCII，其余文本转 LF）。`.gitattributes` 已强制 `*.bat` 使用 CRLF。

**首次运行卡在「加载识别模型」**
要从 hf-mirror.com 下约 500MB，走代理可能失败。代码已默认设置 `HF_ENDPOINT=https://hf-mirror.com` 和 `HF_HUB_DISABLE_XET=1`；若仍失败，可手动把模型放到 `models/faster-whisper-small/`。

**启动时提示「GPU 不可用，回退 CPU」**
说明 CUDA 运行库没找到。`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` 后重启即可。回退后功能完整，只是识别从 0.15s 变成 1.5s。

**按空格没反应**
pynput 监听的是全局键盘。若以管理员权限运行了别的窗口，需以管理员身份运行本程序。

**说了唤醒词没反应**
先在配置页「验证提示词」里输入「小蓝小蓝，现在几点了」点比对：认得出就是识别没问题，
问题在麦克风侧的判定。看下面那行「监听状态」：如果峰值一直低于门槛，说明声音太轻，
把**灵敏度**拉高（门槛会跟着降低）；如果压根没有峰值跳动，就是没听到声音，检查麦克风。
另外回答播报期间唤醒是关掉的（防自激），等它说完再说。

**配置页「最近听到」是一堆莫名其妙的话**
whisper 在近乎静音的音频上会凭空编句子。代码里有能量门槛挡掉大部分，但环境噪声较大时仍可能漏过。
这不影响正确性（编出来的话不会命中提示词），只是白烧一点显卡。把灵敏度调低可以进一步减少。

**桌面形象不出现**
看 `.runtime/pet.log`。常见原因是缺 PySide6（`pip install PySide6`）或缺 `assets/pet.png`
（跑一次 `python pet_asset.py`）。形象显示「没有连接到助手进程」说明助手还没起好或已经退出——
它只轮询本机 HTTP，不依赖助手是否先启动。

**双击 `start.vbs` 没动静**
它启动的就是 `pythonw.exe assistant.py --no-browser` 加 `pythonw.exe pet.py`。
没有反应时先在命令行跑 `python assistant.py --no-browser` 看报错——pythonw 不显示控制台，
所以真出了问题得靠这条命令或者 `.runtime/assistant.log` 看。

**回答读出来很生硬 / 带符号**
调 `SYSTEM_PROMPT` 更严厉地禁止 Markdown；`tts.sanitize()` 也会兜底清理。

**想完全离线**
配置页把「语音合成服务」切到**离线系统语音**即可，识别本就在本地、大模型就在 Ollama，切完断网也能用。代价是声音偏机械，但实测单句合成只要 54ms，比在线快得多。

**配置页打不开 / 打开是空白**
先看控制台有没有打印 `配置页 http://127.0.0.1:xxxx`。端口被占用时会自动顺延到下一个，以打印的地址为准。若端口全被占，会提示启动失败，但语音功能不受影响。

**改了配置没反应**
大模型和合成是即时生效；识别会重建模型，页面底部会显示切换结果。若提示「需重启」，说明改的是端口这类监听参数。想让改动彻底生效，重启一次最稳。

**Git Bash / 终端里连不上 127.0.0.1:11434，但 Ollama 明明开着**
`HTTP_PROXY` 没配 `NO_PROXY`，本地请求被送去代理了。代码已对 `127.0.0.1` 自动绕开代理，命令行里手动调接口时记得用 `curl --noproxy '*'`。

**Ollama 起来了但一个模型都没有**
说明它读的是默认目录 `~/.ollama/models`。桌面版如果配置过模型目录（本机是 `D:\AI\ollamamodels`），必须启动 `ollama app.exe` 而不是 `ollama serve`。程序已按这个顺序处理。

## 目录

```
start.vbs         双击启动（助手 + 桌面形象，无窗口）
stop-all.bat      双击全关（助手 + 形象 + Ollama）
run.bat           带控制台启动助手（调试用）
config.bat        只开配置页
config.py         默认配置（会被 settings.json 覆盖）
settings_store.py 配置校验、持久化、热更新
webserver.py      Web 配置服务（标准库，只监听本机）
webui/index.html  配置页单文件前端
audio_io.py       录音流 + 音频播放
asr.py            本地 faster-whisper / 网络识别，自动回退 CPU
llm.py            Ollama / OpenAI 兼容接口，统一流式输出
tts.py            切句器 + 双引擎合成 + 播放队列
vad.py            能量断句（自适应噪声底）
wake.py           唤醒词检测 + 模糊匹配
pet.py            桌面悬浮形象（PySide6，独立进程）
pet_asset.py      形象抠图：原图 → assets/pet.png
assistant.py      主循环、语音链路、识别热重载
selftest.py       端到端自检
voices.py         音色列表与试听
stop.py           一键关闭（stop.bat / stop-all.bat 背后的实现）
tools/            换行符规整、提交前敏感信息扫描
.githooks/        pre-commit 钩子（挡住密钥被提交）
assets/pet.png    抠好透明通道的形象（pet_src.jpg 是原图，留着方便重新抠）
settings.json     配置页生成的本地配置（不入库）
```

## 提交代码前

仓库已经配好两道保险，防止把 API Key、账号配置之类的东西推上去：

```bash
git config core.hooksPath .githooks   # 新克隆后执行一次
python tools/check_secrets.py --all   # 全量扫描，随时可跑
```

* `.gitignore` 挡掉 `settings.json`、`.env*`、`models/`、`.runtime/`、日志和 pid；
  注意注释必须独占一行，写在条目后面会让整行变成一个不存在的路径，等于没忽略；
* `pre-commit` 钩子对暂存区跑一遍正则扫描（`sk-` 开头的 Key、疑似密码赋值等），
  命中就拒绝提交。

`settings.json` 里可能存着 API Key，所以它**永远不入库**；要分享配置就发 `config.py` 的改法。
