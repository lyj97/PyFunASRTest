"""
音频处理工具：格式转换、片段切割、相邻段合并。
"""
import tempfile
import logging
from dataclasses import dataclass, field

from app.config import MIN_SEGMENT_DURATION

logger = logging.getLogger(__name__)

# pyannote 要求单声道 16kHz WAV
_TARGET_CHANNELS = 1
_TARGET_RATE = 16000

# 非 WAV 格式到 pydub format 名称的映射
_FMT_MAP: dict[str, str] = {
    ".mp3":  "mp3",
    ".m4a":  "mp4",
    ".flac": "flac",
    ".ogg":  "ogg",
    ".aac":  "aac",
    ".wma":  "asf",
}

# 同一说话人相邻段间隔不超过此值（秒）时合并
MERGE_GAP_S: float = 1.5


@dataclass
class SpeakerSegment:
    """pyannote 输出的单条说话人分段（时间 + 标签）。"""
    speaker:  str
    start_s:  float
    end_s:    float


@dataclass
class MergedChunk:
    """
    合并后的说话人块：同一说话人的若干相邻段合并为一条。
    包含多个子段的时间边界，供最终结果拆分回 segments 用。
    """
    speaker:    str
    speaker_id: int
    wav_path:   str               # 临时 WAV 文件（调用方负责删除）
    sub_segs:   list[tuple[int, int]] = field(default_factory=list)
    # sub_segs: [(start_ms, end_ms), ...] 各子段的原始时间戳


def convert_to_wav(src_path: str, suffix: str) -> str:
    """将任意格式音频转换为单声道 16kHz WAV，返回临时文件路径。"""
    from pydub import AudioSegment

    fmt = _FMT_MAP.get(suffix, suffix.lstrip("."))
    logger.info("格式转换 %s → WAV（fmt=%s）", suffix, fmt)
    audio = (
        AudioSegment.from_file(src_path, format=fmt)
        .set_channels(_TARGET_CHANNELS)
        .set_frame_rate(_TARGET_RATE)
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    audio.export(tmp.name, format="wav")
    tmp.close()
    return tmp.name


def collect_segments(diar_iter) -> list[SpeakerSegment]:
    """
    将 community-1 的迭代器 (turn, speaker) 转换为 SpeakerSegment 列表，
    过滤掉过短片段。
    """
    segs: list[SpeakerSegment] = []
    for turn, speaker in diar_iter:
        duration = turn.end - turn.start
        if duration < MIN_SEGMENT_DURATION:
            logger.debug("跳过短片段 [%s] %.2fs (< %.2fs)", speaker, duration, MIN_SEGMENT_DURATION)
            continue
        segs.append(SpeakerSegment(speaker=speaker, start_s=turn.start, end_s=turn.end))
    # 按时间顺序排列
    segs.sort(key=lambda s: s.start_s)
    logger.info("有效分段 %d 条", len(segs))
    return segs


def merge_adjacent(segs: list[SpeakerSegment]) -> list[list[SpeakerSegment]]:
    """
    将相邻的同说话人分段合并为一组：
    - 相邻两段间隔 <= MERGE_GAP_S 且说话人相同 → 合并
    - 否则新起一组

    返回分组列表，每组是一个 SpeakerSegment 子列表，
    组内所有片段属于同一说话人且时间上相邻。
    """
    if not segs:
        return []

    groups: list[list[SpeakerSegment]] = []
    current_group = [segs[0]]

    for seg in segs[1:]:
        prev = current_group[-1]
        gap = seg.start_s - prev.end_s
        if seg.speaker == prev.speaker and gap <= MERGE_GAP_S:
            current_group.append(seg)
        else:
            groups.append(current_group)
            current_group = [seg]

    groups.append(current_group)
    logger.info("合并后共 %d 组（原 %d 段）", len(groups), len(segs))
    return groups


def build_merged_chunks(wav_path: str, groups: list[list[SpeakerSegment]]) -> list[MergedChunk]:
    """
    对每组分段：将多个子段的音频拼接成一个连续 WAV 文件，
    同时记录每个子段的原始时间戳（供后续拆分结果用）。
    """
    from pydub import AudioSegment

    full_audio = AudioSegment.from_wav(wav_path)
    chunks: list[MergedChunk] = []

    for group in groups:
        speaker = group[0].speaker
        spk_num = int(speaker.split("_")[-1]) if "_" in speaker else 0

        # 拼接各子段音频
        merged_audio = AudioSegment.empty()
        sub_segs: list[tuple[int, int]] = []
        for seg in group:
            start_ms = int(seg.start_s * 1000)
            end_ms   = int(seg.end_s   * 1000)
            merged_audio += full_audio[start_ms:end_ms]
            sub_segs.append((start_ms, end_ms))

        # 写临时文件
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        merged_audio.export(tmp.name, format="wav")
        tmp.close()

        chunks.append(MergedChunk(
            speaker    = speaker,
            speaker_id = spk_num,
            wav_path   = tmp.name,
            sub_segs   = sub_segs,
        ))

    logger.info("生成 %d 个合并音频块，共需 %d 次 ASR 推理",
                len(chunks), len(chunks))
    return chunks
