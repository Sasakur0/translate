import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import urlopen

import oss2
from aliyunsdkcore.auth.credentials import AccessKeyCredential
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

TINGWU_DOMAIN = "tingwu.cn-beijing.aliyuncs.com"
TINGWU_VERSION = "2023-09-30"
TINGWU_REGION = "cn-beijing"
TINGWU_PROTOCOL = "https"
TINGWU_TYPE = "offline"
TINGWU_CREATE_URI = "/openapi/tingwu/v2/tasks"
TINGWU_TASK_TIMEOUT_SECONDS = int(os.getenv("TINGWU_TASK_TIMEOUT_SECONDS", "1800"))
TINGWU_TASK_POLL_INTERVAL_SECONDS = float(os.getenv("TINGWU_TASK_POLL_INTERVAL_SECONDS", "5"))

# 千问听悟 + OSS 配置（选择“千问听悟”模式时生效）
ALIBABA_CLOUD_ACCESS_KEY_ID = "请替换为你的AccessKeyId"
ALIBABA_CLOUD_ACCESS_KEY_SECRET = "请替换为你的AccessKeySecret"
ALIYUN_TINGWU_APP_KEY = "请替换为你的TingwuAppKey"
ALIYUN_OSS_ENDPOINT = "https://oss-cn-beijing.aliyuncs.com"
ALIYUN_OSS_BUCKET = "请替换为你的Bucket"
ALIYUN_OSS_OBJECT_PREFIX = "tingwu-input"
ALIYUN_OSS_SIGN_EXPIRE_SECONDS = 7200

TASK_STORE: dict[str, dict[str, Any]] = {}
TASK_LOCK = threading.Lock()


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


def _get_required_setting(value: str, name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"缺少配置项: {name}")
    if value.startswith("请替换为你的"):
        raise HTTPException(status_code=500, detail=f"请先在代码中填写配置项: {name}")
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


def _download_youtube_media(youtube_url: str, work_dir: Path, task_id: str) -> Path:
    outtmpl = str(work_dir / "%(id)s.%(ext)s")

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

    ydl_opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_progress_hook],
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)
            prepared = Path(ydl.prepare_filename(info))
    except TaskCancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"YouTube 下载失败: {exc}") from exc

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


def _resolve_media_file(source_url: str, work_dir: Path, task_id: str) -> tuple[Path, str]:
    if _is_youtube_url(source_url):
        return _download_youtube_media(source_url, work_dir, task_id), "youtube_download"
    return _download_direct_media(source_url, work_dir, task_id), "direct_download"


def _upload_to_oss_and_sign(local_file: Path) -> str:
    access_key_id = _get_required_setting(ALIBABA_CLOUD_ACCESS_KEY_ID, "ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = _get_required_setting(ALIBABA_CLOUD_ACCESS_KEY_SECRET, "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    endpoint = _get_required_setting(ALIYUN_OSS_ENDPOINT, "ALIYUN_OSS_ENDPOINT")
    bucket_name = _get_required_setting(ALIYUN_OSS_BUCKET, "ALIYUN_OSS_BUCKET")
    prefix = _get_required_setting(ALIYUN_OSS_OBJECT_PREFIX, "ALIYUN_OSS_OBJECT_PREFIX")

    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    suffix = (local_file.suffix or ".mp4").lower()
    if len(suffix) > 10 or not suffix.startswith("."):
        suffix = ".mp4"
    safe_name = f"{int(time.time())}-{uuid.uuid4().hex}{suffix}"
    object_key = f"{prefix.rstrip('/')}/{safe_name}"
    result = bucket.put_object_from_file(object_key, str(local_file))
    if result.status != 200:
        raise HTTPException(status_code=502, detail=f"OSS 上传失败，状态码: {result.status}")
    return bucket.sign_url("GET", object_key, ALIYUN_OSS_SIGN_EXPIRE_SECONDS)


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

    command = [sys.executable, str(WHISPER_SCRIPT_PATH), str(local_file), "--format", WHISPER_FORMAT]
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
        with tempfile.TemporaryDirectory(prefix="whisper-job-") as tmp_dir:
            _set_task_progress(task_id, 10, "正在下载媒体文件")
            local_media, _media_mode = _resolve_media_file(video_url, Path(tmp_dir), task_id)
            if engine == "tingwu":
                _set_task_progress(task_id, 30, "媒体下载完成，上传 OSS")
                signed_url = _upload_to_oss_and_sign(local_media)
                _set_task_progress(task_id, 45, "已上传 OSS，创建听悟任务")
                client = _create_tingwu_client()
                payload = _build_tingwu_payload(signed_url, params)
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
