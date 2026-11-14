@echo off
title 视频内容提取器 - 本地服务

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [提示] 服务已在运行，直接打开浏览器...
    start "" http://127.0.0.1:8000
    ping -n 3 127.0.0.1 >nul
    exit /b
)

echo ================================================
echo   正在启动本地服务（首次约需几秒）...
echo   地址: http://127.0.0.1:8000
echo ================================================

set HF_ENDPOINT=https://hf-mirror.com
rem 真实转写引擎（large-v3）；要改配置请编辑项目 .env
start "视频提取器-后端" /min "%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

ping -n 6 127.0.0.1 >nul
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo 服务已就绪，正在打开浏览器...
    start "" http://127.0.0.1:8000
) else (
    echo [错误] 服务未能启动，请查看刚弹出的「视频提取器-后端」窗口里的报错信息。
    pause
)
exit /b
