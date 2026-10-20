from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from huggingface_hub import snapshot_download  # noqa: E402


APP_DIR = Path(__file__).resolve().parent
MODELS_DIR = Path(os.environ.get("MODELS_DIR", str(APP_DIR / "models"))).expanduser().resolve()
ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3"}


def download_model(name: str) -> bool:
    if name not in ALLOWED_MODELS:
        print(f"[skip] unknown model: {name}")
        return False
    target = MODELS_DIR / name
    if (target / "model.bin").is_file():
        print(f"[ok] {name} already exists: {target}")
        return True

    for attempt in range(1, 4):
        try:
            print(f"[download] {name} -> {target} (attempt {attempt}/3)")
            snapshot_download(
                repo_id=f"Systran/faster-whisper-{name}",
                local_dir=str(target),
                max_workers=4,
            )
            if (target / "model.bin").is_file():
                print(f"[ok] {name}")
                return True
        except Exception as exc:
            print(f"[warn] {name}: {exc}")
            if attempt < 3:
                time.sleep(2 * attempt)
    print(f"[fail] {name}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-download faster-whisper models")
    parser.add_argument("--models", default=os.environ.get("PRELOAD_MODELS", ""))
    parser.add_argument("--allow-failure", action="store_true")
    args = parser.parse_args()

    names = [item.strip() for item in args.models.split(",") if item.strip()]
    if not names:
        print("[skip] no models requested")
        return 0

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    results = [download_model(name) for name in names]
    if all(results):
        return 0
    return 0 if args.allow_failure else 1


if __name__ == "__main__":
    sys.exit(main())
