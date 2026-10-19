(() => {
  const thread = document.getElementById('thread');
  const form = document.getElementById('message-form');
  if (!thread || !form) return;

  const scrollToLatest = () => { thread.scrollTop = thread.scrollHeight; };
  scrollToLatest();

  form.addEventListener('htmx:afterRequest', (event) => {
    if (event.detail.successful) {
      form.reset();
      document.getElementById('message-error')?.classList.add('d-none');
      scrollToLatest();
    }
  });
  form.addEventListener('htmx:responseError', () => {
    document.getElementById('message-error')?.classList.remove('d-none');
  });
  thread.addEventListener('htmx:afterSwap', () => {
    const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 180;
    if (nearBottom) scrollToLatest();
  });
})();
