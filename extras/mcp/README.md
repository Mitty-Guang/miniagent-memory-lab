# MCP 接入（把沙箱工具标准化暴露）

把本项目的沙箱工具集（Python 执行 / 文件读写 / 命令执行）包成标准 **MCP Server**，
任何 MCP 兼容客户端（Claude Desktop、Cursor、其他 Agent 框架）都能通过 stdio 发现并调用。

```
extras/mcp/
├── mcp_server.py    # MCP Server：暴露 run_python / read_file / write_file / list_dir / run_command
└── client_demo.py   # MCP 客户端演示：连接 → 列出工具 → 调用（含黑名单拦截演示）
```

## 运行

```powershell
.\.venv\Scripts\python.exe -m pip install -r extras\mcp\requirements-extras.txt

# 客户端演示（会自动以 stdio 拉起 server）
.\.venv\Scripts\python.exe extras\mcp\client_demo.py
```

## 接入其他 MCP 客户端

```jsonc
{
  "mcpServers": {
    "miniagent-tools": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["<repo>/extras/mcp/mcp_server.py"],
      "env": { "MCP_SANDBOX_ROOT": "<允许操作的目录>" }
    }
  }
}
```

## 设计要点

- 复用 `mini_agent/sandbox.py`：子进程隔离、路径白名单、命令黑名单、超时；
- 工具描述即接口文档（MCP 的 `description` 会被客户端 Agent 读取用于选择工具）；
- 沙箱根目录由 `MCP_SANDBOX_ROOT` 控制（默认进程 cwd）。
