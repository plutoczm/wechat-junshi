$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.12 first."
}

py -3.12 -m venv .venv-mobile
& .\.venv-mobile\Scripts\python.exe -m pip install --upgrade pip
& .\.venv-mobile\Scripts\python.exe -m pip install -r requirements-wechat.txt

$skill = ".vendor\goutoujunshi"
if (-not (Test-Path "$skill\SKILL.md")) {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        New-Item -ItemType Directory -Force ".vendor" | Out-Null
        git clone --depth 1 https://github.com/shengjidaguai-china/goutoujunshi.git $skill
    } else {
        Write-Warning "git is unavailable; set GOUTOUJUNSHI_SKILL_DIR manually to keep the upstream skill reference."
    }
}

Write-Host ""
Write-Host "Install complete."
Write-Host "Set JUNSHI_ADMIN_TOKEN and DEEPSEEK_API_KEY in your own environment."
Write-Host "Then run: 5-start-mobile-workbench.bat"
