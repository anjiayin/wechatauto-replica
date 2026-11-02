@echo off
chcp 65001 >nul 2>&1
cd /d D:\Git\wechatauto-replica
call D:\my_conda_envs\wechat-adapter\Scripts\activate.bat
python -m adapter
pause
