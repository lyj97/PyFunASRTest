"""
模型管理：FunASR + pyannote 的加载、全局实例与 lifespan。
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from funasr import AutoModel

from app.config import HF_TOKEN, DEVICE

logger = logging.getLogger(__name__)

# ── 全局模型实例 ───────────────────────────────────
# 由 lifespan 负责初始化和清理，其他模块只读引用
asr_model = None        # FunASR paraformer-zh
diar_pipeline = None    # pyannote speaker-diarization-3.1


def _load_funasr() -> object:
    """加载 FunASR 识别模型（同步，运行于启动阶段）。"""
    logger.info("加载 FunASR 模型（paraformer-zh，device=%s）...", DEVICE)
    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        device=DEVICE,
    )
    logger.info("FunASR 加载完成")
    return model


def _load_pyannote() -> object:
    """加载 pyannote 说话人分段 pipeline（同步，运行于启动阶段）。"""
    from pyannote.audio import Pipeline
    import torch

    logger.info("加载 pyannote 模型（speaker-diarization-community-1，device=%s）...", DEVICE)
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-community-1",
        token=HF_TOKEN,
    )
    pipeline.to(torch.device(DEVICE))
    logger.info("pyannote 加载完成")
    return pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期：启动时并行预加载两套模型，退出时释放。

    两套模型加载彼此独立，使用 asyncio.to_thread 并发执行，
    缩短冷启动时间。
    """
    import asyncio

    global asr_model, diar_pipeline

    if not HF_TOKEN:
        raise RuntimeError(
            "未设置 HF_TOKEN 环境变量，请先执行：export HF_TOKEN='hf_xxxx'"
        )

    logger.info("并行加载两套模型...")
    # 两个加载任务并发执行，谁先完成谁先返回
    asr_model, diar_pipeline = await asyncio.gather(
        asyncio.to_thread(_load_funasr),
        asyncio.to_thread(_load_pyannote),
    )
    logger.info("✅ 所有模型就绪，服务启动成功！")

    yield

    asr_model = None
    diar_pipeline = None
    logger.info("模型已释放")
