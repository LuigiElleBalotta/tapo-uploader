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

// "Watch here": the player is created only when asked for, so the page stays light and YouTube is not contacted before.
document.addEventListener('click', function (event) {
  var button = event.target.closest ? event.target.closest('[data-watch]') : null;
  if (!button) { return; }
  var card = button.closest('.video-card');
  var stage = card && card.querySelector('.video-stage');
  if (!stage || card.querySelector('iframe, video')) { return; }
  var player;
  if (card.dataset.kind === 'youtube') {
    player = document.createElement('iframe');
    player.src = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(card.dataset.ref) + '?autoplay=1';
    player.setAttribute('allow', 'autoplay; fullscreen');
    player.setAttribute('allowfullscreen', '');
    player.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');  // YouTube refuses embeds without a referrer
    player.setAttribute('title', card.dataset.title || 'Video');
  } else {
    player = document.createElement('video');
    player.controls = true;
    player.autoplay = true;
    player.preload = 'metadata';
    player.src = card.dataset.ref;
  }
  player.className = 'video-player';
  stage.textContent = '';
  stage.appendChild(player);
  button.hidden = true;
});
