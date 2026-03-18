/**
 * app.js — Vue 3 应用入口
 *
 * 组合各 composable，暴露模板所需的全部状态与方法。
 *
 * 流程（全自动流水线版）：
 *   1. 上传文件 → POST /api/tasks → 订阅 SSE
 *   2. 后端自动完成 ASR → LLM（无需前端手动触发）
 *   3. 用户随时可在转写面板展开说话人编辑器，修改后点「重新 AI 分析」
 *   4. 点击历史任务 → 上传区隐藏，显示任务详情，可重触发 LLM
 */
const { createApp, ref, computed, onMounted } = Vue;

createApp({
  setup() {
    // ── 共享核心状态 ─────────────────────────────
    const loading        = ref(false);
    const progress       = ref(0);
    const result         = ref(null);       // ASR 结果 { text, segments }
    const errorMsg       = ref('');
    const copied         = ref(false);
    const copiedAnalysis = ref(false);

    // ── LLM 状态 ─────────────────────────────────
    const llmStage     = ref('');
    const llmChunks    = ref('');
    const llmDone      = ref(false);
    const llmTriggered = ref(false);

    // ── 当前任务 ─────────────────────────────────
    const currentTaskId = ref('');

    /**
     * 页面模式：
     *   'new'     — 新建任务，显示上传区
     *   'history' — 查看历史任务，隐藏上传区，显示任务信息栏
     */
    const mode = ref('new');

    function setMode(m) { mode.value = m; }

    // 当前历史任务的基础信息（history 模式下填充）
    const currentTaskInfo = ref(null);   // { filename, created_at, status }

    // ── 重置全部结果 ──────────────────────────────
    function resetResults() {
      result.value       = null;
      errorMsg.value     = '';
      progress.value     = 0;
      copied.value       = false;
      llmStage.value     = '';
      llmChunks.value    = '';
      llmDone.value      = false;
      llmTriggered.value = false;
      currentTaskId.value = '';
      currentTaskInfo.value = null;
    }

    // ── 说话人 composable ─────────────────────────
    const speaker = useSpeaker(result);

    // ASR 完成回调：初始化说话人编辑器
    function onAsrDone(asrResult) {
      const seen = new Set();
      const spks = [];
      for (const s of (asrResult.segments || [])) {
        if (!seen.has(s.speaker_id)) { seen.add(s.speaker_id); spks.push({ id: s.speaker_id }); }
      }
      speaker.initSpeakerRoles(spks);
    }

    // LLM 完成回调：刷新历史列表
    function onLlmDone() { history_.loadHistory(); }

    // ── Stream composable ─────────────────────────
    const stream = useStream({
      result, progress, errorMsg, llmStage, llmChunks, llmDone,
      currentTaskId, loading,
      onAsrDone,
      onLlmDone,
    });

    // ── File composable ───────────────────────────
    const file = useFile(resetResults);

    // ── Task composable ───────────────────────────
    const task = useTask({
      selectedFile: file.selectedFile,
      loading, progress, result, errorMsg,
      llmStage, llmChunks, llmDone, llmTriggered, currentTaskId,
      subscribeStream: stream.subscribeStream,
      loadHistory: () => history_.loadHistory(),
      setMode,
      buildCorrections: speaker.buildCorrections,
      userCorrected: speaker.userCorrected,
    });

    // ── History composable ────────────────────────
    const history_ = useHistory({
      currentTaskId, result, progress, llmChunks, llmDone, errorMsg, loading,
      llmStage, llmTriggered,
      cancelStream:  stream.cancelStream,
      subscribeStream: stream.subscribeStream,
      resetResults,
      restoreSpeakerCorrections: speaker.restoreSpeakerCorrections,
      setMode,
      selectedFile: file.selectedFile,
      fileInput:    file.fileInput,
    });

    // ── 新建任务（从历史模式返回）────────────────
    function startNewTask() {
      stream.cancelStream();
      resetResults();
      speaker.resetSpeaker();
      setMode('new');
      file.clearFile();
    }

    // ── 复制 ─────────────────────────────────────

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

    // ── 时间格式化 ────────────────────────────────

    function formatTime(ms) {
      if (ms == null) return '';
      const t = ms / 1000;
      const m = Math.floor(t / 60);
      const s = (t % 60).toFixed(1).padStart(4, '0');
      return `${String(m).padStart(2, '0')}:${s}`;
    }

    // ── 初始化 ────────────────────────────────────

    onMounted(() => {
      history_.loadHistory();
      // 点击空白处关闭说话人 popup
      document.addEventListener('click', () => { speaker.editingSegIndex.value = null; });
    });

    // ── 暴露给模板 ────────────────────────────────

    return {
      // 基础状态
      loading, progress, result, errorMsg, copied, copiedAnalysis,
      llmStage, llmChunks, llmDone, llmTriggered, currentTaskId,
      mode, currentTaskInfo,

      // 文件
      ...file,

      // 说话人
      ...speaker,

      // 历史
      ...history_,

      // 任务计算属性 & 操作
      ...task,

      // 操作
      startNewTask,
      copyAll, copyAnalysis, formatTime,
    };
  },
}).mount('#app');
