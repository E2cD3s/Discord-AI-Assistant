# Windows 10/11/Server Setup

These instructions assume you are using Windows 10, Windows 11, or Windows Server 2019/2022 with Administrator access.

## 1. Install prerequisites

1. Install [Python 3.10 or newer](https://www.python.org/downloads/windows/) and check **Add Python to PATH** during installation.
2. Install [Git for Windows](https://git-scm.com/download/win) if it is not already available.
3. Download the latest [FFmpeg release](https://www.gyan.dev/ffmpeg/builds/). Extract the archive (for example to `C:\\ffmpeg`) and add the `bin` directory (for example `C:\\ffmpeg\\bin`) to your system `PATH` environment variable.

If you plan to leverage GPU acceleration for Faster-Whisper, install the matching NVIDIA drivers and CUDA toolkit separately.

## 2. Clone the repository

Open **PowerShell** and run:

```powershell
git clone https://github.com/YOUR_USERNAME/Discord-AI-Assistant.git
cd Discord-AI-Assistant
```

## 3. Create and activate a virtual environment

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks script execution, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

Upgrade `pip` after activation to ensure the latest wheels are used:

```powershell
python -m pip install --upgrade pip
```

## 4. Install Python dependencies

```powershell
pip install -r requirements.txt
```

If you encounter errors compiling optional dependencies, install the [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) and retry.

## 5. Copy and edit the configuration

```powershell
Copy-Item config.example.yaml config.yaml
notepad config.yaml
```

Update the Discord bot token, local model paths, and any voice or logging preferences. Windows-style paths such as `C:\\Models\\faster-whisper-medium` are supported.

## 6. Prepare local AI services

- **Ollama**: Install the [Windows release](https://ollama.ai/download) and run `ollama pull mistral` (or your preferred model) from a new terminal.
- **Faster-Whisper**: From the project directory run `python scripts/download_faster_whisper.py medium` to download the Medium checkpoint into `models\faster-whisper-medium`. Adjust the size (for example `small`, `large-v3`) or pass an explicit destination as needed and update `stt.model_path` accordingly.
- **Kokoro voices**: Follow the [Kokoro instructions](https://github.com/hexgrad/kokoro) to download voices locally. Make sure the `voice` value in `config.yaml` matches an installed voice ID.

## 7. Launch the bot

From the activated virtual environment:

```powershell
python -m src.main --config config.yaml
```

Logs are written to the console. If you configured a log file path such as `logs\\bot.log`, it will be created relative to the repository unless you use an absolute path.

## 8. Running in the background (optional)

For persistent deployments, create a scheduled task that runs the activation script and bot command at logon or startup, or use a Windows Service wrapper such as NSSM.
