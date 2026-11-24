/* A árvore usa elementos details nativos. Só trazemos do servidor os números
   de um capítulo quando a pessoa o abre; a edição tem mais de 38 mil versos. */
(() => {
  "use strict";
  const panel = document.querySelector("#indice-biblico");
  if (!panel) return;
  const small = matchMedia("(max-width:960px)");
  const books = panel.querySelector("#bible-index-books");
  const notes = panel.querySelector("#bible-index-notes");
  const tabs = panel.querySelectorAll("[data-index-tab]");
  const query = panel.querySelector("[data-jump-query]");
  let returnTo;
  let tree = books.querySelector(".bible-tree");
  const desk = () => document.querySelector("[data-book-desk]");
  document.documentElement.classList.add("index-ready");

  function chooseTab(name) {
    books.hidden = name !== "books";
    notes.hidden = name !== "notes";
    tabs.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.indexTab === name)));
  }
  function drawer(open, restoreFocus = true) {
    // No desktop o painel nunca sai da margem; o botão do menu apenas o foca.
    const modal = open && small.matches;
    panel.classList.toggle("drawer-open", modal);
    document.body.classList.toggle("index-opened", modal);
    panel.inert = small.matches && !modal;
    if (modal) { panel.setAttribute("role","dialog"); panel.setAttribute("aria-modal","true"); }
    else { panel.removeAttribute("role"); panel.removeAttribute("aria-modal"); }
    document.querySelectorAll("[data-open-bible-index]").forEach(link => link.setAttribute("aria-expanded",String(!small.matches || modal)));
    if (open) {
      panel.querySelector('[data-index-tab][aria-pressed="true"]').focus({preventScroll:true});
    } else if (returnTo && restoreFocus) {
      const visible = returnTo.getClientRects().length ? returnTo : document.querySelector(".index-open");
      visible?.focus({preventScroll:true});
    }
  }
  document.addEventListener("click", event => {
    const opener = event.target.closest("[data-open-bible-index]");
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (opener) {
      event.preventDefault();
      returnTo = opener;
      opener.closest(".mobile-nav")?.removeAttribute("open");
      chooseTab("books"); drawer(true); return;
    }
    if (event.target.closest("[data-index-close]")) { event.preventDefault(); drawer(false); }
    const tab = event.target.closest("[data-index-tab]");
    if (tab) chooseTab(tab.dataset.indexTab);
  });
  document.addEventListener("bible:show-notes", () => { chooseTab("notes"); drawer(true); });
  small.addEventListener("change", () => drawer(false, false));
  panel.addEventListener("keydown", event => {
    if (!panel.classList.contains("drawer-open") || document.querySelector("dialog[open]")) return;
    if (event.key === "Escape") { event.preventDefault(); drawer(false); return; }
    if (event.key !== "Tab") return;
    // O histórico pode ser alterado durante a navegação. Os controles são
    // coletados na hora para manter o foco dentro do menu aberto.
    const controls = [...panel.querySelectorAll("summary,a[href],input,button,select,textarea")].filter(el => !el.disabled && el.getClientRects().length);
    if (event.shiftKey && document.activeElement === controls[0]) {event.preventDefault();controls.at(-1)?.focus();}
    if (!event.shiftKey && document.activeElement === controls.at(-1)) {event.preventDefault();controls[0]?.focus();}
  });

  // Os dados vêm da edição pública, mas nomes e rótulos entram apenas como
  // texto. O template fornece a rota para manter suporte a subdiretórios.
  function make(tag, text, css) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (css) el.className = css;
    return el;
  }
  function populateBook(book) {
    const children = book.querySelector(".tree-chapters");
    if (children.querySelector(".tree-chapter")) return;
    const fragment = document.createDocumentFragment();
    for (const n of book.dataset.chapters.split(",")) {
      const chapter = make("details", undefined, "tree-chapter");
      chapter.dataset.chapter = n;
      const summary = make("summary");
      const caret = make("span","›","branch-caret");
      caret.setAttribute("aria-hidden","true");
      summary.append(caret, " Capítulo " + n); chapter.append(summary); fragment.append(chapter);
    }
    children.append(fragment);
  }
  async function populateChapter(chapter) {
    if (chapter.querySelector(".tree-passages")) return;
    const book = chapter.closest("[data-tree-book]");
    const n = chapter.dataset.chapter;
    const path = book.dataset.chapterUrl.replace(/999$/, n);
    const content = make("div",undefined,"tree-passages");
    const entry = make("a","Abrir capítulo ↗","chapter-entry");
    entry.href = path; entry.dataset.treeGo = "";
    content.append(entry); chapter.append(content);
    const status = make("small","Carregando versículos…"); status.setAttribute("role","status"); content.append(status);
    try {
      const url = tree.dataset.numberUrl.replace("/BOOK/999", "/" + book.dataset.treeBook + "/" + n);
      const response = await fetch(url);
      if (!response.ok) throw new Error();
      const verses = make("div",undefined,"verse-grid");
      for (const v of await response.json()) {
        const link = make("a",v.label);
        link.href = path + "?versiculo=" + v.number + "#v" + v.number;
        link.dataset.treeGo = ""; link.setAttribute("aria-label", book.dataset.bookName + " " + n + ":" + v.label);
        verses.append(link);
      }
      status.replaceWith(verses);
    } catch {
      status.textContent = "Não foi possível carregar os versículos. ";
      const retry = make("button","Tentar novamente"); retry.type = "button";
      retry.addEventListener("click", () => { content.remove(); populateChapter(chapter); });
      status.append(retry);
    }
  }
  // O evento toggle não borbulha. A captura também atende ramos criados
  // depois que a página carregou.
  panel.addEventListener("toggle", event => {
    const branch = event.target;
    if (!branch.open) return;
    if (branch.matches(".tree-book")) populateBook(branch);
    if (branch.matches(".tree-chapter")) populateChapter(branch);
  }, true);

  const fold = value => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  function filterBooks() {
    // O último número pode ser um capítulo. O primeiro em "1 João" faz parte
    // do nome e deve continuar valendo como filtro.
    const raw = fold(query?.value || "");
    const term = raw.replace(/\s+\d+\s*(?:[:,].*)?$/, "");
    const items = [...tree.querySelectorAll(".tree-book")];
    const matches = items.filter(book => fold(book.dataset.bookName).includes(term) || fold(book.dataset.treeBook) === term);
    // Números de página e abreviações aceitas na busca continuam navegáveis.
    for (const book of items) book.hidden = !!term && matches.length > 0 && !matches.includes(book);
    const groups = tree.querySelectorAll(".tree-division");
    for (const group of groups) {
      let item = group.nextElementSibling;
      while (item?.matches(".tree-book") && item.hidden) item = item.nextElementSibling;
      group.hidden = !item?.matches(".tree-book");
    }
  }
  query?.addEventListener("input", filterBooks);
  function positionTree() {
    // Desloca apenas a área rolável do índice. O título do livro continua no
    // campo de visão mesmo quando um link externo abre o último testamento.
    const marker = tree.querySelector('.verse-grid [aria-current="location"]') ||
                   tree.querySelector(".tree-book[open] .tree-chapter[open] > summary");
    if (!marker || !tree.clientHeight) return;
    const y = marker.getBoundingClientRect().top - tree.getBoundingClientRect().top;
    if (y < 0 || y > tree.clientHeight - 48) tree.scrollTop += y - tree.clientHeight * .32;
  }
  document.addEventListener("bible:opened", event => {
    const current = desk().dataset;
    if (current.openBook !== event.detail.previousBook || current.openChapter !== event.detail.previousChapter) {
      tree.replaceWith(event.detail.tree);
      tree = event.detail.tree;
      if (query) query.value = "";
    } else {
      tree.querySelectorAll('[aria-current="location"]').forEach(el => el.removeAttribute("aria-current"));
      const branch = tree.querySelector('[data-tree-book="' + current.openBook + '"] [data-chapter="' + current.openChapter + '"]');
      const active = branch?.querySelector('[href$="#v' + current.openVerse + '"]');
      if (active) active.setAttribute("aria-current","location");
      if (query?.value) {query.value = "";filterBooks();}
    }
    drawer(false,false);
    positionTree();
  });
  const landing = new URL(location.href);
  const requested = landing.searchParams.get("indice");
  chooseTab(requested === "notas" ? "notes" : "books");
  drawer(Boolean(requested || location.hash === "#indice-biblico"),false);
  requestAnimationFrame(positionTree);
})();
