"""共享媒体处理工具 — 下载 / FFmpeg / Whisper / 音视频分析。

提供统一的媒体处理基础设施，供 agent_tools / video_analyzer / engine 共用。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from utils.process_compat import macos_subprocess_kwargs, resolve_executable_for_spawn

_log = logging.getLogger("yukiko.media")

# ---------------------------------------------------------------------------
# FFmpeg 工具
# ---------------------------------------------------------------------------

_ffmpeg_bin: str | None = None
_ffprobe_bin: str | None = None


def _find_bin(name: str) -> str | None:
    """查找可执行文件路径。"""
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        Path(os.environ.get("FFMPEG_HOME", "")) / name,
        Path(os.environ.get("FFMPEG_HOME", "")) / "bin" / name,
    ):
        if candidate.is_file():
            return str(candidate)

    lower_name = name.lower()
    if lower_name in {"ffmpeg", "ffmpeg.exe"}:
        try:
            import imageio_ffmpeg  # type: ignore

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and Path(bundled).is_file():
                return str(Path(bundled))
        except Exception:
            pass

    if lower_name in {"ffprobe", "ffprobe.exe"}:
        ffmpeg_path = _find_bin("ffmpeg")
        if ffmpeg_path:
            ffmpeg_exe = Path(ffmpeg_path)
            sibling = ffmpeg_exe.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            if sibling.is_file():
                return str(sibling)
    return None


def get_ffmpeg() -> str | None:
    global _ffmpeg_bin
    if _ffmpeg_bin is None:
        _ffmpeg_bin = _find_bin("ffmpeg") or _find_bin("ffmpeg.exe") or ""
    return _ffmpeg_bin or None


def get_ffprobe() -> str | None:
    global _ffprobe_bin
    if _ffprobe_bin is None:
        _ffprobe_bin = _find_bin("ffprobe") or _find_bin("ffprobe.exe") or ""
    return _ffprobe_bin or None


async def run_ffmpeg(
    args: list[str],
    *,
    timeout: float = 60.0,
    cwd: str | Path | None = None,
) -> tuple[bool, str]:
    """异步执行 ffmpeg 命令，返回 (success, stderr_output)。"""
    ffmpeg = get_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found"
    ffmpeg = resolve_executable_for_spawn(ffmpeg)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "warning"] + args
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            **macos_subprocess_kwargs(),
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        ok = proc.returncode == 0
        return ok, stderr.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        return False, "ffmpeg timeout"
    except Exception as exc:
        return False, f"ffmpeg error: {exc}"


async def run_ffprobe_json(
    file_path: str | Path,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """用 ffprobe 获取媒体文件的 JSON 元数据。"""
    ffprobe = get_ffprobe()
    if not ffprobe:
        return {}
    ffprobe = resolve_executable_for_spawn(ffprobe)
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(file_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **macos_subprocess_kwargs(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            return {}
        import json
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception:
        return {}



async def extract_audio(
    video_path: str | Path,
    output_path: str | Path | None = None,
    *,
    sample_rate: int = 16000,
    mono: bool = True,
    timeout: float = 60.0,
) -> str | None:
    """从视频/音频文件中提取 WAV 音频（Whisper 友好格式）。

    返回输出文件路径，失败返回 None。
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        return None
    if output_path is None:
        output_path = video_path.with_suffix(".wav")
    output_path = Path(output_path)
    args = [
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(sample_rate),
    ]
    if mono:
        args.extend(["-ac", "1"])
    args.append(str(output_path))
    ok, err = await run_ffmpeg(args, timeout=timeout)
    if ok and output_path.is_file():
        return str(output_path)
    _log.warning("extract_audio_failed | %s | %s", video_path.name, err)
    return None


