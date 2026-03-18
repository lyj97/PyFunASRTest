/**
 * useStream — SSE 流订阅、事件分发、连接管理
 *
 * 依赖：Vue 全局（ref）
 */
const useStream = ({ result, progress, errorMsg, llmStage, llmChunks, llmDone,
                     currentTaskId, loading, onAsrDone, onLlmDone }) => {
  const { ref } = Vue;

  // 当前 SSE 连接的 AbortController
  let _streamAbortCtrl = null;

  /** 取消当前活跃的 SSE 连接，重置 loading */
  function cancelStream() {
    if (_streamAbortCtrl) {
      try { _streamAbortCtrl.abort(); } catch {}
      _streamAbortCtrl = null;
    }
    loading.value = false;
  }

  /** 处理单条 SSE 事件数据 */
  function _handleEvent(data) {
    if (data.error)            { errorMsg.value = data.error; return; }
    if (data.progress != null) { progress.value = data.progress; }
    if (data.result) {
      result.value = data.result;
      // 通知外部 ASR 完成（初始化说话人编辑器）
      if (typeof onAsrDone === 'function') onAsrDone(data.result);
    }
    if (data.llm_stage) { llmStage.value = data.llm_stage; }
    if (data.llm_chunk) { llmChunks.value += data.llm_chunk; }
    if (data.llm_done)  {
      llmDone.value  = true;
      llmStage.value = '';
      if (typeof onLlmDone === 'function') onLlmDone();
    }
  }

  /**
   * 订阅指定任务的 SSE 流。
   * 调用前需确保 loading 和相关状态已正确设置。
   */
  async function subscribeStream(taskId) {
    // 先中止旧连接
    cancelStream();

    currentTaskId.value = taskId;
    loading.value       = true;

    const ctrl = new AbortController();
    _streamAbortCtrl   = ctrl;

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
    }
  }

  return { subscribeStream, cancelStream };
};
