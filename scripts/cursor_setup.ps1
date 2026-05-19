# xskill + Cursor one-shot setup (dirs, junction, import, registry)
# Run from repo root: powershell -ExecutionPolicy Bypass -File scripts\cursor_setup.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent

$XskillHome = Join-Path $env:USERPROFILE ".xskill"
$SkillStore = Join-Path $XskillHome "skill"
$CursorImport = Join-Path $XskillHome "cursor_import"
$CursorSkills = Join-Path $env:USERPROFILE ".cursor\skills"
$ConfigPath = Join-Path $XskillHome "config.yaml"
$ExampleConfig = Join-Path $RepoRoot "examples\config.yaml.example"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$XskillExe = Join-Path $RepoRoot ".venv\Scripts\xskill.exe"

Write-Host "`n[Step 1] Create directories"
New-Item -ItemType Directory -Force -Path $XskillHome, $SkillStore, $CursorImport | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE ".cursor") | Out-Null

Write-Host "`n[Step 2] Copy config.yaml if missing"
if (-not (Test-Path $ConfigPath)) {
    Copy-Item $ExampleConfig $ConfigPath
    Write-Host "Edit $ConfigPath and set llm.api_key / embedding.api_key"
}

Write-Host "`n[Step 3] Junction: .cursor\skills -> .xskill\skill"
if (Test-Path $CursorSkills) {
    $item = Get-Item $CursorSkills -Force
    $reparse = ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    if ($reparse) {
        Write-Host "Already linked: $CursorSkills"
    } else {
        Write-Warning "$CursorSkills exists and is not a junction; remove it manually first."
    }
} else {
    cmd /c "mklink /J `"$CursorSkills`" `"$SkillStore`""
}

Write-Host "`n[Step 4] pip install -e .[dev]"
Set-Location $RepoRoot
if (-not (Test-Path $VenvPython)) { python -m venv .venv }
& $VenvPython -m pip install -q -e ".[dev]"

Write-Host "`n[Step 5] Import Cursor agent-transcripts"
& $VenvPython (Join-Path $RepoRoot "scripts\cursor_import.py")

Write-Host "`n[Step 6] registry add cursor_import"
& $XskillExe registry add $CursorImport --label cursor_import
& $XskillExe registry list

Write-Host "`nDone. Next: edit config.yaml keys, then:"
Write-Host "  .\.venv\Scripts\xskill.exe serve --host 127.0.0.1 --port 8000"
