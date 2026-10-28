/* Appearance only. Applies auto/light/dark before first paint; no data leaves this file. */
(function () {
  'use strict';

  var STORAGE_KEY = 'invoicedesk.theme';
  var MODES = ['auto', 'light', 'dark'];
  var LABELS = { auto: '自动', light: '浅色', dark: '深色' };
  var media =
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)')
      : null;

  function normalize(mode) {
    return MODES.indexOf(mode) >= 0 ? mode : 'auto';
  }

  function load() {
    try {
      return normalize(window.localStorage.getItem(STORAGE_KEY));
    } catch (error) {
      // file:// origins or a private profile may refuse storage; auto is safe.
      return 'auto';
    }
  }

  var mode = load();

  function resolve(value) {
    if (value !== 'auto') return value;
    return media && media.matches ? 'dark' : 'light';
  }

  function apply() {
    document.documentElement.setAttribute('data-theme', resolve(mode));
  }

  function setMode(value) {
    mode = normalize(value);
    apply();
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch (error) {
      // Session-only appearance is acceptable; never block the workbench.
    }
    return mode;
  }

  if (media) {
    var onChange = function () {
      if (mode === 'auto') apply();
    };
    if (typeof media.addEventListener === 'function') {
      media.addEventListener('change', onChange);
    } else if (typeof media.addListener === 'function') {
      media.addListener(onChange);
    }
  }

  apply();

  window.invoiceDeskTheme = {
    modes: MODES.slice(),
    labels: LABELS,
    get: function () {
      return mode;
    },
    resolved: function () {
      return resolve(mode);
    },
    set: setMode,
    cycle: function () {
      return setMode(MODES[(MODES.indexOf(mode) + 1) % MODES.length]);
    },
  };
})();
