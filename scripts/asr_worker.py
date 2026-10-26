"""Owned local Whisper worker. Audio is passed over stdin, never as a file URL."""

import argparse
import json
import sys
from io import BytesIO
from pathlib import Path

MODEL_REPO = "Systran/faster-whisper-base"
MODEL_REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    if args.prepare:
        from huggingface_hub import snapshot_download

        snapshot_download(
            MODEL_REPO,
            revision=MODEL_REVISION,
            local_dir=args.directory,
            allow_patterns=FILES,
            token=False,
            max_workers=2,
        )
    model = WhisperModel(
        str(args.directory),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
        local_files_only=True,
    )
    if args.prepare:
        print(json.dumps({"ready": True, "revision": MODEL_REVISION}))
        return
    content = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
    if not content or len(content) > 8 * 1024 * 1024:
        raise ValueError("invalid audio size")
    segments, info = model.transcribe(
        BytesIO(content),
        language=args.language or None,
        beam_size=3,
        condition_on_previous_text=False,
        vad_filter=True,
    )
    text = "".join(segment.text for segment in segments).strip()
    if len(text) > 12000:
        raise ValueError("transcription too long")
    print(json.dumps({"text": text, "language": info.language}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Native library/provider tracebacks can contain paths or audio text.
        print("Local recognition failed; check dependency/model compatibility.", file=sys.stderr)
        sys.exit(1)
