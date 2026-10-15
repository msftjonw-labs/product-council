$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name ProductCouncil `
        --collect-all openai --collect-all anthropic --collect-all docx `
        --hidden-import keyring.backends.Windows `
        desktop_launcher.py
    if ($LASTEXITCODE -ne 0) {
        throw "Executable build failed (exit code $LASTEXITCODE)."
    }
    Write-Host "Customer executable: $PSScriptRoot\dist\ProductCouncil.exe"
} finally {
    Pop-Location
}
