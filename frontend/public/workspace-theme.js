/* Keep the first paint in sync with the XHS workspace theme. */
(() => {
  let theme = 'dark';
  try {
    const saved = localStorage.getItem('workspace-theme') || localStorage.getItem('theme-mode');
    if (saved === 'light' || saved === 'dark') theme = saved;
    if (localStorage.getItem('workspace-sidebar-collapsed') === 'true') {
      document.documentElement.classList.add('workspace-sidebar-collapsed');
    }
  } catch { /* Storage is optional in private browsing. */ }
  document.documentElement.dataset.workspaceTheme = theme;
  window.addEventListener('storage', event => {
    if (event.key !== 'workspace-theme' && event.key !== 'theme-mode' && event.key !== null) return;
    let next = event.newValue;
    try { if (next !== 'light' && next !== 'dark') next = localStorage.getItem('workspace-theme') || localStorage.getItem('theme-mode'); } catch {}
    document.documentElement.dataset.workspaceTheme = next === 'light' ? 'light' : 'dark';
  });
})();
