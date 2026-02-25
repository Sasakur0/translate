import datetime
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from aliyunsdkcore.auth.credentials import AccessKeyCredential
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL


class TaskCancelledError(Exception):
    pass


class GenerateRequest(BaseModel):
    video_url: str = Field(..., min_length=1)
    params: dict = Field(default_factory=dict)


class GenerateStartResponse(BaseModel):
    code: int
    taskId: str
    status: str
    message: str


class GenerateTaskStatusResponse(BaseModel):
    code: int
    taskId: str
    status: str
    progress: int = 0
    stage: str = ""
    processTime: float = 0
    content: str = ""
    detail: Optional[str] = None


app = FastAPI(title="Video AI Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WHISPER_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "whisper_turbo_transcribe.py"
WHISPER_DEFAULT_LANGUAGE = os.getenv("WHISPER_DEFAULT_LANGUAGE", "zh")
WHISPER_FORMAT = os.getenv("WHISPER_FORMAT", "json")
QWEN3_ASR_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "qwen3_asr_transcribe.py"

TINGWU_DOMAIN = "tingwu.cn-beijing.aliyuncs.com"
TINGWU_VERSION = "2023-09-30"
TINGWU_REGION = "cn-beijing"
TINGWU_PROTOCOL = "https"
TINGWU_TYPE = "offline"
TINGWU_CREATE_URI = "/openapi/tingwu/v2/tasks"
TINGWU_TASK_TIMEOUT_SECONDS = int(os.getenv("TINGWU_TASK_TIMEOUT_SECONDS", "1800"))
TINGWU_TASK_POLL_INTERVAL_SECONDS = float(os.getenv("TINGWU_TASK_POLL_INTERVAL_SECONDS", "5"))

# 千问听悟配置（选择“千问听悟”模式时生效）
ALIBABA_CLOUD_ACCESS_KEY_ID = "LTAI5tH6k6YH7qMeFoM9zBUu"
ALIBABA_CLOUD_ACCESS_KEY_SECRET = "FeJVoSg6ZTEtxeDE1cAusbyIFPDD3r"
ALIYUN_TINGWU_APP_KEY = "hkHe2whE11o5a8EC"
DOUBAO_APP_KEY = "6547067732"
DOUBAO_ACCESS_KEY = "pr-ndhHOOhsnRLWprwl1TM8S11s7kMw8"
DOUBAO_API_KEY = "请替换为你的DoubaoApiKey"
DOUBAO_RESOURCE_ID = "volc.seedasr.auc"
DOUBAO_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DOUBAO_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DOUBAO_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_TRANSLATE_PYTHON = "/opt/anaconda3/envs/translate/bin/python"
DEFAULT_FFMPEG_PATH = "/opt/anaconda3/envs/translate/bin/ffmpeg"
DEFAULT_FFPROBE_PATH = "/opt/anaconda3/envs/translate/bin/ffprobe"
PUBLIC_MEDIA_DIR = Path(os.getenv("PUBLIC_MEDIA_DIR", "/tmp/translate-public-media"))
PUBLIC_MEDIA_TTL_SECONDS = int(os.getenv("PUBLIC_MEDIA_TTL_SECONDS", "7200"))
PUBLIC_MEDIA_SECRET = os.getenv("PUBLIC_MEDIA_SECRET", "translate-dev-secret-change-me")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

TASK_STORE: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.Lock()


def _resolve_ffmpeg_location() -> tuple[str, str]:
    """
    返回可用的 ffmpeg 与 ffprobe 可执行文件路径。
    优先读取环境变量 FFMPEG_PATH / FFPROBE_PATH。
    """
    ffmpeg_path = os.getenv("FFMPEG_PATH", "").strip() or shutil.which("ffmpeg") or ""
    ffprobe_path = os.getenv("FFPROBE_PATH", "").strip() or shutil.which("ffprobe") or ""

    if not ffmpeg_path and Path(DEFAULT_FFMPEG_PATH).exists():
        ffmpeg_path = DEFAULT_FFMPEG_PATH
    if not ffprobe_path and Path(DEFAULT_FFPROBE_PATH).exists():
        ffprobe_path = DEFAULT_FFPROBE_PATH

    if ffmpeg_path and ffprobe_path:
        return ffmpeg_path, ffprobe_path

    # 常见安装位置兜底（Homebrew / Conda / 系统）
    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/anaconda3/bin",
        "/usr/bin",
    ]
    for base in candidates:
        if not ffmpeg_path:
            candidate = str(Path(base) / "ffmpeg")
            if Path(candidate).exists():
                ffmpeg_path = candidate
        if not ffprobe_path:
            candidate = str(Path(base) / "ffprobe")
            if Path(candidate).exists():
                ffprobe_path = candidate
        if ffmpeg_path and ffprobe_path:
            return ffmpeg_path, ffprobe_path

    raise HTTPException(
        status_code=500,
        detail=(
            "未找到 ffmpeg/ffprobe。请先安装 ffmpeg，或设置环境变量 "
            "FFMPEG_PATH 与 FFPROBE_PATH 指向可执行文件。"
        ),
    )


