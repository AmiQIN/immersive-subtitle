@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem 在 PATH 中查找 pythonw 解释器
set "RT="
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
  set "RT=%%~dpP"
  goto run
)

echo 找不到 pythonw.exe。
echo 请先安装 Python 并装好本项目依赖；普通用户请使用打包好的客户端，无需命令行。
pause
exit /b 1

:run
if not exist "%~dp0oversub.py" (
  echo 找不到 oversub.py。
  pause
  exit /b 1
)
start "" "!RT!\pythonw.exe" "%~dp0oversub.py"
exit /b 0
