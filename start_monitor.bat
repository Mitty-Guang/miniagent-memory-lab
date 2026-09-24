@echo off
cd /d "%~dp0"

set "PY=%CD%\.venv312\Scripts\python.exe"
if not exist "%PY%" set "PY=%CD%\.venv\Scripts\python.exe"

echo [MiniAgent] 启动实验监控面板 - 127.0.0.1:8899（读取 results/sweep_*.json + progress.log）
start "MiniAgent Monitor" "%PY%" scripts\monitor.py --port 8899
ping -n 4 127.0.0.1 >nul
start "" http://127.0.0.1:8899/
exit /b 0
