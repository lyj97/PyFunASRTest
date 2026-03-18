"""
FastAPI 路由（全自动流水线 + 说话人更正重触发版）：

  POST /api/tasks                    — 上传音频，创建任务，启动 ASR → 自动接续 LLM
  GET  /api/tasks                    — 历史任务列表
  GET  /api/tasks/{task_id}          — 查询单个任务（含完整结果）
  GET  /api/tasks/{task_id}/stream   — SSE 订阅任务进度（支持断线重连）
  POST /api/tasks/{task_id}/analyze  — 携带说话人更正重新触发 LLM 分析（幂等）
  GET  /api/health                   — 服务健康检查

流程：
  1. POST /tasks      → 创建任务，启动 ASR Worker
  2. ASR 完成后       → 自动触发 LLM Worker（无需前端干预）
  3. 订阅 /stream     → 同时接收 ASR 进度/结果 和 LLM 流式内容
  4. 用户可随时修改说话人，POST /analyze 重新触发 LLM（清空旧结果）
"""
import asyncio
import json
import logging
import queue
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

import app.models as _models
import app.database as db
from app import transcriber
from app.config import ALLOWED_EXTENSIONS, LLM_ENABLED

logger = logging.getLogger(__name__)

router = APIRouter()


# ── SSE 工具 ──────────────────────────────────────

