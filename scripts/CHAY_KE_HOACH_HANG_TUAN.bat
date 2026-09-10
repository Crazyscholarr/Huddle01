@echo off
setlocal
cd /d "%~dp0\.."
set "ADVNV_PY=venv\Scripts\python.exe"
if not exist "%ADVNV_PY%" set "ADVNV_PY=python"
if "%~1"=="" (
  echo Cach dung: scripts\CHAY_KE_HOACH_HANG_TUAN.bat "D:\du-lieu\stories.json"
  echo Hoac dien content_pipeline.weekly_input trong config.yaml roi chay:
  echo "%ADVNV_PY%" scripts\content_pipeline.py run --all
  pause
  exit /b 2
)
"%ADVNV_PY%" scripts\content_pipeline.py run --input "%~1" --all
if errorlevel 1 pause
endlocal
