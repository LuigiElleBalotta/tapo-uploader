// applied before the page paints, so there is no flash of the wrong theme
(function () {
  var saved = null;
  try { saved = localStorage.getItem('theme'); } catch (e) { /* storage may be blocked */ }
  var theme = saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme', theme);
})();
