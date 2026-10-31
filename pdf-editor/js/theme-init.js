/* Applies the site's light/dark choice (same key as the main site) before the editor paints. */
(function () {
  var t = null;
  try { t = localStorage.getItem('aiph_theme'); } catch (e) {}
  if (!t) { try { t = parent.document.documentElement.getAttribute('data-theme'); } catch (e) {} }
  document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
})();
