import argparse
import json
import os
import sys

from faster_whisper import WhisperModel


def to_srt(segments):
    lines = []
    for idx, seg in enumerate(segments, start=1):
        start = format_ts(seg["start"])
        end = format_ts(seg["end"])
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def format_ts(seconds):
    ms = int(seconds * 1000)
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    ms = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    parser = argparse.ArgumentParser(description="Whisper Turbo (faster-whisper)")
    parser.add_argument("input", help="audio/video file path")
    parser.add_argument("--language", default=None, help="language code, e.g. zh")
    parser.add_argument("--format", choices=["txt", "json", "srt"], default="txt")
    parser.add_argument("--beam_size", type=int, default=3)
    parser.add_argument("--vad", action="store_true", default=True)
    parser.add_argument(
        "--progress",
        action="store_true",
        default=True,
        help="print progress to stderr",
    )
    args = parser.parse_args()

    model_name = os.getenv("MODEL_SIZE", "large-v3-turbo")
    device = os.getenv("DEVICE", "cpu")
    compute_type = os.getenv("COMPUTE_TYPE", "int8")

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        args.input,
        beam_size=args.beam_size,
        language=args.language,
        vad_filter=args.vad,
    )

    seg_list = []
    duration = info.duration or 0
    last_percent = -1
    for seg in segments:
        seg_list.append(
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
            }
        )
        if args.progress:
            if duration > 0:
                percent = int(min(100, (seg.end / duration) * 100))
                if percent >= last_percent + 1:
                    last_percent = percent
                    print(
                        f"[progress] {percent}% ({seg.end:.1f}s / {duration:.1f}s)",
                        file=sys.stderr,
                        flush=True,
                    )
            elif len(seg_list) % 10 == 0:
                print(
                    f"[progress] processed {len(seg_list)} segments",
                    file=sys.stderr,
                    flush=True,
                )

    if args.format == "txt":
        print("".join(s["text"] for s in seg_list).strip())
        return

    if args.format == "srt":
        print(to_srt(seg_list))
        return

    payload = {
        "language": info.language,
        "language_probability": info.language_probability,
        "duration": info.duration,
        "segments": seg_list,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