def _is_cancel_requested(task_id: str) -> bool:
    with TASK_LOCK:
        task = TASK_STORE.get(task_id)
        return bool(task and task.get("cancelRequested"))


def _human_bytes(value: Optional[float]) -> str:
    if not value:
        return "0B"
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f}{units[idx]}"


def _resolve_python_runner() -> str:
    configured = os.getenv("TRANSLATE_PYTHON", "").strip()
    if configured and Path(configured).exists():
        return configured
    if Path(DEFAULT_TRANSLATE_PYTHON).exists():
        return DEFAULT_TRANSLATE_PYTHON
    return shutil.which("python3") or shutil.which("python") or sys.executable


def _resolve_public_base_url() -> str:
    configured = PUBLIC_BASE_URL.strip().rstrip("/")
    if configured:
        return configured
    return "http://127.0.0.1:8000"


def _build_public_media_signature(file_id: str, expires: int) -> str:
    payload = f"{file_id}:{expires}".encode("utf-8")
    digest = hmac.new(PUBLIC_MEDIA_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return digest


def _ensure_public_media_dir() -> None:
    PUBLIC_MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_expired_public_media() -> None:
    _ensure_public_media_dir()
    now = int(time.time())
    for file_path in PUBLIC_MEDIA_DIR.iterdir():
        if not file_path.is_file():
            continue
        parts = file_path.name.split("_", 1)
        if len(parts) != 2:
            continue
        try:
            expires = int(parts[0])
        except ValueError:
            continue
        if expires >= now:
            continue
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass


def _publish_local_media_file(local_file: Path, task_id: str) -> tuple[str, Path]:
    _cleanup_expired_public_media()
    _ensure_public_media_dir()

    suffix = local_file.suffix.lower() or ".wav"
    if not suffix.startswith(".") or len(suffix) > 10:
        suffix = ".wav"

    expires = int(time.time()) + PUBLIC_MEDIA_TTL_SECONDS
    file_id = f"{task_id[:8]}-{uuid.uuid4().hex}{suffix}"
    target_name = f"{expires}_{file_id}"
    target_path = PUBLIC_MEDIA_DIR / target_name
    shutil.copy2(local_file, target_path)

    sign = _build_public_media_signature(file_id, expires)
    base = _resolve_public_base_url()
    public_url = f"{base}/api/public-media/{file_id}?expires={expires}&sign={sign}"
    return public_url, target_path


def _get_required_setting(value: str, name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"缺少配置项: {name}")
    if value.startswith("请替换为你的"):
        raise HTTPException(status_code=500, detail=f"请先在代码中填写配置项: {name}")
    return value


def _get_optional_setting(value: str) -> str:
    value = str(value or "").strip()
    if not value or value.startswith("请替换为你的"):
        return ""
    return value


def create_common_request(domain: str, version: str, protocol_type: str, method: str, uri: str) -> CommonRequest:
    request = CommonRequest()
    request.set_accept_format("json")
    request.set_domain(domain)
    request.set_version(version)
    request.set_protocol_type(protocol_type)
    request.set_method(method)
    request.set_uri_pattern(uri)
    request.add_header("Content-Type", "application/json")
    return request


def _is_youtube_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"}


def _download_youtube_media(youtube_url: str, work_dir: Path, task_id: str, prefer_audio: bool = False) -> Path:
    outtmpl = str(work_dir / "%(id)s.%(ext)s")
    ffmpeg_path, _ffprobe_path = _resolve_ffmpeg_location()

    def _progress_hook(status_dict: dict[str, Any]) -> None:
        if _is_cancel_requested(task_id):
            raise TaskCancelledError("任务已取消")
        if status_dict.get("status") != "downloading":
            return
        downloaded = float(status_dict.get("downloaded_bytes") or 0)
        total = float(status_dict.get("total_bytes") or status_dict.get("total_bytes_estimate") or 0)
        percent = int((downloaded / total) * 100) if total > 0 else 0
        mapped = 10 + int(percent * 0.2)  # 下载阶段映射到 10~30
        speed = _human_bytes(status_dict.get("speed"))
        eta = status_dict.get("eta")
        eta_text = f"{int(eta)}s" if isinstance(eta, (int, float)) else "-"
        _set_task_progress(task_id, mapped, f"下载中 {percent}% | {speed}/s | ETA {eta_text}")

    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_progress_hook],
        "ffmpeg_location": str(Path(ffmpeg_path).parent),
        # 减少 TLS/网络抖动导致的下载失败概率
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "concurrent_fragment_downloads": 1,
        "force_ipv4": True,
    }
    if prefer_audio:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}]
    else:
        ydl_opts["format"] = "bv*+ba/b"
        ydl_opts["merge_output_format"] = "mp4"
    max_attempts = 3
    prepared: Optional[Path] = None
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        if _is_cancel_requested(task_id):
            raise TaskCancelledError("任务已取消")
        try:
            if attempt > 1:
                _set_task_progress(task_id, 12, f"YouTube 下载重试中（第 {attempt}/{max_attempts} 次）")
                time.sleep(1.5 * (attempt - 1))
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=True)
                prepared = Path(ydl.prepare_filename(info))
            break
        except TaskCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # 遇到常见网络/TLS错误时继续重试，其他错误也允许有限重试
            if attempt >= max_attempts:
                break
    if prepared is None:
        raise HTTPException(status_code=500, detail=f"YouTube 下载失败: {last_exc}") from last_exc

    if prefer_audio:
        wav_path = prepared.with_suffix(".wav")
        if wav_path.exists():
            return wav_path

    if prepared.exists():
        return prepared
    mp4_path = prepared.with_suffix(".mp4")
    if mp4_path.exists():
        return mp4_path
    raise HTTPException(status_code=500, detail="YouTube 下载成功但未找到本地文件")


