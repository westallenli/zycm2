/* Visible table actions open the existing server-validated maintenance forms. */
(() => {
  document.addEventListener('click', event => {
    const open = event.target.closest('[data-resource-open]');
    if (open) {
      const dialog = document.getElementById(open.dataset.resourceOpen);
      if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
      return;
    }
    const close = event.target.closest('[data-resource-close]');
    if (close) close.closest('dialog')?.close();
  });
})();
