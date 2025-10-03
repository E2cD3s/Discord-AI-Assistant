#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: ./run_assistant.sh [--setup] [--config PATH]

Options:
  --setup         Create/refresh the local virtual environment, upgrade pip,
                  and install Python requirements before running the bot.
  --config PATH   Path to the configuration YAML file. Defaults to config.yaml
                  in the project root, falling back to config.example.yaml.
  -h, --help      Show this help message and exit.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_ACTIVATE="$VENV_DIR/bin/activate"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements.txt"
DEFAULT_CONFIG="$SCRIPT_DIR/config.yaml"
FALLBACK_CONFIG="$SCRIPT_DIR/config.example.yaml"

SETUP=0
CONFIG_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup)
      SETUP=1
      shift
      ;;
    --config)
      shift
      CONFIG_OVERRIDE="${1:-}"
      if [[ -z "$CONFIG_OVERRIDE" ]]; then
        echo "[ERROR] Missing value for --config" >&2
        exit 1
      fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -z "$CONFIG_OVERRIDE" ]]; then
        CONFIG_OVERRIDE="$1"
        shift
      else
        echo "[ERROR] Unknown argument: $1" >&2
        usage
        exit 1
      fi
      ;;
  esac
done

if [[ -n "$CONFIG_OVERRIDE" ]]; then
  CONFIG_FILE="$CONFIG_OVERRIDE"
else
  if [[ -f "$DEFAULT_CONFIG" ]]; then
    CONFIG_FILE="$DEFAULT_CONFIG"
  elif [[ -f "$FALLBACK_CONFIG" ]]; then
    CONFIG_FILE="$FALLBACK_CONFIG"
  else
    echo "[ERROR] Could not find a configuration file. Create config.yaml or pass a path with --config." >&2
    exit 1
  fi
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[ERROR] Configuration file not found at $CONFIG_FILE" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON:-python3}"

if [[ "$SETUP" -eq 1 ]]; then
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "[INFO] Creating virtual environment at $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
fi

if [[ -f "$VENV_ACTIVATE" ]]; then
  # shellcheck disable=SC1090
  source "$VENV_ACTIVATE"
  ACTIVE_PYTHON="python"
else
  echo "[INFO] No virtual environment found at $VENV_ACTIVATE. Using system Python."
  if command -v python >/dev/null 2>&1; then
    ACTIVE_PYTHON="python"
  elif command -v python3 >/dev/null 2>&1; then
    ACTIVE_PYTHON="python3"
  else
    echo "[ERROR] Python is not available on PATH." >&2
    exit 1
  fi
fi

if [[ "$SETUP" -eq 1 ]]; then
  if [[ ! -f "$REQUIREMENTS_FILE" ]]; then
    echo "[ERROR] Requirements file not found at $REQUIREMENTS_FILE" >&2
    exit 1
  fi
  echo "[INFO] Upgrading pip"
  "$ACTIVE_PYTHON" -m pip install --upgrade pip
  echo "[INFO] Installing dependencies from $REQUIREMENTS_FILE"
  "$ACTIVE_PYTHON" -m pip install -r "$REQUIREMENTS_FILE"
fi

exec "$ACTIVE_PYTHON" -m src.main --config "$CONFIG_FILE"
