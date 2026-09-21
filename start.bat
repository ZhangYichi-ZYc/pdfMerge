@echo off
REM 启动 PDF 合并服务（Windows）。
REM
REM 换一台机器部署时，若 python 不在 PATH 上，先设置：
REM     set PYTHON=D:\path\to\python.exe
REM 端口等其余配置见 README 的环境变量表。

setlocal
pushd "%~dp0"

if "%PYTHON%"=="" set PYTHON=python

"%PYTHON%" -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo.
    echo 依赖尚未安装。请先执行：
    echo     "%PYTHON%" -m pip install -r requirements.txt
    echo.
    popd
    exit /b 1
)

echo 正在启动 PDF 合并服务（按 Ctrl+C 停止）...
"%PYTHON%" -m backend %*

popd
endlocal
