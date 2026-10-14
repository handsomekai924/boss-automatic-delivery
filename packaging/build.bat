@echo off
rem Build onedir package + release zip.
rem Output: dist/BossAutoDelivery/            install dir (exe + _internal)
rem         dist/BossAutoDelivery-{ver}.zip   upload as Gitee Release asset
rem
rem Keep this file ASCII-only + CRLF. Non-ASCII text in a bat file makes
rem cmd.exe reparse on codepage switches and shred later lines into garbage
rem commands. Also never leave an odd number of double quotes on a rem line.
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
  echo .venv not found. From the repo root run:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
  exit /b 1
)

rem Wipe dist first. A leftover onefile exe / data/ from an older layout
rem otherwise sits next to the new build and people ship the wrong thing.
if exist "dist" (
  echo Clearing dist\ ...
  rd /s /q "dist"
)
if exist "dist" (
  echo Could not delete dist\. Close anything using files in it and retry.
  exit /b 1
)

.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath dist --workpath build packaging\boss_web.spec
if errorlevel 1 exit /b 1

.venv\Scripts\python.exe packaging\make_zip.py
if errorlevel 1 exit /b 1

echo.
echo Build OK:
echo   install dir   dist\BossAutoDelivery\
echo   release zip   dist\BossAutoDelivery-VERSION.zip  ^(upload as Gitee Release asset^)
echo   self check    dist\BossAutoDelivery\BossAutoDelivery.exe --self-check
pause
