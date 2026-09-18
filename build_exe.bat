@echo off
setlocal enabledelayedexpansion

REM ===========================================================================
REM  SPOTCHECK WINDOWS EXE PACKAGING
REM
REM  Notes on why this is shaped the way it is:
REM
REM  * Everything runs through "python -m". On a machine with more than one
REM    Python, a bare "pip" or "pyinstaller" can belong to a DIFFERENT
REM    interpreter than "python" - you then install the dependencies into one
REM    environment and build against another, and the exe is missing modules
REM    that "pip list" swears are installed.
REM
REM  * PyInstaller EXITS 0 EVEN WHEN IT CANNOT FIND A HIDDEN IMPORT. It prints
REM    "ERROR: Hidden import 'x' not found" and carries on to produce a
REM    perfectly ordinary-looking exe with the module missing. Measured, not
REM    assumed: a build with four unresolvable pyzbar imports still returned
REM    errorlevel 0. So the build log is searched afterwards - the exit code
REM    alone cannot tell you the build is sound.
REM
REM  * Every tab is created inside a try/except. A module the build missed does
REM    not crash anything; the tab just says it is unavailable and the feature
REM    is silently gone. That is why this script launches the built exe once
REM    and reads its own startup log before calling the build a success.
REM ===========================================================================

echo ===============================================================================
echo                 SPOTCHECK WINDOWS EXE PACKAGING AUTOMATION
echo ===============================================================================
echo.

set "BUILD_LOG=build_output.log"
set "APP_DIR=dist\SpotCheck"

REM --------------------------------------------------------------------------
REM 1. Python
REM --------------------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not on PATH.
    echo         Install Python 3.10+ with "Add Python to PATH" ticked.
    pause
    exit /b 1
)
for /f "delims=" %%V in ('python --version 2^>^&1') do set "PYVER=%%V"
echo [1/6] Using !PYVER!
python -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
    echo [ERROR] This is a 32-bit Python. SpotCheck needs 64-bit
    echo         ^(libzbar-64.dll and the OpenCV wheels are 64-bit only^).
    pause
    exit /b 1
)

REM --------------------------------------------------------------------------
REM 2. Dependencies
REM --------------------------------------------------------------------------
echo.
echo [2/6] Installing dependencies...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [WARNING] requirements.txt failed. Retrying the critical packages...
    python -m pip install customtkinter pyinstaller pymupdf pdfplumber opencv-python numpy pyzbar openpyxl Pillow
    if errorlevel 1 (
        echo [ERROR] Dependencies could not be installed. Stopping.
        pause
        exit /b 1
    )
)

REM --------------------------------------------------------------------------
REM 3. Import check BEFORE building
REM
REM Three minutes of PyInstaller is a slow way to discover a typo. Importing
REM every module first fails in two seconds instead, and proves the packages
REM resolve in the very interpreter that is about to do the build.
REM --------------------------------------------------------------------------
echo.
echo [3/6] Checking that every module imports...
python check_imports.py
if errorlevel 1 (
    echo [ERROR] The source does not import cleanly. Fix that before packaging.
    pause
    exit /b 1
)

REM --------------------------------------------------------------------------
REM 4. Clean
REM --------------------------------------------------------------------------
echo.
echo [4/6] Cleaning previous build artifacts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "dist" (
    echo [ERROR] 'dist' could not be removed - SpotCheck.exe is probably still
    echo         running, or the folder is open in Explorer. Close it and retry.
    echo         Building on top of a stale 'dist' produces a mixed folder that
    echo         looks new and is not.
    pause
    exit /b 1
)

REM --------------------------------------------------------------------------
REM 5. Build
REM --------------------------------------------------------------------------
echo.
echo [5/6] Compiling with PyInstaller ^(this takes a few minutes^)...
python -m PyInstaller SpotCheck.spec --noconfirm --log-level=INFO > "%BUILD_LOG%" 2>&1
set "BUILD_RC=!ERRORLEVEL!"

if not "!BUILD_RC!"=="0" (
    echo [WARNING] The spec build returned !BUILD_RC!. Falling back to the CLI form...
    python -m PyInstaller --noconfirm --onedir --windowed --name "SpotCheck" ^
        --collect-all "pyzbar" ^
        --collect-all "customtkinter" ^
        --collect-all "pymupdf" ^
        --collect-all "cv2" ^
        --collect-all "openpyxl" ^
        --collect-all "pdfplumber" ^
        --hidden-import "fitz" ^
        --hidden-import "pyzbar.pyzbar" ^
        --hidden-import "multiprocessing.spawn" ^
        --hidden-import "concurrent.futures.process" ^
        --hidden-import "logger_config" ^
        --hidden-import "settings" ^
        --hidden-import "core.pipeline" ^
        --hidden-import "core.docscan" ^
        --hidden-import "core.text_overlap" ^
        --hidden-import "core.untranslated" ^
        --hidden-import "core.metadata" ^
        --hidden-import "core.page_diff" ^
        --hidden-import "gui.app_window" ^
        --hidden-import "gui.region_dialog" ^
        --hidden-import "gui.comparison_gallery" ^
        --hidden-import "gui.page_diff_view" ^
        --hidden-import "gui.metadata_tab" ^
        --hidden-import "gui.text_checks_tab" ^
        run_gui.py >> "%BUILD_LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Both builds failed. See %BUILD_LOG%.
        pause
        exit /b 1
    )
)

