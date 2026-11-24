/* Um endereço válido abre a passagem. Se houver um erro de digitação, deixamos
   a pessoa no índice com a busca atual para que possa corrigi-la. */
(() => {
  let pending;
  document.addEventListener("submit", async event => {
    const form = event.target.closest("[data-bible-jump]");
    if (!form || !window.fetch) return;
    event.preventDefault();
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    const query = form.elements.q;
    const status = form.querySelector("[data-jump-status]");
    const url = new URL(form.action);
    url.search = new URLSearchParams(new FormData(form)).toString();
    status.textContent = "";
    try {
      const response = await fetch(url, {headers:{Accept:"application/json"}, signal:controller.signal});
      const data = await response.json();
      if (!response.ok) {
        status.textContent = data.message || "Confira a referência e tente novamente.";
        query.focus();
      } else {
        location.assign(data.url);
      }
    } catch {
      if (!controller.signal.aborted) status.textContent = "Não foi possível abrir a passagem. Tente novamente.";
    } finally {
      if (pending === controller) pending = null;
    }
  });
  document.addEventListener("input", event => {
    if (!event.target.matches("[data-jump-query]")) return;
    pending?.abort();
    event.target.form.querySelector("[data-jump-status]").textContent = "";
  });
})();
