/**
 * useSpeaker — 说话人角色/合并/单条覆盖状态管理
 *
 * 依赖：Vue 全局（ref、computed）
 */
const useSpeaker = (result) => {
  const { ref, computed } = Vue;

  // { [speaker_id]: 'interviewer' | 'candidate' | 'unknown' }
  const speakerRoles     = ref({});
  // { [speaker_id]: target_speaker_id }  说话人合并
  const speakerMerges    = ref({});
  // { [segment_index]: speaker_id }      单条语句覆盖
  const segmentOverrides = ref({});
  // 当前弹出编辑 popup 的 segment 索引
  const editingSegIndex  = ref(null);
  // 说话人编辑面板是否展开
  const speakerEditorOpen = ref(false);

  /** 所有唯一说话人（按首次出现顺序） */
  const allSpeakers = computed(() => {
    if (!result.value?.segments) return [];
    const seen = new Set();
    const list = [];
    for (const seg of result.value.segments) {
      if (!seen.has(seg.speaker_id)) {
        seen.add(seg.speaker_id);
        list.push({ id: seg.speaker_id, name: seg.speaker });
      }
    }
    return list;
  });

  /** 未被合并掉的有效说话人 */
  const activeSpeakers = computed(() =>
    allSpeakers.value.filter(spk => speakerMerges.value[spk.id] === undefined)
  );

  /** 是否有任何用户更正 */
  const userCorrected = computed(() =>
    Object.values(speakerRoles.value).some(r => r !== 'unknown') ||
    Object.keys(speakerMerges.value).length > 0 ||
    Object.keys(segmentOverrides.value).length > 0
  );

  /** 计算每条 segment 的有效说话人（应用合并 + 单条覆盖） */
  const effectiveSegments = computed(() => {
    if (!result.value?.segments) return [];
    return result.value.segments.map((seg, i) => {
      let effectiveId = segmentOverrides.value[i];
      if (effectiveId === undefined) {
        const merged = speakerMerges.value[seg.speaker_id];
        effectiveId = merged !== undefined ? merged : seg.speaker_id;
      }
      const displayName = getEffectiveName(effectiveId);
      return { ...seg, effectiveId, displayName };
    });
  });

  /** 获取 speaker_id 的有效角色（经合并后） */
  function getEffectiveRole(speakerId) {
    const merged = speakerMerges.value[speakerId];
    const effectiveId = merged !== undefined ? merged : speakerId;
    return speakerRoles.value[effectiveId] || 'unknown';
  }

  /** 获取 speaker_id 的显示名称 */
  function getEffectiveName(speakerId) {
    const role = getEffectiveRole(speakerId);
    if (role === 'interviewer') return '面试官';
    if (role === 'candidate')   return '候选人';
    const orig = allSpeakers.value.find(s => s.id === speakerId);
    return orig?.name || `说话人${String.fromCharCode(65 + speakerId)}`;
  }

  function speakerInitial(id) {
    return String.fromCharCode(65 + (id % 26));
  }

  // ── 编辑操作 ───────────────────────────────────

  function setSpeakerRole(speakerId, role) {
    speakerRoles.value = { ...speakerRoles.value, [speakerId]: role };
  }

  function setSpeakerMerge(speakerId, targetIdStr) {
    const merges = { ...speakerMerges.value };
    if (targetIdStr === '') {
      delete merges[speakerId];
    } else {
      merges[speakerId] = parseInt(targetIdStr);
      // 被合并的说话人角色跟随目标，移除自身角色记录
      const roles = { ...speakerRoles.value };
      delete roles[speakerId];
      speakerRoles.value = roles;
    }
    speakerMerges.value = merges;
  }

  function setSegmentOverride(segIndex, speakerId) {
    segmentOverrides.value = { ...segmentOverrides.value, [segIndex]: speakerId };
    editingSegIndex.value = null;
  }

  function clearSegmentOverride(segIndex) {
    const overrides = { ...segmentOverrides.value };
    delete overrides[segIndex];
    segmentOverrides.value = overrides;
    editingSegIndex.value = null;
  }

  function toggleSegEdit(index) {
    editingSegIndex.value = editingSegIndex.value === index ? null : index;
  }

  /** ASR 完成时初始化，全部默认 unknown */
  function initSpeakerRoles(speakers) {
    const roles = {};
    for (const spk of speakers) {
      roles[spk.id] = 'unknown';
    }
    speakerRoles.value     = roles;
    speakerMerges.value    = {};
    segmentOverrides.value = {};
    editingSegIndex.value  = null;
  }

  /** 打开历史任务时恢复已保存的更正 */
  function restoreSpeakerCorrections(savedCorrections) {
    if (!savedCorrections) {
      initSpeakerRoles(allSpeakers.value);
      return;
    }
    speakerRoles.value = Object.fromEntries(
      Object.entries(savedCorrections.speaker_roles || {}).map(([k, v]) => [parseInt(k), v])
    );
    speakerMerges.value = Object.fromEntries(
      Object.entries(savedCorrections.speaker_merges || {}).map(([k, v]) => [parseInt(k), parseInt(v)])
    );
    segmentOverrides.value = Object.fromEntries(
      Object.entries(savedCorrections.segment_overrides || {}).map(([k, v]) => [parseInt(k), parseInt(v)])
    );
  }

  /** 重置所有说话人状态 */
  function resetSpeaker() {
    speakerRoles.value     = {};
    speakerMerges.value    = {};
    segmentOverrides.value = {};
    editingSegIndex.value  = null;
    speakerEditorOpen.value = false;
  }

  /** 构造提交给后端的 corrections 对象 */
  function buildCorrections() {
    return {
      speaker_roles: Object.fromEntries(
        Object.entries(speakerRoles.value).map(([k, v]) => [String(k), v])
      ),
      speaker_merges: Object.fromEntries(
        Object.entries(speakerMerges.value).map(([k, v]) => [String(k), v])
      ),
      segment_overrides: Object.fromEntries(
        Object.entries(segmentOverrides.value).map(([k, v]) => [String(k), v])
      ),
      user_corrected: true,
    };
  }

  return {
    speakerRoles, speakerMerges, segmentOverrides, editingSegIndex, speakerEditorOpen,
    allSpeakers, activeSpeakers, effectiveSegments, userCorrected,
    getEffectiveRole, getEffectiveName, speakerInitial,
    setSpeakerRole, setSpeakerMerge,
    setSegmentOverride, clearSegmentOverride, toggleSegEdit,
    initSpeakerRoles, restoreSpeakerCorrections, resetSpeaker, buildCorrections,
  };
};
