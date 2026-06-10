#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo "========================================"
echo "  AutoRUN v1 - Installation Script"
echo "========================================"
echo ""

# ============================================================
# Detect OS and package manager
# ============================================================
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        OS="macos"
    elif [[ "$OSTYPE" == "linux-gnu"* ]] || [[ "$OSTYPE" == "linux"* ]]; then
        OS="linux"
    else
        OS="unknown"
    fi
}

detect_pkg_manager() {
    PKG_MANAGER=""
    if command -v apt-get &>/dev/null; then
        PKG_MANAGER="apt"
    elif command -v dnf &>/dev/null; then
        PKG_MANAGER="dnf"
    elif command -v yum &>/dev/null; then
        PKG_MANAGER="yum"
    elif command -v pacman &>/dev/null; then
        PKG_MANAGER="pacman"
    elif command -v apk &>/dev/null; then
        PKG_MANAGER="apk"
    elif command -v zypper &>/dev/null; then
        PKG_MANAGER="zypper"
    elif command -v brew &>/dev/null; then
        PKG_MANAGER="brew"
    fi
}

detect_os
detect_pkg_manager

# ============================================================
# Step 1: Ensure Python 3.8+
# ============================================================
echo -e "${YELLOW}[1/6] Ensuring Python 3.8+ is available...${NC}"

