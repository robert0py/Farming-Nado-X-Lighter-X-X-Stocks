param(
    [string]$Python = 'python',
    [string]$OutputDirectory = 'dist'
)

$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectDirectory
try {
    & $Python -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --collect-all lighter --hidden-import eth_account --hidden-import cryptography `
        --name Farming-Accounts --paths src --specpath build --workpath build/pyinstaller `
        --distpath $OutputDirectory run.py
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed' }
    $desktopExecutable = (Resolve-Path -LiteralPath (Join-Path $OutputDirectory 'Farming-Accounts.exe')).Path
    $checkProcess = Start-Process -FilePath $desktopExecutable -ArgumentList '--self-check' -WindowStyle Hidden -PassThru -Wait
    if ($checkProcess.ExitCode -ne 0) { throw 'Packaged application self-check failed' }
    $checksum = (Get-FileHash -LiteralPath $desktopExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt') -Value "$checksum  Farming-Accounts.exe" -Encoding ascii
    Write-Output "Built and verified: $desktopExecutable"
}
finally {
    Pop-Location
}