def _download_direct_media(source_url: str, work_dir: Path, task_id: str) -> Path:
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if not suffix or len(suffix) > 10:
        suffix = ".mp4"
    target = work_dir / f"input{suffix}"
    with urlopen(source_url, timeout=120) as response, target.open("wb") as output_file:
        total = int(response.headers.get("Content-Length", "0") or 0)
        downloaded = 0
        while True:
            if _is_cancel_requested(task_id):
                raise TaskCancelledError("任务已取消")
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output_file.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                percent = int((downloaded / total) * 100)
                mapped = 10 + int(percent * 0.2)
                _set_task_progress(task_id, mapped, f"下载中 {percent}%")
    return target


def _resolve_media_file(source_url: str, work_dir: Path, task_id: str, prefer_audio: bool = False) -> tuple[Path, str]:
    if _is_youtube_url(source_url):
        return _download_youtube_media(source_url, work_dir, task_id, prefer_audio=prefer_audio), "youtube_download"
    return _download_direct_media(source_url, work_dir, task_id), "direct_download"


def _resolve_youtube_direct_url(youtube_url: str, task_id: str, prefer_audio: bool = True) -> tuple[str, str]:
    fmt = "bestaudio/best" if prefer_audio else "b/bv*+ba"
    ytdlp_bin = shutil.which("yt-dlp")
    if ytdlp_bin:
        command = [ytdlp_bin]
    else:
        # 兜底：命令行不可用时，使用 python -m yt_dlp
        command = [_resolve_python_runner(), "-m", "yt_dlp"]

    command.extend(["-g", "-f", fmt, "--no-playlist", youtube_url])

    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        if _is_cancel_requested(task_id):
            raise TaskCancelledError("任务已取消")
        try:
            if attempt > 1:
                _set_task_progress(task_id, 12, f"yt-dlp 直链解析重试中（第 {attempt}/3 次）")
                time.sleep(attempt)

            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                raise RuntimeError(stderr or f"yt-dlp 退出码: {completed.returncode}")

            lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
            if not lines:
                raise RuntimeError("yt-dlp 未返回直链")

            direct_url = lines[0]
            ext = Path(urlparse(direct_url).path).suffix.lower().lstrip(".")
            return direct_url, (ext or ("mp3" if prefer_audio else "mp4"))
        except TaskCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise HTTPException(status_code=500, detail=f"YouTube 直链解析失败(yt-dlp -g): {last_exc}")


def _detect_media_format_from_url(source_url: str, default: str = "mp3") -> str:
    suffix = Path(urlparse(source_url).path).suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "ogg", "raw"}:
        return suffix
    return default


def _convert_to_wav16k_mono(source_file: Path, work_dir: Path, task_id: str) -> Path:
    target = work_dir / "qwen_input.wav"
    ffmpeg_path, _ffprobe_path = _resolve_ffmpeg_location()
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(source_file),
        "-ac",
        "1",
        "-ar",
        "16000",
        str(target),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.time()
    while process.poll() is None:
        if _is_cancel_requested(task_id):
            process.terminate()
            process.wait(timeout=10)
            raise TaskCancelledError("任务已取消")
        elapsed = int(time.time() - start)
        _set_task_progress(task_id, 38, f"音频转换中 ({elapsed}s)")
        time.sleep(1)
    if process.returncode != 0 or not target.exists():
        stderr = ""
        if process.stderr is not None:
            stderr = process.stderr.read().strip()
        raise HTTPException(status_code=500, detail=f"音频转换失败: {stderr or 'ffmpeg错误'}")
    return target


def _resolve_qwen_language(params: dict[str, Any]) -> Optional[str]:
    source_language = str(params.get("sourceLanguage", "")).strip().lower()
    if not source_language:
        return "zh"
    if source_language in {"cn", "zh-cn", "zh"}:
        return "zh"
    if source_language == "auto":
        return None
    return source_language


