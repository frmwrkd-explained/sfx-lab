param([Alias('-uninstall')][switch]$Uninstall, [string]$Lab)
$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot
$SkillTarget = Join-Path $HOME '.claude/skills/sfx-lab'
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw 'Installation command failed.' } }
$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
$Uv = if ($UvCommand) { $UvCommand.Source } else { Join-Path $HOME '.local/bin/uv.exe' }
if ($Uninstall) {
    if (Test-Path $Uv) { & $Uv tool uninstall sfx-lab }
    # Remove links or the marked copy installed by the previous Windows installer.
    $Target = Get-Item -Force $SkillTarget -ErrorAction SilentlyContinue
    if ($Target -and $Target.LinkType) { $Target.Delete() }
    elseif (Test-Path (Join-Path $SkillTarget '.sfx-lab-installed')) { Remove-Item -Recurse -Force $SkillTarget }
    Write-Host 'Removed the tool and installed skill. Your lab is untouched.'
    exit 0
}
if (-not (Test-Path $Uv)) {
    Invoke-Expression (Invoke-RestMethod 'https://astral.sh/uv/0.11.7/install.ps1')
    $Uv = Join-Path $HOME '.local/bin/uv.exe'
}
& $Uv tool install --force --python 3.12 $Repo
Check-Exit
& $Uv tool update-shell
$BinDir = & $Uv tool dir --bin
$SfxLab = if ($BinDir) { Join-Path $BinDir 'sfx-lab.exe' } else { Join-Path $HOME '.local/bin/sfx-lab.exe' }
if (-not (Test-Path $SfxLab)) { $SfxLab = Join-Path $HOME '.local/bin/sfx-lab.exe' }
if (-not $Lab) {
    $Python = & $Uv python find 3.12
    Check-Exit
    $Lab = & $Python -c "import json,os; from pathlib import Path; p=Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'sfx-lab/config.json'; d=json.loads(p.read_text()) if p.exists() else {}; r=Path(d.get('default_lab','')).expanduser(); print(r if (r/'.sfx-lab/config.json').is_file() else '')"
    Check-Exit
}
if (-not $Lab) {
    if (-not [Console]::IsInputRedirected) { $Lab = Read-Host "Sound library folder [$HOME/SFX Lab]" }
    if (-not $Lab) { $Lab = Join-Path $HOME 'SFX Lab' }
}
& $SfxLab init $Lab
Check-Exit
New-Item -ItemType Directory -Force (Split-Path $SkillTarget) | Out-Null
$Target = Get-Item -Force $SkillTarget -ErrorAction SilentlyContinue
if ($Target -and $Target.LinkType) { $Target.Delete() }
elseif ($Target) {
    $Backup = "$SkillTarget.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    while (Test-Path $Backup) { $Backup += '-1' }
    Move-Item $SkillTarget $Backup
}
# Junctions work without Windows Developer Mode or administrator rights.
New-Item -ItemType Junction -Path $SkillTarget -Target (Join-Path $Repo 'skill') | Out-Null
Write-Host 'Done. In Claude Code, say: Open SFX Lab. Then click the gear icon, paste your ElevenLabs API key, and press Save.'
