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

rem The React shell is built first: PyInstaller packs boss_web/static/dist.
rem Without it the frozen exe starts into an empty page, so fail loudly here.
where pnpm >nul 2>nul
if errorlevel 1 (
  echo pnpm not found. The frontend build needs Node.js, then:
  echo   npm install -g pnpm
  echo Re-run build.bat after that.
  exit /b 1
)
call pnpm install
if errorlevel 1 exit /b 1
pushd apps\web
call pnpm run build
if errorlevel 1 (
  popd
  exit /b 1
)
popd

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