async def extract_keyframes(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    max_frames: int = 8,
    interval_seconds: float = 0,
    timeout: float = 60.0,
) -> list[str]:
    """从视频中提取关键帧图片。

    如果 interval_seconds > 0，按固定间隔提取；否则使用场景检测。
    返回提取的图片路径列表。
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not video_path.is_file():
        return []

    pattern = str(output_dir / "frame_%04d.jpg")

    if interval_seconds > 0:
        args = [
            "-i", str(video_path),
            "-vf", f"fps=1/{interval_seconds}",
            "-frames:v", str(max_frames),
            "-q:v", "3",
            pattern,
        ]
    else:
        # 场景检测 + 均匀采样兜底
        args = [
            "-i", str(video_path),
            "-vf", f"select='gt(scene,0.3)',setpts=N/FRAME_RATE/TB",
            "-frames:v", str(max_frames),
            "-vsync", "vfr",
            "-q:v", "3",
            pattern,
        ]

    ok, err = await run_ffmpeg(args, timeout=timeout)
    frames = sorted(output_dir.glob("frame_*.jpg"))

    # 场景检测可能提取太少，回退到均匀采样
    if len(frames) < 2 and interval_seconds <= 0:
        probe = await run_ffprobe_json(video_path)
        duration = _get_duration(probe)
        if duration > 0:
            step = max(1.0, duration / max_frames)
            args2 = [
                "-i", str(video_path),
                "-vf", f"fps=1/{step}",
                "-frames:v", str(max_frames),
                "-q:v", "3",
                pattern,
            ]
            await run_ffmpeg(args2, timeout=timeout)
            frames = sorted(output_dir.glob("frame_*.jpg"))

    return [str(f) for f in frames[:max_frames]]


def _get_duration(probe: dict[str, Any]) -> float:
    """从 ffprobe JSON 中提取时长（秒）。"""
    fmt = probe.get("format", {})
    dur = fmt.get("duration")
    if dur:
        try:
            return float(dur)
        except (ValueError, TypeError):
            pass
    for stream in probe.get("streams", []):
        dur = stream.get("duration")
        if dur:
            try:
                return float(dur)
            except (ValueError, TypeError):
                pass
    return 0.0


def get_media_info(probe: dict[str, Any]) -> dict[str, Any]:
    """从 ffprobe JSON 中提取常用媒体信息。"""
    fmt = probe.get("format", {})
    info: dict[str, Any] = {
        "duration": _get_duration(probe),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "format_name": fmt.get("format_name", ""),
        "has_audio": False,
        "has_video": False,
    }
    for stream in probe.get("streams", []):
        codec_type = stream.get("codec_type", "")
        if codec_type == "video":
            info["has_video"] = True
            info["width"] = int(stream.get("width", 0) or 0)
            info["height"] = int(stream.get("height", 0) or 0)
            info["video_codec"] = stream.get("codec_name", "")
        elif codec_type == "audio":
            info["has_audio"] = True
            info["audio_codec"] = stream.get("codec_name", "")
            info["sample_rate"] = int(stream.get("sample_rate", 0) or 0)
    return info


# ---------------------------------------------------------------------------
# Whisper 语音转文字
# ---------------------------------------------------------------------------

_whisper_model: Any = None
_whisper_lock = asyncio.Lock()


async def transcribe_audio_enhanced(
    audio_path: str | Path,
    *,
    model_size: str = "base",
    language: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """魔改版多轮语音识别系统。
    
    采用多次解析和加权计分的机制：
    - Pass 1: 零温度带束搜索 (求稳)
    - Pass 2: 温度回退降级 + 无前置上下文干扰 (防幻觉)
    - Pass 3: 专属二次元/网络提词引导 (懂梗)
    最终按平均 logprob 和非语音概率计分，选取最优解，并自带分段排版。
    """
    audio_path = Path(audio_path)
    if not audio_path.is_file():
        return {"text": "", "score": -999, "pass": "none"}

    try:
        import whisper  # type: ignore
    except ImportError:
        _log.warning("whisper not installed, run: pip install openai-whisper")
        return {"text": "", "score": -999, "pass": "error"}

    global _whisper_model
    async with _whisper_lock:
        if _whisper_model is None:
            _log.info("whisper_loading_model | size=%s", model_size)
            loop = asyncio.get_running_loop()
            _whisper_model = await loop.run_in_executor(
                None, lambda: whisper.load_model(model_size)
            )
            _log.info("whisper_model_loaded | size=%s", model_size)

    model = _whisper_model
    loop = asyncio.get_running_loop()

    def _score_result(res: dict[str, Any]) -> float:
        segments = res.get("segments", [])
        if not segments:
            return -999.0
        avg_logprob = sum(s.get("avg_logprob", -1.0) for s in segments) / len(segments)
        avg_no_speech = sum(s.get("no_speech_prob", 1.0) for s in segments) / len(segments)
        
        # 分数计算公式 (logprob 越接近0越好，no_speech越小越好)
        return float(avg_logprob * 0.7 - avg_no_speech * 0.3)

    def _format_segments(res: dict[str, Any]) -> str:
        texts = []
        for s in res.get("segments", []):
            start = f"{s.get('start', 0):.1f}s"
            end = f"{s.get('end', 0):.1f}s"
            txt = s.get("text", "").strip()
            if txt:
                texts.append(f"[{start} - {end}] {txt}")
        if not texts:
            return res.get("text", "").strip()
        return "\n".join(texts)

    def _do_transcribes() -> list[dict[str, Any]]:
        results = []
        
        # =======================================================
        # Pass 1: 零温度带束搜索 (标准最优路径) 
        # =======================================================
        try:
            r1 = model.transcribe(
                str(audio_path), language=language, fp16=False,
                temperature=0.0, beam_size=5
            )
            r1["_pass"] = "Pass-1-BeamSearch"
            r1["_score"] = _score_result(r1)
            results.append(r1)
        except Exception as e:
            _log.warning("transcribe pass 1 error: %s", e)

        # 优化短路：如果第一次效果极好，直接返回不跑后面的了，省点算力
        if results and results[0].get("_score", -999.0) > -0.3:
            return results

        # =======================================================
        # Pass 2: 防止幻觉和复读机的回退模式
        # =======================================================
        try:
            r2 = model.transcribe(
                str(audio_path), language=language, fp16=False,
                temperature=(0.2, 0.4, 0.6), # 允许 Whisper 自动回退寻找稳定态
                condition_on_previous_text=False # 切断上下文关联，防止复读机幻觉
            )
            r2["_pass"] = "Pass-2-NoContext"
            r2["_score"] = _score_result(r2)
            results.append(r2)
        except Exception as e:
            pass

        # =======================================================
        # Pass 3: 二次元/日常梗的 Prompt 增强引导
        # =======================================================
        try:
            r3 = model.transcribe(
                str(audio_path), language=language, fp16=False,
                temperature=0.0,
                initial_prompt="这是一段日常群聊语音，包含二次元、原神、游戏、技术梗等网络常用语。"
            )
            r3["_pass"] = "Pass-3-PromptGuided"
            r3["_score"] = _score_result(r3)
            results.append(r3)
        except Exception as e:
            pass

        return results

    try:
        all_results = await asyncio.wait_for(
            loop.run_in_executor(None, _do_transcribes),
            timeout=timeout,
        )
        if not all_results:
            return {"text": "", "score": -999, "pass": "error"}

        # 排序：分数从高到低
        all_results.sort(key=lambda x: x.get("_score", -999.0), reverse=True)
        best = all_results[0]
        
        best_text = best.get("text", "").strip()
        formatted_text = _format_segments(best)
        score = best.get("_score", -999.0)
        pass_name = best.get("_pass", "unknown")

        _log.info("whisper_enhanced_transcribed | file=%s | chars=%d | best_pass=%s | score=%.2f", 
                  audio_path.name, len(best_text), pass_name, score)
        
        return {
            "text": best_text,
            "formatted_text": formatted_text,
            "score": score,
            "pass": pass_name,
            "raw_segments": best.get("segments", [])
        }
        
    except asyncio.TimeoutError:
        _log.warning("whisper_timeout | file=%s", audio_path.name)
        return {"text": "", "score": -999, "pass": "timeout"}
    except Exception as exc:
        _log.warning("whisper_error | file=%s | %s", audio_path.name, exc)
        return {"text": "", "score": -999, "pass": "error"}

async def transcribe_audio(
    audio_path: str | Path,
    *,
    model_size: str = "base",
    language: str | None = None,
    timeout: float = 120.0,
) -> str:
    """（向后兼容层）使用 OpenAI Whisper 本地模型转录音频为文字。"""
    res = await transcribe_audio_enhanced(audio_path, model_size=model_size, language=language, timeout=timeout)
    return res.get("text", "")


# ---------------------------------------------------------------------------
# 通用下载
# ---------------------------------------------------------------------------


async def download_file(
    url: str,
    output_path: str | Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_size_mb: float = 100.0,
) -> bool:
    """异步下载文件到指定路径。"""
    import httpx

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_size_mb * 1024 * 1024)

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, verify=True
        ) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                total = 0
                with open(output_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(8192):
                        total += len(chunk)
                        if total > max_bytes:
                            _log.warning("download_size_exceeded | url=%s | max=%sMB", url[:80], max_size_mb)
                            output_path.unlink(missing_ok=True)
                            return False
                        f.write(chunk)
        return output_path.is_file() and output_path.stat().st_size > 0
    except Exception as exc:
        _log.warning("download_failed | url=%s | %s", url[:80], exc)
        output_path.unlink(missing_ok=True)
        return False


def file_hash(path: str | Path, algo: str = "md5") -> str:
    """计算文件哈希。"""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(name: str, max_len: int = 80) -> str:
    """将任意字符串转为安全文件名。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"_+", "_", name).strip("_. ")
    return name[:max_len] if name else "unnamed"