def _resolve_doubao_language(params: dict[str, Any]) -> Optional[str]:
    source_language = str(params.get("sourceLanguage", "")).strip().lower()
    if not source_language or source_language == "auto":
        return None
    mapping = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "en": "en-US",
        "ja": "ja-JP",
        "id": "id-ID",
        "es": "es-MX",
        "pt": "pt-BR",
        "de": "de-DE",
        "fr": "fr-FR",
        "ko": "ko-KR",
        "fil": "fil-PH",
        "ms": "ms-MY",
        "th": "th-TH",
        "ar": "ar-SA",
    }
    return mapping.get(source_language)


def _detect_audio_format(audio_file: Path) -> str:
    suffix = audio_file.suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "ogg", "raw"}:
        return suffix
    return "wav"


def _extract_doubao_status(headers: Any) -> str:
    return str(headers.get("X-Api-Status-Code") or headers.get("x-api-status-code") or "").strip()


def _extract_doubao_message(headers: Any) -> str:
    return str(headers.get("X-Api-Message") or headers.get("x-api-message") or "").strip()


def _build_doubao_friendly_error(status_code: str, message: str) -> str:
    if status_code == "45000006":
        return (
            "豆包无法下载音频（Invalid audio URI）。当前提供的链接对豆包服务端不可访问或已过期。"
            "请改用公网可匿名访问且稳定的音频直链（wav/mp3/ogg），"
            "不要使用短时效或需要特殊请求头的链接。"
        )
    base = f"豆包识别失败: {status_code}"
    if message:
        return f"{base} {message}"
    return base


