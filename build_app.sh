#!/bin/bash
# ===============================================================================
#          SPOTCHECK MACOS & LINUX PACKAGING AUTOMATION
# ===============================================================================

set -e

echo "==============================================================================="
echo "                SPOTCHECK MULTI-PLATFORM BUILD SCRIPT"
echo "==============================================================================="
echo ""

# 1. Detect Operating System
OS_NAME="$(uname -s)"
echo "[1/4] Detected Operating System: $OS_NAME"

# 2. Check and install system zbar library if needed
if [ "$OS_NAME" = "Darwin" ]; then
    echo "Checking zbar dependency for macOS..."
    if ! command -v brew &> /dev/null; then
        echo "[WARNING] Homebrew is not installed. Please ensure zbar is installed via 'brew install zbar'"
    else
        brew install zbar || true
    fi
elif [ "$OS_NAME" = "Linux" ]; then
    echo "Checking zbar dependency for Linux..."
    if command -v apt-get &> /dev/null; then
        sudo apt-get update && sudo apt-get install -y libzbar0 || true
    fi
fi

# 3. Check Python & install dependencies
echo ""
echo "[2/4] Checking Python and dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# 4. Clean previous builds
echo ""
echo "[3/4] Cleaning previous build artifacts..."
rm -rf build dist

# 5. Build with PyInstaller
echo ""
echo "[4/4] Building standalone SpotCheck package..."
pyinstaller SpotCheck.spec --noconfirm

echo ""
echo "==============================================================================="
if [ "$OS_NAME" = "Darwin" ]; then
    echo "[SUCCESS] macOS application successfully created!"
    echo "Location: $(pwd)/dist/SpotCheck.app (or dist/SpotCheck)"
else
    echo "[SUCCESS] Linux standalone executable successfully created!"
    echo "Location: $(pwd)/dist/SpotCheck"
fi
echo "==============================================================================="
