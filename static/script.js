const form = document.getElementById('spotted-form');
const textarea = document.getElementById('content');
const counter = document.getElementById('count');
const status = document.getElementById('status');
const submitBtn = document.getElementById('submit-btn');

textarea.addEventListener('input', () => {
  counter.textContent = textarea.value.length;
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const content = textarea.value.trim();
  if (!content) return;

  submitBtn.disabled = true;
  status.textContent = '';
  status.className = 'status';

  try {
    const res = await fetch('/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const data = await res.json();

    if (!res.ok) {
      throw new Error(data.error || 'Coś poszło nie tak.');
    }

    textarea.value = '';
    counter.textContent = '0';
    status.textContent = 'Karteczka przypięta. Czeka na moderację.';
  } catch (err) {
    status.textContent = err.message;
    status.classList.add('error');
  } finally {
    submitBtn.disabled = false;
  }
});