def _run_doubao_asr_with_progress(audio_url: str, audio_file: Path, params: dict[str, Any], task_id: str) -> str:
    # 兼容两种鉴权：
    # 1) 单 API Key（推荐）: DOUBAO_API_KEY
    # 2) 旧版双参数: DOUBAO_APP_KEY + DOUBAO_ACCESS_KEY
    api_key = _get_optional_setting(DOUBAO_API_KEY)
    app_key = _get_optional_setting(DOUBAO_APP_KEY)
    access_key = _get_optional_setting(DOUBAO_ACCESS_KEY)

    if api_key:
        access_key = api_key
    if not access_key:
        access_key = _get_required_setting(DOUBAO_ACCESS_KEY, "DOUBAO_ACCESS_KEY 或 DOUBAO_API_KEY")
    if not app_key and not api_key:
        app_key = _get_required_setting(DOUBAO_APP_KEY, "DOUBAO_APP_KEY（或配置 DOUBAO_API_KEY）")

    resource_id = _get_required_setting(DOUBAO_RESOURCE_ID, "DOUBAO_RESOURCE_ID")
    request_id = uuid.uuid4().hex
    language = _resolve_doubao_language(params)
    audio_format = _detect_audio_format(audio_file)

    submit_payload: dict[str, Any] = {
        "user": {"uid": f"local-{task_id[:12]}"},
        "audio": {
            "url": audio_url,
            "format": audio_format,
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
        },
    }
    if language:
        submit_payload["audio"]["language"] = language

    headers = {
        "Content-Type": "application/json",
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    if app_key:
        headers["X-Api-App-Key"] = app_key
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    submit_req = Request(
        DOUBAO_SUBMIT_URL,
        data=json.dumps(submit_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(submit_req, timeout=60) as resp:
            status_code = _extract_doubao_status(resp.headers)
            message = _extract_doubao_message(resp.headers)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"豆包任务提交失败: {exc}") from exc

    if status_code != "20000000":
        raise HTTPException(status_code=502, detail=_build_doubao_friendly_error(status_code, message))

    _set_task_progress(task_id, 60, "豆包任务已提交，等待识别")
    start = time.time()
    while True:
        if _is_cancel_requested(task_id):
            raise TaskCancelledError("任务已取消")
        if time.time() - start > TINGWU_TASK_TIMEOUT_SECONDS:
            raise HTTPException(status_code=504, detail="豆包任务查询超时")

        query_headers = {
            "Content-Type": "application/json",
            "X-Api-Access-Key": access_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": request_id,
        }
        if app_key:
            query_headers["X-Api-App-Key"] = app_key
        if api_key:
            query_headers["Authorization"] = f"Bearer {api_key}"
        query_req = Request(
            DOUBAO_QUERY_URL,
            data=b"{}",
            headers=query_headers,
            method="POST",
        )
        try:
            with urlopen(query_req, timeout=60) as resp:
                status_code = _extract_doubao_status(resp.headers)
                message = _extract_doubao_message(resp.headers)
                body_text = resp.read().decode("utf-8", errors="ignore").strip()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"豆包任务查询失败: {exc}") from exc

        if status_code == "20000000":
            payload: dict[str, Any] = {}
            if body_text:
                try:
                    payload = json.loads(body_text)
                except json.JSONDecodeError:
                    payload = {}
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            if isinstance(result, dict):
                text = str(result.get("text", "")).strip()
                if text:
                    return text
            raise HTTPException(status_code=500, detail="豆包识别成功但未返回文本")

        if status_code in {"20000001", "20000002"}:
            elapsed = int(time.time() - start)
            dynamic_progress = min(95, 60 + elapsed // 2)
            _set_task_progress(task_id, dynamic_progress, "豆包识别中")
            time.sleep(DOUBAO_POLL_INTERVAL_SECONDS)
            continue

        if status_code == "20000003":
            raise HTTPException(status_code=422, detail="豆包识别失败: 静音音频")

        raise HTTPException(status_code=502, detail=_build_doubao_friendly_error(status_code, message))


def _prepare_stable_public_audio_url(video_url: str, task_id: str) -> tuple[str, Path]:
    _set_task_progress(task_id, 10, "正在下载媒体并准备稳定链接")
    with tempfile.TemporaryDirectory(prefix="doubao-proxy-") as tmp_dir:
        work_dir = Path(tmp_dir)
        local_media, _mode = _resolve_media_file(video_url, work_dir, task_id, prefer_audio=True)
        _set_task_progress(task_id, 25, "媒体下载完成，转换为 16k 单声道 wav")
        wav_file = _convert_to_wav16k_mono(local_media, work_dir, task_id)
        _set_task_progress(task_id, 35, "正在发布临时公网音频链接")
        public_url, published_file = _publish_local_media_file(wav_file, task_id)
    return public_url, published_file


def _run_qwen3_asr_with_progress(audio_file: Path, params: dict[str, Any], task_id: str) -> str:
    if not QWEN3_ASR_SCRIPT_PATH.exists():
        raise HTTPException(status_code=500, detail=f"未找到 Qwen3-ASR 脚本: {QWEN3_ASR_SCRIPT_PATH}")

    device_meta_path = audio_file.parent / "qwen_device_meta.json"
    command = [_resolve_python_runner(), str(QWEN3_ASR_SCRIPT_PATH), str(audio_file), "--device-meta", str(device_meta_path)]
    language = _resolve_qwen_language(params)
    if language:
        command.extend(["--language", language])

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.time()
    device_desc = ""
    while process.poll() is None:
        if _is_cancel_requested(task_id):
            process.terminate()
            process.wait(timeout=10)
            raise TaskCancelledError("任务已取消")

        if not device_desc and device_meta_path.exists():
            try:
                meta = json.loads(device_meta_path.read_text(encoding="utf-8"))
                device = str(meta.get("device", "")).strip()
                dtype = str(meta.get("dtype", "")).strip()
                fallback = bool(meta.get("fallback", False))
                if device:
                    base = f"设备: {device}{f'/{dtype}' if dtype else ''}"
                    device_desc = f"{base} (已回退)" if fallback else base
            except Exception:
                pass

        elapsed = time.time() - start
        dynamic_progress = min(95, 45 + int(elapsed / 2))
        stage = "Qwen3-ASR 转写中"
        if device_desc:
            stage += f"（{device_desc}）"
        _set_task_progress(task_id, dynamic_progress, stage)
        time.sleep(1)

    stdout = ""
    stderr = ""
    if process.stdout is not None:
        stdout = process.stdout.read().strip()
    if process.stderr is not None:
        stderr = process.stderr.read().strip()

    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Qwen3-ASR 转写失败: {stderr or '未知错误'}")
    if not stdout:
        raise HTTPException(status_code=500, detail="Qwen3-ASR 转写失败: 未返回内容")
    return stdout


def _create_tingwu_client() -> AcsClient:
    access_key_id = _get_required_setting(ALIBABA_CLOUD_ACCESS_KEY_ID, "ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = _get_required_setting(ALIBABA_CLOUD_ACCESS_KEY_SECRET, "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    credentials = AccessKeyCredential(access_key_id, access_key_secret)
    return AcsClient(region_id=TINGWU_REGION, credential=credentials)


def _build_tingwu_payload(file_url: str, params: dict[str, Any]) -> dict[str, Any]:
    app_key = _get_required_setting(ALIYUN_TINGWU_APP_KEY, "ALIYUN_TINGWU_APP_KEY")
    target_language = str(params.get("targetLanguage", "en")).strip() or "en"
    mode = str(params.get("type", "")).strip()
    body: dict[str, Any] = {
        "AppKey": app_key,
        "Input": {
            "SourceLanguage": str(params.get("sourceLanguage", "cn")).strip() or "cn",
            "TaskKey": "task" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            "FileUrl": file_url,
        },
        "Parameters": {
            "Transcription": {"DiarizationEnabled": True, "Diarization": {"SpeakerCount": 2}},
            "TranslationEnabled": True,
            "Translation": {"TargetLanguages": [target_language]},
            "LlmOutputLanguage": target_language,
            "MeetingAssistanceEnabled": True,
            "MeetingAssistance": {"Types": ["Actions", "KeyInformation"]},
            "AutoChaptersEnabled": True,
            "TextPolishEnabled": True,
        },
    }
    if mode in {"summary", "clip"}:
        body["Parameters"]["SummarizationEnabled"] = True
        body["Parameters"]["Summarization"] = {
            "Types": ["Paragraph", "Conversational", "QuestionsAnswering", "MindMap"]
        }
    return body


def _decode_response(raw: bytes) -> dict[str, Any]:
    try:
        if isinstance(raw, bytes):
            return json.loads(raw.decode("utf-8"))
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"通义听悟返回格式异常: {exc}") from exc


def _pick_by_paths(data: Any, paths: list[list[str]]) -> Any:
    for path in paths:
        cur = data
        found = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found:
            return cur
    return None


def _extract_tingwu_task_id(create_result: dict[str, Any]) -> str:
    task_id = _pick_by_paths(create_result, [["Data", "TaskId"], ["Data", "TaskID"], ["TaskId"], ["TaskID"]])
    if not task_id:
        raise HTTPException(status_code=502, detail="创建通义听悟任务成功但未返回 TaskId")
    return str(task_id)


def _extract_tingwu_status(task_result: dict[str, Any]) -> str:
    status = _pick_by_paths(task_result, [["Data", "Status"], ["Data", "TaskStatus"], ["Status"], ["TaskStatus"]])
    return str(status).upper() if status else ""


def _find_text_in_result(data: Any) -> str:
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, list):
        parts = [_find_text_in_result(item) for item in data]
        return "\n".join([p for p in parts if p]).strip()
    if isinstance(data, dict):
        preferred_keys = ["Text", "Content", "SentenceText", "DisplayText", "Summary", "Transcript", "Transcription"]
        for key in preferred_keys:
            if key in data:
                value = _find_text_in_result(data[key])
                if value:
                    return value
        values = [_find_text_in_result(v) for v in data.values()]
        values = [v for v in values if v]
        if values:
            values.sort(key=len, reverse=True)
            return values[0]
    return ""


def _poll_tingwu_result(client: AcsClient, tingwu_task_id: str, local_task_id: str) -> dict[str, Any]:
    start = time.time()
    while True:
        if _is_cancel_requested(local_task_id):
            raise TaskCancelledError("任务已取消")
        request = create_common_request(
            TINGWU_DOMAIN,
            TINGWU_VERSION,
            TINGWU_PROTOCOL,
            "GET",
            f"{TINGWU_CREATE_URI}/{tingwu_task_id}",
        )
        request.add_query_param("type", TINGWU_TYPE)
        task_result = _decode_response(client.do_action_with_exception(request))
        status = _extract_tingwu_status(task_result)

        if status in {"SUCCEEDED", "SUCCESS", "COMPLETED", "FINISHED"}:
            return task_result
        if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
            raise HTTPException(
                status_code=502,
                detail=f"通义听悟任务失败，TaskId={tingwu_task_id}，响应: {json.dumps(task_result, ensure_ascii=False)}",
            )
        if time.time() - start > TINGWU_TASK_TIMEOUT_SECONDS:
            raise HTTPException(status_code=504, detail=f"通义听悟任务超时，TaskId={tingwu_task_id}")

        elapsed = time.time() - start
        dynamic_progress = min(95, 55 + int(elapsed / TINGWU_TASK_POLL_INTERVAL_SECONDS))
        _set_task_progress(local_task_id, dynamic_progress, "通义听悟处理中")
        time.sleep(TINGWU_TASK_POLL_INTERVAL_SECONDS)


def _resolve_whisper_language(params: dict[str, Any]) -> Optional[str]:
    source_language = str(params.get("sourceLanguage", "")).strip().lower()
    if not source_language:
        return WHISPER_DEFAULT_LANGUAGE
    if source_language in {"cn", "zh-cn", "zh"}:
        return "zh"
    if source_language == "auto":
        return None
    return source_language


def _extract_whisper_text(whisper_result: dict[str, Any]) -> str:
    segments = whisper_result.get("segments")
    if isinstance(segments, list) and segments:
        return "".join(str(seg.get("text", "")) for seg in segments).strip()
    return str(whisper_result.get("text", "")).strip()


def _format_preview_text(whisper_result: dict[str, Any]) -> str:
    extracted_text = _extract_whisper_text(whisper_result)
    return extracted_text or "(空结果)"


def _format_tingwu_preview_text(task_result: dict[str, Any]) -> str:
    extracted_text = _find_text_in_result(task_result)
    return extracted_text or "(空结果)"


def _create_local_task() -> str:
    task_id = uuid.uuid4().hex
    now = time.time()
    with TASK_LOCK:
        TASK_STORE[task_id] = {
            "status": "PENDING",
            "progress": 0,
            "stage": "任务已创建",
            "cancelRequested": False,
            "code": 202,
            "processTime": 0,
            "content": "",
            "detail": None,
            "createdAt": now,
            "updatedAt": now,
        }
    return task_id


def _update_local_task(task_id: str, **kwargs: Any) -> None:
    with TASK_LOCK:
        task = TASK_STORE.get(task_id)
        if not task:
            return
        task.update(kwargs)
        task["updatedAt"] = time.time()


def _set_task_progress(task_id: str, progress: int, stage: str) -> None:
    _update_local_task(task_id, progress=max(0, min(100, progress)), stage=stage)


def _run_whisper_transcribe_with_progress(local_file: Path, params: dict[str, Any], task_id: str) -> dict[str, Any]:
    if not WHISPER_SCRIPT_PATH.exists():
        raise HTTPException(status_code=500, detail=f"未找到 Whisper 脚本: {WHISPER_SCRIPT_PATH}")

    command = [_resolve_python_runner(), str(WHISPER_SCRIPT_PATH), str(local_file), "--format", WHISPER_FORMAT]
    language = _resolve_whisper_language(params)
    if language:
        command.extend(["--language", language])

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_lines: list[str] = []
    progress_re = re.compile(r"\[progress\]\s+(\d+)%")
    duration_re = re.compile(r"\(([\d.]+)s\s*/\s*([\d.]+)s\)")

    # 读取 whisper 的进度日志并映射到任务进度 35~95
    if process.stderr is not None:
        for line in process.stderr:
            if _is_cancel_requested(task_id):
                process.terminate()
                process.wait(timeout=10)
                raise TaskCancelledError("任务已取消")
            line = line.strip()
            if line:
                stderr_lines.append(line)
            match = progress_re.search(line)
            if match:
                whisper_percent = int(match.group(1))
                mapped = 35 + int(whisper_percent * 0.6)
                duration_match = duration_re.search(line)
                if duration_match:
                    processed = float(duration_match.group(1))
                    total = float(duration_match.group(2))
                    _set_task_progress(
                        task_id,
                        mapped,
                        f"Whisper 转写中 {whisper_percent}% ({processed:.1f}s/{total:.1f}s)",
                    )
                else:
                    _set_task_progress(task_id, mapped, f"Whisper 转写中 {whisper_percent}%")

    stdout = ""
    if process.stdout is not None:
        stdout = process.stdout.read().strip()

    return_code = process.wait()
    if return_code != 0:
        stderr_text = "\n".join(stderr_lines).strip()
        raise HTTPException(status_code=500, detail=f"Whisper 转写失败: {stderr_text or '未知错误'}")

    if not stdout:
        raise HTTPException(status_code=500, detail="Whisper 转写失败: 未返回内容")

    if WHISPER_FORMAT == "json":
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"Whisper JSON 解析失败: {exc}") from exc

    return {"text": stdout, "segments": []}


