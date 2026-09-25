$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$TaskName = "wechat-junshi-mobile"
$Python = Join-Path (Get-Location) ".venv-mobile\\Scripts\\python.exe"
if (-not (Test-Path $Python)) {
    throw "Missing .venv-mobile. Run 4-install-mobile-workbench.bat first."
}

$admin = [Environment]::GetEnvironmentVariable("JUNSHI_ADMIN_TOKEN", "User")
$deepseek = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($admin) -or $admin.Length -lt 32) {
    throw "Set JUNSHI_ADMIN_TOKEN as a persistent USER environment variable before enabling autostart."
}
if ([string]::IsNullOrWhiteSpace($deepseek)) {
    throw "Set DEEPSEEK_API_KEY as a persistent USER environment variable before enabling autostart."
}

$origin = [Environment]::GetEnvironmentVariable("JUNSHI_PUBLIC_ORIGIN", "User")
if ([string]::IsNullOrWhiteSpace($origin)) {
    [Environment]::SetEnvironmentVariable("JUNSHI_PUBLIC_ORIGIN", "http://127.0.0.1:8787", "User")
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $Python -Argument "-m mobile" -WorkingDirectory (Get-Location).Path
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "wechat-junshi Android control and WeChat bridge. Requires an interactive logged-in Windows session." -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "It starts at your Windows logon and restarts after unexpected exits."
Write-Host "B mode still requires this same interactive user session to keep WeChat logged in and usable."
