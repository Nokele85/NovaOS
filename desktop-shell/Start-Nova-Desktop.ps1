param(
  [string]$ServerUrl = 'https://nova.example.org/characters/nova/live2d?desktop=1',
  [int]$Width = 280,
  [int]$Height = 500
)

$ErrorActionPreference = 'Stop'
$shell = Split-Path -Parent $MyInvocation.MyCommand.Path
$tokenFile = 'D:\AI Projekte\YuriOS\secrets\owner_token'

if (-not (Test-Path -LiteralPath (Join-Path $shell 'node_modules\electron\dist\electron.exe'))) {
  throw 'Electron fehlt. Einmal aus diesem Ordner `npm.cmd install` ausführen.'
}
if (-not (Test-Path -LiteralPath $tokenFile)) {
  throw "Token-Datei nicht gefunden: $tokenFile"
}

Set-Location -LiteralPath $shell
& npm.cmd start -- `
  "--url=$ServerUrl" `
  "--token-file=$tokenFile" `
  "--width=$Width" `
  "--height=$Height" `
  '--on-top=true'
