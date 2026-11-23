// Applies the saved Theme and Text size before the page paints, so it does not flash.
// app.js keeps them current after that.
(function () {
  try {
    var saved = JSON.parse(localStorage.getItem('settings'));
    var root = document.documentElement;
    if (saved && (saved.theme === 'light' || saved.theme === 'dark')) root.dataset.theme = saved.theme;
    if (saved && (saved.size === 'small' || saved.size === 'large')) root.dataset.size = saved.size;
  } catch (e) { /* storage unavailable or damaged: keep the defaults */ }
})();
