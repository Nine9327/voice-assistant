"""模板：最小的 stdio MCP 服务（复制这个目录改名就能用）。

MCP 的最小实现只要三个方法：initialize / tools/list / tools/call。
协议是「一行一个 JSON」的 JSON-RPC 2.0，不需要任何第三方依赖。
"""
import json
import sys

TOOLS = [
    {
        "name": "hello",
        "description": "打个招呼，用来验证服务通了",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "名字"}},
        },
    },
]


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "example", "version": "1.0"},
            }})
        elif method == "notifications/initialized":
            pass  # 通知不需要回复
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            if params.get("name") == "hello":
                who = args.get("name") or "世界"
                text = f"你好，{who}！"
            else:
                text = f"没有这个工具：{params.get('name')}"
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}],
            }})
        elif mid is not None:
            # 不认识的方法也要回一个，否则客户端会一直等
            send({"jsonrpc": "2.0", "id": mid, "result": {}})


if __name__ == "__main__":
    main()
