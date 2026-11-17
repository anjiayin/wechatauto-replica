@echo off
REM =====================================================================
REM 微信客服系统 一键启动（使用 bundle 自带的嵌入式 Python，开箱即用）
REM 由 build_p0.py 复制到离线包根目录；请勿用系统/veighna 的 python 启动，
REM 否则会因缺 pyperclip / uiautomation 等依赖导致图片语音功能降级。
REM =====================================================================
setlocal
set "BUNDLE=%~dp0"
set "PYEXE=%BUNDLE%runtime\python\python.exe"
set "KEFUAPP=%BUNDLE%kefu-system"

if not exist "%PYEXE%" (
    echo [错误] 找不到内置 Python：%PYEXE%
    echo 请确认离线包完整，不要移动或删除 runtime 目录。
    pause
    exit /b 1
)

REM 启动器职责：版本真源 / 资源根 / QT 可视化 / PATH 追加 runtime
set "KEFU_VERSION_FILE=%BUNDLE%version.txt"
set "KEFU_APP_ROOT=%KEFUAPP%"
set "QT_ACCESSIBILITY=1"
set "PATH=%BUNDLE%runtime;%PATH%"

cd /d "%KEFUAPP%"
echo 正在启动微信客服系统，内置 Python：%PYEXE%
echo 启动后浏览器访问：http://127.0.0.1:8765
echo.
"%PYEXE%" "%KEFUAPP%\run.py" %*

set "CODE=%ERRORLEVEL%"
echo.
echo 服务已退出（code=%CODE%）。
pause
endlocal & exit /b %CODE%
