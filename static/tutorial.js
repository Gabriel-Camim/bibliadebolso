/* Um ensaio de leitura dentro de um diálogo nativo. As ações demonstrativas
   acontecem só na miniatura: não mudam o versículo, as marcações nem o chat
   que o leitor deixou aberto por trás. */
(() => {
  "use strict";
  const dialog = document.querySelector("#welcome-tour");
  if (!dialog || typeof dialog.showModal !== "function") return;

  const steps = [
    {tab:"Versículo", title:"Um versículo para começar", pose:"welcome", kind:"reading",
      copy:"Aproxime o chaveiro ou abra o site para ler uma passagem. Toque em “Novo versículo” quando quiser descobrir outra.",
      label:"João 1:5", excerpt:"A luz brilha nas trevas, e as trevas não a venceram.",
      action:"Novo versículo", afterLabel:"Gênesis 1:1", afterText:"No princípio, Deus criou os céus e a terra.", afterAction:"Sortear de novo",
      target:"#draw"},
    {tab:"Bíblia", title:"Abra o livro", pose:"reading", kind:"bible",
      copy:"A aba Bíblia abre a capa em Gênesis. Para ler o capítulo do seu sorteio, use “Navegar até o versículo”. Toque nas palavras para selecionar a passagem.",
      label:"Bíblia Sagrada", excerpt:"Abra a capa para chegar à primeira página.",
      action:"Abrir a capa", afterLabel:"Gênesis · Capítulo 1", afterText:"¹ No princípio, Deus criou os céus e a terra.", afterAction:"Fechar a capa",
      target:"#bible-link, .main-nav a[href^='/biblia'], .scripture-verses a[data-select-passage]"},
    {tab:"Marcar", title:"Guarde a passagem", pose:"teaching", kind:"bookmark",
      copy:"Com sua conta aberta, use “Marcar página” no versículo sorteado ou na Bíblia. A fita guarda o ponto da leitura em seu índice particular.",
      label:"Gênesis 1:1", excerpt:"No princípio, Deus criou os céus e a terra.",
      action:"Marcar página", afterLabel:"Marca-página de exemplo", afterText:"Gênesis 1:1 ficou marcado. O índice reúne as passagens que você guardar.", afterAction:"Desfazer exemplo",
      target:"[data-bookmark-shortcut], [data-draw-bookmark] button, [data-draw-bookmark] a"},
    {tab:"Índice", title:"Volte ao ponto marcado", pose:"reading", kind:"index",
      copy:"Na Bíblia, abra “Índice” e escolha “Marca-páginas”. Selecione uma referência para voltar àquela página. No computador, o índice fica à esquerda.",
      label:"Índice da Bíblia", excerpt:"Encontre o ponto que você guardou.",
      action:"Abrir marca-páginas", afterLabel:"Marca-páginas › Gênesis", afterText:"Capítulo 1 › Gênesis 1:1", afterAction:"Voltar ao índice",
      target:"#index-notes-tab, [data-open-bible-index].index-open, [data-draw-bookmark] a"},
    {tab:"Estudo", title:"Leve sua pergunta ao guia", pose:"listening", kind:"study",
      copy:"“Estudar com a IA” prepara uma pergunta sobre o versículo. Entre na sua conta, ajuste a mensagem e envie quando quiser. Você tem cinco perguntas grátis por mês.",
      label:"Estudo guiado", excerpt:"Prepare uma pergunta sobre a passagem que acabou de ler.",
      action:"Estudar com a IA", afterLabel:"Rascunho de exemplo", afterText:"Quero estudar Gênesis 1:1. Qual é o contexto desta passagem?", afterAction:"Voltar à passagem",
      target:"#study-link, .verse-study, #question, .main-nav a[href='/estudo']"}
  ];
  const title = dialog.querySelector("#tour-title");
  const portrait = dialog.querySelector("[data-tour-portrait]");
  const example = dialog.querySelector("[data-tour-example]");
  const demo = dialog.querySelector("[data-try-example]");
  const locate = dialog.querySelector("[data-find-action]");
  const route = dialog.querySelector(".tour-route ol");
  const next = dialog.querySelector("[data-tour-next]");
  const back = dialog.querySelector("[data-tour-back]");
  const replayButtons = document.querySelectorAll("[data-replay-tutorial]");
  let index = 0, tried = false, opener = null, recorded = false;

  // Os cinco nomes do percurso estão sempre visíveis. Os botões são nativos
  // para que teclado e leitor de tela também possam ir direto a uma lição.
  const stops = steps.map((step, position) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = step.tab;
    button.setAttribute("aria-label", "Etapa " + (position + 1) + ", " + step.tab);
    button.addEventListener("click", () => present(position));
    item.append(button);
    route.append(item);
    return button;
  });

  function visibleTarget() {
    // Os menus para desktop e telefone coexistem no HTML. Só oferecemos
    // a ação quando o elemento correspondente está disponível nessa tela.
    return [...document.querySelectorAll(steps[index].target)].find(node => {
      const rect = node.getBoundingClientRect();
      if (node.disabled || node.closest("[hidden], [inert], [aria-hidden='true']") ||
          !rect.width || !rect.height || rect.right <= 0 || rect.left >= document.documentElement.clientWidth) return false;
      return getComputedStyle(node).visibility === "visible";
    });
  }

  function showExample() {
    const step = steps[index];
    example.dataset.kind = step.kind;
    example.toggleAttribute("data-tried", tried);
    example.querySelector("[data-example-label]").textContent = tried ? step.afterLabel : step.label;
    example.querySelector("[data-example-content]").textContent = tried ? step.afterText : step.excerpt;
    demo.textContent = tried ? step.afterAction : step.action;
    demo.setAttribute("aria-pressed", String(tried));
    // A luz da última lição é o mesmo efeito que aponta para o estudo real.
    demo.classList.toggle("ai-attention", step.kind === "study" && !tried);
  }

  function present(position) {
    index = position;
    tried = false;
    const step = steps[index];
    dialog.querySelector("[data-tour-phase]").textContent = "Etapa " + (index + 1) + " de " + steps.length + " · " + step.tab;
    title.textContent = step.title;
    dialog.querySelector("#tour-copy").textContent = step.copy;
    const image = dialog.dataset[step.pose];
    if (portrait.getAttribute("src") !== image) {
      portrait.src = image;
      portrait.classList.remove("tour-pose-enter");
      requestAnimationFrame(() => portrait.classList.add("tour-pose-enter"));
    }
    stops.forEach((stop, n) => {
      if (n === position) stop.setAttribute("aria-current", "step");
      else stop.removeAttribute("aria-current");
      stop.toggleAttribute("data-past", n < position);
    });
    back.disabled = position === 0;
    next.firstChild.textContent = position === steps.length - 1 ? "Concluir " : "Continuar ";
    locate.hidden = !visibleTarget();
    showExample();
    title.focus({preventScroll:true});
    dialog.scrollTop = 0;
  }

  function remember() {
    if (recorded) return;
    recorded = true;
    // O servidor lembra apenas que este guia foi apresentado. keepalive
    // deixa o pedido terminar mesmo quando o leitor segue imediatamente.
    fetch(dialog.dataset.seenUrl, {
      method:"POST", headers:{"X-CSRF-Token":dialog.dataset.token, Accept:"application/json"}, keepalive:true
    }).then(response => { if (!response.ok) recorded = false; }).catch(() => { recorded = false; });
  }

  function openTour(button = null) {
    if (dialog.open) return;
    opener = button;
    document.documentElement.classList.add("tour-active");
    dialog.showModal();
    present(0);
    remember();
  }

  function finish(target = null) {
    dialog.close();
    document.documentElement.classList.remove("tour-active");
    // Uma repetição manual devolve o foco ao botão que chamou o guia.
    // “Mostrar na página” só destaca o controle: a pessoa escolhe ativá-lo.
    const fallback = document.querySelector("main h1") || document.querySelector(".brand");
    const focus = target || opener || fallback;
    if (!focus) return;
    if (!focus.matches("a,button,input,textarea,[tabindex]")) focus.tabIndex = -1;
    focus.focus({preventScroll:true});
    if (target) {
      focus.scrollIntoView({block:"center", behavior:"instant"});
      focus.classList.add("tour-target");
      setTimeout(() => focus.classList.remove("tour-target"), 2600);
    }
  }

  const forward = () => index + 1 < steps.length ? present(index + 1) : finish();
  back.addEventListener("click", () => present(Math.max(0, index - 1)));
  next.addEventListener("click", forward);
  dialog.querySelector("[data-skip-stage]").addEventListener("click", forward);
  dialog.querySelector("[data-end-tour]").addEventListener("click", () => finish());
  demo.addEventListener("click", () => { tried = !tried; showExample(); });
  locate.addEventListener("click", () => finish(visibleTarget()));
  dialog.addEventListener("cancel", event => { event.preventDefault(); finish(); });
  for (const button of replayButtons) {
    button.hidden = false;
    button.addEventListener("click", () => openTour(button));
  }
  // Ao entrar pela aba Bíblia, a leitora vê a capa abrir antes do convite.
  // O limite também funciona se o sistema reduzir a animação da encadernação.
  if (dialog.dataset.first === "1") {
    let pending = true;
    function begin() {
      if (!pending || document.visibilityState === "hidden") return;
      pending = false;
      if (!document.querySelector("dialog[open]")) openTour();
    }
    if (document.querySelector(".book-intro")) {
      document.addEventListener("bible:cover-opened", begin, {once:true});
      setTimeout(begin, 2700);
    } else requestAnimationFrame(begin);
    document.addEventListener("visibilitychange", () => { if (pending && !document.hidden) begin(); });
  }
})();
