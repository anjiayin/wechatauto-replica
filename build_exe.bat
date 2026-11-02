@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   微信适配程序打包
echo ========================================
echo.

cd /d D:\Git\wechatauto-replica

call D:\my_conda_envs\wechat-adapter\Scripts\activate.bat

echo [1/3] 正在构建...
D:\my_conda_envs\wechat-adapter\Scripts\pyinstaller.exe ^
    --name wechat-adapter ^
    --onefile ^
    --console ^
    --clean ^
    --noconfirm ^
    --add-data "wx_adapter_config.json;." ^
    --hidden-import wechatauto ^
    --hidden-import wechatauto.db ^
    --hidden-import wechatauto.guia ^
    --hidden-import wechatauto.sender ^
    --hidden-import wechatauto.media ^
    --hidden-import wechatauto.moment ^
    --hidden-import wechatauto.uia_driver ^
    --hidden-import wechatauto.utils ^
    --hidden-import wechatauto.utils.lock ^
    --hidden-import wechatauto.ui ^
    --hidden-import wechatauto.uia ^
    --hidden-import wechatauto.msgs ^
    --hidden-import websockets ^
    --hidden-import websockets.legacy ^
    --hidden-import websockets.legacy.server ^
    --hidden-import uiautomation ^
    --hidden-import win32api ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import win32process ^
    --hidden-import pywintypes ^
    --hidden-import pyperclip ^
    --hidden-import PIL ^
    --hidden-import psutil ^
    --hidden-import colorama ^
    --hidden-import cryptography ^
    --hidden-import winsdk ^
    --hidden-import imageio_ffmpeg ^
    --hidden-import zstandard ^
    --hidden-import pyautogui ^
    --hidden-import cv2 ^
    --hidden-import numpy ^
    --collect-all wechatauto ^
    --collect-all websockets ^
    --collect-all uiautomation ^
    --distpath dist ^
    --workpath build ^
    --specpath . ^
    adapter\__main__.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] 构建失败！
    pause
    exit /b 1
)

echo.
echo [2/3] 复制配置文件到 dist 目录...
copy wx_adapter_config.json dist\wx_adapter_config.json >nul

echo [3/3] 构建完成！
echo.
echo 输出文件: dist\wechat-adapter.exe
echo 配置文件: dist\wx_adapter_config.json
echo.
echo 运行方式: 
echo   1. 先启动 zhb_client (npm start)
echo   2. 再运行 dist\wechat-adapter.exe
echo.
pause
