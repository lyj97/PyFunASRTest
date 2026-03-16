"""
FastAPI 路由：
  POST /api/transcribe  — 接收音频，以 SSE 流式推送进度，ASR 完成后继续推送 LLM 分析
  GET  /api/health      — 服务健康检查
"""
import asyncio
import json
import logging
import queue
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, UploadFile, HTTPException
from fastapi.responses import StreamingResponse

import app.models as _models
from app import transcriber
from app.config import ALLOWED_EXTENSIONS, LLM_ENABLED

logger = logging.getLogger(__name__)

router = APIRouter()


# ── SSE 工具 ──────────────────────────────────────

def _sse(data: dict) -> str:
    """将字典序列化为 SSE 格式字符串。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 路由 ──────────────────────────────────────────

@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    """
    语音转文字 + LLM 面试分析（SSE 流式进度 + 最终结果）。

    响应格式：text/event-stream
    每条事件为 JSON：
      ASR 阶段：
        { "progress": 0-99 }                               — 进度更新
        { "progress": 100, "result": { text, segments } }  — ASR 完成
      LLM 阶段：
        { "llm_stage": "analyzing" }                       — AI 分析开始
        { "llm_chunk": "..." }                             — LLM token 流式输出
        { "llm_done": true, "analysis": { markdown } }    — LLM 分析完成
        { "llm_done": true, "error": "..." }               — LLM 出错
      通用错误：
        { "error": "..." }                                 — 出错
    """
    if _models.asr_model is None or _models.diar_pipeline is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式：{suffix}，支持：{', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    logger.info("收到识别请求：%s", file.filename)

    # 将上传文件落盘（线程池任务需要文件路径）
    tmp_path: str | None = None
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(await file.read())

    # 线程 → 协程的进度管道
    progress_q: queue.SimpleQueue = queue.SimpleQueue()

    loop = asyncio.get_event_loop()

    def _worker():
        """
        在线程池中依次运行：
          ① ASR 识别（进度 0-100%）
          ② LLM 面试分析（直接分析，不做场景判断）
        所有进度/数据通过 queue 传给异步生成器。
        """
        # ── ① ASR 阶段 ──────────────────────────────
        try:
            result = transcriber.run(
                tmp_path, suffix,
                progress_cb=lambda pct: progress_q.put(("progress", pct)),
            )
            progress_q.put(("result", result))
        except Exception as e:
            logger.exception("ASR 识别失败")
            progress_q.put(("error", str(e)))
            return  # ASR 失败则不继续 LLM

        # ── ② LLM 阶段 ──────────────────────────────
        if not LLM_ENABLED:
            logger.info("LLM_ENABLED=false，跳过 LLM 分析")
            return

        if not result.get("text"):
            logger.info("ASR 结果为空，跳过 LLM 分析")
            return

        try:
            # 延迟导入，避免未配置 NVIDIA_API_KEY 时启动报错
            from app.llm import LLMAnalyzer
            analyzer = LLMAnalyzer()

            # 直接进入面试分析（流式）
            progress_q.put(("llm_stage", "analyzing"))
            for chunk in analyzer.analyze_interview_stream(result["text"], result.get("segments", [])):
                if isinstance(chunk, str):
                    progress_q.put(("llm_chunk", chunk))
                else:
                    if "error" in chunk:
                        progress_q.put(("llm_done", {"error": chunk["error"]}))
                    else:
                        progress_q.put(("llm_done", {"analysis": chunk}))

        except Exception as e:
            logger.exception("LLM 分析失败")
            progress_q.put(("llm_done", {"error": str(e)}))

    # 启动后台线程，不 await（让生成器驱动进度）
    future = loop.run_in_executor(None, _worker)

    async def _generate():
        """
        异步生成器：持续从 queue 读取，转成 SSE 事件流。
        同时处理 ASR 进度事件和 LLM 分析事件。
        """
        last_pct = -1
        # llm_done 是整个流的终止信号
        asr_done = False
        try:
            while True:
                # 非阻塞检查 queue；若空则让出事件循环 0.1s
                try:
                    kind, payload = progress_q.get_nowait()
                except queue.Empty:
                    # 如果 future 已完成且 queue 为空，说明 worker 全部结束
                    if asr_done and future.done():
                        break
                    await asyncio.sleep(0.1)
                    continue

                # ── ASR 阶段事件 ──────────────────────
                if kind == "progress":
                    pct = int(payload)
                    if pct != last_pct:        # 去重，避免重复推同一进度
                        last_pct = pct
                        yield _sse({"progress": pct})

                elif kind == "result":
                    yield _sse({"progress": 100, "result": payload})
                    asr_done = True
                    # 若未启用 LLM，ASR 完成即可关闭
                    if not LLM_ENABLED:
                        break

                elif kind == "error":
                    yield _sse({"error": payload})
                    break

                # ── LLM 阶段事件 ──────────────────────
                elif kind == "llm_stage":
                    yield _sse({"llm_stage": payload})

                elif kind == "llm_chunk":
                    yield _sse({"llm_chunk": payload})

                elif kind == "llm_done":
                    yield _sse({"llm_done": True, **payload})
                    break  # LLM 完成，关闭 SSE 连接

        finally:
            # 清理临时文件
            Path(tmp_path).unlink(missing_ok=True)
            # 确保 future 完成（即使生成器提前退出）
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=1)
            except Exception:
                pass

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 禁止 Nginx 缓冲，确保实时推送
        },
    )


@router.get("/health")
async def health():
    """服务健康检查"""
    return {
        "status":           "ok",
        "asr_model_loaded": _models.asr_model is not None,
        "pyannote_loaded":  _models.diar_pipeline is not None,
        "llm_enabled":      LLM_ENABLED,
    }