REM The exit code is not the whole story - see the note at the top.
findstr /c:"ERROR: Hidden import" "%BUILD_LOG%" >nul
if not errorlevel 1 (
    echo.
    echo [WARNING] PyInstaller could not resolve these imports:
    findstr /c:"ERROR: Hidden import" "%BUILD_LOG%"
    echo           The exe was still produced. Whatever needs them will be
    echo           missing at run time. Usually a package that is not installed.
)

REM Stylesheets live next to the exe, not inside _internal, because that is
REM where core\templates.py looks for them. Copied rather than bundled so the
REM ones already saved travel with the build.
if exist "templates" (
    echo   Copying stylesheet templates next to the exe...
    xcopy /e /i /y "templates" "%APP_DIR%\templates" >nul
)

REM --------------------------------------------------------------------------
REM 6. Verify
REM --------------------------------------------------------------------------
echo.
echo [6/6] Verifying the build...

if not exist "%APP_DIR%\SpotCheck.exe" (
    if exist "dist\SpotCheck.exe" (
        echo [SUCCESS] Single-file SpotCheck.exe built at %CD%\dist\SpotCheck.exe
        echo           ^(no folder to verify - skipping the DLL and smoke checks^)
        goto :done
    )
    echo [ERROR] No executable in 'dist'. See %BUILD_LOG%.
    pause
    exit /b 1
)

echo   Checking the pyzbar DLLs...
set "ZBAR_OK=0"
if exist "%APP_DIR%\_internal\pyzbar\libzbar-64.dll" set "ZBAR_OK=1"
if exist "%APP_DIR%\_internal\libzbar-64.dll" set "ZBAR_OK=1"
if "!ZBAR_OK!"=="1" (
    echo     [+] libzbar-64.dll present
) else (
    echo     [WARN] libzbar-64.dll MISSING - barcodes will be located but not decoded
)
set "ICONV_OK=0"
if exist "%APP_DIR%\_internal\pyzbar\libiconv.dll" set "ICONV_OK=1"
if exist "%APP_DIR%\_internal\libiconv.dll" set "ICONV_OK=1"
if "!ICONV_OK!"=="1" (
    echo     [+] libiconv.dll present
) else (
    echo     [WARN] libiconv.dll MISSING
)

REM Verify MSVCR120.dll architecture to prevent 0xc0000020 Bad Image on target laptops
if exist "%APP_DIR%\_internal\msvcr120.dll" (
    python -c "import struct; f=open(r'%APP_DIR%\_internal\msvcr120.dll','rb'); f.seek(0x3c); off=struct.unpack('<I',f.read(4))[0]; f.seek(off+4); mach=struct.unpack('<H',f.read(2))[0]; exit(0 if mach==0x8664 else 1)" >nul 2>&1
    if errorlevel 1 (
        echo     [WARN] _internal\msvcr120.dll was 32-bit (x86)! Replacing with 64-bit System32 copy...
        copy /y "%SystemRoot%\System32\msvcr120.dll" "%APP_DIR%\_internal\msvcr120.dll" >nul
    ) else (
        echo     [+] msvcr120.dll present (64-bit verified)
    )
) else (
    if exist "%SystemRoot%\System32\msvcr120.dll" (
        echo     [+] Copying 64-bit msvcr120.dll from System32...
        copy /y "%SystemRoot%\System32\msvcr120.dll" "%APP_DIR%\_internal\msvcr120.dll" >nul
    )
)

REM The real test: start it, let it build its tabs, read its own log. Every tab
REM is wrapped in a try/except, so a missing module shows up here as
REM "unavailable" and nowhere else.
echo   Starting the exe once to see whether it comes up...
start "" /d "%APP_DIR%" "SpotCheck.exe"
timeout /t 20 /nobreak >nul
taskkill /im SpotCheck.exe /f >nul 2>&1

set "NEWEST="
for /f "delims=" %%F in ('dir /b /o-d "%APP_DIR%\logs\spotcheck_2*.log" 2^>nul') do (
    if not defined NEWEST set "NEWEST=%APP_DIR%\logs\%%F"
)

if not defined NEWEST (
    echo     [WARN] The exe wrote no log. It may not have started at all.
    echo         Rebuild with a console to see why:
    echo             set SPOTCHECK_DEBUG_BUILD=1 ^&^& build_exe.bat
    goto :done
)

echo     [+] Startup log: !NEWEST!
findstr /c:"unavailable:" "!NEWEST!" >nul
if not errorlevel 1 (
    echo     [WARN] A tab failed to build in the packaged app:
    findstr /c:"unavailable:" "!NEWEST!"
)
findstr /c:"Failed to load" "!NEWEST!" >nul
if not errorlevel 1 (
    echo     [WARN] A dependency did not load inside the exe:
    findstr /c:"Failed to load" "!NEWEST!"
    echo         pyzbar also needs the Microsoft Visual C++ 2013 Redistributable
    echo         ^(x64^) on the machine that RUNS the exe, not just this one.
)
findstr /c:"Traceback" "!NEWEST!" >nul
if not errorlevel 1 (
    echo     [WARN] The packaged app raised an exception on startup - see the log.
)

:done
echo.
echo ===============================================================================
echo [DONE] %CD%\%APP_DIR%\SpotCheck.exe
echo        Ship the whole 'dist\SpotCheck' folder, not just the exe.
echo        Full build log: %CD%\%BUILD_LOG%
echo ===============================================================================
echo.
pause
endlocal