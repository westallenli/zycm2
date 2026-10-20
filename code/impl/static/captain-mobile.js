(() => {
  function reset(button) {
    if (!button.dataset.sending) return;
    button.textContent = button.dataset.sending;
    button.classList.remove('mc-sending');
    delete button.dataset.sending;
  }
  window.addEventListener('pageshow', () => document.querySelectorAll('button[data-sending]').forEach(reset));
  document.querySelectorAll('form[action="/callouts/reply"]').forEach(form => {
    const mode = form.querySelector('[name=can_depart]');
    const date = form.querySelector('[data-phone-eta]');
    const people = form.querySelector('[name=expected_people]');
    const reason = form.querySelector('[name=reason]');
    const eta = form.querySelector('[name=eta]');
    if (!mode || !date || !people || !reason || !eta) return;
    const sync = () => {
      const yes = mode.value === 'yes';
      date.required = people.required = yes;
      date.disabled = people.disabled = eta.disabled = !yes;
      reason.disabled = yes;
      reason.required = !yes;
    };
    mode.addEventListener('change', sync);
    sync();
    form.addEventListener('submit', () => {
      eta.value = mode.value === 'yes' && date.value ? new Date(date.value).toISOString() : '';
    });
  });
  document.addEventListener('submit', event => {
    const button = event.submitter;
    if (!button) return;
    if (button.dataset.sending) { event.preventDefault(); return; }
    button.dataset.sending = button.textContent;
    button.textContent = '正在提交…';
    button.classList.add('mc-sending');
    setTimeout(() => reset(button), 12000);
  });
})();
