"""
LLM 分析模块：ASR 转写完成、用户确认说话人后进行面试评估。

支持两种模式：
  - 已更正（user_corrected=true）：直接使用用户指定的说话人角色评估
  - 未更正（默认）：提示 LLM 自行判断面试官/候选人身份，并在报告中说明依据

ASR 误差处理原则：
  - 转写乱码、断句异常属于技术误差，不代表候选人真实表达，不应因此扣分
  - 候选人真实的逻辑错误、知识盲点、回答不充分应客观指出，不要过度美化
"""
import json
import logging
from typing import Iterator, Optional, Union

import httpx

from app.config import NVIDIA_API_KEY, NVIDIA_BASE_URL, NVIDIA_MODEL

logger = logging.getLogger(__name__)

_ANALYZE_TIMEOUT = httpx.Timeout(connect=10, read=120, write=10, pool=5)

# ── System Prompt ─────────────────────────────────

_SYSTEM_PROMPT = """\
你是一名经验丰富的技术面试评估专家。请根据面试录音转写记录，对候选人的表现进行客观评估。

【重要说明】
转写记录来自语音识别（ASR），可能存在技术误差：词语乱码、重复片段、断句异常等。
这些属于识别噪音，不代表候选人的真实表达——分析时请跳过明显乱码，聚焦语义。
但候选人真实的逻辑错误、知识盲点、回答不充分、表达混乱，应当客观指出，不要美化回避。

【输出格式（Markdown）】

## 角色说明
（简述判断面试官/候选人身份的依据，或说明由用户已确认）

## 面试总评
（整体印象与综合能力，2-3 句话）

## 问题逐一分析

### 问题 N：{问题摘要}
- **候选人回答摘要**：（忽略 ASR 噪音，提炼实际语义）
- **亮点**：...
- **不足与建议**：...

## 综合评分
- 专业能力：⭐⭐⭐⭐☆（简短说明）
- 表达能力：⭐⭐⭐☆☆（简短说明）
- 潜力评估：⭐⭐⭐⭐☆（简短说明）

## 录用建议
（明确给出：录用 / 待定 / 不录用，并说明理由）\
"""

# ── User Prompt 模板 ──────────────────────────────

# 用户已更正说话人角色
_USER_TMPL_CORRECTED = """\
以下是面试对话记录，说话人角色**已由用户确认**（{role_summary}）：

{dialogue}

请按照要求进行评估。在"角色说明"部分注明：说话人角色已由用户确认，无需推断。\
"""

# 用户未更正，由 LLM 自行判断
_USER_TMPL_AUTO = """\
以下是面试对话记录。说话人编号（说话人A/B/C…）由 ASR **自动区分**，准确性不保证：
可能存在同一人被识别为多个说话人、或不同人被识别为同一说话人的情况。

{dialogue}

请先根据对话内容（提问方式、问题类型、回答模式、专业术语使用等）判断谁是面试官、谁是候选人，
在"角色说明"部分说明判断依据。若无法区分，以"对话方A"/"对话方B"代替并说明理由。
随后按照要求进行评估。\
"""


# ── LLMAnalyzer ──────────────────────────────────