PYTHON_CMD=""
for candidate in python3 python; do
    if cmd_path=$(command -v "$candidate" 2>/dev/null); then
        # Skip Microsoft Store stubs on Windows (WSL)
        case "$cmd_path" in
            */WindowsApps/*) continue ;;
        esac
        PYTHON_CMD="$candidate"
        break
    fi
done

install_python() {
    echo "        Python 3 not found. Attempting auto-install..."
    case "$PKG_MANAGER" in
        apt)
            echo "        Installing via apt..."
            sudo apt-get update -qq
            sudo apt-get install -y python3 python3-venv python3-pip
            ;;
        dnf)
            echo "        Installing via dnf..."
            sudo dnf install -y python3 python3-pip
            ;;
        yum)
            echo "        Installing via yum..."
            sudo yum install -y python3 python3-pip
            ;;
        pacman)
            echo "        Installing via pacman..."
            sudo pacman -S --noconfirm python python-pip
            ;;
        apk)
            echo "        Installing via apk..."
            sudo apk add python3 py3-pip py3-venv
            ;;
        zypper)
            echo "        Installing via zypper..."
            sudo zypper install -y python3 python3-pip
            ;;
        brew)
            echo "        Installing via Homebrew..."
            brew install python@3.12
            ;;
        *)
            echo -e "${RED}[ERROR]${NC} Cannot auto-install Python on this system."
            echo "        Please install Python 3.8+ manually:"
            echo "        https://www.python.org/downloads/"
            exit 1
            ;;
    esac
    for candidate in python3 python; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON_CMD="$candidate"
            return
        fi
    done
}

if [[ -z "$PYTHON_CMD" ]]; then
    install_python
fi

if [[ -z "$PYTHON_CMD" ]]; then
    echo -e "${RED}[ERROR]${NC} Python installation failed. Please install manually."
    exit 1
fi

PYVER=$("$PYTHON_CMD" --version 2>&1 | awk '{print $2}')
echo -e "        ${GREEN}Found Python $PYVER${NC}"

MAJOR=$(echo "$PYVER" | cut -d. -f1)
MINOR=$(echo "$PYVER" | cut -d. -f2)
if [[ "$MAJOR" -lt 3 ]] || { [[ "$MAJOR" -eq 3 ]] && [[ "$MINOR" -lt 8 ]]; }; then
    echo -e "${RED}[ERROR]${NC} Python 3.8+ required, found $PYVER"
    exit 1
fi

# ============================================================
# Step 2: Ensure pip is available
# ============================================================
echo ""
echo -e "${YELLOW}[2/6] Ensuring pip is available...${NC}"

if ! "$PYTHON_CMD" -m pip --version &>/dev/null; then
    echo "        pip not found, bootstrapping..."
    "$PYTHON_CMD" -m ensurepip --upgrade &>/dev/null || {
        case "$PKG_MANAGER" in
            apt) sudo apt-get install -y python3-pip ;;
            dnf) sudo dnf install -y python3-pip ;;
            pacman) sudo pacman -S --noconfirm python-pip ;;
            apk) sudo apk add py3-pip ;;
            *)
                echo -e "${RED}[ERROR]${NC} Could not install pip. Please install Python with pip."
                exit 1
                ;;
        esac
    }
fi
echo -e "        ${GREEN}pip OK${NC}"

# ============================================================
# Step 3: Create and activate virtual environment
# ============================================================
echo ""
echo -e "${YELLOW}[3/6] Setting up virtual environment...${NC}"

if ! "$PYTHON_CMD" -m venv --help &>/dev/null 2>&1; then
    echo "        python3-venv not found, installing..."
    case "$PKG_MANAGER" in
        apt) sudo apt-get install -y python3-venv ;;
        dnf) sudo dnf install -y python3-venv ;;
        apk) sudo apk add py3-venv ;;
        *) ;;
    esac
fi

test_venv_healthy() {
    local venv_python="$1"
    "$venv_python" --version &>/dev/null || return 1
    "$venv_python" -m pip --version &>/dev/null || return 1
    "$venv_python" -c "import fastapi" 2>/dev/null || return 1
    return 0
}

NEED_CREATE=true
if [[ -d ".venv" ]]; then
    VENV_PYTHON=".venv/bin/python"
    if [[ -f "$VENV_PYTHON" ]]; then
        if test_venv_healthy "$VENV_PYTHON"; then
            echo "        Using existing .venv/ (healthy)"
            NEED_CREATE=false
        else
            echo -e "        ${YELLOW}Existing .venv/ is broken (missing dependencies), recreating...${NC}"
            rm -rf .venv
        fi
    else
        echo -e "        ${YELLOW}Existing .venv/ is incomplete, recreating...${NC}"
        rm -rf .venv
    fi
fi

if $NEED_CREATE; then
    echo "        Creating .venv ..."
    "$PYTHON_CMD" -m venv .venv
    echo -e "        ${GREEN}Virtual environment created.${NC}"
fi

# shellcheck source=/dev/null
source .venv/bin/activate
echo -e "        ${GREEN}Virtual environment activated.${NC}"

# ============================================================
# Step 4: Install dependencies
# ============================================================
echo ""
echo -e "${YELLOW}[4/6] Installing dependencies...${NC}"

pip install --upgrade pip >/dev/null 2>&1 || echo -e "        ${YELLOW}Warning: pip upgrade failed, continuing...${NC}"
# Ensure pip is functional
pip --version &>/dev/null || {
    echo -e "${RED}[ERROR]${NC} pip is not functional. Try deleting .venv and re-running."
    exit 1
}
echo "        Installing packages from requirements.txt..."
pip install -r requirements.txt
echo -e "        ${GREEN}Dependencies installed.${NC}"

# ============================================================
# Step 5: Ensure pyproject.toml entry point is correct
# ============================================================
echo ""
echo -e "${YELLOW}[5/6] Checking project configuration...${NC}"

if [[ -f pyproject.toml ]]; then
    # Fix old-style entry points to use root-level main:cli_main
    if grep -qE 'autorun.*=.*"AutoRUN_v1\.' pyproject.toml 2>/dev/null; then
        echo "        Fixing autorun entry point..."
        if [[ "$(uname)" == "Darwin" ]]; then
            sed -i '' 's/autorun = "[^"]*"/autorun = "main:cli_main"/' pyproject.toml
        else
            sed -i 's/autorun = "[^"]*"/autorun = "main:cli_main"/' pyproject.toml
        fi
        echo -e "        ${GREEN}Entry point fixed.${NC}"
    fi
fi
echo -e "        ${GREEN}Configuration OK.${NC}"

# ============================================================
# Step 6: Install project in editable mode
# ============================================================
echo ""
echo -e "${YELLOW}[6/6] Installing AutoRUN (pip install -e .)...${NC}"
echo ""

pip install -e .
if [[ $? -ne 0 ]]; then
    echo ""
    echo -e "${YELLOW}[WARNING] pip install -e . failed.${NC}"
    echo "         autorun command will not be available."
    echo "         You can still run: python main.py"
else
    echo ""
    echo -e "        ${GREEN}autorun command installed successfully.${NC}"

    # Add .venv/bin to PATH via shell profile so autorun works from anywhere
    VENV_BIN="$SCRIPT_DIR/.venv/bin"
    for PROFILE in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.config/fish/config.fish"; do
        if [[ -f "$PROFILE" ]]; then
            if ! grep -q "$VENV_BIN" "$PROFILE" 2>/dev/null; then
                echo "export PATH=\"$VENV_BIN:\$PATH\"" >> "$PROFILE"
                echo -e "        ${GREEN}Added to PATH in ${PROFILE}${NC}"
            fi
        fi
    done
    echo -e "        ${GREEN}(Restart terminal or run 'source ~/.bashrc' to take effect)${NC}"
fi

# Create autorun.sh wrapper for convenience
cat > autorun.sh << 'WRAPPER'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.venv/bin/activate" 2>/dev/null
autorun "$@"
WRAPPER
chmod +x autorun.sh
echo -e "        ${GREEN}Created autorun.sh (run ./autorun.sh to launch).${NC}"

echo ""
echo -e "${CYAN}========================================"
echo "  Installation complete!"
echo -e "========================================${NC}"
echo ""
echo "  Quick start:"
echo "    autorun                       Start REPL"
echo "    autorun --web                 Start Web UI"
echo "    autorun --setup               Configure API"
echo ""
echo "  If autorun not found, activate venv first:"
echo "    source .venv/bin/activate"
echo "    autorun"
echo ""
echo "  Or run via wrapper script:"
echo "    ./autorun.sh"
echo ""
echo "  Or run directly:"
echo "    python main.py"
echo "    python main.py --web"
echo ""

# Drop the venv activation on exit — user should source it themselves next time
