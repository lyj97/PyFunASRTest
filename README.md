# PyFunASRTest

基于阿里 **FunASR**（paraformer-zh）+ **pyannote**（speaker-diarization-community-1）的中文语音识别与说话人分离服务，提供 FastAPI HTTP 接口和 Web 前端。

---

## 功能特性

- **中文语音识别**：使用 FunASR paraformer-zh 模型，附带 VAD 端点检测和标点恢复
- **说话人分离**：使用 pyannote speaker-diarization-community-1，自动识别多说话人
- **SSE 流式进度推送**：识别过程实时推送进度（0–100%），无需轮询
- **多格式支持**：`.wav` `.mp3` `.m4a` `.flac` `.ogg` `.aac` `.wma`
- **Apple Silicon 加速**：自动检测 MPS 设备，CPU 兜底

---

## 环境要求

- Python 3.10+
- ffmpeg（pydub 音频格式转换依赖）
- HuggingFace 账号 Token（用于下载 pyannote 模型）

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> Apple Silicon 用户建议先单独安装 `torch`/`torchaudio` 的 MPS 版本，再安装其余依赖。

### 2. 安装 ffmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get install ffmpeg
```

### 3. 设置 HuggingFace Token

前往 [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) 创建 Token，并接受 [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) 模型的使用条款。

```bash
export HF_TOKEN="hf_xxxxxxxxxxxxxxxxxxxx"
```

### 4. 启动服务

```bash
python main.py
```

服务默认监听 `http://0.0.0.0:8000`，首次启动会自动下载并加载模型（耗时较长，请耐心等待）。

---

## API 文档

启动后访问 `http://localhost:8000/docs` 查看交互式 Swagger 文档。

### POST `/api/transcribe`

上传音频文件，以 SSE 流式返回识别进度和结果。

**请求**：`multipart/form-data`，字段名 `file`

**响应**：`text/event-stream`，每条事件为 JSON：

| 事件类型 | 示例 |
|---------|------|
| 进度更新 | `{ "progress": 42 }` |
| 识别完成 | `{ "progress": 100, "result": { "text": "...", "segments": [...] } }` |
| 识别失败 | `{ "error": "错误信息" }` |

`segments` 中每条记录包含：

```json
{
  "speaker": "说话人1",
  "speaker_id": 0,
  "text": "识别文本",
  "start_ms": 1000,
  "end_ms": 3500
}
```

### GET `/api/health`

服务健康检查，返回模型加载状态。

```json
{
  "status": "ok",
  "asr_model_loaded": true,
  "pyannote_loaded": true
}
```

---

## 项目结构

```
PyFunASRTest/
├── main.py              # 入口：FastAPI 实例组装与启动
├── requirements.txt     # 依赖清单
├── app/
│   ├── config.py        # 全局配置（设备检测、环境变量、日志）
│   ├── models.py        # 模型管理（FunASR + pyannote 加载与 lifespan）
│   ├── api.py           # HTTP 路由（/api/transcribe、/api/health）
│   ├── transcriber.py   # 识别核心流程（分段 → 合并 → 并行 ASR）
│   └── audio.py         # 音频工具（格式转换、切割、说话人段合并）
├── static/
│   └── index.html       # Web 前端
└── audio/               # 测试音频（可选）
```

---

## 配置说明

通过环境变量调整运行参数：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HF_TOKEN` | 无（必填） | HuggingFace 访问 Token |

其余参数在 [`app/config.py`](app/config.py) 中修改：

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `MIN_SEGMENT_DURATION` | `0.3` 秒 | 过滤过短说话片段的阈值 |
| `ASR_MAX_WORKERS` | `None`（CPU 核数） | 并行 ASR 的最大线程数 |
| `MERGE_GAP_S`（audio.py） | `1.5` 秒 | 同说话人相邻段合并间隔阈值 |
