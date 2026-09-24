@echo off
rem 重建桌面快捷方式（移动仓库位置或换机后运行一次即可）
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$repo = '%~dp0'.TrimEnd('\');" ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$shell = New-Object -ComObject WScript.Shell;" ^
  "$items = @(@{n='MiniAgent（GUI+监控）.lnk'; b='start_all.bat'; d='一键启动 MiniAgent GUI(:8901) 与实验监控(:8899)'}, @{n='MiniAgent GUI.lnk'; b='start_gui.bat'; d='只启动 MiniAgent GUI(:8901，局域网可访问)'});" ^
  "foreach ($it in $items) { $l = $shell.CreateShortcut((Join-Path $desktop $it.n)); $l.TargetPath = (Join-Path $repo $it.b); $l.WorkingDirectory = $repo; $l.Description = $it.d; $l.IconLocation = \"$env:SystemRoot\System32\shell32.dll,13\"; $l.Save(); Write-Host ('已创建: ' + $it.n) }"
echo.
echo 桌面快捷方式已重建。
pause >nul
