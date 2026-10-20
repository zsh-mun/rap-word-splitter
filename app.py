from __future__ import annotations

import argparse
import csv
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# Keep model downloads friendly to mainland-China networks. These variables must
# be set before faster-whisper / huggingface_hub are imported.
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("PYTHONUTF8", "1")

from flask import Flask, jsonify, request, send_file, send_from_directory, session  # noqa: E402
from werkzeug.exceptions import RequestEntityTooLarge  # noqa: E402

import jieba  # noqa: E402

jieba.setLogLevel(60)
jieba.initialize()


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.environ.get("DATA_DIR", str(APP_DIR / "data"))).expanduser().resolve()
JOBS_DIR = DATA_DIR / "jobs"
MODELS_DIR = Path(os.environ.get("MODELS_DIR", str(APP_DIR / "models"))).expanduser().resolve()
BIN_DIR = APP_DIR / "bin"

ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
MODEL_LABELS = {
    "tiny": "Tiny · 最快",
    "base": "Base · 轻量",
    "small": "Small · 推荐",
    "medium": "Medium · 更准但慢",
    "large-v3": "Large v3 · 最准最慢",
}
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "small").strip()
if DEFAULT_MODEL not in ALLOWED_MODELS:
    DEFAULT_MODEL = "small"
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 3072, 1, 4096)
MAX_CONTENT_BYTES = MAX_UPLOAD_MB * 1024 * 1024
JOB_TTL_HOURS = env_int("JOB_TTL_HOURS", 24, 0, 720)
MAX_PENDING_JOBS = env_int("MAX_PENDING_JOBS", 3, 1, 20)
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "").strip()
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32)
ALLOWED_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts"}

JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
VIDEO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Video:")
AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio:")
ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u3400-\u9fff]")

PUNCT = set(
    "，。！？、；：,.!?;:…—–·“”‘’\"'（）()《》<>【】[]{}「」『』"
    "~`@#$%^&*+=_|\\/"
)
TRAILING_PUNCT = set("，。！？、；：,.!?;:…—–·“”‘’\"'）)]】】》」』")

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_BYTES
app.config["JSON_AS_ASCII"] = False
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.json.ensure_ascii = False


def find_ffmpeg() -> str | None:
    candidates = [
        BIN_DIR / "ffmpeg.exe",
        BIN_DIR / "ffmpeg",
    ]
    env_path = os.environ.get("FFMPEG_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_filename(text: str, fallback: str = "词", max_len: int = 32) -> str:
    cleaned = ILLEGAL_FILENAME_RE.sub("", (text or "").strip())
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def strip_word(text: str) -> str:
    value = (text or "").replace("\u200b", "").strip()
    while value and (value[0] in PUNCT or value[0].isspace()):
        value = value[1:]
    while value and (value[-1] in TRAILING_PUNCT or value[-1].isspace()):
        value = value[:-1]
    return value.strip()


def round3(value: float) -> float:
    return round(float(value), 3)


def format_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}:{remainder:05.2f}"


def run_ffmpeg(args: list[str], timeout: int = 7200) -> subprocess.CompletedProcess[str]:
    if not FFMPEG:
        raise RuntimeError("没有找到 ffmpeg。请确认程序目录里的 bin\\ffmpeg.exe 没有被删除。")
    command = [FFMPEG, "-hide_banner", "-nostdin", "-y", *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg 处理超时了。视频可能太长，建议先剪成 10 分钟以内再试。") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        detail = detail[-1800:]
        raise RuntimeError(f"ffmpeg 处理失败：{detail or '未知错误'}")
    return completed


def probe_media(path: Path) -> dict[str, Any]:
    if not FFMPEG:
        raise RuntimeError("没有找到 ffmpeg。")
    completed = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    match = DURATION_RE.search(output)
    duration = 0.0
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    has_video = bool(VIDEO_STREAM_RE.search(output))
    has_audio = bool(AUDIO_STREAM_RE.search(output))
    return {
        "duration": round3(duration),
        "has_video": has_video,
        "has_audio": has_audio,
    }


JOBS_LOCK = threading.RLock()
JOBS: dict[str, dict[str, Any]] = {}
JOB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="word-cut-job")
MODEL_CACHE: dict[str, Any] = {}
MODEL_LOCK = threading.Lock()


def get_job(job_id: str) -> dict[str, Any] | None:
    if not JOB_ID_RE.fullmatch(job_id or ""):
        return None
    with JOBS_LOCK:
        return JOBS.get(job_id)


def job_dir_for(job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id or ""):
        raise ValueError("无效的任务编号")
    path = (JOBS_DIR / job_id).resolve()
    if path.parent != JOBS_DIR.resolve():
        raise ValueError("无效的任务路径")
    return path


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in job.items() if not key.startswith("_")}
    result.pop("raw_words", None)
    result["id"] = job["id"]
    result["model_label"] = MODEL_LABELS.get(job.get("settings", {}).get("model"), job.get("settings", {}).get("model", ""))
    return result


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(changes)
        if changes.get("status") in {"done", "ready", "error"}:
            persist_job(job)


def persist_job(job: dict[str, Any]) -> None:
    try:
        job_dir = job_dir_for(job["id"])
        job_dir.mkdir(parents=True, exist_ok=True)
        target = job_dir / "job.json"
        temp = job_dir / "job.json.tmp"
        payload = {key: value for key, value in job.items() if not key.startswith("_")}
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
    except Exception:
        pass


