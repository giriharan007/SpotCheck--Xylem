@echo off
setlocal enabledelayedexpansion

echo ===============================================================================
echo                 SPOTCHECK WINDOWS EXE PACKAGING AUTOMATION
echo ===============================================================================
echo.

:: 1. Check Python installation
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not found in PATH!
    echo Please install Python 3.10+ and ensure 'Add Python to PATH' is checked.
    pause
    exit /b 1
)

echo [1/4] Checking and installing required dependencies...
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Some dependencies failed to install. Retrying critical packages...
    pip install customtkinter pyinstaller pymupdf pdfplumber opencv-python numpy pyzbar openpyxl Pillow
)

:: 2. Clean previous builds
echo.
echo [2/4] Cleaning previous build artifacts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

:: 3. Build Executable with PyInstaller
echo.
echo [3/4] Compiling SpotCheck into standalone Windows Executable (.exe)...
pyinstaller SpotCheck.spec --noconfirm

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] PyInstaller build failed! Falling back to direct CLI command...
    pyinstaller --noconfirm --onedir --windowed --name "SpotCheck" ^
        --hidden-import "fitz" ^
        --hidden-import "pymupdf" ^
        --hidden-import "pdfplumber" ^
        --hidden-import "cv2" ^
        --hidden-import "numpy" ^
        --hidden-import "pyzbar" ^
        --hidden-import "openpyxl" ^
        --hidden-import "customtkinter" ^
        --hidden-import "FirstPage" ^
        --hidden-import "LastPage" ^
        --hidden-import "Barcode_QR_Check" ^
        --hidden-import "Toc" ^
        --hidden-import "crop_pdf_images" ^
        --hidden-import "Compare_cropped_images" ^
        --hidden-import "main" ^
        app_gui.py
)

:: 4. Verify Output
echo.
if exist "dist\SpotCheck\SpotCheck.exe" (
    echo ===============================================================================
    echo [SUCCESS] SpotCheck.exe successfully built!
    echo Location: %CD%\dist\SpotCheck\SpotCheck.exe
    echo ===============================================================================
    echo.
    echo You can distribute the entire folder 'dist\SpotCheck' to any Windows computer.
) else if exist "dist\SpotCheck.exe" (
    echo ===============================================================================
    echo [SUCCESS] Standalone SpotCheck.exe successfully built!
    echo Location: %CD%\dist\SpotCheck.exe
    echo ===============================================================================
) else (
    echo [ERROR] Executable was not found in 'dist'. Please review the build logs above.
)

echo.
pause
