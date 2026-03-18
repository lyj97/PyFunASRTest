/**
 * Vue 3 应用逻辑（任务持久化 + 说话人更正版）
 *
 * 流程：
 *   1. 上传文件 → POST /api/tasks → 订阅 ASR SSE
 *   2. ASR 完成后，前端展示「说话人编辑器」：
 *      - 批量为每个说话人指定角色（面试官/候选人）或合并同一人
 *      - 点击气泡头像可单独覆盖该条语句的说话人
 *   3. 用户点击「开始 AI 分析」→ POST /api/tasks/{id}/analyze（携带更正）
 *   4. 再次订阅 SSE → LLM 流式分析结果
 *   5. 历史任务列表支持恢复查看
 */
const { createApp, ref, computed, onMounted, watch } = Vue;

createApp({
  setup() {
    // ── 基础状态 ───────────────────────────────────
    const selectedFile   = ref(null);
    const loading        = ref(false);
    const progress       = ref(0);
    const result         = ref(null);       // ASR 结果 { text, segments }
    const errorMsg       = ref('');
    const isDragging     = ref(false);
    const copied         = ref(false);
    const copiedAnalysis = ref(false);
    const fileInput      = ref(null);

    // ── LLM 状态 ────────────────────────────────────
    const llmStage      = ref('');
    const llmChunks     = ref('');
    const llmDone       = ref(false);
    const llmTriggered  = ref(false);   // ASR 完成后是否已手动触发 LLM

    // ── 当前任务 ─────────────────────────────────────
    const currentTaskId  = ref('');
    let   _streamAbortCtrl = null;   // 当前 SSE 连接的 AbortController

    // ── 历史任务列表 ─────────────────────────────────
    const historyTasks = ref([]);
    const historyOpen  = ref(true);

    // ── 说话人编辑状态 ───────────────────────────────
    /**
     * speaker_roles: { [speaker_id]: 'interviewer' | 'candidate' | 'unknown' }
     * speaker_merges: { [speaker_id]: target_speaker_id }   说话人合并
     * segment_overrides: { [segment_index]: speaker_id }    单条语句覆盖
     * editingSegIndex: 当前正在弹出编辑 popup 的 segment 索引，null=无
     */
    const speakerRoles      = ref({});
    const speakerMerges     = ref({});
    const segmentOverrides  = ref({});
    const editingSegIndex   = ref(null);

    const formats = ['WAV', 'MP3', 'M4A', 'FLAC', 'OGG', 'AAC', 'WMA'];

    // ── 说话人工具 ───────────────────────────────────

    /** 从 ASR 结果中提取唯一说话人列表（按首次出现顺序） */
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

    /** 未被合并掉的有效说话人（显示在编辑器行里） */
    const activeSpeakers = computed(() =>
      allSpeakers.value.filter(spk => speakerMerges.value[spk.id] === undefined)
    );

    /** 是否有任何用户更正 */
    const userCorrected = computed(() =>
      Object.values(speakerRoles.value).some(r => r !== 'unknown') ||
      Object.keys(speakerMerges.value).length > 0 ||
      Object.keys(segmentOverrides.value).length > 0
    );

    /** 获取 speaker_id 的有效角色标签（经过合并后） */
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

    /**
     * 计算每条 segment 的有效说话人（应用合并 + 单条覆盖），
     * 返回附带 effectiveId / displayName 的新数组（响应式）。
     */
    const effectiveSegments = computed(() => {
      if (!result.value?.segments) return [];
      return result.value.segments.map((seg, i) => {
        // 1. 单条覆盖
        let effectiveId = segmentOverrides.value[i];
        if (effectiveId === undefined) {
          // 2. 合并
          const merged = speakerMerges.value[seg.speaker_id];
          effectiveId = merged !== undefined ? merged : seg.speaker_id;
        }
        const displayName = getEffectiveName(effectiveId);
        return { ...seg, effectiveId, displayName };
      });
    });

    /** 用于单条编辑 popup：点击后可选择的有效说话人 */
    function segEditOptions(currentEffectiveId) {
      // 显示所有未合并说话人（activeSpeakers）
      return activeSpeakers.value;
    }

    // ── 说话人编辑操作 ───────────────────────────────

    function setSpeakerRole(speakerId, role) {
      speakerRoles.value = { ...speakerRoles.value, [speakerId]: role };
    }

    function setSpeakerMerge(speakerId, targetIdStr) {
      const merges = { ...speakerMerges.value };
      if (targetIdStr === '') {
        // 取消合并
        delete merges[speakerId];
      } else {
        merges[speakerId] = parseInt(targetIdStr);
        // 同时从 speakerRoles 中移除被合并的说话人（角色跟随目标）
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

    /** 初始化说话人角色为 'unknown'（ASR 完成时调用） */
    function _initSpeakerRoles(speakers) {
      const roles = {};
      for (const spk of speakers) {
        roles[spk.id] = 'unknown';
      }
      speakerRoles.value = roles;
      speakerMerges.value = {};
      segmentOverrides.value = {};
    }

    /** 从已保存的更正数据恢复编辑器状态（打开历史任务时） */
    function _restoreSpeakerCorrections(savedCorrections) {
      if (!savedCorrections) {
        // 没有保存的更正，从 ASR 结果初始化
        _initSpeakerRoles(allSpeakers.value);
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

    // ── 计算属性 ─────────────────────────────────────

    const stageName = computed(() => {
      if (llmStage.value === 'analyzing') return 'AI 面试分析中…';
      const p = progress.value;
      if (p === 0)  return '准备中…';
      if (p < 5)    return '准备音频…';
      if (p < 50)   return '说话人分段中…';
      if (p < 55)   return '音频分割中…';
      if (p < 95)   return '语音识别中…';
      if (p < 100)  return '收尾处理中…';
      return '识别完成';
    });

    const llmProgressWidth = computed(() =>
      llmStage.value ? '100%' : progress.value + '%'
    );

    const renderedMarkdown = computed(() =>
      llmChunks.value ? marked.parse(llmChunks.value) : ''
    );

    const speakerCount = computed(() =>
      result.value?.segments
        ? new Set(result.value.segments.map(s => s.speaker_id)).size
        : 0
    );

    const showPanels = computed(() =>
      !!(result.value || llmChunks.value || llmDone.value)
    );

    // ASR 完成且 LLM 尚未触发时，显示说话人编辑器
    const showSpeakerEditor = computed(() =>
      result.value !== null && !llmTriggered.value
    );

    // ── 文件操作 ─────────────────────────────────────

    function speakerInitial(id) {
      return String.fromCharCode(65 + (id % 26));
    }

    function onFileChange(e) {
      const f = e.target.files[0];
      if (f) _setFile(f);
    }
    function onDrop(e) {
      isDragging.value = false;
      const f = e.dataTransfer.files[0];
      if (f) _setFile(f);
    }
    function _setFile(f) {
      selectedFile.value = f;
      _resetResults();
    }
    function clearFile() {
      selectedFile.value = null;
      _resetResults();
      if (fileInput.value) fileInput.value.value = '';
    }
    function _resetResults() {
      result.value         = null;
      errorMsg.value       = '';
      progress.value       = 0;
      copied.value         = false;
      llmStage.value       = '';
      llmChunks.value      = '';
      llmDone.value        = false;
      llmTriggered.value   = false;
      currentTaskId.value  = '';
      speakerRoles.value   = {};
      speakerMerges.value  = {};
      segmentOverrides.value = {};
      editingSegIndex.value = null;
    }

    // ── SSE 事件分发 ─────────────────────────────────

    function _handleEvent(data) {
      if (data.error)            { errorMsg.value = data.error; return; }
      if (data.progress != null) { progress.value = data.progress; }
      if (data.result) {
        result.value = data.result;
        // ASR 完成，初始化说话人编辑器（全部默认 unknown）
        const segs = data.result.segments || [];
        const seen = new Set();
        const spks = [];
        for (const s of segs) {
          if (!seen.has(s.speaker_id)) { seen.add(s.speaker_id); spks.push({ id: s.speaker_id }); }
        }
        _initSpeakerRoles(spks);
      }
      if (data.llm_stage) { llmStage.value = data.llm_stage; }
      if (data.llm_chunk) { llmChunks.value += data.llm_chunk; }
      if (data.llm_done)  {
        llmDone.value  = true;
        llmStage.value = '';
        _loadHistory();
      }
    }

    // ── 订阅 SSE 流 ──────────────────────────────────

    /** 取消当前活跃的 SSE 连接（若存在），同时清除 loading 状态。 */
    function _cancelStream() {
      if (_streamAbortCtrl) {
        try { _streamAbortCtrl.abort(); } catch {}
        _streamAbortCtrl = null;
      }
      // 无论是否有活跃连接，都重置 loading——
      // 切换任务时必须保证 loading 是干净的初始状态
      loading.value = false;
    }

    async function _subscribeStream(taskId) {
      // 先中止旧连接，再建立新连接
      _cancelStream();

      currentTaskId.value = taskId;
      llmChunks.value = '';
      llmDone.value   = false;
      llmStage.value  = '';
      loading.value   = true;

      // 每次连接使用独立的 AbortController
      const ctrl = new AbortController();
      _streamAbortCtrl = ctrl;

      try {
        const res = await fetch(`/api/tasks/${taskId}/stream`, { signal: ctrl.signal });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const reader  = res.body.getReader();
        const decoder = new TextDecoder();
        let   buf     = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const parts = buf.split('\n\n');
          buf = parts.pop();
          for (const part of parts) {
            const line = part.trim();
            if (!line.startsWith('data:')) continue;
            try { _handleEvent(JSON.parse(line.slice(5).trim())); }
            catch { /* 忽略解析失败的帧 */ }
          }
        }
      } catch (e) {
        // AbortError 是主动切换任务触发的，不显示错误
        if (e.name !== 'AbortError') {
          errorMsg.value = e.message || '网络请求失败，请检查服务是否正常运行';
        }
      } finally {
        // 仅当本次连接仍是"当前连接"时才重置 loading
        if (_streamAbortCtrl === ctrl) {
          _streamAbortCtrl = null;
          loading.value = false;
        }
        _loadHistory();
      }
    }

    // ── 第一阶段：上传文件，启动 ASR ─────────────────

    async function doTranscribe() {
      if (!selectedFile.value || loading.value) return;
      loading.value = true;
      _resetResults();

      try {
        const fd = new FormData();
        fd.append('file', selectedFile.value);

        const res = await fetch('/api/tasks', { method: 'POST', body: fd });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const { task_id } = await res.json();
        _loadHistory();

        loading.value = false;
        await _subscribeStream(task_id);  // ASR 阶段 SSE（完成后自动关闭）

      } catch (e) {
        errorMsg.value = e.message || '网络请求失败，请检查服务是否正常运行';
        loading.value = false;
      }
    }

    // ── 第二阶段：触发 LLM 分析 ──────────────────────

    async function doAnalyze(skipCorrections = false) {
      if (!currentTaskId.value || loading.value) return;

      llmTriggered.value = true;
      loading.value      = true;
      llmChunks.value    = '';
      llmDone.value      = false;
      llmStage.value     = '';
      errorMsg.value     = '';

      // 构造更正数据（仅有用户更正才发送）
      const corrections = (!skipCorrections && userCorrected.value) ? {
        speaker_roles:     Object.fromEntries(
          Object.entries(speakerRoles.value).map(([k, v]) => [String(k), v])
        ),
        speaker_merges:    Object.fromEntries(
          Object.entries(speakerMerges.value).map(([k, v]) => [String(k), v])
        ),
        segment_overrides: Object.fromEntries(
          Object.entries(segmentOverrides.value).map(([k, v]) => [String(k), v])
        ),
        user_corrected: true,
      } : {};

      try {
        const res = await fetch(`/api/tasks/${currentTaskId.value}/analyze`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(corrections),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }

        _loadHistory();
        loading.value = false;
        // 订阅 LLM SSE（服务端已注册好 live_q，可直接连接）
        await _subscribeStream(currentTaskId.value);

      } catch (e) {
        errorMsg.value     = e.message || '启动 AI 分析失败';
        loading.value      = false;
        llmTriggered.value = false;
      }
    }

    // ── 历史任务列表 ─────────────────────────────────

    async function _loadHistory() {
      try {
        const res = await fetch('/api/tasks');
        if (!res.ok) return;
        historyTasks.value = await res.json();
      } catch { /* 静默忽略 */ }
    }

    async function openHistoryTask(task) {
      // 不再用 loading 守卫——先取消当前 SSE 再切换
      _cancelStream();
      _resetResults();
      selectedFile.value = null;
      if (fileInput.value) fileInput.value.value = '';

      if (task.status === 'done' || task.status === 'error') {
        // 已完成：直接拉取完整结果
        const res  = await fetch(`/api/tasks/${task.task_id}`).catch(() => null);
        if (!res?.ok) return;
        const full = await res.json();
        progress.value      = full.progress || 0;
        result.value        = full.asr_result || null;
        llmChunks.value     = full.llm_chunks || '';
        llmDone.value       = full.llm_done || false;
        errorMsg.value      = full.error_msg || '';
        currentTaskId.value = task.task_id;
        llmTriggered.value  = true;
        _restoreSpeakerCorrections(full.speaker_roles);
        return;
      }

      if (task.status === 'asr_done') {
        // ASR 完成，等待用户触发 LLM：展示结果和编辑器
        const res  = await fetch(`/api/tasks/${task.task_id}`).catch(() => null);
        if (!res?.ok) return;
        const full = await res.json();
        progress.value      = 100;
        result.value        = full.asr_result || null;
        currentTaskId.value = task.task_id;
        llmTriggered.value  = false;
        _restoreSpeakerCorrections(full.speaker_roles);
        return;
      }

      if (task.status === 'llm_running') {
        // LLM 进行中：重连 SSE
        llmTriggered.value = true;
        await _subscribeStream(task.task_id);
        return;
      }

      // asr_running / pending：重连 ASR SSE
      await _subscribeStream(task.task_id);
    }

    // ── 复制 ─────────────────────────────────────────

    async function copyAll() {
      await _copyText(result.value?.text || '');
      copied.value = true;
      setTimeout(() => (copied.value = false), 2000);
    }

    async function copyAnalysis() {
      await _copyText(llmChunks.value);
      copiedAnalysis.value = true;
      setTimeout(() => (copiedAnalysis.value = false), 2000);
    }

    async function _copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
    }

    // ── 历史任务辅助 ─────────────────────────────────

    function formatTaskTime(iso) {
      if (!iso) return '';
      try {
        const d = new Date(iso);
        const p = n => String(n).padStart(2, '0');
        return `${d.getMonth()+1}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
      } catch { return ''; }
    }

    function taskStatusLabel(task) {
      const map = {
        pending:     '排队中',
        asr_running: 'ASR 识别',
        asr_done:    '待分析',
        llm_running: 'AI 分析',
        done:        '完成',
        error:       '出错',
      };
      return map[task.status] || task.status;
    }

    function taskStatusClass(task) {
      if (task.status === 'done')     return 'ts-done';
      if (task.status === 'error')    return 'ts-error';
      if (task.status === 'asr_done') return 'ts-asr-done';
      return 'ts-running';
    }

    // ── 时间格式化 ───────────────────────────────────

    function formatTime(ms) {
      if (ms == null) return '';
      const t = ms / 1000;
      const m = Math.floor(t / 60);
      const s = (t % 60).toFixed(1).padStart(4, '0');
      return `${String(m).padStart(2, '0')}:${s}`;
    }

    // ── 初始化 ───────────────────────────────────────

    onMounted(() => {
      _loadHistory();
      // 点击空白处关闭 popup
      document.addEventListener('click', () => { editingSegIndex.value = null; });
    });

    // ── 暴露给模板 ───────────────────────────────────

    return {
      selectedFile, loading, progress, result, errorMsg,
      isDragging, copied, copiedAnalysis, fileInput, formats,
      llmStage, llmChunks, llmDone, llmTriggered, currentTaskId,
      historyTasks, historyOpen,
      // 说话人编辑
      speakerRoles, speakerMerges, segmentOverrides, editingSegIndex,
      allSpeakers, activeSpeakers, effectiveSegments, userCorrected,
      // 计算属性
      stageName, speakerCount, llmProgressWidth, renderedMarkdown,
      showPanels, showSpeakerEditor,
      // 方法
      speakerInitial, getEffectiveName, getEffectiveRole,
      setSpeakerRole, setSpeakerMerge,
      setSegmentOverride, clearSegmentOverride, toggleSegEdit, segEditOptions,
      onFileChange, onDrop, clearFile,
      doTranscribe, doAnalyze,
      copyAll, copyAnalysis, formatTime,
      openHistoryTask, formatTaskTime, taskStatusLabel, taskStatusClass,
    };
  },
}).mount('#app');
