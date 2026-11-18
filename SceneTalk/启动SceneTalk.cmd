@echo off
cd /d "%~dp0"
if not exist "%~dp0SceneTalk.html" (
  echo 找不到 SceneTalk.html，请确认本文件和 SceneTalk.html 在同一个文件夹。
  pause
  exit /b
)
where pythonw.exe >nul 2>nul
if %errorlevel% equ 0 (
  start "" pythonw "%~dp0server.pyw"
) else (
  start "" /min python "%~dp0server.pyw"
)
