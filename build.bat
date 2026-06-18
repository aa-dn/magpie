@echo off
setlocal enabledelayedexpansion

echo =====================================================
echo   Reverse Image Search -- Build Standalone .exe
echo =====================================================
echo.

REM ── Resolve source directory (where this .bat lives) ─
set "SRC=%~dp0"
REM Remove trailing backslash
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"

REM ── Use a local temp folder to avoid OneDrive permission issues ──
set "BUILD_DIR=%TEMP%\ris_build"
set "OUT_DIR=%SRC%\dist"

echo Source:      %SRC%
echo Build temp:  %BUILD_DIR%
echo Output:      %OUT_DIR%
echo.

REM ── Step 1: Install dependencies ─────────────────────
echo [1/4] Installing dependencies...
pip install requests openpyxl Pillow pyinstaller
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed.
    echo Make sure Python is installed and added to PATH.
    pause
    exit /b 1
)
echo.

REM ── Step 2: Copy source files to local temp folder ───
echo [2/4] Copying source files to local build folder...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%BUILD_DIR%"

copy "%SRC%\gui.py"                   "%BUILD_DIR%\gui.py"                   >nul
copy "%SRC%\reverse_image_search.py"  "%BUILD_DIR%\reverse_image_search.py"  >nul

if errorlevel 1 (
    echo.
    echo ERROR: Could not copy source files to %BUILD_DIR%
    pause
    exit /b 1
)
echo   Copied to %BUILD_DIR%
echo.

REM ── Step 3: Build the exe from the local temp folder ─
echo [3/4] Building ReverseImageSearch.exe...
echo        (This takes 1-3 minutes on first run)
echo.

cd /d "%BUILD_DIR%"

pyinstaller ^
  --onefile ^
  --windowed ^
  --name "ReverseImageSearch" ^
  --hidden-import=openpyxl ^
  --hidden-import=openpyxl.cell._writer ^
  --hidden-import=PIL ^
  --hidden-import=PIL.Image ^
  --hidden-import=PIL.JpegImagePlugin ^
  --hidden-import=PIL.PngImagePlugin ^
  --hidden-import=PIL.GifImagePlugin ^
  --hidden-import=PIL.WebPImagePlugin ^
  --hidden-import=PIL.BmpImagePlugin ^
  --hidden-import=requests ^
  --hidden-import=charset_normalizer ^
  --collect-all openpyxl ^
  --distpath "%BUILD_DIR%\dist" ^
  --workpath "%BUILD_DIR%\work" ^
  --specpath "%BUILD_DIR%" ^
  --clean ^
  --noconfirm ^
  gui.py

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. Check the output above.
    pause
    exit /b 1
)

REM ── Step 4: Copy exe back to project dist folder ─────
echo.
echo [4/4] Copying exe to output folder...

if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
copy "%BUILD_DIR%\dist\ReverseImageSearch.exe" "%OUT_DIR%\ReverseImageSearch.exe" >nul

if errorlevel 1 (
    echo.
    echo ERROR: Could not copy exe to %OUT_DIR%
    echo The built file is still available at: %BUILD_DIR%\dist\ReverseImageSearch.exe
    pause
    exit /b 1
)

echo.
echo =====================================================
echo   Done!
echo.
echo   Executable:  %OUT_DIR%\ReverseImageSearch.exe
echo.
echo   This single file can be sent to anyone on Windows.
echo   No Python or other software required to run it.
echo =====================================================
echo.

explorer "%OUT_DIR%"
pause
