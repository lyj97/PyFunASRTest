"""
应用入口：组装 FastAPI 实例并启动服务。
"""
# ① 最先导入 config，确保 logging.basicConfig(force=True) 在所有模块之前生效
import app.config  # noqa: F401

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.models import lifespan
from app.api import router

# ── FastAPI 实例 ───────────────────────────────────
app = FastAPI(
    title="FunASR 语音识别服务",
    description="基于阿里 FunASR + pyannote 的中文语音识别 API",
    version="2.0.0",
    lifespan=lifespan,
)

# ── 静态文件 ───────────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ── 前端页面 ───────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = static_dir / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="前端页面未找到")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

# ── 注册 API 路由（/api 前缀）─────────────────────
app.include_router(router, prefix="/api")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_config=None,   # 禁止 uvicorn 覆盖我们的 logging 配置
    )
