/* O servidor sorteia e registra cada versículo. Este arquivo cuida da troca
   sem recarregar a página, das poses do desenho e dos controles de leitura. */
"use strict";
const root = document.documentElement;
const cookieBox = document.querySelector("#cookie-box");
const face = document.querySelector("#reader-face");
const size = document.querySelector("#reader-size");
const fonts = {classic: 'Baskervville,"Palatino Linotype",Georgia,serif', serif:'Georgia,serif', sans:'"DM Sans","Segoe UI",sans-serif'};
const sizes = {normal:"", large:"clamp(2.35rem,4vw,3.5rem)", larger:"clamp(2.8rem,5vw,4.2rem)"};
const preferenceKey = "biblia.aparencia.v1";
// O mesmo aviso atende aos favoritos e às conversas. Usar textContent impede
// que uma mensagem do servidor ou um título seja interpretado como marcação.
function showToast(message, error = false) {
  const region = document.querySelector("#site-toasts");
  const note = document.createElement("div");
  note.className = "toast-item";
  note.toggleAttribute("data-error", error);
  note.textContent = message;
  region.replaceChildren(note);
  setTimeout(() => note.remove(), 3600);
}
window.addEventListener("site:toast", event => {
  if (event.detail?.message) showToast(event.detail.message, Boolean(event.detail.error));
});
for (const flash of document.querySelectorAll(".toast-flash")) {
  showToast(flash.textContent.trim());
  flash.remove();
}

function markFavorite(button, saved) {
  button.disabled = saved;
  button.setAttribute("aria-pressed", String(saved));
  button.querySelector("[data-favorite-label]").textContent = saved ? "Nos meus favoritos" : "Favoritar versículo";
}

// A delegação continua funcionando quando se vira a página da Bíblia e os
// controles antigos são substituídos. Não há navegação ao salvar um marcador.
document.addEventListener("submit", async event => {
  const form = event.target.closest("form[data-favorite-form]");
  if (!form || !window.fetch) return;
  event.preventDefault();
  const button = form.querySelector("[data-favorite-button]");
  if (button.disabled) return;
  const data = new FormData(form);
  const verseId = form.dataset.verseId;
  button.disabled = true;
  try {
    const response = await fetch(form.action, {method:"POST", body:data, headers:{Accept:"application/json"}});
    if (!response.ok || !response.headers.get("Content-Type")?.includes("application/json")) throw Error();
    const result = await response.json();
    if (!result.saved) throw Error();
    for (const visible of document.querySelectorAll("[data-favorite-form]")) {
      // Uma pessoa pode já ter sorteado outro verso enquanto este POST viajava.
      if (visible.dataset.verseId === String(result.verse_id)) {
        markFavorite(visible.querySelector("[data-favorite-button]"), true);
      }
    }
    showToast(result.message);
  } catch {
    if (form.isConnected && form.dataset.verseId === verseId && button.getAttribute("aria-pressed") !== "true") {
      button.disabled = false;
    }
    showToast("Não foi possível salvar o versículo. Confira sua sessão e tente novamente.", true);
  }
});

// Fechar o menu compacto ao sair dele facilita alternar entre o menu do site
// e as conversas do chat em uma tela pequena. O elemento details funciona sem JS.
const compactMenu = document.querySelector(".mobile-nav");
document.addEventListener("click", event => {
  if (compactMenu.open && !compactMenu.contains(event.target)) compactMenu.open = false;
});
compactMenu.addEventListener("keydown", event => {
  if (event.key === "Escape") { compactMenu.open = false; compactMenu.querySelector("summary").focus(); }
});

function applyReading(save) {
  if (!face || !size) return;
  root.style.setProperty("--reading-face", fonts[face.value]);
  root.style.setProperty("--reading-size", sizes[size.value]);
  // Bloquear o armazenamento opcional pelo navegador não impede a leitura.
  if (save && root.dataset.cookies === "preferences") {
    try { localStorage.setItem(preferenceKey, JSON.stringify([face.value, size.value])); } catch {}
  }
}
if (face && size) {
  try {
    const options = root.dataset.cookies === "preferences" && JSON.parse(localStorage.getItem(preferenceKey));
    if (Array.isArray(options) && Object.hasOwn(fonts,options[0]) && Object.hasOwn(sizes,options[1])) {
      [face.value, size.value] = options;
    }
  } catch { /* O navegador pode apagar os dados entre duas visitas. */ }
  applyReading(false);
  [face,size].forEach(select => select.addEventListener("change", () => applyReading(true)));
}

