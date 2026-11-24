/* Cada marcação pertence ao perfil autenticado. O servidor valida a passagem
   e o titular. O navegador guarda a lista apenas na memória desta página. */
(() => {
  "use strict";
  const dialog = document.querySelector("#bible-note-editor");
  if (!dialog) return;
  const form = dialog.querySelector("form");
  const fields = form.elements;
  const panel = document.querySelector(".note-collection");
  const tree = panel.querySelector("[data-note-folders]");
  const filter = panel.querySelector("[data-note-search]");
  const listStatus = panel.querySelector("[data-note-list-status]");
  const notify = (message, error = false) => window.dispatchEvent(new CustomEvent("site:toast", {detail:{message,error}}));
  const selectionButton = document.querySelector("[data-note-selection]");
  const base = dialog.dataset.notesUrl;
  const readOnly = dialog.dataset.noteReadonly === "true";
  let notes = [];
  let draft, selectedText, chapterNumbers = [], passageFilter;
  let selectionFrame = 0, listRequest = 0, saving = false, loaded = false;
  const colors = new Set(["gold","sage","rose","blue","none"]);
  const passageSelector = "[data-select-passage][data-note-book]";
  const visiblePassages = () => Array.from(document.querySelectorAll(passageSelector))
    .filter(link => !link.closest(".turning-sheet") && link.getClientRects().length);
  const allPassages = () => Array.from(document.querySelectorAll(passageSelector))
    .filter(link => !link.closest(".turning-sheet"));

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function passage(link) {
    const data = link.dataset;
    return {book:data.noteBook, book_name:data.noteBookName, chapter:Number(data.noteChapter),
      start_verse:Number(data.noteStart), end_verse:Number(data.noteEnd), start_offset:null, end_offset:null};
  }
  function sameChapter(first, second) { return first.book === second.book && first.chapter === second.chapter; }
  function overlaps(first, second) {
    return sameChapter(first,second) && first.start_verse <= second.end_verse && second.start_verse <= first.end_verse;
  }
  function reference(entry) {
    return entry.book_name + " " + entry.chapter + ":" + entry.start_verse +
      (entry.start_verse === entry.end_verse ? "" : "–" + entry.end_verse);
  }
  function searchKey(value) {
    return String(value || "").toLocaleLowerCase("pt-BR").normalize("NFD").replace(/[\u0300-\u036f]/g,"");
  }

  // O explorador agrupa os registros por livro e capítulo. O conteúdo pessoal
  // sempre entra como texto, inclusive quando a nota contém sinais de HTML.
  function folder(parent, key, title, className, opened) {
    const group = element("details",className);
    group.dataset.folder = key;
    group.open = opened;
    const label = element("summary","",title);
    const count = element("span","note-total","0");
    label.append(count);
    const children = element("div","note-branch");
    group.append(label,children);
    parent.append(group);
    return {children,count,total:0};
  }
  function renderTree() {
    const opened = new Set(Array.from(tree.querySelectorAll("details[open]"),group=>group.dataset.folder));
    const existing = new Set(Array.from(tree.querySelectorAll("details"),group=>group.dataset.folder));
    const query = searchKey(filter.value.trim());
    const active = visiblePassages().find(link=>link.hasAttribute("aria-current")) || visiblePassages()[0];
    const groups = new Map();
    const fragment = document.createDocumentFragment();
    let count = 0;
    for (const note of notes) {
      if (passageFilter && !overlaps(note,passageFilter)) continue;
      if (query && !searchKey([note.reference,note.title,note.body,note.excerpt].join(" ")).includes(query)) continue;
      const autoOpen = Boolean(query || passageFilter);
      const bookKey = note.book;
      const chapterKey = note.book + "/" + note.chapter;
      if (!groups.has(bookKey)) {
        const expand = autoOpen || opened.has(bookKey) || (!existing.has(bookKey) && active?.dataset.noteBook === bookKey);
        groups.set(bookKey,folder(fragment,bookKey,note.book_name,"note-book",expand));
      }
      const book = groups.get(bookKey);
      if (!groups.has(chapterKey)) {
        const isCurrent = active && sameChapter(passage(active),note);
        groups.set(chapterKey,folder(book.children,chapterKey,"Capítulo "+note.chapter,"note-chapter",
          autoOpen || opened.has(chapterKey) || (!existing.has(chapterKey) && isCurrent)));
      }
      const chapter = groups.get(chapterKey);
      book.count.textContent = String(++book.total);
      chapter.count.textContent = String(++chapter.total);
      const row = element("div","note-row");
      const link = element("a");
      link.href = note.url;
      link.dataset.selectPassage = "";
      link.dataset.noteLink = note.id;
      if (active?.hasAttribute("aria-current") && overlaps(note,passage(active))) link.setAttribute("aria-current","location");
      const dot = element("span","note-dot");
      dot.dataset.tone = note.color;
      dot.classList.toggle("quick-ribbon",note.quick);
      dot.setAttribute("aria-hidden","true");
      link.append(dot,element("strong","",note.title || note.reference));
      if (note.title || note.body || note.excerpt) link.append(element("small","",note.title ? note.reference : (note.body || note.excerpt).slice(0,65)));
      const edit = element("button");
      edit.type = "button";
      edit.dataset.noteEdit = note.id;
      edit.setAttribute("aria-label",(readOnly ? "Abrir marcação: " : "Editar marcação: ")+(note.title || note.reference));
      edit.title = edit.getAttribute("aria-label");
      edit.append(panel.querySelector("#note-edit-icon").content.cloneNode(true));
      row.append(link,edit);
      chapter.children.append(row);
      count++;
    }
    panel.querySelector("[data-note-search-wrap]").hidden = !notes.length;
    tree.replaceChildren(fragment);
    const clear = panel.querySelector("[data-note-clear-filter]");
    clear.hidden = !passageFilter;
    if (passageFilter) clear.textContent = (passageFilter.wholeChapter
      ? passageFilter.book_name + " " + passageFilter.chapter : reference(passageFilter)) + " ×";
    if (loaded) listStatus.textContent = count ? "" : notes.length ? "Nenhuma marcação encontrada." :
      readOnly ? "Nenhuma marcação salva." : "Use “Marcar página” no livro para voltar a este ponto depois.";
  }

  function showBookmarks(entry, id) {
    // A aba pessoal mostra cada referência com um trecho, agrupada na ordem
    // do livro. No celular o mesmo comando abre a gaveta de navegação.
    passageFilter = entry;
    filter.value = "";
    renderTree();
    document.dispatchEvent(new Event("bible:show-notes"));
    if (id) {
      const link = tree.querySelector('[data-note-link="' + id + '"]');
      if (link) {
        link.focus({preventScroll:true});
        link.scrollIntoView({block:"nearest"});
      }
    }
  }

  function paintIndex() {
    // Os livros estão sempre na árvore. Capítulos e versículos são montados
    // sob demanda; refazemos só os indicadores dos ramos existentes no DOM.
    const books = document.querySelector(".bible-tree");
    const total = document.querySelector("[data-bookmark-count]");
    total.hidden = !notes.length;
    total.textContent = notes.length;
    const byBook = new Map();
    for (const note of notes) {
      if (!byBook.has(note.book)) byBook.set(note.book,new Map());
      const chapters = byBook.get(note.book);
      if (!chapters.has(note.chapter)) chapters.set(note.chapter,[]);
      chapters.get(note.chapter).push(note);
    }
    for (const book of books.querySelectorAll("[data-tree-book]")) {
      const chapters = byBook.get(book.dataset.treeBook);
      book.firstElementChild.toggleAttribute("data-with-bookmarks",Boolean(chapters));
      for (const branch of book.querySelectorAll("[data-chapter]")) {
        const marks = chapters?.get(Number(branch.dataset.chapter)) || [];
        branch.firstElementChild.toggleAttribute("data-with-bookmarks",Boolean(marks.length));
        const passages = branch.querySelector(".tree-passages");
        if (!passages) continue;
        let jump = passages.querySelector("[data-chapter-marks]");
        if (!marks.length) jump?.remove();
        else {
          if (!jump) {
            jump = element("button","chapter-marks");
            jump.type = "button";
            jump.dataset.chapterMarks = "";
            passages.insertBefore(jump,passages.firstChild);
          }
          jump.textContent = marks.length + (marks.length === 1 ? " marca-página" : " marca-páginas") + " ↗";
        }
        for (const link of passages.querySelectorAll(".verse-grid a")) {
          const verse = Number(link.hash.slice(2));
          const marked = marks.some(note=>verse >= note.start_verse && verse <= note.end_verse);
          link.toggleAttribute("data-with-bookmarks",marked);
          if (marked) link.setAttribute("aria-description","Passagem com marca-página.");
          else link.removeAttribute("aria-description");
        }
      }
    }
  }

  function paintShortcut() {
    const shortcut = document.querySelector("[data-quick-bookmark]");
    if (!shortcut) return;
    const loc = shortcut.elements;
    const note = notes.find(item => item.quick && item.book === loc.book.value &&
      item.chapter === Number(loc.chapter.value) && item.start_verse === Number(loc.start_verse.value));
    const button = shortcut.querySelector("button");
    button.toggleAttribute("data-saved",Boolean(note));
    button.querySelector("span").textContent = note ? "Ver marca-página" : "Marcar página";
    if (note) button.title = "Ver " + note.reference + " no índice";
    else button.title = "Guardar esta passagem no índice";
    return note;
  }

  // Mesmo botão, dois estados: guardar sem sair do livro, e visitar a fita
  // no índice. Mesmo que a conexão caia, repetir a ação mantém uma só fita.
  document.addEventListener("submit", async event => {
    const shortcut = event.target.closest("[data-quick-bookmark]");
    if (!shortcut) return;
    event.preventDefault();
    const button = shortcut.querySelector("button");
    if (button.disabled) return;
    const saved = paintShortcut();
    if (saved) {showBookmarks(saved,saved.id);return;}
    button.disabled = true;
    try {
      const response = await fetch(shortcut.action,{method:"POST",headers:{Accept:"application/json"},body:new FormData(shortcut)});
      if (!response.headers.get("Content-Type")?.includes("application/json")) throw Error("Entre novamente para marcar a página.");
      const result = await response.json();
      if (!response.ok) throw Error(result.message || "Não foi possível guardar a página.");
      ++listRequest;
      notes = notes.filter(note=>note.id !== result.note.id);
      notes.push(result.note);
      loaded = true;
      paint();renderTree();notify(result.message);
      await loadNotes();
    } catch (error) {
      notify(error instanceof TypeError ? "Sem conexão. Tente marcar a página novamente." : error.message,true);
    } finally {button.disabled = false;}
  });
  document.addEventListener("bible:index-branch",paintIndex);
  document.addEventListener("click",event=>{
    const link = event.target.closest("[data-chapter-marks]");
    if (!link) return;
    const book = link.closest("[data-tree-book]").dataset;
    showBookmarks({book:book.treeBook,book_name:book.bookName,chapter:Number(link.closest("[data-chapter]").dataset.chapter),
      start_verse:1,end_verse:999,wholeChapter:true});
  });

  // O texto do acervo pode ter capitular e números em sobrescrito. Os offsets
  // são medidos somente nas palavras, em pontos de código Unicode como no Python.
  function textNodes(link) {
    const walk = document.createTreeWalker(link,NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walk.nextNode()) if (!walk.currentNode.parentElement.closest("sup")) nodes.push(walk.currentNode);
    return nodes;
  }
  function paintVerse(link, matches) {
    for (const old of link.querySelectorAll("mark.note-ink")) old.replaceWith(...old.childNodes);
    link.normalize();
    const ink = matches.filter(note => colors.has(note.color) && note.color !== "none");
    if (!ink.length) return;
    const loc = passage(link);
    let position = 0;
    for (const node of textNodes(link)) {
      const chars = Array.from(node.data);
      const start = position, end = position + chars.length;
      position = end;
      // Fronteiras de todas as marcações permitem sobreposição sem duplicar
      // letras. A última tinta aplicada aparece por cima; todas ficam no índice.
      const ranges = ink.map(note => ({
        note, from:note.start_verse === loc.start_verse && note.start_offset != null ? note.start_offset : 0,
        to:note.end_verse === loc.end_verse && note.end_offset != null ? note.end_offset : Infinity
      })).filter(r=>r.from < end && r.to > start).sort((a,b)=>a.note.updated_at-b.note.updated_at || a.note.id-b.note.id);
      if (!ranges.length) continue;
      const bounds = new Set([start,end]);
      for (const range of ranges) { bounds.add(Math.max(start,range.from)); bounds.add(Math.min(end,range.to)); }
      const ordered = Array.from(bounds).sort((a,b)=>a-b);
      const result = document.createDocumentFragment();
      for (let i=1;i<ordered.length;i++) {
        const from = ordered[i-1], to = ordered[i];
        const content = chars.slice(from-start,to-start).join("");
        const coverage = ranges.filter(range=>range.from < to && range.to > from);
        if (coverage.length) {
          const mark = element("mark","note-ink",content);
          mark.dataset.tone = coverage.at(-1).note.color;
          mark.toggleAttribute("data-overlap",coverage.length>1);
          result.append(mark);
        } else result.append(document.createTextNode(content));
      }
      node.replaceWith(result);
    }
  }
  function paint() {
    paintIndex();
    paintShortcut();
    for (const link of allPassages()) {
      const matches = notes.filter(note=>overlaps(note,passage(link)));
      paintVerse(link,matches);
      const tools = link.closest("li").querySelector(".verse-tools");
      if (!tools) continue;
      tools.querySelector("[data-note-related]")?.remove();
      if (!matches.length) continue;
      const related = element("button","",matches.length === 1 ? (readOnly ? "Abrir marcação" : "Editar marcação") : "Ver "+matches.length+" marcações");
      related.dataset.noteRelated = "";
      related.type = "button";
      tools.append(related);
    }
  }
  async function loadNotes() {
    const current = ++listRequest;
    try {
      const response = await fetch(base,{headers:{Accept:"application/json"},cache:"no-store"});
      const payload = await response.json();
      if (current !== listRequest) return;
      if (!response.ok) throw Error(payload.message || "Não foi possível carregar as marcações.");
      notes = payload.notes;
      loaded = true;
      panel.querySelector("[data-note-reload]").hidden = true;
      paint(); renderTree();
    } catch (error) {
      if (current !== listRequest) return;
      listStatus.textContent = error.message === "Failed to fetch" ? "Não foi possível carregar suas marcações." : error.message;
      panel.querySelector("[data-note-reload]").hidden = false;
    }
  }

  // O Range nativo também cobre textos repartidos pela capitular ou por outro
  // marca-texto. Comparamos cada nó com o intervalo antes de somar offsets.
  function selectedPart(link,range) {
    let position = 0, first = null, last = null;
    const parts = [];
    for (const node of textNodes(link)) {
      const chars = Array.from(node.data).length;
      if (range.intersectsNode(node)) {
        const lo = range.startContainer === node ? range.startOffset : 0;
        const hi = range.endContainer === node ? range.endOffset : node.length;
        if (hi > lo && range.comparePoint(node,hi) >= 0 && range.comparePoint(node,lo) <= 0) {
          if (first == null) first = position + Array.from(node.data.slice(0,lo)).length;
          last = position + Array.from(node.data.slice(0,hi)).length;
          parts.push(node.data.slice(lo,hi));
        }
      }
      position += chars;
    }
    return first == null ? null : {first,last,text:parts.join("")};
  }
  function syncSelection() {
    selectionFrame = 0;
    selectedText = null;
    selectionButton.hidden = true;
    const selection = window.getSelection();
    if (readOnly || dialog.open || !selection?.rangeCount || selection.isCollapsed) return;
    const range = selection.getRangeAt(0);
    const insideVerse = node => (node.nodeType === Node.TEXT_NODE ? node.parentElement : node)?.closest(".scripture-verses");
    if (!insideVerse(range.startContainer) || !insideVerse(range.endContainer)) return;
    const parts = visiblePassages().filter(link=>range.intersectsNode(link)).map(link=>({
      location:passage(link), slice:selectedPart(link,range)
    })).filter(part=>part.slice?.text.trim());
    if (!parts.length) return;
    const first = parts[0], last = parts.at(-1);
    const inChapter = parts.every(part=>sameChapter(part.location,first.location));
    selectionButton.disabled = !inChapter;
    selectionButton.querySelector("span").textContent = inChapter ? "Marcar seleção" : "Selecione um capítulo por vez";
    if (inChapter) selectedText = {...first.location,end_verse:last.location.end_verse,start_offset:first.slice.first,
      end_offset:last.slice.last,excerpt:parts.map(part=>part.slice.text).join(" ").slice(0,250)};
    const rect = range.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > innerHeight) return;
    selectionButton.hidden = false;
    const button = selectionButton.getBoundingClientRect();
    selectionButton.style.left = Math.max(8,Math.min(innerWidth-button.width-8,rect.left))+"px";
    selectionButton.style.top = Math.max(8,Math.min(innerHeight-button.height-8,
      rect.top > button.height+14 ? rect.top-button.height-7 : rect.bottom+7))+"px";
  }
  function queueSelection() {
    if (!selectionFrame) selectionFrame = requestAnimationFrame(syncSelection);
  }
  document.addEventListener("selectionchange",queueSelection);
  window.addEventListener("scroll",()=>{if (!selectionButton.hidden) queueSelection();},{passive:true,capture:true});
  // Mantém o intervalo do navegador enquanto o botão recebe o clique.
  selectionButton.addEventListener("pointerdown",event=>event.preventDefault());
  selectionButton.addEventListener("click",()=>{ if (selectedText) openEditor(selectedText); });
  // Uma seleção por arraste sobre links não deve navegar para o verso ao soltar.
  document.addEventListener("click",event=>{
    if (event.target.closest(passageSelector) && window.getSelection()?.toString().trim()) {
      event.preventDefault(); queueSelection();
    }
  },true);

  function setRangeChoices(start, end) {
    const startSelect = fields.start_verse, endSelect = fields.end_verse;
    startSelect.replaceChildren(...chapterNumbers.map(v=>new Option(v.label,String(v.start))));
    startSelect.value = String(start);
    const available = chapterNumbers.filter(v=>v.end>=start);
    endSelect.replaceChildren(...available.map(v=>new Option(v.label,String(v.end))));
    endSelect.value = String(available.some(v=>v.end===end) ? end : available[0]?.end);
  }
  function updatePreview() {
    dialog.querySelector("[data-note-reference]").textContent = reference(draft);
    const quote = dialog.querySelector("[data-note-quote]");
    quote.hidden = !draft.excerpt;
    quote.textContent = draft.excerpt || "";
  }
  async function openEditor(entry) {
    if (saving) return;
    form.reset();
    // Um token por criação permite repetir após falha de rede sem duplicar.
    // Em um teste pelo IP local do computador, alguns celulares disponibilizam
    // getRandomValues mesmo quando randomUUID requer uma conexão HTTPS.
    const requestId = crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2,"0")).join("");
    draft = {...entry,request_id:requestId};
    const opened = draft;
    fields.title.value = entry.title || "";
    fields.body.value = entry.body || "";
    fields.color.value = entry.color || "gold";
    dialog.querySelector("#note-editor-title").textContent = readOnly ? "Marca-página" : entry.id ? "Editar marcação" : "Marcar passagem";
    fields.title.readOnly = readOnly;
    fields.body.readOnly = readOnly;
    fields.start_verse.disabled = fields.end_verse.disabled = readOnly || Boolean(entry.quick);
    for (const color of form.querySelectorAll("[name=color]")) color.disabled = readOnly;
    form.querySelector(".note-save").hidden = readOnly;
    if (readOnly) {
      form.querySelector(".note-personal").textContent = "Esta marcação pertence ao seu perfil. Você pode apagá-la.";
      form.querySelector(".note-cancel").textContent = "Fechar";
    }
    dialog.querySelector("[data-note-delete]").hidden = !entry.id;
    dialog.querySelector("[data-note-confirm]").hidden = true;
    dialog.querySelector(".note-editor-actions").hidden = false;
    dialog.querySelector("[data-note-error]").hidden = true;
    chapterNumbers = [{start:entry.start_verse,end:entry.start_verse,label:String(entry.start_verse)}];
    if (entry.end_verse !== entry.start_verse) chapterNumbers.push({start:entry.end_verse,end:entry.end_verse,label:String(entry.end_verse)});
    setRangeChoices(entry.start_verse,entry.end_verse);
    updatePreview();
    selectionButton.hidden = true;
    if (!dialog.open) dialog.showModal();
    fields.title.focus({preventScroll:true});
    try {
      const url = dialog.dataset.numbersUrl.replace("/BOOK/999","/"+encodeURIComponent(entry.book)+"/"+entry.chapter);
      const response = await fetch(url);
      if (!response.ok) throw Error();
      const numbers = await response.json();
      if (opened !== draft || !dialog.open) return;
      chapterNumbers = numbers.map(item=>({start:item.number,end:Number(item.label.split(/[-–]/).at(-1)),label:item.label}));
      setRangeChoices(draft.start_verse,draft.end_verse);
    } catch {
      if (opened === draft && dialog.open) formError("O índice do capítulo não carregou. A seleção atual continua disponível.");
    }
  }
  function formError(message) {
    const status = dialog.querySelector("[data-note-error]");
    status.textContent = message; status.hidden = false;
  }
  form.addEventListener("change",event=>{
    if (event.target !== fields.start_verse && event.target !== fields.end_verse) return;
    const start = Number(fields.start_verse.value), end = Number(fields.end_verse.value);
    setRangeChoices(start,end);
    Object.assign(draft,{start_verse:start,end_verse:Number(fields.end_verse.value),start_offset:null,end_offset:null,excerpt:""});
    updatePreview();
  });

  async function submit(remove) {
    if (saving || !draft || (readOnly && !remove)) return;
    const current = draft;
    saving = true;
    const buttons = Array.from(form.querySelectorAll("button"));
    buttons.forEach(button=>button.disabled=true);
    dialog.querySelector("[data-note-error]").hidden = true;
    const payload = remove ? {revision:draft.revision} : {
      book:draft.book,chapter:draft.chapter,start_verse:draft.start_verse,end_verse:draft.end_verse,
      start_offset:draft.start_offset ?? null,end_offset:draft.end_offset ?? null,
      title:fields.title.value,body:fields.body.value,color:fields.color.value,
      revision:draft.revision,request_id:draft.request_id
    };
    const url = base + (draft.id ? "/"+draft.id : "") + (remove ? "/excluir" : "");
    try {
      const response = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json",
        Accept:"application/json","X-CSRF-Token":fields.csrf_token.value},body:JSON.stringify(payload)});
      if (!response.headers.get("Content-Type")?.includes("application/json")) throw Error("A sessão mudou. Volte a entrar para salvar suas marcações.");
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 409) await loadNotes();
        throw Error(result.message || "Não foi possível atualizar a marcação.");
      }
      // Resposta confirmada: a tinta muda na hora. A leitura posterior sincroniza
      // a árvore, inclusive com edições feitas noutra aba do mesmo perfil.
      ++listRequest;
      notes = notes.filter(note=>note.id !== (remove ? current.id : result.note.id));
      if (!remove) notes.push(result.note);
      loaded = true;
      dialog.close(); paint(); renderTree(); notify(result.message);
      await loadNotes();
    } catch (error) {
      formError(error instanceof TypeError ? "Não foi possível conectar. Tente novamente." : error.message);
    } finally { saving=false; buttons.forEach(button=>button.disabled=false); }
  }
  form.addEventListener("submit",event=>{event.preventDefault();submit(false);});
  form.querySelectorAll("[data-note-close]").forEach(button=>button.addEventListener("click",()=>dialog.close()));
  form.querySelector("[data-note-remove]").addEventListener("click",()=>submit(true));
  function confirmDelete(show) {
    dialog.querySelector("[data-note-confirm]").hidden = !show;
    dialog.querySelector(".note-editor-actions").hidden = show;
    dialog.querySelector(show ? "[data-note-keep]" : "[data-note-delete]").focus();
  }
  form.querySelector("[data-note-delete]").addEventListener("click",()=>confirmDelete(true));
  form.querySelector("[data-note-keep]").addEventListener("click",()=>confirmDelete(false));
  dialog.addEventListener("cancel",event=>{ if (saving) event.preventDefault(); });
  dialog.addEventListener("close",()=>{draft=null;});
  filter.addEventListener("input",renderTree);
  panel.querySelector("[data-note-clear-filter]").addEventListener("click",()=>{passageFilter=null;renderTree();});
  panel.querySelector("[data-note-reload]").addEventListener("click",loadNotes);
  document.addEventListener("click",event=>{
    const button = event.target.closest("[data-note-create],[data-note-edit],[data-note-related]");
    if (!button) return;
    if (button.hasAttribute("data-note-edit")) {
      const entry = notes.find(note=>note.id===Number(button.dataset.noteEdit));
      if (entry) openEditor(entry);
      return;
    }
    const shown = visiblePassages();
    const link = button.closest("li")?.querySelector(passageSelector) ||
      shown.find(node=>node.hasAttribute("aria-current")) || shown[0];
    if (!link) return;
    const entry = passage(link);
    if (button.hasAttribute("data-note-related")) {
      const matches = notes.filter(note=>overlaps(note,entry));
      if (matches.length === 1) openEditor(matches[0]);
      else {passageFilter=entry;filter.value="";renderTree();document.dispatchEvent(new Event("bible:show-notes"));}
    } else openEditor({...entry,excerpt:textNodes(link).map(node=>node.data).join("").slice(0,220)});
  });
  document.addEventListener("bible:opened",()=>{
    selectionButton.hidden = true; selectedText=null; passageFilter=null;
    paint();renderTree();
  });
  loadNotes();
})();
