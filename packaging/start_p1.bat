@echo off
REM =====================================================================
REM 微信客服系统 P1 一键启动（托盘 + 默认浏览器 + 自动更新）
REM 使用 bundle 自带的嵌入式 Python 启动 launcher.pyc（编译后的字节码）
REM 由 build_p1.py 复制到离线包根目录并统一为 GBK + CRLF
REM =====================================================================
setlocal
set "BUNDLE=%~dp0"
set "PYEXE=%BUNDLE%runtime\python\python.exe"
set "LAUNCHER=%BUNDLE%launcher.pyc"

if not exist "%PYEXE%" (
    echo [错误] 找不到内置 Python：%PYEXE%
    echo 请确认程序完整，不要移动或删除 runtime 目录。
    pause
    exit /b 1
)
if not exist "%LAUNCHER%" (
    echo [错误] 找不到启动程序：%LAUNCHER%
    pause
    exit /b 1
)

start "" "%PYEXE%" "%LAUNCHER%"
echo 微信客服已启动到系统托盘，稍后会自动打开操作界面。
echo 若未自动打开，请双击托盘图标选择「打开操作界面」。
endlocal