def load_existing_jobs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for job_file in JOBS_DIR.glob("*/job.json"):
        try:
            payload = json.loads(job_file.read_text(encoding="utf-8"))
            job_id = payload.get("id")
            if not job_id or not JOB_ID_RE.fullmatch(job_id):
                continue
            if payload.get("status") in {"queued", "probe", "extract", "model", "transcribe", "postprocess", "cutting"}:
                payload["status"] = "error"
                payload["message"] = "上次运行被中断，请重新上传或重试。"
                payload["error"] = "程序在这段视频处理完成前退出了。已保留原始文件。"
            JOBS[job_id] = payload
        except Exception:
            continue


def extract_audio(job_id: str, source: Path, audio_path: Path) -> None:
    update_job(
        job_id,
        stage="extract",
        progress=12,
        message="正在提取音轨…",
    )
    run_ffmpeg(
        [
            "-i", str(source),
            "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(audio_path),
        ]
    )
    if not audio_path.exists() or audio_path.stat().st_size < 64:
        raise RuntimeError("没有提取到有效音轨，这个文件可能没有声音。")


def ensure_model(job_id: str, model_name: str) -> Path:
    model_name = model_name if model_name in ALLOWED_MODELS else DEFAULT_MODEL
    target = MODELS_DIR / model_name
    if (target / "model.bin").is_file():
        return target

    update_job(
        job_id,
        stage="model",
        progress=22,
        message=f"正在下载 {model_name} 语音模型（首次使用，约几十秒到几分钟）…",
    )
    try:
        from huggingface_hub import snapshot_download

        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=f"Systran/faster-whisper-{model_name}",
            local_dir=str(target),
            max_workers=4,
        )
    except Exception as exc:
        raise RuntimeError(
            f"{model_name} 模型下载失败。请检查网络后重试，或在设置里换用 tiny / base 模型。\n{exc}"
        ) from exc
    if not (target / "model.bin").is_file():
        raise RuntimeError(f"{model_name} 模型文件不完整，请重试。")
    return target


def load_model(job_id: str, model_name: str) -> Any:
    model_name = model_name if model_name in ALLOWED_MODELS else DEFAULT_MODEL
    with MODEL_LOCK:
        if model_name in MODEL_CACHE:
            return MODEL_CACHE[model_name]
        from faster_whisper import WhisperModel

        model_path = ensure_model(job_id, model_name)
        update_job(
            job_id,
            stage="model",
            progress=28,
            message=f"正在加载 {model_name} 模型…",
        )
        cpu_threads = max(1, min(8, (os.cpu_count() or 4) - 1))
        model = WhisperModel(
            str(model_path),
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
            num_workers=1,
        )
        if len(MODEL_CACHE) >= 2:
            oldest_key = next(iter(MODEL_CACHE))
            MODEL_CACHE.pop(oldest_key, None)
        MODEL_CACHE[model_name] = model
        return model


def transcribe(
    job_id: str,
    audio_path: Path,
    model_name: str,
    language: str,
    duration: float,
) -> tuple[list[dict[str, Any]], str]:
    model = load_model(job_id, model_name)
    update_job(
        job_id,
        stage="transcribe",
        progress=32,
        message="正在逐词听写，请稍等…",
    )
    language_value = None if language in {"", "auto"} else language
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language_value,
        task="transcribe",
        beam_size=5,
        best_of=5,
        patience=1.0,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 250, "speech_pad_ms": 180},
        condition_on_previous_text=False,
        without_timestamps=False,
    )

    raw_words: list[dict[str, Any]] = []
    transcript_parts: list[str] = []
    total_duration = max(duration, float(getattr(info, "duration", 0.0) or 0.0), 0.1)
    segment_count = 0

    for segment in segments_iter:
        segment_count += 1
        sentence = (segment.text or "").strip()
        if sentence:
            transcript_parts.append(sentence)
        words = getattr(segment, "words", None) or []
        segment_start = float(getattr(segment, "start", 0.0) or 0.0)
        segment_end = float(getattr(segment, "end", segment_start) or segment_start)

        if not words and sentence:
            # Extremely rare fallback: distribute a segment evenly across its
            # visible characters so the job can still continue.
            chars = [ch for ch in sentence if ch not in PUNCT and not ch.isspace()]
            if chars:
                step = max((segment_end - segment_start) / len(chars), 0.03)
                for char_index, char in enumerate(chars):
                    start = segment_start + char_index * step
                    raw_words.append(
                        {
                            "text": char,
                            "start": round3(start),
                            "end": round3(start + step),
                            "sentence": sentence,
                            "sentence_index": segment_count,
                            "probability": 0.0,
                        }
                    )
        else:
            for word in words:
                text = strip_word(getattr(word, "word", "") or "")
                if not text:
                    continue
                start = float(getattr(word, "start", segment_start) or segment_start)
                end = float(getattr(word, "end", segment_end) or segment_end)
                if end <= start:
                    end = start + 0.06
                try:
                    probability = float(getattr(word, "probability", 0.0) or 0.0)
                except Exception:
                    probability = 0.0
                raw_words.append(
                    {
                        "text": text,
                        "start": round3(max(0.0, start)),
                        "end": round3(max(start + 0.02, end)),
                        "sentence": sentence,
                        "sentence_index": segment_count,
                        "probability": round(probability, 3),
                    }
                )

        progress = 32 + 42 * clamp(segment_end / total_duration, 0.0, 1.0)
        update_job(
            job_id,
            progress=round(progress, 1),
            message=f"正在逐词听写 · 已处理到 {format_clock(segment_end)}",
            recognized_count=len(raw_words),
        )

    transcript = "".join(transcript_parts).strip()
    return raw_words, transcript


