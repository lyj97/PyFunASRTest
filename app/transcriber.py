"""
识别核心流程：
  1. 音频预处理（格式转换）
  2. torchaudio → pyannote 说话人分段
  3. 相邻同说话人段合并
  4. 按说话人并行 ASR（减少推理次数）
  5. 结果按时间排序返回

进度通过 progress_cb(pct: int) 回调汇报，供 SSE 推送。
"""
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import app.models as _models
from app.audio import (
    MergedChunk,
    collect_segments,
    merge_adjacent,
    build_merged_chunks,
    convert_to_wav,
)
from app.config import ASR_MAX_WORKERS

logger = logging.getLogger(__name__)

ProgressCb = Callable[[int], None]   # pct: 0-100


# ── pyannote ProgressHook ─────────────────────────

class _DiarHook:
    """
    将 pyannote 内部进度映射到外部进度区间 [start_pct, end_pct]。
    pyannote 会多次调用 hook(completed, total, ...)，我们按比例折算。
    """
    def __init__(self, cb: ProgressCb, start: int, end: int):
        self._cb, self._start, self._end = cb, start, end
        self._last = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def __call__(self, *args, **kwargs):
        # pyannote hook 有两种调用形式：
        #   进度通知：hook(completed=N, total=M)  — 两个整数
        #   产物通知：hook("segmentation", obj)   — 字符串 + 对象
        # 后者不是进度信息，直接忽略
        completed = kwargs.get("completed", args[0] if len(args) > 0 else None)
        total     = kwargs.get("total",     args[1] if len(args) > 1 else None)
        if not isinstance(completed, (int, float)) or not isinstance(total, (int, float)):
            return
        if not total or not self._cb:
            return
        pct = self._start + int((completed / total) * (self._end - self._start))
        pct = min(pct, self._end)
        if pct != self._last:
            self._last = pct
            self._cb(pct)


# ── 单块 ASR ─────────────────────────────────────

def _transcribe_chunk(chunk: MergedChunk) -> list[dict]:
    """
    对一个合并块执行 FunASR 识别。
    返回按子段时间边界拆分后的 segments 列表。
    识别完毕后自动删除临时 WAV。
    """
    try:
        result = _models.asr_model.generate(
            input=chunk.wav_path,
            batch_size_s=300,
        )
        full_text: str = result[0].get("text", "").strip() if result else ""
        logger.info("[%s] %d 子段 → %s", chunk.speaker, len(chunk.sub_segs),
                    (full_text[:30] + "…") if len(full_text) > 30 else full_text or "（空）")

        if not full_text or not chunk.sub_segs:
            return []

        # 将整块文字均分到各子段（粗粒度，但时间边界准确）
        # 若只有 1 个子段，直接归属
        n = len(chunk.sub_segs)
        char_list = list(full_text)
        total_chars = len(char_list)

        segs: list[dict] = []
        for i, (start_ms, end_ms) in enumerate(chunk.sub_segs):
            # 按子段时长比例分配字符
            seg_dur = end_ms - start_ms
            total_dur = sum(e - s for s, e in chunk.sub_segs)
            char_count = max(1, round(total_chars * seg_dur / total_dur)) if total_dur else (total_chars // n)

            start_idx = sum(
                max(1, round(total_chars * (e - s) / total_dur))
                for s, e in chunk.sub_segs[:i]
            ) if total_dur else (i * (total_chars // n))
            end_idx = start_idx + char_count if i < n - 1 else total_chars

            seg_text = "".join(char_list[start_idx:end_idx]).strip()
            if not seg_text:
                continue

            segs.append({
                "speaker":    f"说话人{chunk.speaker_id + 1}",
                "speaker_id": chunk.speaker_id,
                "text":       seg_text,
                "start_ms":   start_ms,
                "end_ms":     end_ms,
            })

        return segs
    finally:
        if os.path.exists(chunk.wav_path):
            os.unlink(chunk.wav_path)


# ── 主流程 ────────────────────────────────────────

def run(audio_path: str, suffix: str,
        progress_cb: Optional[ProgressCb] = None) -> dict:
    """
    完整识别流程（同步，供 run_in_executor 调用）。

    进度区间分配：
      0  →  5%  : 音频预处理
      5  → 50%  : pyannote 说话人分段（ProgressHook 细粒度）
      50 → 55%  : 分段合并 + 音频切块
      55 → 95%  : 并行 ASR（每完成一块更新）
      100%       : 完成
    """
    import torchaudio

    def cb(pct: int):
        if progress_cb:
            progress_cb(pct)

    wav_tmp: Optional[str] = None

    try:
        # ① 音频预处理
        cb(2)
        if suffix != ".wav":
            wav_tmp = convert_to_wav(audio_path, suffix)
            wav_path = wav_tmp
        else:
            wav_path = audio_path
        cb(5)

        # ② torchaudio 内存加载 → pyannote 分段
        logger.info("加载音频到内存...")
        waveform, sample_rate = torchaudio.load(wav_path)
        logger.info("音频：shape=%s sr=%d", waveform.shape, sample_rate)

        logger.info("开始说话人分段...")
        with _DiarHook(progress_cb, start=5, end=50) as hook:
            raw_output = _models.diar_pipeline(
                {"waveform": waveform, "sample_rate": sample_rate},
                hook=hook,
            )
        cb(50)

        # ③ 收集 → 合并相邻同说话人段
        segs = collect_segments(raw_output.speaker_diarization)
        if not segs:
            logger.warning("未检测到有效说话片段")
            cb(100)
            return {"text": "", "segments": []}

        groups  = merge_adjacent(segs)
        chunks  = build_merged_chunks(wav_path, groups)
        cb(55)
        logger.info("共 %d 个合并块，启动并行 ASR...", len(chunks))

        # ④ 并行 ASR
        total = len(chunks)
        completed_count = 0
        all_segs: list[dict] = []

        with ThreadPoolExecutor(max_workers=ASR_MAX_WORKERS) as executor:
            future_to_chunk = {
                executor.submit(_transcribe_chunk, c): c
                for c in chunks
            }
            for future in as_completed(future_to_chunk):
                try:
                    sub = future.result()
                    all_segs.extend(sub)
                except Exception:
                    logger.exception("ASR 块识别失败")
                finally:
                    completed_count += 1
                    pct = 55 + int(completed_count / total * 40)
                    cb(pct)

        # ⑤ 按时间排序，拼装全文
        all_segs.sort(key=lambda s: s["start_ms"])
        full_text = "".join(s["text"] for s in all_segs)
        logger.info("识别完成：%d 段，%d 字", len(all_segs), len(full_text))
        cb(100)

        return {"text": full_text, "segments": all_segs}

    except Exception:
        logger.exception("识别流程发生异常")
        raise
    finally:
        if wav_tmp and os.path.exists(wav_tmp):
            os.unlink(wav_tmp)
