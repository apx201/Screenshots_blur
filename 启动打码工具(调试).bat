@echo off
rem 截图打码工具 · 调试启动器（保留控制台，能看到报错信息）
setlocal enabledelayedexpansion
set "HERE=%~dp0"
set "SCRIPT=%HERE%uid_blur.py"
set "PY="

if exist "%HERE%python.exe" set "PY=%HERE%python.exe"

if not defined PY (
    for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do (
        if exist "%%D\dependencies\python\python.exe" set "PY=%%D\dependencies\python\python.exe"
    )
)

if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)

if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)

if not defined PY (
    echo [错误] 没有找到 Python 运行环境。
    echo 请安装 Python 3.9+ 并勾选 Add Python to PATH，或把 python.exe 放到本目录。
    pause
    exit /b 1
)

echo 使用 Python: %PY%
"%PY%" -c "import PIL; print('Pillow', PIL.__version__)" || (
    echo [错误] 该 Python 缺少 Pillow 库，请先执行: "%PY%" -m pip install pillow
    pause
    exit /b 1
)

"%PY%" "%SCRIPT%" %*

if errorlevel 1 pause
