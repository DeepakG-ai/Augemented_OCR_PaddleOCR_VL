# build.ps1 — build client_agent.exe with PyInstaller
# Run from the client/ folder:  .\build.ps1

$ErrorActionPreference = "Stop"

# Install deps if needed
pip install -r requirements.txt

# Build single-file exe
pyinstaller `
    --onefile `
    --console `
    --name "augocr-agent" `
    --add-data "client_agent.json;." `
    client_agent.py

Write-Host ""
Write-Host "Build complete: dist\augocr-agent.exe"
Write-Host "Copy dist\augocr-agent.exe and client_agent.json to the client machine."
Write-Host "Edit client_agent.json to set the correct server_url before distributing."
