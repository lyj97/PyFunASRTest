/**
 * useTask — 任务主流程
 *   - doTranscribe：上传文件，创建任务，订阅 SSE（后端自动 ASR→LLM）
 *   - doReAnalyze：携带说话人更正重新触发 LLM（随时可调用）
 *
 * 依赖：Vue 全局（ref、computed）
 */
const useTask = ({
  selectedFile, loading, progress, result, errorMsg,
  llmStage, llmChunks, llmDone, llmTriggered, currentTaskId,
  subscribeStream, loadHistory, setMode,
  buildCorrections, userCorrected,
}) => {
  const { ref, computed } = Vue;

  // ── 计算属性 ───────────────────────────────────

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

  const speakerCount = computed(() =>
    result.value?.segments
      ? new Set(result.value.segments.map(s => s.speaker_id)).size
      : 0
  );

  const renderedMarkdown = computed(() =>
    llmChunks.value ? marked.parse(llmChunks.value) : ''
  );

  const showPanels = computed(() =>
    !!(result.value || llmChunks.value || llmDone.value)
  );

  // ── 上传并启动 ASR（后端自动接续 LLM）─────────

  async function doTranscribe() {
    if (!selectedFile.value || loading.value) return;

    errorMsg.value     = '';
    progress.value     = 0;
    result.value       = null;
    llmChunks.value    = '';
    llmDone.value      = false;
    llmTriggered.value = false;
    loading.value      = true;

    // 切换到新任务模式
    setMode('new');

    try {
      const fd = new FormData();
      fd.append('file', selectedFile.value);

      const res = await fetch('/api/tasks', { method: 'POST', body: fd });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const { task_id } = await res.json();
      loadHistory();

      llmTriggered.value = true;  // 后端会自动接续 LLM，标记已触发
      loading.value = false;
      await subscribeStream(task_id);

    } catch (e) {
      errorMsg.value = e.message || '网络请求失败，请检查服务是否正常运行';
      loading.value  = false;
    }
  }

  // ── 重触发 LLM 分析（携带用户更正）─────────────

  async function doReAnalyze() {
    if (!currentTaskId.value || loading.value) return;

    loading.value   = true;
    llmChunks.value = '';
    llmDone.value   = false;
    llmStage.value  = '';
    errorMsg.value  = '';
    llmTriggered.value = true;

    const corrections = userCorrected.value ? buildCorrections() : {};

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

      loadHistory();
      loading.value = false;
      // 订阅新的 LLM SSE 流
      await subscribeStream(currentTaskId.value);

    } catch (e) {
      errorMsg.value = e.message || '启动 AI 分析失败';
      loading.value  = false;
    }
  }

  return {
    stageName, speakerCount, renderedMarkdown, showPanels,
    doTranscribe, doReAnalyze,
  };
};
