[CmdletBinding()]
param([switch]$EnableDesktop)

$ErrorActionPreference = 'Stop'
if ([System.Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) {
    throw 'Run this in the signed-in Windows desktop session.'
}

$cua = (Get-Command cua-driver.exe -CommandType Application).Source
Get-Command codex -ErrorAction Stop | Select-Object -ExpandProperty Source
& $cua --version
if ($LASTEXITCODE -ne 0) { throw 'Unable to run Cua Driver.' }

if ($EnableDesktop) {
    & $cua autostart enable
    if ($LASTEXITCODE -ne 0) { throw 'Unable to register Cua Driver for this Windows account.' }
    & $cua autostart kick
    if ($LASTEXITCODE -ne 0) { throw 'Unable to start Cua Driver in the desktop session.' }
}

whoami /user
Get-NetIPConfiguration |
    Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } |
    Select-Object InterfaceAlias, @{Name = 'IPv4'; Expression = { $_.IPv4Address.IPAddress }}
Get-Service sshd -ErrorAction SilentlyContinue
& $cua autostart status
