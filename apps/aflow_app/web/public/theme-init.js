// Blocking same-origin script: apply the browser preference before styles paint.
try {
  var mode = localStorage.getItem('aflow.appearance');
  document.documentElement.dataset.theme = mode === 'dark' || (mode !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
} catch (_) {
  // CSS follows the OS if storage is unavailable.
}
