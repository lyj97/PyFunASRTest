"""
全局配置：从环境变量读取参数，初始化日志。
"""
import os
import logging

# ── 日志 ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    force=True,  # 强制覆盖 uvicorn 等框架预设的 handler
)

# ── HuggingFace Token ─────────────────────────────
# 启动前执行：export HF_TOKEN="hf_xxxx"
HF_TOKEN: str = os.environ.get("HF_TOKEN", "")

# ── 设备 ──────────────────────────────────────────
# FunASR 和 pyannote 共用同一个设备配置
def detect_device() -> str:
    """优先使用 MPS（Apple Silicon），其次 CPU。"""
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"

DEVICE: str = detect_device()

# ── 音频 ──────────────────────────────────────────
ALLOWED_EXTENSIONS: set[str] = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma"}

# 跳过过短片段的阈值（秒）
MIN_SEGMENT_DURATION: float = 0.3

# 并行 ASR 的最大线程数（None = CPU 核心数）
ASR_MAX_WORKERS: int | None = None

# ── NVIDIA LLM ────────────────────────────────────
# 启动前执行：export NVIDIA_API_KEY="nvapi-xxxx"
NVIDIA_API_KEY: str = os.environ.get("NVIDIA_API_KEY", "")

# NVIDIA 兼容 OpenAI 的接口基础 URL
NVIDIA_BASE_URL: str = os.environ.get(
    "NVIDIA_BASE_URL",
    "https://integrate.api.nvidia.com/v1",
)

# 使用的模型名称，可通过环境变量覆盖
NVIDIA_MODEL: str = os.environ.get(
    "NVIDIA_MODEL",
    "z-ai/glm4.7",
)

# 是否启用 LLM 分析（设为 false 可跳过 LLM 阶段，仅做 ASR）
LLM_ENABLED: bool = os.environ.get("LLM_ENABLED", "true").lower() != "false"