def _sse(data: dict) -> str:
    """将字典序列化为 SSE 格式字符串。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 在途任务的实时队列注册表 ─────────────────────
# task_id -> SimpleQueue（仅当前活跃 Worker 存活时有效）
_live_queues: dict[str, queue.SimpleQueue] = {}


# ── ASR Worker ────────────────────────────────────

def _run_asr_worker(task_id: str, audio_path: str, suffix: str,
                    live_q: queue.SimpleQueue,
                    loop: asyncio.AbstractEventLoop) -> None:
    """
    ASR 识别：进度写库 + 推送实时队列。
    完成后自动在同一 live_q 上接续启动 LLM Worker，无需前端手动触发。
    """
    asr_ok = False
    try:
        def _progress_cb(pct: int):
            db.update_progress(task_id, pct)
            live_q.put(("progress", pct))

        result = transcriber.run(audio_path, suffix, progress_cb=_progress_cb)
        db.update_asr_result(task_id, result)   # status → asr_done（瞬态）
        live_q.put(("result", result))
        asr_ok = True
    except Exception as e:
        logger.exception("ASR 识别失败 task=%s", task_id)
        db.update_task_error(task_id, str(e))
        live_q.put(("error", str(e)))
    finally:
        if not asr_ok:
            # ASR 失败，终止整个流水线
            live_q.put(("__done__", None))
            _live_queues.pop(task_id, None)

    if not asr_ok:
        return

    # ASR 成功 → 同一 live_q 上自动接续 LLM（不中断 SSE 连接）
    if not LLM_ENABLED:
        # LLM 未启用，直接关闭流
        live_q.put(("__done__", None))
        _live_queues.pop(task_id, None)
        return

    logger.info("ASR 完成，自动接续 LLM task=%s", task_id)
    _run_llm_worker(task_id, corrections={}, live_q=live_q)


# ── LLM Worker ────────────────────────────────────

def _run_llm_worker(task_id: str, corrections: dict,
                    live_q: queue.SimpleQueue) -> None:
    """
    只做 LLM 分析：从库中读取 ASR 结果，携带说话人更正信息。
    """
    try:
        task = db.get_task(task_id)
        asr_result = task.get("asr_result") if task else None

        if not asr_result or not asr_result.get("text"):
            db.update_llm_done(task_id)
            live_q.put(("__done__", None))
            return

        from app.llm import LLMAnalyzer
        analyzer = LLMAnalyzer()

        live_q.put(("llm_stage", "analyzing"))
        for chunk in analyzer.analyze_interview_stream(
            asr_result["text"],
            asr_result.get("segments", []),
            corrections=corrections or None,
        ):
            if isinstance(chunk, str):
                db.append_llm_chunk(task_id, chunk)
                live_q.put(("llm_chunk", chunk))
            else:
                if "error" in chunk:
                    db.update_llm_done(task_id, error=chunk["error"])
                    live_q.put(("llm_done", {"error": chunk["error"]}))
                else:
                    db.update_llm_done(task_id)
                    live_q.put(("llm_done", {}))

    except Exception as e:
        logger.exception("LLM 分析失败 task=%s", task_id)
        err = str(e)
        db.update_llm_done(task_id, error=err)
        live_q.put(("llm_done", {"error": err}))
    finally:
        live_q.put(("__done__", None))
        _live_queues.pop(task_id, None)


# ── 路由 ──────────────────────────────────────────

@router.post("/tasks", status_code=202)
async def create_task(file: UploadFile = File(...)):
    """
    上传音频，创建任务，启动 ASR。

    返回：{ task_id, filename, status }
    """
    if _models.asr_model is None or _models.diar_pipeline is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式：{suffix}，支持：{', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # 音频持久保存（TTL 到期才删）
    audio_dir = db.get_audio_dir()
    tmp_name = __import__("uuid").uuid4().hex
    audio_path = str(audio_dir / f"{tmp_name}{suffix}")
    with open(audio_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    task_id = db.create_task(file.filename, audio_path)

    # 创建实时队列，启动 ASR 后台线程
    live_q: queue.SimpleQueue = queue.SimpleQueue()
    _live_queues[task_id] = live_q

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_asr_worker, task_id, audio_path, suffix, live_q, loop)

    logger.info("创建任务 task_id=%s file=%s", task_id, file.filename)
    return {"task_id": task_id, "filename": file.filename, "status": "pending"}


@router.post("/tasks/{task_id}/analyze", status_code=202)
async def analyze_task(
    task_id: str,
    corrections: dict = Body(default={}),
):
    """
    用户修改说话人后重新触发 LLM 分析（幂等，任意时刻可调用）。

    - 若当前有 LLM Worker 在跑，先通过替换队列使其孤立（旧队列自行退出）
    - 清空旧 llm_chunks，重新开始分析
    - 前端调用后重新订阅 /stream 即可接收新结果

    Body（可选）：
    {
      "speaker_roles":      { "0": "interviewer", "1": "candidate" },
      "speaker_merges":     { "2": 1 },
      "segment_overrides":  { "5": 0 },
      "user_corrected":     true
    }
    """
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 仅 ASR 完成后才允许（含已完成/出错的任务，支持重分析）
    if task["status"] not in ("asr_done", "llm_running", "done", "error"):
        raise HTTPException(status_code=409, detail=f"ASR 尚未完成（当前状态：{task['status']}）")
    if not task["asr_result"]:
        raise HTTPException(status_code=409, detail="ASR 结果为空，无法分析")
    if not LLM_ENABLED:
        raise HTTPException(status_code=503, detail="LLM_ENABLED=false，服务未启用 LLM")

    # 保存用户更正（有则覆盖，无则保持旧值）
    if corrections:
        db.save_speaker_corrections(task_id, corrections)

    # 重置 LLM 输出，状态切换为 llm_running
    db.reset_llm_for_rerun(task_id)

    # 创建新队列（旧 Worker 若还在跑，写入孤立队列后自行退出，不影响新流）
    live_q: queue.SimpleQueue = queue.SimpleQueue()
    _live_queues[task_id] = live_q

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_llm_worker, task_id, corrections, live_q)

    logger.info("重触发 LLM 分析 task_id=%s user_corrected=%s", task_id, bool(corrections))
    return {"task_id": task_id, "status": "llm_running"}


@router.get("/tasks/{task_id}/stream")
async def task_stream(task_id: str):
    """
    SSE 订阅任务进度（支持断线重连）。

    连接时先推送数据库中的当前最新状态，再切换到实时队列。
    - ASR 阶段：推进度/结果后关闭（asr_done 时等用户触发 /analyze）
    - LLM 阶段：推已有 llm_chunks 快照，再接管 live_q 实时流
    """
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def _generate():
        # ── 推送当前最新状态快照 ──────────────────────
        if task["progress"] > 0:
            yield _sse({"progress": task["progress"]})

        if task["asr_result"]:
            yield _sse({"progress": 100, "result": task["asr_result"]})

        # ASR 阶段失败
        if task["status"] == "error" and task["error_msg"] and not task["asr_result"]:
            yield _sse({"error": task["error_msg"]})
            return

        # ASR 完成但 LLM 尚未启动（极短暂的中间态，直接结束本次快照，等 Worker 接续）
        if task["status"] == "asr_done":
            return

        # LLM 已有内容（重连恢复）
        if task["llm_chunks"]:
            yield _sse({"llm_stage": "analyzing"})
            yield _sse({"llm_chunk": task["llm_chunks"]})

        # LLM 已完成
        if task["llm_done"]:
            if task["status"] == "error" and task["error_msg"]:
                yield _sse({"llm_done": True, "error": task["error_msg"]})
            else:
                yield _sse({"llm_done": True})
            return

        # ── 切换为实时队列推流 ─────────────────────────
        live_q = _live_queues.get(task_id)
        if live_q is None:
            # Worker 已结束但库中状态未完整更新（边界情况）
            return

        last_pct = task["progress"]

        try:
            while True:
                try:
                    kind, payload = live_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue

                if kind == "__done__":
                    break

                if kind == "progress":
                    pct = int(payload)
                    if pct != last_pct:
                        last_pct = pct
                        yield _sse({"progress": pct})

                elif kind == "result":
                    yield _sse({"progress": 100, "result": payload})

                elif kind == "error":
                    yield _sse({"error": payload})
                    break

                elif kind == "llm_stage":
                    yield _sse({"llm_stage": payload})

                elif kind == "llm_chunk":
                    yield _sse({"llm_chunk": payload})

                elif kind == "llm_done":
                    yield _sse({"llm_done": True, **payload})
                    break

        except asyncio.CancelledError:
            logger.info("SSE 客户端断线 task=%s（后台继续写库）", task_id)
            raise

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/tasks")
async def list_tasks():
    """历史任务列表（最近 50 条，按创建时间倒序）。"""
    return JSONResponse(content=db.list_tasks(limit=50))


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """查询单个任务详情（含完整 ASR 结果、说话人更正、LLM 分析）。"""
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse(content=task)


@router.get("/health")
async def health():
    """服务健康检查"""
    return {
        "status":           "ok",
        "asr_model_loaded": _models.asr_model is not None,
        "pyannote_loaded":  _models.diar_pipeline is not None,
        "llm_enabled":      LLM_ENABLED,
    }
