"""
LLM 分析模块：ASR 转写完成后直接进行面试评估。

使用 httpx 直接发起流式 HTTP 请求，手动解析 SSE（Server-Sent Events），
不依赖 openai 包。NVIDIA 接口完全兼容 OpenAI chat/completions 协议。
"""
import json
import logging
from typing import Iterator, Union

import httpx

from app.config import NVIDIA_API_KEY, NVIDIA_BASE_URL, NVIDIA_MODEL

logger = logging.getLogger(__name__)

# 面试分析：连接 10s，读取 120s（LLM 生成较慢）
_ANALYZE_TIMEOUT = httpx.Timeout(connect=10, read=120, write=10, pool=5)

# ── Prompt 常量 ───────────────────────────────────

_INTERVIEW_SYSTEM = """你是一名经验丰富的面试官。请根据以下面试录音转写记录，对候选人的表现进行客观评估。

输出格式要求（使用 Markdown）：

## 面试总评
（整体印象、综合能力评价，2-3句话）

## 问题逐一分析
（对每道面试题分别分析，格式如下）

### 问题 N：{问题简短摘要}
- **候选人回答摘要**：...
- **回答正确/亮点**：...
- **不足与补充建议**：...

## 综合评分
- 专业能力：⭐⭐⭐⭐☆（附简要说明）
- 表达能力：⭐⭐⭐☆☆（附简要说明）
- 潜力评估：⭐⭐⭐⭐☆（附简要说明）

## 录用建议
（给出明确的录用/待定/不录用建议，并说明理由）"""

_INTERVIEW_USER_TMPL = (
    "以下是面试录音的逐字转写记录，格式为「说话人：内容」：\n\n"
    "{dialogue}\n\n"
    "请按照要求进行评估。"
)


# ── LLMAnalyzer ──────────────────────────────────

class LLMAnalyzer:
    """
    封装所有 LLM 调用逻辑，使用 httpx 直接访问 NVIDIA /chat/completions 接口。
    """

    def __init__(self):
        if not NVIDIA_API_KEY:
            raise RuntimeError(
                "未设置 NVIDIA_API_KEY 环境变量，请先执行：export NVIDIA_API_KEY='nvapi-xxxx'"
            )
        self._endpoint = f"{NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
        self._model    = NVIDIA_MODEL
        # 基础请求头
        self._headers  = {
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Content-Type":  "application/json",
        }
        logger.info("LLMAnalyzer 初始化完成，endpoint=%s model=%s",
                    self._endpoint, self._model)

    def analyze_interview_stream(
        self,
        text: str,
        segments: list[dict],
    ) -> Iterator[Union[str, dict]]:
        """
        流式分析面试录音。

        Yields:
            str  — LLM 输出的 token chunk（实时推送）
            dict — 最终结构化结果 {"markdown": 完整文本}（最后一项）
                   或 {"error": "..."}（出错时）
        """
        dialogue = _format_dialogue(segments)
        logger.info("开始流式面试分析，对话段数：%d", len(segments))

        payload = {
            "model":       self._model,
            "messages": [
                {"role": "system", "content": _INTERVIEW_SYSTEM},
                {"role": "user",   "content": _INTERVIEW_USER_TMPL.format(dialogue=dialogue)},
            ],
            "temperature": 0.6,
            "top_p":       0.7,
            "max_tokens":  4096,
            "stream":      True,
        }
        # SSE 流式响应需要 Accept: text/event-stream
        headers = {**self._headers, "Accept": "text/event-stream"}

        full_text: list[str] = []

        try:
            with httpx.Client(timeout=_ANALYZE_TIMEOUT) as client:
                with client.stream("POST", self._endpoint,
                                   headers=headers, json=payload) as resp:
                    resp.raise_for_status()

                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue

                        data_str = line[5:].strip()   # 去掉 "data:" 前缀

                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                            delta = chunk["choices"][0].get("delta", {})
                            token = delta.get("content") or ""
                            if token:
                                full_text.append(token)
                                yield token   # 实时推送给调用方
                        except json.JSONDecodeError:
                            logger.warning("流式帧 JSON 解析失败，跳过：%r", data_str[:200])
                        except (KeyError, IndexError) as e:
                            logger.warning("流式帧结构异常（%s），跳过：%r", e, data_str[:200])

            # 最终结构化结果
            total = "".join(full_text)
            logger.info("面试分析流式输出完成，共 %d 个 token，总字数 %d", len(full_text), len(total))
            yield {"markdown": total}

        except httpx.HTTPStatusError as e:
            logger.error(
                "LLM 流式请求 HTTP 错误：status=%s body=%s",
                e.response.status_code, e.response.text[:500],
            )
            yield {"error": f"LLM 请求失败（HTTP {e.response.status_code}）：{e.response.text[:200]}"}
        except Exception:
            logger.exception("LLM 流式分析发生未知异常")
            yield {"error": "LLM 分析失败，请检查 NVIDIA API 配置及网络连接"}


# ── 工具函数 ──────────────────────────────────────

def _format_dialogue(segments: list[dict]) -> str:
    """
    将 ASR segments 格式化为对话文本，供 LLM 理解。
    格式：说话人A：内容\n说话人B：内容\n...
    """
    if not segments:
        return "（无有效对话内容）"

    lines = []
    for seg in segments:
        speaker = seg.get("speaker", "未知说话人")
        text    = seg.get("text", "").strip()
        if text:
            lines.append(f"{speaker}：{text}")

    return "\n".join(lines)
