/**
 * useFile — 文件选择、拖拽、清除
 * 仅在"新建任务"模式下使用。
 */
const useFile = (resetResults) => {
  const { ref } = Vue;

  const selectedFile = ref(null);
  const isDragging   = ref(false);
  const fileInput    = ref(null);

  const formats = ['WAV', 'MP3', 'M4A', 'FLAC', 'OGG', 'AAC', 'WMA'];

  function _setFile(f) {
    selectedFile.value = f;
    resetResults();
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

  function clearFile() {
    selectedFile.value = null;
    resetResults();
    if (fileInput.value) fileInput.value.value = '';
  }

  return { selectedFile, isDragging, fileInput, formats, onFileChange, onDrop, clearFile };
};