def _run_generate_task(task_id: str, video_url: str, params: dict[str, Any]) -> None:
    start_time = time.time()
    engine = str(params.get("engine", "local")).strip().lower() or "local"
    _update_local_task(task_id, status="RUNNING", progress=5, stage="任务开始处理")
    try:
        if _is_cancel_requested(task_id):
            raise TaskCancelledError("任务已取消")
        if engine in {"tingwu", "doubao_asr"}:
            if engine == "doubao_asr" and _is_youtube_url(video_url):
                media_url, media_file = _prepare_stable_public_audio_url(video_url, task_id)
            else:
                if _is_youtube_url(video_url):
                    _set_task_progress(task_id, 10, "正在用 yt-dlp -g 解析 YouTube 直链")
                    media_url, media_ext = _resolve_youtube_direct_url(video_url, task_id, prefer_audio=True)
                else:
                    _set_task_progress(task_id, 10, "使用输入链接直连调用")
                    media_url = video_url
                    media_ext = _detect_media_format_from_url(video_url)

            if engine == "tingwu":
                _set_task_progress(task_id, 35, "链接就绪，创建听悟任务")
                client = _create_tingwu_client()
                payload = _build_tingwu_payload(media_url, params)
                create_req = create_common_request(
                    TINGWU_DOMAIN,
                    TINGWU_VERSION,
                    TINGWU_PROTOCOL,
                    "PUT",
                    TINGWU_CREATE_URI,
                )
                create_req.add_query_param("type", TINGWU_TYPE)
                create_req.set_content(json.dumps(payload).encode("utf-8"))
                create_result = _decode_response(client.do_action_with_exception(create_req))
                tingwu_task_id = _extract_tingwu_task_id(create_result)
                _set_task_progress(task_id, 55, f"听悟任务已创建: {tingwu_task_id}")
                task_result = _poll_tingwu_result(client, tingwu_task_id, task_id)
                preview = _format_tingwu_preview_text(task_result)
            else:
                _set_task_progress(task_id, 45, "链接就绪，提交豆包任务")
                if _is_youtube_url(video_url):
                    doubao_text = _run_doubao_asr_with_progress(media_url, media_file, params, task_id)
                else:
                    media_hint = Path(urlparse(media_url).path or f"input.{media_ext}")
                    doubao_text = _run_doubao_asr_with_progress(media_url, media_hint, params, task_id)
                preview = doubao_text.strip() or "(空结果)"
        else:
            with tempfile.TemporaryDirectory(prefix="whisper-job-") as tmp_dir:
                _set_task_progress(task_id, 10, "正在下载媒体文件")
                local_media, _media_mode = _resolve_media_file(video_url, Path(tmp_dir), task_id, prefer_audio=False)
                if engine == "qwen3_asr":
                    _set_task_progress(task_id, 30, "媒体下载完成，转换音频")
                    qwen_audio = _convert_to_wav16k_mono(local_media, Path(tmp_dir), task_id)
                    _set_task_progress(task_id, 45, "音频转换完成，Qwen3-ASR 转写中")
                    qwen_text = _run_qwen3_asr_with_progress(qwen_audio, params, task_id)
                    preview = qwen_text.strip() or "(空结果)"
                else:
                    _set_task_progress(task_id, 30, "媒体下载完成，准备转写")
                    whisper_result = _run_whisper_transcribe_with_progress(local_media, params, task_id)
                    preview = _format_preview_text(whisper_result)
        _set_task_progress(task_id, 100, "处理完成")
        _update_local_task(
            task_id,
            status="SUCCESS",
            code=200,
            processTime=round(time.time() - start_time, 2),
            content=preview,
            detail=None,
        )
    except TaskCancelledError as exc:
        _update_local_task(
            task_id,
            status="CANCELED",
            progress=100,
            stage="任务已取消",
            code=499,
            processTime=round(time.time() - start_time, 2),
            detail=str(exc),
        )
    except HTTPException as exc:
        _update_local_task(
            task_id,
            status="FAILED",
            progress=100,
            stage="处理失败",
            code=exc.status_code,
            processTime=round(time.time() - start_time, 2),
            detail=str(exc.detail),
        )
    except Exception as exc:  # noqa: BLE001
        _update_local_task(
            task_id,
            status="FAILED",
            progress=100,
            stage="处理失败",
            code=500,
            processTime=round(time.time() - start_time, 2),
            detail=f"后端处理失败: {exc}",
        )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/public-media/{file_id}")
