@echo off
rem 打成单文件 exe。产物：dist\BossAutoDelivery.exe
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo 找不到 .venv，请先在仓库根目录执行：
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
  exit /b 1
)

.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath dist --workpath build packaging\boss_web.spec
if errorlevel 1 exit /b 1

echo.
echo 打包完成： dist\BossAutoDelivery.exe
echo 自检：     dist\BossAutoDelivery.exe --self-check
