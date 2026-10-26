document.addEventListener('DOMContentLoaded', function () {
  var button = document.getElementById('theme-toggle');
  if (!button) { return; }
  button.addEventListener('click', function () {
    var root = document.documentElement;
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) { /* storage may be blocked */ }
  });
});
