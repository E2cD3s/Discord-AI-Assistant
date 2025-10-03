# Discord AI Assistant

An offline-ready Discord bot that listens to voice, understands speech using Whisper, reasons with a locally hosted Ollama model, and talks back using Kokoro text-to-speech.

## Features

- ✅ Wake-word activated conversations in text channels
- ✅ Configurable rotating status messages to keep your bot presence fresh
- ✅ Conversation memory with configurable history length and sampling parameters
- ✅ Voice pipeline powered by Faster-Whisper speech-to-text and Kokoro text-to-speech
- ✅ Slash-friendly commands to join/leave voice, trigger recordings, and reset conversation state
- ✅ Works fully offline with local Ollama, Whisper and Kokoro models

## Requirements

- Python 3.10+
- FFmpeg installed and available on your PATH for Discord voice playback
- A GPU is recommended (the sample server is an i7-7700 with RTX 2070 SUPER and 64 GB RAM)
- Locally hosted services and models:
  - [Ollama](https://ollama.ai) running a Hugging Face compatible model (configure in `config.yaml`)
  - [Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model downloaded to disk
  - [Kokoro](https://github.com/hexgrad/kokoro) voices installed locally

## Setup

The bot runs on both Linux (Debian/Ubuntu) and Windows 10/11/Server. Use the platform-specific guides below for detailed instructions, including installing Python, FFmpeg, and other prerequisites:

- [Linux setup guide](docs/setup-linux.md)
- [Windows setup guide](docs/setup-windows.md)

### Quick start (after prerequisites)

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows use: .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. Copy the example configuration and adjust the values for your environment:

   ```bash
   cp config.example.yaml config.yaml
   # On Windows (PowerShell): Copy-Item config.example.yaml config.yaml
   ```

   Update the Discord token, Ollama model name (for example `llama3`, `mistral`, or any Hugging Face model served by Ollama), speech-to-text paths, and Kokoro voice.

4. Download or generate the models referenced in the configuration:

   - **Ollama**: `ollama pull mistral` (or your preferred Hugging Face model)
   - **Faster-Whisper**: download the model directory into `models/faster-whisper-medium` (or another path referenced in `config.yaml`)
   - **Kokoro**: follow the [Kokoro project](https://github.com/hexgrad/kokoro) instructions to place the pipeline weights in an accessible location. Reference the [VOICES.md table on Hugging Face](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) for valid speaker IDs.

5. Run the bot:

   ```bash
   python -m src.main --config config.yaml
   ```

## Commands

| Command | Description |
| --- | --- |
| `!reset` | Clears the conversation history for the current channel. |
| `!ask <question>` | Sends a prompt directly to the assistant and replies with the answer. |
| `!join` | Summons the bot to your current voice channel. |
| `!leave` | Disconnects the bot from voice. |
| `!listen [timeout]` | Records the voice channel for the specified seconds (default 15), transcribes speech, and replies with text and synthesized audio. |
| `!say <text>` | Forces the assistant to speak the provided text in voice chat. |
| `!status` | Displays key runtime configuration details. |

## Wake Word

The bot listens for the configured wake word in text channels (default `hey assistant`). After hearing it, the assistant will respond directly in the channel or spawn a thread (configurable) and optionally speak back if it is present in a voice channel.

A per-channel cooldown prevents accidental rapid triggers. Configure `wake_word_cooldown_seconds` in `config.yaml` to tune responsiveness.

## Logging

Logs are written to STDOUT and an optional rotating file (configured via the `logging` section). This makes it easier to trace inference latencies, transcriptions, and Discord events.

## Troubleshooting

- Ensure Ollama is running locally and accessible at the configured host/port.
- Confirm FFmpeg is installed for audio playback.
- If Kokoro voices are missing, install the assets according to the upstream README and double-check the `voice` name.
- Whisper model loading is eager; incorrect paths will raise clear `FileNotFoundError` exceptions during startup.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
