# Linux (Debian/Ubuntu) Setup

These steps have been validated on Debian-based distributions (Debian 12, Ubuntu 22.04). Adjust package manager commands if you are using another distribution.

## 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg git
```

If you plan to use GPU acceleration with Faster-Whisper, install the appropriate CUDA drivers separately.

## 2. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/Discord-AI-Assistant.git
cd Discord-AI-Assistant
```

## 3. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Upgrade `pip` after activation to ensure the latest wheels are used:

```bash
pip install --upgrade pip
```

## 4. Install Python dependencies

```bash
pip install -r requirements.txt
```

If you see build errors about `soundfile` or `numpy`, confirm that `python3-dev` and your compiler toolchain are installed (`sudo apt install build-essential python3-dev`).

## 5. Copy and edit the configuration

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

Update the Discord bot token, local model paths, and any voice or logging preferences.

## 6. Prepare local AI services

- **Ollama**: Install the [Linux release](https://ollama.ai/download) and run `ollama pull mistral` (or your preferred model).
- **Faster-Whisper**: Download the desired model directory to the path referenced in `config.yaml` (for example `models/faster-whisper-medium`).
- **Kokoro voices**: Follow the [Kokoro instructions](https://github.com/hexgrad/kokoro) to download voices locally. Make sure the `voice` value in `config.yaml` matches an installed voice ID.

## 7. Launch the bot

```bash
python -m src.main --config config.yaml
```

Logs are streamed to the console. If you configured a log file path it will be created relative to the repository unless you used an absolute path.

## 8. Service management (optional)

For long-running deployments, consider creating a `systemd` service that activates the virtual environment and starts the bot on boot.
