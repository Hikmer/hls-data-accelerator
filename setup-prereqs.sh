#!/usr/bin/env bash
# setup-prereqs.sh — Cross-platform bootstrap for macOS and Linux
# Installs PowerShell Core if missing, then runs setup-prereqs.ps1

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_ONLY=0
for argument in "$@"; do
    case "$argument" in
        -[Cc][Hh][Ee][Cc][Kk][Oo][Nn][Ll][Yy]) CHECK_ONLY=1 ;;
    esac
done

echo ""
echo -e "${CYAN}+============================================================+${NC}"
echo -e "${CYAN}|    BOOTSTRAP — Med Device FHIR Platform Prerequisites      |${NC}"
echo -e "${CYAN}+============================================================+${NC}"
echo ""

# Detect OS
if [[ "$OSTYPE" == "darwin"* ]]; then
    PLATFORM="macOS"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    PLATFORM="Linux"
    # Detect distro
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        DISTRO="$ID"
    else
        DISTRO="unknown"
    fi
else
    echo -e "${RED}Unsupported OS: $OSTYPE${NC}"
    echo "Use setup-prereqs.ps1 on Windows."
    exit 1
fi

echo -e "  Platform: ${PLATFORM}"
echo ""

# Check for PowerShell Core
if command -v pwsh &> /dev/null; then
    PWSH_VER=$(pwsh --version 2>/dev/null)
    echo -e "  ${GREEN}✓ PowerShell Core ($PWSH_VER)${NC}"
else
    if [[ "$CHECK_ONLY" == 1 ]]; then
        echo -e "  ${RED}PowerShell Core not found; CheckOnly will not install it.${NC}"
        echo "  Install: https://learn.microsoft.com/powershell/scripting/install/installing-powershell"
        exit 1
    fi
    echo -e "  ${YELLOW}PowerShell Core not found — installing...${NC}"
    echo ""

    if [[ "$PLATFORM" == "macOS" ]]; then
        # Check for Homebrew
        if ! command -v brew &> /dev/null; then
            echo -e "  ${YELLOW}Homebrew not found — installing Homebrew first...${NC}"
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        echo -e "  ${CYAN}Installing PowerShell via Homebrew...${NC}"
        brew install --cask powershell
    elif [[ "$PLATFORM" == "Linux" ]]; then
        case "$DISTRO" in
            ubuntu|debian)
                echo -e "  ${CYAN}Installing PowerShell via apt (Ubuntu/Debian)...${NC}"
                # Import Microsoft GPG key and repo
                sudo apt-get update -qq
                sudo apt-get install -y -qq wget apt-transport-https software-properties-common
                source /etc/os-release
                wget -q "https://packages.microsoft.com/config/$ID/$VERSION_ID/packages-microsoft-prod.deb"
                sudo dpkg -i packages-microsoft-prod.deb
                rm packages-microsoft-prod.deb
                sudo apt-get update -qq
                sudo apt-get install -y -qq powershell
                ;;
            fedora|rhel|centos)
                echo -e "  ${CYAN}Installing PowerShell via dnf (Fedora/RHEL)...${NC}"
                sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
                curl https://packages.microsoft.com/config/rhel/8/prod.repo | sudo tee /etc/yum.repos.d/microsoft.repo
                sudo dnf install -y powershell
                ;;
            *)
                echo -e "  ${RED}Unsupported Linux distro: $DISTRO${NC}"
                echo "  Install PowerShell manually: https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-linux"
                exit 1
                ;;
        esac
    fi

    # Verify install
    if command -v pwsh &> /dev/null; then
        PWSH_VER=$(pwsh --version 2>/dev/null)
        echo -e "  ${GREEN}✓ PowerShell Core installed ($PWSH_VER)${NC}"
    else
        echo -e "  ${RED}✗ PowerShell Core installation failed${NC}"
        echo "  Install manually: https://learn.microsoft.com/powershell/scripting/install/installing-powershell"
        exit 1
    fi
fi

echo ""

# Check for other core tools and offer to install
MISSING=0

if ! command -v az &> /dev/null; then
    echo -e "  ${YELLOW}Azure CLI not found${NC}"
    if [[ "$PLATFORM" == "macOS" ]]; then
        echo -e "    Install: ${CYAN}brew install azure-cli${NC}"
    else
        echo -e "    Install: ${CYAN}curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash${NC}"
    fi
    MISSING=1
fi

if ! command -v node &> /dev/null; then
    echo -e "  ${YELLOW}Node.js not found${NC}"
    if [[ "$PLATFORM" == "macOS" ]]; then
        echo -e "    Install: ${CYAN}brew install node${NC}"
    else
        echo -e "    Install: ${CYAN}curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs${NC}"
    fi
    MISSING=1
fi

HAS_PYTHON=0
for candidate in python3.14 python3.13 python3 python; do
    if command -v "$candidate" &> /dev/null && "$candidate" -I -B -c 'import sys; sys.exit(not ((3, 13) <= sys.version_info[:2] <= (3, 14)))' 2>/dev/null; then
        HAS_PYTHON=1
        break
    fi
done
if [[ "$HAS_PYTHON" == 0 ]]; then
    echo -e "  ${YELLOW}Python 3.13-3.14 not found${NC}"
    if [[ "$PLATFORM" == "macOS" ]]; then
        echo -e "    Install: ${CYAN}brew install python@3.13${NC}"
    else
        echo -e "    Install Python 3.13 or 3.14 with venv support from your distribution or https://www.python.org/downloads/."
    fi
    MISSING=1
fi

if ! command -v git &> /dev/null; then
    echo -e "  ${YELLOW}Git not found${NC}"
    echo -e "    Install: ${CYAN}https://git-scm.com${NC}"
    MISSING=1
fi

if [ $MISSING -eq 1 ]; then
    echo ""
    echo -e "  ${YELLOW}Install the missing tools above, then re-run this script.${NC}"
    echo ""
fi

# Delegate to the PowerShell prereqs script
echo -e "  ${CYAN}Running PowerShell setup script...${NC}"
echo ""
pwsh -NoProfile -File "$SCRIPT_DIR/setup-prereqs.ps1" "$@"