def serve_public_media(file_id: str, expires: int, sign: str):
    expected = _build_public_media_signature(file_id, expires)
    if not hmac.compare_digest(expected, sign):
        raise HTTPException(status_code=403, detail="无效签名")
    now = int(time.time())
    if expires < now:
        raise HTTPException(status_code=410, detail="链接已过期")

    file_path = PUBLIC_MEDIA_DIR / f"{expires}_{file_id}"
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type = "audio/wav"
    suffix = file_path.suffix.lower()
    if suffix == ".mp3":
        media_type = "audio/mpeg"
    elif suffix == ".ogg":
        media_type = "audio/ogg"
    return FileResponse(path=str(file_path), media_type=media_type, filename=file_path.name)


@app.post("/api/generate", response_model=GenerateStartResponse)
def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    if not str(req.video_url).strip():
        raise HTTPException(status_code=400, detail="video_url 不能为空")

    local_task_id = _create_local_task()
    background_tasks.add_task(_run_generate_task, local_task_id, req.video_url, req.params)
    return GenerateStartResponse(
        code=202,
        taskId=local_task_id,
        status="PENDING",
        message="任务已创建，正在后台处理中",
    )


@app.post("/api/generate/{task_id}/cancel")
def cancel_generate_task(task_id: str):
    with TASK_LOCK:
        task = TASK_STORE.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        status = str(task.get("status", "")).upper()
        if status in {"SUCCESS", "FAILED", "CANCELED"}:
            return {"code": 200, "taskId": task_id, "status": status, "message": "任务已结束，无需取消"}
        task["cancelRequested"] = True
        task["stage"] = "取消中..."
        task["updatedAt"] = time.time()
    return {"code": 200, "taskId": task_id, "status": "CANCELING", "message": "取消请求已发送"}


@app.get("/api/generate/{task_id}", response_model=GenerateTaskStatusResponse)
def get_generate_status(task_id: str):
    with TASK_LOCK:
        task = TASK_STORE.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        snapshot = dict(task)

    return GenerateTaskStatusResponse(
        code=int(snapshot.get("code", 200)),
        taskId=task_id,
        status=str(snapshot.get("status", "PENDING")),
        progress=int(snapshot.get("progress", 0)),
        stage=str(snapshot.get("stage", "")),
        processTime=float(snapshot.get("processTime", 0)),
        content=str(snapshot.get("content", "")),
        detail=snapshot.get("detail"),
    )
