/**
 * useHistory — 历史任务列表加载、打开历史任务
 *
 * 依赖：Vue 全局（ref）
 */
const useHistory = ({
  currentTaskId, result, progress, llmChunks, llmDone, errorMsg, loading,
  llmStage, llmTriggered,
  cancelStream, subscribeStream,
  resetResults, restoreSpeakerCorrections,
  setMode,      // 切换 'new' | 'history' 模式
  selectedFile, fileInput,
}) => {
  const { ref } = Vue;

  const historyTasks = ref([]);
  const historyOpen  = ref(true);

  /** 加载历史任务列表（静默失败） */
  async function loadHistory() {
    try {
      const res = await fetch('/api/tasks');
      if (!res.ok) return;
      historyTasks.value = await res.json();
    } catch { /* 静默忽略 */ }
  }

  /** 点击历史任务：切换到历史模式，加载对应任务 */
  async function openHistoryTask(task) {
    cancelStream();
    resetResults();
    // 切换到历史模式（隐藏上传区）
    setMode('history');
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
      restoreSpeakerCorrections(full.speaker_roles);
      return;
    }

    if (task.status === 'asr_done') {
      // ASR 完成但 LLM 尚未开始：展示转写结果，等用户触发重分析
      const res  = await fetch(`/api/tasks/${task.task_id}`).catch(() => null);
      if (!res?.ok) return;
      const full = await res.json();
      progress.value      = 100;
      result.value        = full.asr_result || null;
      currentTaskId.value = task.task_id;
      llmTriggered.value  = false;
      restoreSpeakerCorrections(full.speaker_roles);
      return;
    }

    if (task.status === 'llm_running') {
      // LLM 进行中：重连 SSE
      llmTriggered.value = true;
      await subscribeStream(task.task_id);
      return;
    }

    // asr_running / pending：重连 ASR SSE（会自动接续 LLM）
    await subscribeStream(task.task_id);
  }

  // ── 辅助函数 ──────────────────────────────────

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

  return {
    historyTasks, historyOpen,
    loadHistory, openHistoryTask,
    formatTaskTime, taskStatusLabel, taskStatusClass,
  };
};
