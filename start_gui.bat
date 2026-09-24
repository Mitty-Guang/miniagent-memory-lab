@echo off
cd /d "%~dp0"

rem 选解释器：优先 .venv312（支持 LangGraph interrupt），回退 .venv
set "PY=%CD%\.venv312\Scripts\python.exe"
if not exist "%PY%" set "PY=%CD%\.venv\Scripts\python.exe"

echo [MiniAgent] 解释器: %PY%
echo [MiniAgent] 启动 GUI - 本机 127.0.0.1:8901 / 局域网同端口，无口令
start "MiniAgent GUI" "%PY%" scripts\gui.py --port 8901 --host 0.0.0.0

ping -n 5 127.0.0.1 >nul
start "" http://127.0.0.1:8901/
echo [MiniAgent] 浏览器已打开；GUI 日志在标题为 MiniAgent GUI 的窗口里，关掉该窗口即停止服务。
exit /b 0
