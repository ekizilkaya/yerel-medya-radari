@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

rem Python'u bul: once proje sanal ortami, sonra Anaconda, sonra PATH
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PY=%USERPROFILE%\anaconda3\python.exe"
) else (
    set "PY=python"
)

echo Yerel Medya Radari baslatiliyor...  http://localhost:8000
echo (LM Studio'nun acik olmasi gerekmez; modeller dogrudan yuklenir.)
echo.
start "" http://localhost:8000
"%PY%" app\server.py
pause
