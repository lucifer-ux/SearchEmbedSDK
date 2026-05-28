# MCP Integration

This project now includes a separate MCP adapter server in `mcp_server.py`.

The adapter does not replace existing logic. It calls your existing FastAPI
backend (`unimain.py`) over HTTP and exposes MCP tools on top.

## 1) Start Unified Backend

```powershell
cd SearchEmbedSDK
uvicorn unimain:app --host 127.0.0.1 --port 8000
```

## 2) Start MCP Server (stdio)

```powershell
cd SearchEmbedSDK
<!-- to be added as proper contextcore  -->
python mcp_server.py
```

Environment variables:

- `CONTEXTCORE_API_BASE_URL` (default: `http://127.0.0.1:8000`)
- `CONTEXTCORE_MCP_TIMEOUT_SECONDS` (default: `120`)
- `MCP_TRANSPORT` (default: `stdio`)

## 3) Claude Desktop Config

Add an MCP server entry pointing to `mcp_server.py`.

```json
{
  "mcpServers": {
    "contextcore-unified": {
      "command": "C:\\Users\\USER\\Documents\\SDKSearchImplementation\\SearchEmbedSDK\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\USER\\Documents\\SDKSearchImplementation\\SearchEmbedSDK\\mcp_server.py"
      ],
      "env": {
        "CONTEXTCORE_API_BASE_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

## 4) Tools Exposed by MCP Server

- `server_info`
- `health`
- `unified_search`
- `run_llm`
- `index_scan`
- `image_index_status`
- `recent_activity`
- `file_preflight`
- `storage_status`

## 5) Other LLM Interfaces

Any interface that supports MCP stdio can run the same command setup.

For MCP hosts that support non-stdio transports, you can set:

```powershell
$env:MCP_TRANSPORT="streamable-http"
python mcp_server.py
```

`mcp_server.py` is intentionally isolated so backend behavior in `unimain.py`
stays unchanged.