class LLMAnalyzer:
    """封装所有 LLM 调用逻辑，使用 httpx 直接访问 NVIDIA /chat/completions 接口。"""

    def __init__(self):
        if not NVIDIA_API_KEY:
            raise RuntimeError(
                "未设置 NVIDIA_API_KEY 环境变量，请先执行：export NVIDIA_API_KEY='nvapi-xxxx'"
            )
        self._endpoint = f"{NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
        self._model    = NVIDIA_MODEL
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
        corrections: Optional[dict] = None,
    ) -> Iterator[Union[str, dict]]:
        """
        流式分析面试录音。

        Args:
            text:        ASR 完整转写文本（备用）
            segments:    ASR segments 列表，含 speaker_id/speaker/text/start_ms/end_ms
            corrections: 用户说话人更正信息，结构：
                         {
                           speaker_roles:     { str(speaker_id): 'interviewer'|'candidate' },
                           speaker_merges:    { str(speaker_id): int(target_id) },
                           segment_overrides: { str(index): int(speaker_id) },
                           user_corrected:    bool
                         }
                         为 None 或 empty 时 LLM 自行判断角色。

        Yields:
            str  — LLM 输出的 token（实时流）
            dict — {"markdown": 完整文本} 或 {"error": "..."}
        """
        user_corrected = bool(corrections and corrections.get("user_corrected"))

        # 根据更正信息计算有效 segments（带 display_name）
        effective = _apply_corrections(segments, corrections or {})
        dialogue  = _format_dialogue(effective)

        if user_corrected:
            role_summary = _build_role_summary(corrections)
            user_content = _USER_TMPL_CORRECTED.format(
                role_summary=role_summary,
                dialogue=dialogue,
            )
        else:
            user_content = _USER_TMPL_AUTO.format(dialogue=dialogue)

        logger.info(
            "开始流式面试分析，segments=%d，user_corrected=%s",
            len(segments), user_corrected,
        )

        payload = {
            "model":    self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_content},
            ],
            "temperature": 0.6,
            "top_p":       0.7,
            "max_tokens":  4096,
            "stream":      True,
        }
        headers = {**self._headers, "Accept": "text/event-stream"}
        full_text: list[str] = []

        try:
            with httpx.Client(timeout=_ANALYZE_TIMEOUT) as client:
                with client.stream("POST", self._endpoint,
                                   headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            token = chunk["choices"][0].get("delta", {}).get("content") or ""
                            if token:
                                full_text.append(token)
                                yield token
                        except (json.JSONDecodeError, KeyError, IndexError) as e:
                            logger.warning("流式帧解析失败（%s），跳过：%r", e, data_str[:200])

            total = "".join(full_text)
            logger.info("分析完成，共 %d 字", len(total))
            yield {"markdown": total}

        except httpx.HTTPStatusError as e:
            logger.error("LLM HTTP 错误：status=%s", e.response.status_code)
            yield {"error": f"LLM 请求失败（HTTP {e.response.status_code}）：{e.response.text[:200]}"}
        except Exception:
            logger.exception("LLM 分析发生未知异常")
            yield {"error": "LLM 分析失败，请检查 NVIDIA API 配置及网络连接"}


# ── 工具函数 ──────────────────────────────────────

_ROLE_LABELS = {"interviewer": "面试官", "candidate": "候选人"}


def _apply_corrections(segments: list[dict], corrections: dict) -> list[dict]:
    """
    根据用户更正，计算每条 segment 的有效说话人 ID 和显示名称。

    更正优先级（从高到低）：
      1. segment_overrides[index]   — 单条语句覆盖
      2. speaker_merges[speaker_id] — 说话人合并
      3. 原始 speaker_id

    显示名称优先级：
      1. speaker_roles 指定的角色名（面试官/候选人）
      2. 原始 ASR speaker 字段（说话人A/B/C...）
    """
    speaker_roles: dict     = corrections.get("speaker_roles", {})
    speaker_merges: dict    = corrections.get("speaker_merges", {})
    segment_overrides: dict = corrections.get("segment_overrides", {})

    result = []
    for i, seg in enumerate(segments):
        original_id = seg.get("speaker_id", 0)

        # 1. 单条覆盖
        effective_id = segment_overrides.get(str(i))
        if effective_id is not None:
            effective_id = int(effective_id)
        else:
            effective_id = original_id

        # 2. 合并
        merged = speaker_merges.get(str(effective_id))
        if merged is not None:
            effective_id = int(merged)

        # 3. 显示名称
        role = speaker_roles.get(str(effective_id))
        if role in _ROLE_LABELS:
            display_name = _ROLE_LABELS[role]
        else:
            # 回退到原始 ASR 说话人名
            orig_seg = next(
                (s for s in segments if s.get("speaker_id") == effective_id), None
            )
            display_name = (orig_seg or seg).get("speaker", f"说话人{chr(65 + effective_id % 26)}")

        result.append({**seg, "effective_id": effective_id, "display_name": display_name})

    return result


def _format_dialogue(effective_segments: list[dict]) -> str:
    """将有效 segments 格式化为对话文本，供 LLM 理解。"""
    if not effective_segments:
        return "（无有效对话内容）"
    lines = []
    for seg in effective_segments:
        name = seg.get("display_name") or seg.get("speaker", "未知说话人")
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"{name}：{text}")
    return "\n".join(lines)


def _build_role_summary(corrections: dict) -> str:
    """生成角色摘要字符串，用于已更正时的 Prompt。"""
    speaker_roles: dict  = corrections.get("speaker_roles", {})
    speaker_merges: dict = corrections.get("speaker_merges", {})

    parts = []
    for sid_str, role in speaker_roles.items():
        label = _ROLE_LABELS.get(role, role)
        spk_letter = chr(65 + int(sid_str) % 26)
        parts.append(f"说话人{spk_letter}={label}")

    for src_str, tgt in speaker_merges.items():
        src_letter = chr(65 + int(src_str) % 26)
        tgt_letter = chr(65 + int(tgt) % 26)
        parts.append(f"说话人{src_letter}与说话人{tgt_letter}为同一人")

    return "、".join(parts) if parts else "已由用户确认"
