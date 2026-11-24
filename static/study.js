/* O histórico já vem formatado pelo Flask. Durante o streaming, construímos
   a mesma estrutura com nós de texto; a resposta da IA nunca vira HTML livre. */
(() => {
  "use strict";
  const workspace = document.querySelector("[data-chat-workspace]");
  const archive = document.querySelector("[data-chat-archive]");
  if (!workspace && !archive) return;

  const page = workspace || archive;
  const form = page.querySelector("#study-form");
  const question = form?.elements.question;
  const transcript = page.querySelector("#chat-transcript");
  const scrollport = page.querySelector("#chat-scrollport");
  const prompts = page.querySelector("#chat-prompts");
  const allowance = page.querySelector("[data-free-allowance]");
  const passage = page.querySelector(".passage-disclosure");
  let sending = false;
  let controller;
  let currentRequest;

  function element(tag, text) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function toast(message, error = false) {
    window.dispatchEvent(new CustomEvent("site:toast", {detail:{message, error}}));
  }

  function localPath(value) {
    // Links de páginas e fontes vêm do servidor, nunca do texto gerado.
    if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return null;
    const url = new URL(value, location.origin);
    return url.origin === location.origin ? url.pathname + url.search + url.hash : null;
  }

  function inlineTokens(parent, text, partial) {
    // Código, negrito e itálico. Trechos entre as marcas viram textContent,
    // inclusive caracteres como "<". Um ** ainda aberto pode atravessar pacotes.
    const pattern = /`([^`]+)`|\*\*(.+?)\*\*|__(.+?)__|\*([^*]+?)\*/g;
    let offset = 0;
    for (const match of text.matchAll(pattern)) {
      parent.append(document.createTextNode(text.slice(offset, match.index)));
      if (match[1] !== undefined) parent.append(element("code", match[1]));
      else if (match[4] !== undefined) parent.append(element("em", match[4]));
      else parent.append(element("strong", match[2] ?? match[3]));
      offset = match.index + match[0].length;
    }
    const end = text.slice(offset);
    const pendingBold = partial && end.match(/(\*\*|__)([^*_]*)$/);
    if (pendingBold) {
      parent.append(document.createTextNode(end.slice(0, pendingBold.index)), element("strong", pendingBold[2]));
    } else {
      parent.append(document.createTextNode(end));
    }
  }

  function renderAnswer(container, text, partial = false) {
    const content = document.createDocumentFragment();
    let currentTag = "";
    let block;
    // Uma marca de fechamento pode chegar em dois pacotes; aguarde o próximo
    // caractere antes de desenhar um asterisco solto na ponta da resposta.
    const visibleText = partial ? text.replace(/(?<!\*)\*$/, "").replace(/(?<!_)_$/, "") : text;
    for (const sourceLine of visibleText.split(/\r?\n/)) {
      const line = sourceLine.trim();
      if (!line) { currentTag = ""; continue; }
      const heading = line.match(/^#{1,6}\s+(.+)/);
      const boldTitle = line.match(/^(?:\*\*([^*]+)\*\*|__([^_]+)__)$/);
      const bullet = line.match(/^[-*+]\s+(.+)/);
      const numbered = line.match(/^\d+[.)]\s+(.+)/);
      const quote = line.match(/^>\s?(.*)/);
      let tag = "p";
      let body = line;
      if (heading) { tag = "h3"; body = heading[1]; }
      else if (boldTitle) { tag = "h3"; body = boldTitle[1] ?? boldTitle[2]; }
      else if (bullet) { tag = "ul"; body = bullet[1]; }
      else if (numbered) { tag = "ol"; body = numbered[1]; }
      else if (quote) { tag = "blockquote"; body = quote[1]; }

      if (tag !== currentTag || tag === "h3") {
        block = element(tag);
        content.append(block);
      } else if (tag === "p" || tag === "blockquote") {
        block.append(document.createTextNode(" "));
      }
      if (tag === "ul" || tag === "ol") {
        const item = element("li");
        inlineTokens(item, body, partial);
        block.append(item);
      } else {
        inlineTokens(block, body, partial);
      }
      currentTag = tag;
    }
    container.replaceChildren(content);
  }

  function addSources(parent, sources) {
    if (!sources?.length) return;
    const details = element("details");
    details.className = "chat-sources";
    details.append(element("summary", "Passagens consultadas ⌄"));
    const list = element("ul");
    for (const source of sources) {
      const item = element("li");
      const url = localPath(source.url);
      const label = element(url ? "a" : "strong", source.reference);
      if (url) label.setAttribute("href", url);
      item.append(label, element("p", source.text));
      list.append(item);
    }
    details.append(list);
    parent.append(details);
  }

  function setPose(pose) {
    if (!workspace) return;
    const guide = page.querySelector(".study-shepherd");
    const face = page.querySelector("#guide-face");
    guide.src = "/static/personagens/jesus-" + pose + ".png";
    face.src = "/static/personagens/jesus-" + (pose === "le" ? "escuta" : pose) + ".png";
  }

  function scrollToEnd() {
    if (scrollport) scrollport.scrollTop = scrollport.scrollHeight;
  }

  function fitComposer() {
    if (!question) return;
    question.style.height = "auto";
    question.style.height = Math.min(question.scrollHeight + 2, 120) + "px";
  }

  // O servidor calcula o saldo entre todos os chats da conta. Não subtraímos
  // nada no navegador ao clicar, pois uma resposta interrompida libera a vaga.
  function updateAllowance(remaining) {
    if (!allowance || remaining === undefined) return;
    allowance.hidden = remaining === null;
    if (Number.isInteger(remaining)) {
      allowance.querySelector("[data-remaining]").textContent = String(remaining);
      allowance.querySelector("[data-renewal]").hidden = remaining !== 0;
    }
    if (form) form.hidden = remaining === 0;
    if (prompts && remaining === 0) prompts.hidden = true;
  }

  function setCurrent(id, url, title) {
    const cleanURL = localPath(url);
    if (!cleanURL || !Number.isInteger(id)) return;
    if (!workspace.dataset.current) passage.open = false;
    workspace.dataset.current = String(id);
    form.elements.chat_id.value = String(id);
    const list = workspace.querySelector("[data-thread-list]");
    for (const old of list.children) {
      old.classList.remove("thread-current");
      old.querySelector(".thread-link").removeAttribute("aria-current");
    }
    let row = Array.from(list.children).find(item => item.dataset.threadId === String(id));
    if (!row) {
      row = document.querySelector("#new-thread-row").content.firstElementChild.cloneNode(true);
      row.dataset.threadId = String(id);
      row.querySelector("[data-thread-title]").textContent = title;
      row.querySelector(".thread-link").title = title;
      row.querySelector("[data-thread-ref]").textContent = workspace.dataset.reference;
      row.querySelector("[data-thread-delete]").action = workspace.dataset.deletePattern.replace("/0/", "/" + id + "/");
    }
    row.querySelector(".thread-link").href = cleanURL;
    row.querySelector(".thread-link").setAttribute("aria-current", "page");
    row.classList.add("thread-current");
    list.prepend(row);
    workspace.querySelector("[data-empty-threads]").hidden = true;
    history.replaceState(null, "", cleanURL);
  }

  function clearCurrent() {
    // A exclusão bem-sucedida limpa o painel na hora, inclusive se uma
    // resposta em andamento estava chegando. O usuário continua no caderno.
    currentRequest = null;
    controller?.abort();
    sending = false;
    if (form) {
      form.elements.chat_id.value = "";
      form.elements.token.value = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("");
      question.value = "Quero estudar " + workspace.dataset.reference + ". Qual é o contexto desta passagem?";
      question.readOnly = false;
      form.querySelector("[type=submit]").disabled = false;
      form.querySelector("#chat-error").hidden = true;
      fitComposer();
    }
    transcript.replaceChildren();
    workspace.dataset.current = "";
    passage.open = true;
    workspace.querySelector("#study-status").textContent = "";
    if (prompts) prompts.hidden = Boolean(form?.hidden);
    scrollport.scrollTop = 0;
    setPose("le");
    history.replaceState(null, "", form ? workspace.dataset.fresh : workspace.dataset.history);
  }

  // Delegar os eventos à lista inclui também a conversa criada pelo primeiro
  // envio. A remoção só afeta a interface depois do sucesso confirmado pelo Flask.
  page.addEventListener("click", event => {
    const cancel = event.target.closest("[data-cancel-delete]");
    if (cancel) cancel.closest("details").open = false;
  });
  page.addEventListener("submit", async event => {
    const deletion = event.target.closest("form[data-thread-delete]");
    if (!deletion || !window.fetch) return;
    event.preventDefault();
    const row = deletion.closest("[data-thread-id]");
    const button = deletion.querySelector("[type=submit]");
    if (button.disabled) return;
    button.disabled = true;
    try {
      const response = await fetch(deletion.action, {method:"POST",body:new FormData(deletion),headers:{Accept:"application/json"}});
      if (!response.ok || !(response.headers.get("Content-Type") || "").includes("application/json")) throw Error();
      const result = await response.json();
      if (String(result.deleted_id) !== row.dataset.threadId) throw Error();
      if (workspace && workspace.dataset.current === row.dataset.threadId) clearCurrent();
      const focusAfterDelete = row.nextElementSibling?.querySelector("a") || row.previousElementSibling?.querySelector("a");
      row.remove();
      const list = page.querySelector("[data-thread-list]");
      page.querySelector("[data-empty-threads]").hidden = list.childElementCount > 0;
      (focusAfterDelete || page.querySelector(".new-thread, .archive-return"))?.focus();
      toast(result.message || "Conversa excluída.");
    } catch {
      toast("Não foi possível excluir a conversa. Tente novamente.", true);
      button.disabled = false;
    }
  });

  if (!workspace) return;
  const rail = workspace.querySelector("#chat-rail");
  const menu = workspace.querySelector(".chat-hamburger");
  const mobile = matchMedia("(max-width:800px)");
  function changeMenu(open, returnFocus = true) {
    workspace.classList.toggle("menu-open", open);
    menu.setAttribute("aria-expanded", String(open));
    workspace.querySelector(".chat-scrim").hidden = !open;
    workspace.querySelector(".chat-main").inert = open;
    rail.inert = mobile.matches && !open;
    if (open) rail.querySelector(".rail-close").focus();
    else if (returnFocus && mobile.matches) menu.focus();
  }
  menu.addEventListener("click", () => changeMenu(true));
  workspace.querySelectorAll("[data-close-menu]").forEach(button => button.addEventListener("click", () => changeMenu(false)));
  mobile.addEventListener("change", () => changeMenu(false, false));
  changeMenu(false, false);
  rail.addEventListener("keydown", event => {
    if (!workspace.classList.contains("menu-open")) return;
    if (event.key === "Escape") { changeMenu(false); return; }
    if (event.key !== "Tab") return;
    const controls = Array.from(rail.querySelectorAll("a[href],button:not([disabled]),summary")).filter(node => node.getClientRects().length);
    if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1).focus(); }
    else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus(); }
  });

  if (transcript.childElementCount) requestAnimationFrame(scrollToEnd);
  if (!form) return;
  fitComposer();
  question.addEventListener("input", () => { fitComposer(); if (!sending) setPose(question.value.trim() ? "escuta" : "le"); });
  prompts?.addEventListener("click", event => {
    const choice = event.target.closest("[data-prompt]");
    if (!choice || sending) return;
    question.value = choice.dataset.prompt;
    question.dispatchEvent(new Event("input"));
    question.focus();
  });
  question.addEventListener("keydown", event => {
    // Enter envia, Shift+Enter quebra a linha. Teclados IME podem usar
    // Enter para concluir uma palavra; nesse caso o navegador cuida da tecla.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !sending) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  async function receiveLines(response, onEvent) {
    // Uma leitura de rede pode terminar no meio de uma linha ou de um acento.
    // O decodificador e o restante da linha sobrevivem até a próxima leitura.
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let remaining = "";
    try {
      for (;;) {
        const {done, value} = await reader.read();
        remaining += decoder.decode(value, {stream:!done});
        const lines = remaining.split("\n");
        remaining = lines.pop();
        for (const line of lines) if (line.trim()) onEvent(JSON.parse(line));
        if (done) break;
      }
      if (remaining.trim()) onEvent(JSON.parse(remaining));
    } finally {
      reader.releaseLock();
    }
  }

  form.addEventListener("submit", async event => {
    // Navegadores sem streams usam o POST HTML normal. Formulários inválidos
    // (inclusive consentimento) são interceptados pela validação nativa antes.
    if (!window.fetch || !window.ReadableStream || !window.TextDecoder) return;
    event.preventDefault();
    if (sending || !question.value.trim()) return;
    sending = true;
    controller = new AbortController();
    const uniqueRequest = Symbol();
    currentRequest = uniqueRequest;
    const data = new FormData(form);
    const message = question.value.trim();
    const button = form.querySelector("[type=submit]");
    const errorBox = form.querySelector("#chat-error");
    const status = workspace.querySelector("#study-status");
    const turn = document.querySelector("#pending-turn").content.firstElementChild.cloneNode(true);
    const prose = turn.querySelector(".chat-prose");
    const indicator = turn.querySelector(".waiting-ink");
    let text = "";
    let frame = 0;
    let done = false;
    const paint = () => {
      frame = 0;
      // Ao subir na conversa, a pessoa pode continuar lendo sem ser puxada
      // para o final cada vez que a IA envia uma palavra.
      const nearEnd = scrollport.scrollHeight - scrollport.scrollTop - scrollport.clientHeight < 100;
      renderAnswer(prose, text, !done);
      if (nearEnd) scrollToEnd();
    };
    turn.querySelector(".reader-bubble p").textContent = message;
    transcript.append(turn);
    if (prompts) prompts.hidden = true;
    errorBox.replaceChildren();
    errorBox.hidden = true;
    question.value = ""; question.readOnly = true; fitComposer();
    button.disabled = true;
    setPose("le");
    status.textContent = "Consultando as passagens…";
    scrollToEnd();
    try {
      const response = await fetch(form.action, {method:"POST", body:data, signal:controller.signal,
        headers:{Accept:"application/x-ndjson"}});
      const type = response.headers.get("Content-Type") || "";
      if (!response.ok || !type.includes("application/x-ndjson") || !response.body) {
        let notice = "A conexão com o estudo foi interrompida. Tente novamente.";
        if (type.includes("application/json")) {
          const problem = await response.json();
          notice = problem.message || notice;
          updateAllowance(problem.free_remaining);
          const path = localPath(problem.url);
          if (path) {
            const action = element("a", "Continuar");
            action.href = path;
            errorBox.append(document.createTextNode(notice + " "), action);
          }
        } else if (response.redirected) notice = "A sessão mudou. Entre novamente para continuar.";
        throw Error(notice);
      }
      await receiveLines(response, chunk => {
        if (currentRequest !== uniqueRequest) return;
        if (chunk.type === "start") {
          setCurrent(chunk.chat_id, chunk.url, message);
          form.querySelector(".chat-consent")?.remove();
        } else if (chunk.type === "delta") {
          text += chunk.text;
          if (!indicator.hidden) {
            indicator.hidden = true;
            status.textContent = "O guia está respondendo…";
            setPose("ensina");
          }
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (chunk.type === "done") {
          done = true;
          updateAllowance(chunk.free_remaining);
          text = chunk.answer ?? text;
          cancelAnimationFrame(frame); paint();
          indicator.remove();
          addSources(turn.querySelector(".assistant-reply"), chunk.sources);
          form.elements.token.value = chunk.next_token;
          status.textContent = "Resposta concluída.";
        } else if (chunk.type === "error") {
          throw Error(chunk.message || "O guia está indisponível. Tente novamente.");
        }
      });
      if (!done) throw Error("A resposta foi interrompida. Sua pergunta está pronta para tentar novamente.");
    } catch (failure) {
      if (currentRequest !== uniqueRequest) return;
      // A resposta parcial não entra no histórico. O mesmo token é mantido
      // para recuperar um envio que o servidor já tenha concluído.
      turn.remove();
      question.value = message;
      if (!errorBox.textContent) errorBox.textContent = failure.name === "TypeError" ? "Não foi possível conectar. Tente novamente." : failure.message;
      errorBox.hidden = false;
      if (prompts && !transcript.childElementCount && !form.hidden) prompts.hidden = false;
      status.textContent = "";
    } finally {
      cancelAnimationFrame(frame);
      if (currentRequest === uniqueRequest) {
        currentRequest = null;
        sending = false;
        question.readOnly = false;
        button.disabled = false;
        fitComposer();
        setPose(done ? "ensina" : "escuta");
      }
    }
  });
})();
