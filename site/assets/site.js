// Keep installation instructions and navigation usable without JavaScript.
document.querySelectorAll('[data-copy]').forEach((button) => {
  const command = document.getElementById(button.dataset.copy);
  const status = button.closest('.command')?.nextElementSibling;
  if (!command || !status) return;
  button.hidden = false;
  button.addEventListener('click', async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(command.textContent.trim());
      status.textContent = 'Install command copied.';
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'Command selected. Use your device’s Copy action.';
    }
  });
});

// This is an illustration of the TUI, not a Markdown parser or a live agent.
document.querySelectorAll('[data-markdown-demo]').forEach((demo) => {
  const button = demo.querySelector('[data-markdown-toggle]');
  const formatted = demo.querySelector('[data-markdown-formatted]');
  const raw = demo.querySelector('[data-markdown-raw]');
  if (!button || !formatted || !raw) return;
  button.hidden = false;
  button.addEventListener('click', () => {
    const showRaw = button.getAttribute('aria-pressed') !== 'true';
    button.setAttribute('aria-pressed', String(showRaw));
    formatted.hidden = showRaw;
    raw.hidden = !showRaw;
    button.textContent = showRaw ? 'F7 · Show formatted text' : 'F7 · Show raw Markdown';
  });
});
