param(
  [int]$Port = 8769,
  [string]$TokenFile = "D:\AI Projekte\YuriOS\secrets\elecrow_mcp_token"
)
$ErrorActionPreference = "Stop"
$token = (Get-Content -LiteralPath $TokenFile -Raw).Trim()
if (-not $token) { throw "Elecrow MCP token is empty" }
$env:ELECROW_MCP_TOKEN = $token
$env:ELECROW_WORKSPACE = "D:\AI Projekte\YuriOS\elecrow-firmware"
python "D:\AI Projekte\YuriOS\tools\elecrow_mcp_server.py" --http --host 0.0.0.0 --port $Port
