/* A edição funciona com links normais. Quando há JavaScript, substituímos
   somente a abertura do livro para preservar o cenário, a fonte e o foco. */
(() => {
  "use strict";
  const reader = document.querySelector("[data-book-reader]");
  if (!reader) return;
  const smallScreen = window.matchMedia("(max-width:760px)");
  let turning;

  // A capa existe apenas na entrada principal da Bíblia. Removê-la após a
  // animação libera a camada 3D e sinaliza ao tutorial que a leitura abriu.
  const intro = reader.querySelector(".book-intro");
  if (intro) {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      intro.remove();
      document.dispatchEvent(new Event("bible:cover-opened"));
    };
    intro.addEventListener("animationend", event => {if (event.target === intro) finish();});
    // O foco que o índice ou o tutorial move por conta própria não encurta
    // a abertura. Um gesto deliberado deixa a página disponível de imediato.
    reader.addEventListener("pointerdown", event => {if (event.isTrusted) finish();}, {once:true});
    reader.addEventListener("keydown", event => {if (event.isTrusted) finish();}, {once:true});
    setTimeout(finish, 2400); // Também encerra se o navegador omitir animationend.
  }

  // As duas faces em movimento são decorativas. A folha nova já pode receber
  // foco e ser lida por leitores de tela durante a passagem da animação.
  function turnPaper(oldDesk, newDesk, direction) {
    if (!direction) return;
    const mobile = smallScreen.matches;
    const forward = direction === "next";
    const face = oldDesk.querySelector(mobile ? ".leaf-current" : forward ? ".recto" : ".verso");
    const reverse = newDesk.querySelector(mobile ? ".leaf-current" : forward ? ".verso" : ".recto");
    if (!face || !reverse) return;
    const sheet = document.createElement("div");
    sheet.className = "turning-sheet " + (forward ? "turn-next" : "turn-previous");
    sheet.inert = true;
    sheet.setAttribute("aria-hidden","true");
    for (const [page, side] of [[face,"front"],[reverse,"back"]]) {
      const copy = page.cloneNode(true);
      copy.className = "volume-page page-facsimile " + side;
      copy.removeAttribute("data-page-number");
      copy.querySelectorAll("[id]").forEach(node => node.removeAttribute("id"));
      copy.querySelectorAll(".verse-tools").forEach(node => node.remove());
      sheet.append(copy);
    }
    newDesk.querySelector(".volume-sheets").append(sheet);
    sheet.addEventListener("animationend", event => {if (event.target === sheet) sheet.remove();});
    // Também liberamos as cópias se a aba deixou de exibir a animação.
    setTimeout(() => sheet.remove(), 850);
  }

  // A navegação por setas também funciona para quem lê com teclado. Quando
  // um seletor, menu ou formulário tem o foco, as setas mantêm sua função.
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const open = reader.querySelector(".scripture-tools details[open]");
      if (open) { open.open = false; open.querySelector("summary").focus(); }
      return;
    }
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey ||
        event.target.closest("#indice-biblico,input,select,textarea,button,[contenteditable=true]") ||
        document.querySelector("dialog[open],.mobile-nav[open],.scripture-tools details[open],.bible-index.drawer-open")) return;
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const direction = event.key === "ArrowRight" ? "next" : "previous";
    const layout = smallScreen.matches ? "single" : "double";
    const link = reader.querySelector('.leaf-' + layout + '[data-book-turn="' + direction + '"]');
    if (link) { event.preventDefault(); link.click(); }
  });

  async function openBook(url, direction) {
    if (turning) turning.abort();
    const controller = new AbortController();
    turning = controller;
    const oldDesk = reader.querySelector("[data-book-desk]");
    oldDesk.setAttribute("aria-busy", "true");
    try {
      const activeChapter = oldDesk.dataset.openBook + "/" + oldDesk.dataset.openChapter;
      const response = await fetch(url, {signal:controller.signal, headers:{Accept:"text/html","X-Bible-Chapter":activeChapter}});
      if (!response.ok) throw new Error("Falha ao virar a página");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const newDesk = page.querySelector("[data-book-desk]");
      const newIndex = page.querySelector(".bible-tree");
      if (controller.signal.aborted) return;
      if (!newDesk) throw new Error("Leitor indisponível");
      const sameChapter = activeChapter === newDesk.dataset.openBook + "/" + newDesk.dataset.openChapter;
      if (!sameChapter && !newIndex) throw new Error("Índice indisponível");
      // O índice acompanha o livro e o capítulo novos. O seletor tipográfico
      // permanece no DOM para continuar ligado às preferências do leitor.
      oldDesk.replaceWith(newDesk);
      document.title = newDesk.dataset.pageTitle;
      if (location.href !== url.href) history.pushState({biblia:true}, "", url.href);
      document.dispatchEvent(new CustomEvent("bible:opened", {detail:{tree:newIndex,previousBook:oldDesk.dataset.openBook,previousChapter:oldDesk.dataset.openChapter}}));

      const cover = newDesk.querySelector(".volume-cover");
      if (direction) {
        cover.dataset.turn = direction;
        newDesk.dataset.turn = direction;
        turnPaper(oldDesk, newDesk, direction);
      }
      const focus = direction ? cover : newDesk.querySelector('[aria-current="location"]') || cover;
      if (focus) {
        focus.focus({preventScroll:true});
        const top = focus.getBoundingClientRect().top;
        if (top < -24 || top > window.innerHeight - 80)
          focus.scrollIntoView({block:"start", behavior:"smooth"});
      }
    } catch (error) {
      // A navegação convencional ainda pode recuperar uma resposta se a
      // conexão do fetch foi interrompida. Cliques substituídos só terminam.
      if (!controller.signal.aborted) location.assign(url.href);
    } finally {
      if (turning === controller) { turning = null; oldDesk.removeAttribute("aria-busy"); }
    }
  }

  reader.addEventListener("click", event => {
    const link = event.target.closest("a[data-book-turn],a[data-select-passage],a[data-tree-go]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.altKey ||
        event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    // Arrastar para selecionar texto deve abrir espaço para o marca-texto.
    // Também cancelamos a navegação nativa do link no fim desse gesto.
    if (link.matches(".scripture-verses [data-select-passage]") && window.getSelection()?.toString().trim()) return;
    openBook(new URL(link.href), link.dataset.bookTurn);
  });
  // Um gesto horizontal vira uma folha no celular. O eixo vertical e o gesto
  // de pinça continuam livres para rolar e ampliar passagens longas.
  let finger;
  reader.addEventListener("touchstart", event => {
    finger = null;
    if (!smallScreen.matches || event.touches.length !== 1 || !event.target.closest(".volume-page") ||
        event.target.closest(".verse-tools") || document.getSelection()?.toString()) return;
    const point = event.touches[0];
    if (point.clientX > 24 && point.clientX < innerWidth - 24) finger = {x:point.clientX,y:point.clientY};
  }, {passive:true});
  reader.addEventListener("touchcancel", () => { finger = null; }, {passive:true});
  reader.addEventListener("touchend", event => {
    if (!finger || event.touches.length) return;
    const point = event.changedTouches[0];
    const dx = point.clientX - finger.x, dy = point.clientY - finger.y;
    finger = null;
    if (Math.abs(dx) < 62 || Math.abs(dx) < Math.abs(dy)*1.8 || document.getSelection()?.toString()) return;
    const direction = dx < 0 ? "next" : "previous";
    const target = reader.querySelector('.leaf-single[data-book-turn="' + direction + '"]');
    if (target) {
      if (event.cancelable) event.preventDefault();
      openBook(new URL(target.href), direction);
    }
  }, {passive:false});
  // O botão Voltar restaura inclusive os controles nativos e o verso marcado.
  window.addEventListener("popstate", () => location.reload());

  // Um único menu aberto por vez deixa o leitor com espaço no celular.
  document.addEventListener("click", event => {
    reader.querySelectorAll(".scripture-tools details[open]").forEach(details => {
      if (!details.contains(event.target)) details.open = false;
    });
  });

})();
