/**
 * Vue 3 应用逻辑
 * 负责：文件上传、SSE 事件处理（ASR 进度 + LLM 流式分析）、复制等交互
 */
const { createApp, ref, computed } = Vue;

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
    const llmStage  = ref('');      // 'analyzing' | ''
    const llmChunks = ref('');      // 流式累积的 Markdown 文字
    const llmDone   = ref(false);   // LLM 是否完成

    const formats = ['WAV', 'MP3', 'M4A', 'FLAC', 'OGG', 'AAC', 'WMA'];

    // ── 计算属性 ─────────────────────────────────────

    /** 按钮文字 / 进度标签 */
    const stageName = computed(() => {
      if (llmStage.value === 'analyzing') return 'AI 面试分析中…';
      const p = progress.value;
      if (p === 0)  return '准备中…';
      if (p < 5)    return '准备音频…';
      if (p < 50)   return '说话人分段中…';
      if (p < 55)   return '音频分割中…';
      if (p < 95)   return '语音识别中…';
      if (p < 100)  return '收尾处理中…';
      return '识别完成，等待 AI 分析…';
    });

    /** 进度条宽度；LLM 阶段固定 100% */
    const llmProgressWidth = computed(() =>
      llmStage.value ? '100%' : progress.value + '%'
    );

    /** 将累积的 Markdown 文字渲染为 HTML */
    const renderedMarkdown = computed(() =>
      llmChunks.value ? marked.parse(llmChunks.value) : ''
    );

    /** 说话人数量（去重） */
    const speakerCount = computed(() =>
      result.value?.segments
        ? new Set(result.value.segments.map(s => s.speaker_id)).size
        : 0
    );

    /** 是否显示结果双栏区域 */
    const showPanels = computed(() =>
      !!(result.value || llmChunks.value || llmDone.value)
    );

    // ── 文件操作 ─────────────────────────────────────

    function speakerInitial(id) {
      return String.fromCharCode(65 + (id % 26)); // A B C …
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
      result.value    = null;
      errorMsg.value  = '';
      progress.value  = 0;
      copied.value    = false;
      llmStage.value  = '';
      llmChunks.value = '';
      llmDone.value   = false;
    }

    // ── 核心：发起识别请求，消费 SSE 流 ──────────────

    async function doTranscribe() {
      if (!selectedFile.value || loading.value) return;
      loading.value = true;
      _resetResults();

      try {
        const fd = new FormData();
        fd.append('file', selectedFile.value);

        const res = await fetch('/api/transcribe', { method: 'POST', body: fd });
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

          // 按 \n\n 切分 SSE 事件块
          const parts = buf.split('\n\n');
          buf = parts.pop(); // 不完整的末尾留到下次

          for (const part of parts) {
            const line = part.trim();
            if (!line.startsWith('data:')) continue;
            try {
              _handleEvent(JSON.parse(line.slice(5).trim()));
            } catch { /* 忽略解析失败的帧 */ }
          }
        }
      } catch (e) {
        errorMsg.value = e.message || '网络请求失败，请检查服务是否正常运行';
      } finally {
        loading.value = false;
      }
    }

    /** 分发 SSE 事件到对应状态 */
    function _handleEvent(data) {
      // ASR 阶段
      if (data.error)              { errorMsg.value  = data.error; return; }
      if (data.progress != null)   { progress.value  = data.progress; }
      if (data.result)             { result.value    = data.result; }

      // LLM 阶段
      if (data.llm_stage) { llmStage.value   = data.llm_stage; }
      if (data.llm_chunk) { llmChunks.value += data.llm_chunk; }
      if (data.llm_done)  { llmDone.value = true; llmStage.value = ''; }
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

    // ── 时间格式化 ───────────────────────────────────

    function formatTime(ms) {
      if (ms == null) return '';
      const t = ms / 1000;
      const m = Math.floor(t / 60);
      const s = (t % 60).toFixed(1).padStart(4, '0');
      return `${String(m).padStart(2, '0')}:${s}`;
    }

    // ── 暴露给模板 ───────────────────────────────────

    return {
      selectedFile, loading, progress, result, errorMsg,
      isDragging, copied, copiedAnalysis, fileInput, formats,
      llmStage, llmChunks, llmDone,
      stageName, speakerCount, llmProgressWidth, renderedMarkdown, showPanels,
      speakerInitial, onFileChange, onDrop, clearFile,
      doTranscribe, copyAll, copyAnalysis, formatTime,
    };
  },
}).mount('#app');
