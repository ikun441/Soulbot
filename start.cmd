@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 echo 提示：未安装 Git；启动不受影响，更新项目时需要 Git。
if defined SOULBOT_PYTHON (
  "%SOULBOT_PYTHON%" scripts\bootstrap.py %*
  goto finish
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\bootstrap.py %*
  goto finish
)
where py >nul 2>nul
if not errorlevel 1 (
  for %%V in (3.11 3.12 3.13) do (
    py -%%V -c "import sys" >nul 2>nul
    if not errorlevel 1 (
      py -%%V scripts\bootstrap.py %*
      goto finish
    )
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python scripts\bootstrap.py %*
  goto finish
)
echo 未找到 Python 3.11–3.13，请按 README.md 安装 Python 并勾选添加到 PATH。
exit /b 1
:finish
set "SOULBOT_EXIT=%errorlevel%"
if not "%SOULBOT_EXIT%"=="0" (
  echo 启动失败，请查看上方错误和 README.md。
  pause
)
exit /b %SOULBOT_EXIT%
