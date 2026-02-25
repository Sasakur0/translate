#!/usr/bin/env python3
# coding=utf-8

import argparse
import json
import os
import sys

import torch
from qwen_asr import Qwen3ASRModel


def _resolve_device() -> str:
    preferred = os.getenv("QWEN3_ASR_DEVICE", "auto").strip().lower()
    if preferred not in {"auto", "mps", "cpu", "cuda"}:
        preferred = "auto"

    if preferred == "mps":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if preferred == "cuda":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if preferred == "cpu":
        return "cpu"

    # auto: 优先 CUDA，其次 MPS，最后 CPU
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _resolve_dtype(device: str):
    preferred = os.getenv("QWEN3_ASR_DTYPE", "auto").strip().lower()
    mapping = {
        "float16": torch.float16,
        "fp16": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
    }
    if preferred in mapping:
        return mapping[preferred]

    if device in {"cuda", "mps"}:
        return torch.float16
    return torch.float32


def _build_asr_model(model_id: str, device: str, torch_dtype):
    device_map = "cuda:0" if device == "cuda" else device
    max_batch_size = int(os.getenv("QWEN3_ASR_MAX_BATCH", "1"))
    max_new_tokens = int(os.getenv("QWEN3_ASR_MAX_NEW_TOKENS", "1024"))
    return Qwen3ASRModel.from_pretrained(
        model_id,
        dtype=torch_dtype,
        device_map=device_map,
        max_inference_batch_size=max_batch_size,
        max_new_tokens=max_new_tokens,
    )


def _transcribe(asr_model, audio_path: str, language: str | None):
    return asr_model.transcribe(
        audio=audio_path,
        language=_normalize_language(language),
    )


def _normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    value = language.strip().lower()
    if not value:
        return None
    mapping = {
        "zh": "Chinese",
        "zh-cn": "Chinese",
        "zh-hans": "Chinese",
        "en": "English",
    }
    return mapping.get(value, language)


def _write_device_meta(path: str | None, device: str, torch_dtype, fallback: bool) -> None:
    if not path:
        return
    payload = {
        "device": device,
        "dtype": str(torch_dtype).replace("torch.", ""),
        "fallback": fallback,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        # meta 仅用于展示，不影响主流程
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen3-ASR-0.6B transcription")
    parser.add_argument("input", help="audio file path")
    parser.add_argument("--language", default=None, help="language code, e.g. zh")
    parser.add_argument("--device-meta", default=None, help="optional path to write selected device metadata")
    args = parser.parse_args()

    model_id = os.getenv("QWEN3_ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")
    device = _resolve_device()
    torch_dtype = _resolve_dtype(device)

    try:
        asr = _build_asr_model(model_id, device, torch_dtype)
        _write_device_meta(args.device_meta, device, torch_dtype, fallback=False)
        print(f"[device] using {device} ({str(torch_dtype).replace('torch.', '')})", file=sys.stderr, flush=True)
    except Exception as first_exc:  # noqa: BLE001
        # 自动回退：设备不可用或后端不兼容时回到 CPU
        if device != "cpu":
            try:
                asr = _build_asr_model(model_id, "cpu", torch.float32)
                _write_device_meta(args.device_meta, "cpu", torch.float32, fallback=True)
                print(
                    f"[device] fallback to cpu (float32), reason: {first_exc}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as second_exc:  # noqa: BLE001
                print(f"Qwen3-ASR 初始化失败: {second_exc}", file=sys.stderr)
                return 1
        else:
            print(f"Qwen3-ASR 初始化失败: {first_exc}", file=sys.stderr)
            return 1

    try:
        result = _transcribe(asr, str(args.input), args.language)
    except Exception as exc:  # noqa: BLE001
        # 推理阶段也做一次 CPU 回退，解决 MPS/CUDA 在长音频上的内存问题
        if device != "cpu":
            try:
                print(
                    f"[device] inference fallback to cpu (float32), reason: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                asr = _build_asr_model(model_id, "cpu", torch.float32)
                _write_device_meta(args.device_meta, "cpu", torch.float32, fallback=True)
                result = _transcribe(asr, str(args.input), args.language)
            except Exception as cpu_exc:  # noqa: BLE001
                print(f"Qwen3-ASR 推理失败: {cpu_exc}", file=sys.stderr)
                return 1
        else:
            print(f"Qwen3-ASR 推理失败: {exc}", file=sys.stderr)
            return 1

    text = ""
    if isinstance(result, list) and result:
        first = result[0]
        text = str(getattr(first, "text", "")).strip()
        if not text and isinstance(first, dict):
            text = str(first.get("text", "")).strip()
    elif isinstance(result, dict):
        text = str(result.get("text", "")).strip()
    else:
        text = str(result).strip()
    if not text:
        print("Qwen3-ASR 返回空文本", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
