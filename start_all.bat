@echo off
cd /d "%~dp0"

echo ============================================
echo  MiniAgent Memory Lab - 一键启动
echo   GUI   -  127.0.0.1:8901  /  局域网同端口
echo   监控  -  127.0.0.1:8899
echo ============================================
echo.

call "%~dp0start_gui.bat"
call "%~dp0start_monitor.bat"

ping -n 3 127.0.0.1 >nul
echo [MiniAgent] 已启动。停止：关闭标题为 MiniAgent GUI / MiniAgent Monitor 的窗口。
exit /b 0