document.querySelector("[data-cookies-open]").addEventListener("click", () => {
  cookieBox.hidden = false;
  cookieBox.querySelector("button").focus();
});
cookieBox.querySelector("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form, event.submitter);
  try {
    const response = await fetch(form.action, {method:"POST",body:data,headers:{Accept:"application/json"}});
    if (!response.ok) throw Error();
    root.dataset.cookies = (await response.json()).choice;
    if (root.dataset.cookies === "essential") {
      try { localStorage.removeItem(preferenceKey); } catch {}
    } else applyReading(true);
    cookieBox.hidden = true;
  } catch {
    // O envio HTML mantém o formulário usável se a atualização em segundo plano falhar.
    const choice = document.createElement("input");
    choice.type = "hidden"; choice.name = "choice"; choice.value = data.get("choice");
    form.append(choice); form.submit();
  }
});

const draw = document.querySelector("#draw");
if (draw) {
  const scene = document.querySelector("#illustration");
  const jesus = scene.querySelector("#shepherd");
  const lamb = scene.querySelector(".lamb-one");
  const studyLink = document.querySelector("#study-link");
  let settleTimer;
  // Uma resposta tardia não pode substituir uma leitura mais recente. O botão
  // aguarda a operação, e o link de estudo passa a apontar para o mesmo texto.
  draw.addEventListener("click", async () => {
    clearTimeout(settleTimer);
    draw.disabled = true;
    document.querySelector("#draw-error").textContent = "";
    jesus.src = "/static/personagens/jesus-le.png";
    try {
      const response = await fetch("/leitor/novo", {method:"POST",headers:{"X-CSRF-Token":draw.dataset.token}});
      if (!response.ok) throw Error();
      const verse = await response.json();
      document.querySelector("#verse-text").textContent = verse.text;
      document.querySelector("#verse-reference").textContent = verse.reference;
      studyLink.href = verse.study_url;
      // Passagem, favorito e estudo mudam juntos. O recibo preserva o instante
      // deste sorteio para que o perfil exiba a data correta ao salvar.
      document.querySelector("#bible-link").href = verse.bible_url;
      const favoriteReceipt = document.querySelector("#favorite-receipt");
      if (favoriteReceipt) {
        favoriteReceipt.value = verse.draw_token || "";
        favoriteReceipt.form.dataset.verseId = String(verse.verse_id);
        markFavorite(favoriteReceipt.form.querySelector("[data-favorite-button]"), verse.saved);
        // A pessoa pode ter saído da conta em outra aba entre dois sorteios.
        favoriteReceipt.form.hidden = !verse.draw_token;
      }
      const reading = document.querySelector("#reading");
      // O sorteio reaproveita o mesmo botão. Reiniciamos o halo quando a
      // referência muda. Na Bíblia o botão já nasce novo a cada seleção.
      studyLink.classList.remove("ai-attention");
      void studyLink.offsetWidth;
      studyLink.classList.add("ai-attention");
      reading.classList.remove("reading-new");
      requestAnimationFrame(() => reading.classList.add("reading-new"));
      jesus.src = "/static/personagens/jesus-ensina.png";
      lamb.src = "/static/personagens/ovelha-salta.png";
      scene.classList.add("drawing");
    } catch {
      document.querySelector("#draw-error").textContent = "O sorteio falhou. Tente novamente.";
    } finally {
      draw.disabled = false;
      settleTimer = setTimeout(() => {
        scene.classList.remove("drawing");
        jesus.src = "/static/personagens/jesus-acolhe.png";
        lamb.src = "/static/personagens/ovelha-passo-1.png";
      },1100);
    }
  });
}
