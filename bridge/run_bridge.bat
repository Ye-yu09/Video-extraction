@echo off
rem 定时心跳：提交 bili-scroll 新挑中的视频 → 提取器；回收完成写 essence。钉死用提取器 venv 的 python。
cd /d "%~dp0"
"..\.venv\Scripts\python.exe" bili_bridge.py --autostart >> bridge.log 2>&1
