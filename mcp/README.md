# 把自己的 MCP 服务放在这里，助手会自动认出来

不用在配置页一条条添加 —— 往这个目录里丢文件就行，重启助手（或在 MCP 页面点「重连」）即可生效。

## 两种放法

**1. 单个文件**（最省事）

```
mcp/
├── weather.py        → 自动识别成服务「weather」，用当前 Python 解释器跑
├── browser.js        → 自动识别成服务「browser」，用 node 跑
└── mytool.exe        → 直接作为可执行文件跑
```

**2. 一个目录**（要用到多个文件、依赖或环境变量时）

```
mcp/
└── weather/
    ├── manifest.json   ← 可选
    └── server.py       ← 入口，按 server / main / __main__ / index 顺序找
```

`manifest.json` 全部字段都可省略，省略时按上面那条顺序猜入口：

```json
{
  "name": "weather",
  "command": "D:\\python.exe",
  "args": ["D:\\tools\\weather\\server.py"],
  "env": { "API_KEY": "xxx" },
  "enabled": true
}
```

- `name`：显示名，缺省用目录名
- `enabled: false`：留着但不加载（临时关掉一个服务很方便）
- 写了 `command` 就完全按你写的执行，不再猜入口

## 规矩

- **下划线或点开头**的目录/文件会被跳过 —— `_example/` 就是模板，复制改名就能用；
- 在配置页手动配过的**同名服务优先**，目录里的不会覆盖它；
- 目录里的服务在配置页是**只读**的：能测试连接、看工具列表，但不能在这里删改 ——
  改目录里的文件才是唯一的真相；
- 填了 `env` 的 Key 不会出现在配置页上，也不会写进 `mcp_servers.json`。

## 关于安全

这里放什么就会被执行什么 —— 只放你自己写的、或者你看过源码的服务。
外部下载的服务请先读一眼再放进来。
