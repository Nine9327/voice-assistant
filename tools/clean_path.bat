@echo off
chcp 65001 >nul
rem Remove dead miniconda entries from the SYSTEM Path.
rem Needs Administrator: right click -> Run as administrator.

net session >nul 2>&1
if errorlevel 1 (
  echo [!] This needs Administrator rights.
  echo     Right click this file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -Command "$o=[Environment]::GetEnvironmentVariable('Path','Machine'); $dead=@($o -split ';' | Where-Object { $_ -and ($_ -match 'miniconda') }); if($dead.Count -eq 0){ Write-Host 'Nothing to clean - no miniconda entry in the system Path.' } else { $keep=@($o -split ';' | Where-Object { $_ -and ($_ -notmatch 'miniconda') }); $bak=Join-Path $env:USERPROFILE ('system-path-backup-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.txt'); Set-Content -LiteralPath $bak -Value $o -Encoding UTF8; [Environment]::SetEnvironmentVariable('Path',($keep -join ';'),'Machine'); Write-Host ('Backup saved: ' + $bak); Write-Host ('Removed ' + $dead.Count + ' dead entries:'); $dead | ForEach-Object { Write-Host ('  ' + $_) }; Write-Host 'Restart the terminal to pick up the new Path.' }"

echo.
pause
