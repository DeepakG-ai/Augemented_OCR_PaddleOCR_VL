# build.ps1 — build client_agent.exe with PyInstaller
# Run from the client/ folder:  .\build.ps1

$ErrorActionPreference = "Stop"

$Python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

# Install deps if needed
& $Python -m pip install -r requirements.txt

# Build single-file exe. The agent only needs stdlib + requests, so exclude
# backend/OCR/science packages even if they exist in the build environment.
& $Python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --name "augocr-agent" `
    --exclude-module "asyncpg" `
    --exclude-module "cv2" `
    --exclude-module "fastapi" `
    --exclude-module "mlflow" `
    --exclude-module "numpy" `
    --exclude-module "paddle" `
    --exclude-module "paddleocr" `
    --exclude-module "paddlex" `
    --exclude-module "pandas" `
    --exclude-module "PIL" `
    --exclude-module "torch" `
    --exclude-module "transformers" `
    --exclude-module "uvicorn" `
    client_agent.py

$exe = Join-Path $PSScriptRoot "dist\augocr-agent.exe"
$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 2)

Write-Host ""
Write-Host "Build complete: dist\augocr-agent.exe ($sizeMb MB)"
Write-Host "Copy dist\augocr-agent.exe and client_agent.json to the client machine."
Write-Host "Edit client_agent.json to set the correct server_url before distributing."
