"""Helper script to download Faster-Whisper models.

Usage examples:

    python scripts/download_faster_whisper.py medium models/faster-whisper-medium
    python scripts/download_faster_whisper.py small

The script wraps ``faster_whisper.download_model`` and ensures the target directory
is created before download. The model is fetched from the Hugging Face Hub using
Systran's pre-converted CTranslate2 checkpoints.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from faster_whisper import download_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a Faster-Whisper model to a local directory.",
    )
    parser.add_argument(
        "model",
        help=(
            "Model size or Hugging Face repo id to download (e.g. tiny, base, "
            "small, medium, large-v3, Systran/faster-whisper-medium)"
        ),
    )
    parser.add_argument(
        "destination",
        nargs="?",
        default=None,
        help=(
            "Optional output directory. Defaults to models/<model> if omitted "
            "and <model> is a known size."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Optional Hugging Face cache directory to reuse across runs.",
    )
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Do not hit the network; only use existing cached files.",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Optional git revision (branch, tag, commit) to download.",
    )
    parser.add_argument(
        "--use-auth-token",
        default=None,
        help="Optional Hugging Face auth token or True to use the stored token.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    destination: Path | None
    if args.destination is not None:
        destination = Path(args.destination)
    else:
        destination = Path("models") / f"faster-whisper-{args.model}"

    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        output_dir = str(destination)
    else:
        output_dir = None

    path = download_model(
        args.model,
        output_dir=output_dir,
        local_files_only=args.local_files_only,
        cache_dir=args.cache_dir,
        revision=args.revision,
        use_auth_token=args.use_auth_token,
    )

    print(f"Model downloaded to: {path}")


if __name__ == "__main__":
    main()