def build_char_timeline(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chars: list[dict[str, Any]] = []
    for word in words:
        text = "".join(ch for ch in word["text"] if not ch.isspace())
        if not text:
            continue
        start = float(word["start"])
        end = float(word["end"])
        if end <= start:
            end = start + 0.06
        step = max((end - start) / len(text), 0.002)
        probability = float(word.get("probability") or 0.0)
        for index, char in enumerate(text):
            if char in PUNCT:
                continue
            char_start = start + index * step
            chars.append(
                {
                    "text": char,
                    "start": round3(char_start),
                    "end": round3(char_start + step),
                    "sentence": word.get("sentence", ""),
                    "sentence_index": word.get("sentence_index", 0),
                    "probability": probability,
                }
            )
    return chars


def make_token(
    text: str,
    start: float,
    end: float,
    sentence: str,
    sentence_index: int,
    probability: float,
) -> dict[str, Any]:
    return {
        "text": text.strip(),
        "start": round3(max(0.0, start)),
        "end": round3(max(start + 0.02, end)),
        "sentence": sentence,
        "sentence_index": sentence_index,
        "probability": round(clamp(probability, 0.0, 1.0), 3),
    }


def word_frequency(text: str) -> int:
    try:
        return int(jieba.get_FREQ(text) or 0)
    except Exception:
        return 0


def split_token_text(text: str) -> list[str]:
    """Split long Chinese ASR tokens into small dictionary words.

    Whisper often emits a correct token, a few lone characters, then another
    token (e.g. 今天 / 天 / 气 / 很好). We keep short tokens, split unusually
    long ones with jieba's frequency table, and later merge lone characters.
    """
    if not CJK_RE.search(text):
        return [text]
    parts: list[str] = []
    position = 0
    while position < len(text):
        matched = ""
        for length in (3, 2):
            if position + length <= len(text):
                candidate = text[position:position + length]
                if word_frequency(candidate) > 0:
                    matched = candidate
                    break
        if matched:
            parts.append(matched)
            position += len(matched)
        else:
            parts.append(text[position])
            position += 1
    if len(parts) <= 1:
        return [text]
    if all(len(part) == 1 for part in parts):
        # Likely a name or an out-of-vocabulary phrase. Keeping it whole is
        # safer than exploding it into meaningless characters.
        return [text]
    return parts


def split_word_units(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for word in words:
        text = "".join(char for char in word["text"] if not char.isspace())
        if not text:
            continue
        parts = split_token_text(text)
        if len(parts) == 1:
            units.append({**word, "text": text})
            continue
        start = float(word["start"])
        end = float(word["end"])
        total_chars = sum(len(part) for part in parts)
        cursor = start
        for part in parts:
            part_end = cursor + (end - start) * len(part) / max(total_chars, 1)
            units.append(
                {
                    **word,
                    "text": part,
                    "start": round3(cursor),
                    "end": round3(part_end),
                }
            )
            cursor = part_end
    return units


def merge_lone_chinese_characters(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    position = 0
    while position < len(units):
        current = units[position]
        if len(current["text"]) == 1 and CJK_RE.match(current["text"]):
            combined: tuple[int, str] | None = None
            for length in (3, 2):
                group = units[position:position + length]
                if len(group) != length:
                    continue
                if not all(len(item["text"]) == 1 and CJK_RE.match(item["text"]) for item in group):
                    continue
                candidate = "".join(item["text"] for item in group)
                if word_frequency(candidate) > 0:
                    combined = (length, candidate)
                    break
            if combined:
                length, candidate = combined
                group = units[position:position + length]
                probabilities = [float(item.get("probability", 0.0) or 0.0) for item in group]
                merged.append(
                    make_token(
                        candidate,
                        group[0]["start"],
                        group[-1]["end"],
                        group[0].get("sentence", ""),
                        int(group[0].get("sentence_index", 0) or 0),
                        sum(probabilities) / len(probabilities),
                    )
                )
                position += length
                continue
        merged.append(current)
        position += 1
    return merged


def segment_words(
    raw_words: list[dict[str, Any]],
    granularity: str,
    duration: float,
) -> list[dict[str, Any]]:
    if not raw_words:
        return []

    combined_text = "".join(word["text"] for word in raw_words)
    has_cjk = bool(CJK_RE.search(combined_text))

    if granularity == "char" and has_cjk:
        tokens = build_char_timeline(raw_words)
    elif granularity == "word" and has_cjk:
        tokens = []
        grouped: dict[int, list[dict[str, Any]]] = {}
        for word in raw_words:
            grouped.setdefault(int(word.get("sentence_index", 0)), []).append(word)
        for sentence_index in sorted(grouped):
            sentence_words = grouped[sentence_index]
            units = split_word_units(sentence_words)
            for unit in merge_lone_chinese_characters(units):
                tokens.append(
                    make_token(
                        unit["text"],
                        unit["start"],
                        unit["end"],
                        unit.get("sentence", ""),
                        sentence_index,
                        float(unit.get("probability", 0.0) or 0.0),
                    )
                )
    else:
        tokens = [
            make_token(
                word["text"],
                float(word["start"]),
                float(word["end"]),
                word.get("sentence", ""),
                int(word.get("sentence_index", 0)),
                float(word.get("probability", 0.0) or 0.0),
            )
            for word in raw_words
            if strip_word(word.get("text", ""))
        ]

    tokens = [token for token in tokens if token["text"].strip()]
    tokens.sort(key=lambda item: (item["start"], item["end"]))
    cleaned: list[dict[str, Any]] = []
    for index, token in enumerate(tokens, start=1):
        token["id"] = index
        token["start"] = round3(clamp(token["start"], 0.0, max(duration - 0.01, 0.0)))
        token["end"] = round3(clamp(token["end"], token["start"] + 0.02, max(duration, token["start"] + 0.02)))
        token["duration"] = round3(token["end"] - token["start"])
        cleaned.append(token)
    return cleaned


def clip_bounds(
    words: list[dict[str, Any]],
    index: int,
    duration: float,
    padding: float,
) -> tuple[float, float]:
    word = words[index]
    core_start = float(word["start"])
    core_end = float(word["end"])
    previous = words[index - 1] if index > 0 else None
    following = words[index + 1] if index + 1 < len(words) else None

    # Padding is a maximum, not a promise. If two words are glued together in
    # the source, only leave a tiny 12ms safety margin; otherwise a 60ms pad
    # would audibly include the neighbouring word.
    if previous is None:
        left_pad = padding
    else:
        left_gap = core_start - float(previous["end"])
        left_pad = min(padding, max(0.012, left_gap / 2))
    if following is None:
        right_pad = padding
    else:
        right_gap = float(following["start"]) - core_end
        right_pad = min(padding, max(0.012, right_gap / 2))

    start = max(0.0, core_start - left_pad)
    end = min(duration, core_end + right_pad)
    minimum = 0.20
    if end - start < minimum:
        center = (start + end) / 2
        start = max(0.0, center - minimum / 2)
        end = min(duration, start + minimum)
        if end - start < minimum and start > 0:
            start = max(0.0, end - minimum)
    if end <= start:
        end = min(duration, start + 0.05)
    return round3(start), round3(end)


def build_video_filter(quality: str) -> str:
    if quality == "720p":
        return "scale=-2:720:flags=lanczos"
    if quality == "1080p":
        return "scale=-2:1080:flags=lanczos"
    return "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def cut_single_clip(
    source: Path,
    clips_dir: Path,
    filename_base: str,
    start: float,
    duration: float,
    output_format: str,
    quality: str,
    has_video: bool,
) -> dict[str, str]:
    files: dict[str, str] = {}
    if output_format in {"video", "both"} and has_video:
        video_name = f"{filename_base}.mp4"
        video_path = clips_dir / video_name
        run_ffmpeg(
            [
                "-ss", f"{start:.3f}",
                "-i", str(source),
                "-t", f"{duration:.3f}",
                "-map", "0:v:0",
                "-map", "0:a:0?",
                "-vf", build_video_filter(quality),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-ac", "2",
                "-sn", "-dn",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                "-threads", "2",
                str(video_path),
            ]
        )
        if video_path.is_file() and video_path.stat().st_size > 0:
            files["mp4"] = video_name

    if output_format in {"audio", "both"} or not has_video:
        audio_name = f"{filename_base}.wav"
        audio_path = clips_dir / audio_name
        run_ffmpeg(
            [
                "-ss", f"{start:.3f}",
                "-i", str(source),
                "-t", f"{duration:.3f}",
                "-vn", "-sn", "-dn",
                "-map", "0:a:0",
                "-c:a", "pcm_s16le",
                "-ar", "48000",
                "-ac", "2",
                "-avoid_negative_ts", "make_zero",
                str(audio_path),
            ]
        )
        if audio_path.is_file() and audio_path.stat().st_size > 0:
            files["wav"] = audio_name

    if not files:
        raise RuntimeError("切片没有生成有效文件")
    return files


def cut_clips(job: dict[str, Any]) -> list[dict[str, Any]]:
    job_id = job["id"]
    job_dir = job_dir_for(job_id)
    clips_dir = job_dir / "clips"
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    source = job_dir / job["source"]["stored_name"]
    duration = float(job["source"]["duration"])
    words = job.get("words") or []
    settings = job.get("settings") or {}
    padding = clamp(float(settings.get("padding", 0.06)), 0.0, 0.3)
    output_format = settings.get("format", "video")
    if output_format not in {"video", "audio", "both"}:
        output_format = "video"
    quality = settings.get("quality", "720p")
    if quality not in {"original", "720p", "1080p"}:
        quality = "720p"
    has_video = bool(job["source"].get("has_video"))

    total = len(words)
    if total == 0:
        raise RuntimeError("词表是空的，没有可以切分的内容。")

    width = max(3, len(str(total)))
    tasks: list[tuple[int, dict[str, Any], float, float, str]] = []
    for index, word in enumerate(words, start=1):
        start, end = clip_bounds(words, index - 1, duration, padding)
        filename_base = f"{index:0{width}d}_{safe_filename(word['text'])}"
        tasks.append((index, word, start, end, filename_base))

    clips: list[dict[str, Any]] = []
    errors: list[str] = []
    workers = max(1, min(3, (os.cpu_count() or 4) // 2))
    update_job(
        job_id,
        stage="cutting",
        progress=80,
        message=f"正在切出 {total} 个片段…",
        clips=[],
    )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="word-cut-clip") as pool:
        future_map = {}
        for index, word, start, end, filename_base in tasks:
            future = pool.submit(
                cut_single_clip,
                source,
                clips_dir,
                filename_base,
                start,
                end - start,
                output_format,
                quality,
                has_video,
            )
            future_map[future] = (index, word, start, end, filename_base)

        completed_count = 0
        for future in as_completed(future_map):
            index, word, start, end, filename_base = future_map[future]
            completed_count += 1
            try:
                files = future.result()
                clips.append(
                    {
                        "index": index,
                        "text": word["text"],
                        "start": start,
                        "end": end,
                        "duration": round3(end - start),
                        "core_start": word["start"],
                        "core_end": word["end"],
                        "sentence": word.get("sentence", ""),
                        "confidence": word.get("probability", 0.0),
                        "files": files,
                    }
                )
            except Exception as exc:
                errors.append(f"第 {index} 个「{word['text']}」切片失败：{exc}")

            progress = 80 + 17 * (completed_count / total)
            update_job(
                job_id,
                progress=round(progress, 1),
                message=f"正在切片 · {completed_count}/{total}",
            )

    clips.sort(key=lambda item: item["index"])
    if not clips:
        detail = errors[0] if errors else "所有片段都切分失败。"
        raise RuntimeError(detail)

    job["clips"] = clips
    job["cut_errors"] = errors[:8]
    update_job(job_id, clips=clips, cut_errors=errors[:8])
    return clips


def build_zip(job: dict[str, Any]) -> Path:
    job_id = job["id"]
    job_dir = job_dir_for(job_id)
    clips_dir = job_dir / "clips"
    stem = safe_filename(Path(job["source"]["name"]).stem, fallback="视频", max_len=48)
    zip_path = job_dir / f"{stem}_词切片.zip"
    if zip_path.exists():
        zip_path.unlink()

    clips = job.get("clips") or []
    csv_buffer = io.StringIO()
    csv_writer = csv.writer(csv_buffer)
    csv_writer.writerow(["序号", "词语", "开始秒", "结束秒", "时长秒", "文件名", "原句"])
    for clip in clips:
        filename = clip["files"].get("mp4") or clip["files"].get("wav") or ""
        csv_writer.writerow(
            [
                clip["index"],
                clip["text"],
                f"{clip['start']:.3f}",
                f"{clip['end']:.3f}",
                f"{clip['duration']:.3f}",
                filename,
                clip.get("sentence", ""),
            ]
        )

    index_payload = {
        "source": job["source"],
        "settings": job.get("settings", {}),
        "transcript": job.get("transcript", ""),
        "words": job.get("words", []),
        "clips": clips,
    }
    guide = (
        "词切 · RAP 素材机\n"
        "================\n\n"
        "这个压缩包包含：\n"
        "- clips/：按词切好的视频或音频片段\n"
        "- 词表.csv：可以用 Excel 打开的时间轴\n"
        "- word_index.json：完整时间点，方便导入剪辑软件或继续处理\n"
        "- transcript.txt：识别出的完整文字\n\n"
        "文件名规则：序号_词.mp4（例如 0012_喜欢.mp4）\n"
        "MP4 已是 48kHz 音频，可直接拖进剪映、Premiere、DaVinci Resolve。\n"
        "如果识别有错，回到网页点「编辑词表」修正文字和时间点，再重新切片即可。\n"
    )

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for clip in clips:
            for kind in ("mp4", "wav"):
                filename = clip["files"].get(kind)
                if not filename:
                    continue
                file_path = clips_dir / filename
                if file_path.is_file():
                    archive.write(file_path, f"clips/{filename}", compress_type=zipfile.ZIP_STORED)
        archive.writestr("词表.csv", "\ufeff" + csv_buffer.getvalue())
        archive.writestr("word_index.json", json.dumps(index_payload, ensure_ascii=False, indent=2))
        archive.writestr("transcript.txt", job.get("transcript", ""))
        archive.writestr("使用说明.txt", guide)
    return zip_path


def process_job(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    job_dir = job_dir_for(job_id)
    source = job_dir / job["source"]["stored_name"]
    started = time.time()
    try:
        if not source.is_file():
            raise RuntimeError("上传的视频文件不见了，请重新上传。")
        if not FFMPEG:
            raise RuntimeError("没有找到 ffmpeg，程序无法处理视频。")

        update_job(job_id, status="probe", stage="probe", progress=5, message="正在读取视频信息…")
        info = probe_media(source)
        if not info["has_audio"]:
            raise RuntimeError("这个文件里没有音轨，换一个有声音的视频或音频文件再试。")
        if info["duration"] < 0.3:
            raise RuntimeError("视频太短了，至少需要 0.3 秒。")
        job["source"].update(info)
        update_job(job_id, source=job["source"])

        audio_path = job_dir / "audio-16k.wav"
        extract_audio(job_id, source, audio_path)

        raw_words, transcript = transcribe(
            job_id,
            audio_path,
            job["settings"].get("model", DEFAULT_MODEL),
            job["settings"].get("language", "zh"),
            float(info["duration"]),
        )
        if not raw_words:
            raise RuntimeError("没有识别到说话内容。可能是纯音乐、音量太小，或者语言选错了。")

        update_job(
            job_id,
            stage="postprocess",
            progress=77,
            message="正在整理词和时间点…",
            raw_words=raw_words,
            transcript=transcript,
        )
        words = segment_words(
            raw_words,
            job["settings"].get("granularity", "word"),
            float(info["duration"]),
        )
        if not words:
            raise RuntimeError("识别出了文字，但没有整理出可切分的词。")
        update_job(job_id, words=words, word_count=len(words), transcript=transcript)

        if not job["settings"].get("auto_cut", True):
            update_job(
                job_id,
                status="ready",
                stage="ready",
                progress=80,
                message="识别完成。校对词表后点「保存并重新切片」。",
                elapsed_seconds=round(time.time() - started, 1),
            )
            return

        clips = cut_clips(job)
        update_job(job_id, stage="zip", progress=99, message="正在打包 ZIP…", clips=clips)
        zip_path = build_zip(job)
        update_job(
            job_id,
            status="done",
            stage="done",
            progress=100,
            message=f"完成，共切出 {len(clips)} 个片段。",
            clips=clips,
            zip=zip_path.name,
            clip_count=len(clips),
            elapsed_seconds=round(time.time() - started, 1),
            finished_at=now_iso(),
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            stage="error",
            progress=100,
            message="处理失败",
            error=str(exc),
            elapsed_seconds=round(time.time() - started, 1),
        )
    finally:
        audio_path = job_dir / "audio-16k.wav"
        if audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass


def process_recut(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    started = time.time()
    try:
        update_job(
            job_id,
            status="cutting",
            stage="cutting",
            progress=80,
            message="正在按新词表重新切片…",
            error=None,
            zip=None,
        )
        clips = cut_clips(job)
        update_job(job_id, stage="zip", progress=99, message="正在打包 ZIP…", clips=clips)
        zip_path = build_zip(job)
        update_job(
            job_id,
            status="done",
            stage="done",
            progress=100,
            message=f"重新切片完成，共 {len(clips)} 个片段。",
            clips=clips,
            zip=zip_path.name,
            clip_count=len(clips),
            elapsed_seconds=round(time.time() - started, 1),
            finished_at=now_iso(),
        )
    except Exception as exc:
        update_job(
            job_id,
            status="error",
            stage="error",
            progress=100,
            message="重新切片失败",
            error=str(exc),
        )


def parse_settings(form: Any) -> dict[str, Any]:
    model = str(form.get("model", DEFAULT_MODEL)).strip()
    if model not in ALLOWED_MODELS:
        model = DEFAULT_MODEL
    language = str(form.get("language", "zh")).strip()
    if language not in {"zh", "auto", "en"}:
        language = "zh"
    granularity = str(form.get("granularity", "word")).strip()
    if granularity not in {"word", "char"}:
        granularity = "word"
    try:
        padding = clamp(float(form.get("padding", 0.06)), 0.0, 0.3)
    except (TypeError, ValueError):
        padding = 0.06
    output_format = str(form.get("format", "video")).strip()
    if output_format not in {"video", "audio", "both"}:
        output_format = "video"
    quality = str(form.get("quality", "720p")).strip()
    if quality not in {"original", "720p", "1080p"}:
        quality = "720p"
    auto_cut_value = str(form.get("auto_cut", "true")).lower()
    return {
        "model": model,
        "language": language,
        "granularity": granularity,
        "padding": round(padding, 3),
        "format": output_format,
        "quality": quality,
        "auto_cut": auto_cut_value in {"1", "true", "yes", "on"},
    }


def validate_words(payload: Any, duration: float) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("词表不能为空。")
    if len(payload) > 5000:
        raise ValueError("一次最多处理 5000 个词。")
    result: list[dict[str, Any]] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        text = strip_word(str(raw.get("text", "")))
        if not text:
            continue
        if len(text) > 80:
            text = text[:80]
        try:
            start = float(raw.get("start", 0.0))
            end = float(raw.get("end", start + 0.1))
        except (TypeError, ValueError):
            continue
        start = clamp(start, 0.0, max(duration - 0.01, 0.0))
        end = clamp(end, start + 0.02, max(duration, start + 0.02))
        result.append(
            {
                "text": text,
                "start": round3(start),
                "end": round3(end),
                "sentence": str(raw.get("sentence", ""))[:500],
                "sentence_index": int(raw.get("sentence_index", 0) or 0),
                "probability": round(clamp(float(raw.get("probability", 0.0) or 0.0), 0.0, 1.0), 3),
            }
        )
    result.sort(key=lambda item: (item["start"], item["end"]))
    for index, word in enumerate(result, start=1):
        word["id"] = index
        word["duration"] = round3(word["end"] - word["start"])
    if not result:
        raise ValueError("没有有效的词。")
    return result


def job_is_busy(job: dict[str, Any]) -> bool:
    return job.get("status") in {"queued", "probe", "extract", "model", "transcribe", "postprocess", "cutting", "zip"}


def pending_job_count() -> int:
    with JOBS_LOCK:
        return sum(1 for job in JOBS.values() if job_is_busy(job))


CLEANUP_STARTED = False


def cleanup_old_jobs() -> None:
    if JOB_TTL_HOURS <= 0:
        return
    cutoff = time.time() - JOB_TTL_HOURS * 3600
    with JOBS_LOCK:
        snapshot = list(JOBS.items())
    for job_id, job in snapshot:
        if job_is_busy(job) or job.get("status") not in {"done", "ready", "error"}:
            continue
        try:
            job_dir = job_dir_for(job_id)
            if not job_dir.is_dir() or job_dir.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            continue
        with JOBS_LOCK:
            JOBS.pop(job_id, None)


def cleanup_loop() -> None:
    while True:
        time.sleep(1800)
        try:
            cleanup_old_jobs()
        except Exception:
            pass


def start_cleanup_thread() -> None:
    global CLEANUP_STARTED
    if JOB_TTL_HOURS <= 0 or CLEANUP_STARTED:
        return
    CLEANUP_STARTED = True
    threading.Thread(target=cleanup_loop, daemon=True, name="job-cleanup").start()


def initialize_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    load_existing_jobs()
    start_cleanup_thread()


@app.after_request
def add_headers(response: Any) -> Any:
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


AUTH_EXEMPT_PATHS = {"/api/health", "/api/session", "/api/login"}


@app.before_request
def require_site_password() -> Any:
    if not SITE_PASSWORD or not request.path.startswith("/api/"):
        return None
    if request.path in AUTH_EXEMPT_PATHS:
        return None
    if session.get("auth") is True:
        return None
    return jsonify({"error": "需要访问口令。", "code": "auth_required"}), 401


@app.get("/api/session")
def session_state() -> Any:
    return jsonify(
        {
            "required": bool(SITE_PASSWORD),
            "authenticated": bool(session.get("auth") is True),
        }
    )


@app.post("/api/login")
def login() -> Any:
    if not SITE_PASSWORD:
        return jsonify({"ok": True, "required": False})
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password", ""))
    if hmac.compare_digest(password.encode("utf-8"), SITE_PASSWORD.encode("utf-8")):
        session.clear()
        session["auth"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"error": "访问口令不对。"}), 401


@app.post("/api/logout")
def logout() -> Any:
    session.clear()
    return jsonify({"ok": True})


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(_: Any) -> Any:
    return jsonify({"error": f"文件太大了，单个文件请控制在 {MAX_UPLOAD_MB}MB 以内。"}), 413


@app.get("/")
def index() -> Any:
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/health")
def health() -> Any:
    models = {}
    for name in sorted(ALLOWED_MODELS):
        models[name] = (MODELS_DIR / name / "model.bin").is_file()
    return jsonify(
        {
            "ok": bool(FFMPEG),
            "ffmpeg": bool(FFMPEG),
            "models": models,
            "default_model": DEFAULT_MODEL,
            "max_upload_mb": MAX_UPLOAD_MB,
            "job_ttl_hours": JOB_TTL_HOURS,
            "max_pending_jobs": MAX_PENDING_JOBS,
            "site_password_required": bool(SITE_PASSWORD),
            "version": "1.0.0",
        }
    )


@app.post("/api/jobs")
def create_job() -> Any:
    if pending_job_count() >= MAX_PENDING_JOBS:
        return jsonify({"error": "当前排队的视频太多了，等前面的任务完成后再试。"}), 429
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "没有收到文件，请重新选择视频。"}), 400
    original_name = uploaded.filename
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTS:
        return jsonify({"error": f"不支持 {extension or '这种'} 格式，请换 MP4 / MOV / MKV / MP3 等常见格式。"}), 400
    settings = parse_settings(request.form)
    job_id = uuid.uuid4().hex[:12]
    job_dir = job_dir_for(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"source{extension}"
    source_path = job_dir / stored_name
    uploaded.save(source_path)
    size = source_path.stat().st_size if source_path.exists() else 0
    if size > MAX_CONTENT_BYTES:
        source_path.unlink(missing_ok=True)
        return jsonify({"error": f"文件太大了，单个文件请控制在 {MAX_UPLOAD_MB}MB 以内。"}), 413
    if size <= 0:
        return jsonify({"error": "上传的文件是空的，请重新选择。"}), 400

    job = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "progress": 3,
        "message": "已收到文件，正在排队…",
        "error": None,
        "created_at": now_iso(),
        "source": {
            "name": original_name,
            "stored_name": stored_name,
            "size": size,
            "extension": extension,
            "duration": 0.0,
            "has_video": extension in VIDEO_EXTS,
            "has_audio": True,
        },
        "settings": settings,
        "words": [],
        "raw_words": [],
        "clips": [],
        "transcript": "",
        "recognized_count": 0,
        "elapsed_seconds": 0,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    persist_job(job)
    JOB_EXECUTOR.submit(process_job, job_id)
    return jsonify(public_job(job)), 202


@app.get("/api/jobs/<job_id>")
def get_job_state(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在，可能已经被清理。"}), 404
    with JOBS_LOCK:
        return jsonify(public_job(job))


@app.post("/api/jobs/<job_id>/recut")
def recut(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在。"}), 404
    if job_is_busy(job):
        return jsonify({"error": "这个任务还在处理中，请等它完成。"}), 409
    payload = request.get_json(silent=True) or {}
    try:
        duration = float(job["source"].get("duration") or 0.0)
        words = validate_words(payload.get("words", job.get("words")), duration)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    settings = dict(job.get("settings") or {})
    if "padding" in payload:
        try:
            settings["padding"] = round(clamp(float(payload["padding"]), 0.0, 0.3), 3)
        except (TypeError, ValueError):
            pass
    if payload.get("format") in {"video", "audio", "both"}:
        settings["format"] = payload["format"]
    if payload.get("quality") in {"original", "720p", "1080p"}:
        settings["quality"] = payload["quality"]
    with JOBS_LOCK:
        job["words"] = words
        job["word_count"] = len(words)
        job["settings"] = settings
        job["status"] = "cutting"
        job["stage"] = "cutting"
        job["progress"] = 80
        job["message"] = "正在准备重新切片…"
        job["error"] = None
        job["zip"] = None
        job["clips"] = []
    persist_job(job)
    JOB_EXECUTOR.submit(process_recut, job_id)
    return jsonify(public_job(job)), 202


@app.post("/api/jobs/<job_id>/resegment")
def resegment(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在。"}), 404
    if job_is_busy(job):
        return jsonify({"error": "这个任务还在处理中，请等它完成。"}), 409
    raw_words = job.get("raw_words") or []
    if not raw_words:
        return jsonify({"error": "没有原始识别数据，无法重新分词。请重新上传视频。"}), 400
    payload = request.get_json(silent=True) or {}
    granularity = payload.get("granularity", "word")
    if granularity not in {"word", "char"}:
        granularity = "word"
    duration = float(job["source"].get("duration") or 0.0)
    words = segment_words(raw_words, granularity, duration)
    if not words:
        return jsonify({"error": "重新分词后词表为空。"}), 400
    with JOBS_LOCK:
        job["words"] = words
        job["word_count"] = len(words)
        job["settings"]["granularity"] = granularity
        job["status"] = "cutting"
        job["stage"] = "cutting"
        job["progress"] = 80
        job["message"] = "正在按新粒度重新分词并切片…"
        job["error"] = None
        job["zip"] = None
        job["clips"] = []
    persist_job(job)
    JOB_EXECUTOR.submit(process_recut, job_id)
    return jsonify(public_job(job)), 202


@app.get("/api/jobs/<job_id>/media/<int:index>/<kind>")
def get_media(job_id: str, index: int, kind: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在。"}), 404
    clip = next((item for item in (job.get("clips") or []) if int(item.get("index", -1)) == index), None)
    if not clip:
        return jsonify({"error": "片段不存在。"}), 404
    filename = (clip.get("files") or {}).get(kind)
    if not filename:
        return jsonify({"error": "这个片段没有该格式。"}), 404
    clips_dir = (job_dir_for(job_id) / "clips").resolve()
    file_path = (clips_dir / filename).resolve()
    if file_path.parent != clips_dir or not file_path.is_file():
        return jsonify({"error": "片段文件不存在。"}), 404
    as_attachment = request.args.get("download") in {"1", "true", "yes"}
    return send_file(
        file_path,
        conditional=True,
        as_attachment=as_attachment,
        download_name=filename,
        max_age=3600,
    )


@app.get("/api/jobs/<job_id>/download.zip")
def download_zip(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在。"}), 404
    zip_name = job.get("zip")
    if not zip_name:
        return jsonify({"error": "还没有生成压缩包，请先完成切片。"}), 404
    job_dir = job_dir_for(job_id)
    zip_path = (job_dir / zip_name).resolve()
    if zip_path.parent != job_dir or not zip_path.is_file():
        return jsonify({"error": "压缩包不存在，请重新切片。"}), 404
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=zip_name,
        conditional=True,
        max_age=0,
    )


@app.get("/api/jobs/<job_id>/export.json")
def export_json(job_id: str) -> Any:
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在。"}), 404
    payload = {
        "source": job.get("source"),
        "settings": job.get("settings"),
        "transcript": job.get("transcript"),
        "words": job.get("words"),
        "clips": job.get("clips"),
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return send_file(
        io.BytesIO(data),
        mimetype="application/json",
        as_attachment=True,
        download_name="word_index.json",
    )


def find_free_port(start: int = 8765) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="词切 · RAP 素材机")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    port = args.port or find_free_port()
    url = f"http://{args.host}:{port}"
    print("")
    print("  词切 · RAP 素材机")
    print("  -----------------")
    print(f"  打开浏览器访问：{url}")
    print("  关闭这个窗口即可停止服务。")
    print("")

    if not args.no_browser:
        def open_later() -> None:
            time.sleep(1.0)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=open_later, daemon=True).start()

    app.run(host=args.host, port=port, debug=False, use_reloader=False, threaded=True)


initialize_storage()


if __name__ == "__main__":
    main()
