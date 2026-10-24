'use strict';
(() => {
  const DEFAULT = 224, MIN = 184, KEY = 'polymer.sidebarWidth.v1';
  const bounds = viewport => ({ min: MIN, max: Math.max(MIN, Math.min(360, viewport - 480)) });
  const clamp = (width, viewport) => Math.round(Math.max(MIN, Math.min(bounds(viewport).max, Number.isFinite(width) ? width : DEFAULT)));
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { bounds, clamp };
    return;
  }
  const handle = document.getElementById('sidebar-resizer');
  const root = document.documentElement;
  const viewport = () => document.documentElement.clientWidth;
  let preferred = DEFAULT, drag = null;
  try {
    const saved = localStorage.getItem(KEY);
    if (saved !== null && Number.isFinite(Number(saved))) preferred = Math.max(MIN, Math.min(360, Number(saved)));
  } catch { /* Storage may be unavailable; resizing still works for this page. */ }
  function apply() {
    const width = clamp(preferred, viewport()), limits = bounds(viewport());
    root.style.setProperty('--workspace-sidebar-width', `${width}px`);
    handle.setAttribute('aria-valuemin', limits.min);
    handle.setAttribute('aria-valuemax', limits.max);
    handle.setAttribute('aria-valuenow', width);
    handle.setAttribute('aria-valuetext', `${width}픽셀`);
    requestAnimationFrame(() => state.viewer?.resize());
  }
  function save() {
    try { localStorage.setItem(KEY, String(preferred)); } catch { /* Optional preference. */ }
  }
  function reset() { preferred = DEFAULT; apply(); save(); }
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0 || innerWidth <= 600) return;
    event.preventDefault();
    handle.focus({ preventScroll: true });
    drag = { id: event.pointerId, start: preferred };
    handle.setPointerCapture(event.pointerId);
    root.classList.add('sidebar-resizing');
  });
  handle.addEventListener('pointermove', event => {
    if (drag?.id !== event.pointerId) return;
    preferred = clamp(event.clientX, viewport());
    apply();
  });
  function finish(cancelled) {
    if (!drag) return;
    if (cancelled) preferred = drag.start;
    drag = null;
    root.classList.remove('sidebar-resizing');
    apply();
    if (!cancelled) save();
  }
  handle.addEventListener('pointerup', () => finish(false));
  handle.addEventListener('pointercancel', () => finish(true));
  handle.addEventListener('lostpointercapture', () => finish(true));
  handle.addEventListener('dblclick', reset);
  handle.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Escape'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Escape') { finish(true); return; }
    if (event.key === 'Home') return reset();
    preferred = event.key === 'End' ? bounds(viewport()).max : clamp(clamp(preferred, viewport()) + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 40 : 10), viewport());
    apply(); save();
  });
  document.getElementById('sidebar-reset').addEventListener('click', reset);
  document.querySelectorAll('.sidebar .nav-item').forEach(item => {
    item.title = item.querySelector('.nav-label').textContent.trim();
  });
  window.addEventListener('resize', () => { finish(true); apply(); });
  apply();
})();
