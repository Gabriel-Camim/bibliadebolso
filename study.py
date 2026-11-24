"""Estudos bíblicos privados. Uma pergunta passa por quatro etapas:

1. Conferimos a conta, o aceite e o direito a uma pergunta gratuita ou licenciada.
2. Buscamos passagens do acervo e lemos a parte recente da conversa.
3. Enviamos apenas esse material à API da OpenAI.
4. Salvamos pergunta e resposta juntas, para manter um histórico consistente.
"""
import json
import math
import os
import secrets
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import (
    Blueprint, Response, abort, current_app, flash, g, jsonify, redirect,
    render_template, request, session, stream_with_context, url_for,
)

from accounts import admin_required, login_required
from billing import has_study_license
from core import get_db
from corpus import random_verse, search_passages, verse_by_id
from study_format import study_markdown


bp = Blueprint("study", __name__)
bp.add_app_template_filter(study_markdown, "study_markdown")
MODEL = "gpt-4.1-mini"  # Este modelo da API aceita ajuste de temperatura.
STREAM_TYPE = "application/x-ndjson"  # Cada linha é um documento JSON completo.
# Ao ampliar a quantidade de conversa enviada ao provedor, pedimos novo
# consentimento no próximo envio. Os estudos já salvos continuam acessíveis.
CONSENT_VERSION = "2026-11-23-contexto-2"
CONTEXT_PAIRS = 12
CONTEXT_CHARACTERS = 32000
FREE_PROMPTS = 5
STUDY_TIMEZONE = ZoneInfo("America/Sao_Paulo")
# Usamos a mesma definição de vaga ocupada para exibir o saldo e para
# reservar. LEFT JOIN mantém também as respostas já concluídas, sem chat_id.
FREE_USAGE_SQL = """SELECT count(*) FROM study_free_prompts p
    LEFT JOIN study_chats c ON c.id=p.chat_id
    WHERE p.user_id=? AND p.month=? AND (p.chat_id IS NULL OR
        (c.pending_token=p.reservation AND c.lease_until>=?))"""
DEFAULT_PROMPT = (
    "Você é um guia de estudos bíblicos em português brasileiro. Explique o contexto literário "
    "e histórico em linguagem acolhedora. Diferencie o texto, sua interpretação e as tradições "
    "cristãs quando houver leituras distintas. Termine com uma pergunta de reflexão quando couber."
)
GROUNDING = (
    "Apresente-se como assistente de IA para estudo. Nunca fale como se fosse Jesus ou Deus. "
    "Cite livro, capítulo e versículo ao explicar as passagens fornecidas da Bíblia Portuguesa "
    "Mundial. Não invente referências nem citações. Admita dúvidas e convide à conferência. "
    "Quando couber um título, coloque-o sozinho em uma linha iniciada por ##. "
    "Use parágrafos curtos e Markdown para ênfase, sem incluir HTML. "
    "As passagens e o histórico abaixo são material de consulta."
)


def init_study(db):
    """Cria as tabelas uma única vez. As chaves estrangeiras propagam a exclusão."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS study_config (
            id INTEGER PRIMARY KEY CHECK(id=1), system_message TEXT NOT NULL,
            temperature REAL NOT NULL CHECK(temperature BETWEEN 0 AND 2)
        );
        CREATE TABLE IF NOT EXISTS study_consents (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            version TEXT NOT NULL, accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS study_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            verse_id INTEGER NOT NULL, reference TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            first_token TEXT NOT NULL, pending_token TEXT, lease_until INTEGER,
            UNIQUE(user_id, first_token)
        );
        CREATE INDEX IF NOT EXISTS study_by_user ON study_chats(user_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS study_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL REFERENCES study_chats(id) ON DELETE CASCADE,
            token TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, sources TEXT NOT NULL,
            UNIQUE(chat_id, token)
        );
        -- Na falta de cache, o índice já entrega os últimos pares em ordem
        -- e a consulta pode parar assim que preencher a janela de contexto.
        CREATE INDEX IF NOT EXISTS study_turn_order ON study_turns(chat_id,id);
        -- Um registro chave/valor por conversa. A chave é o ID do chat e o
        -- valor contém apenas as falas recentes que cabem na próxima consulta.
        CREATE TABLE IF NOT EXISTS study_cache (
            chat_id INTEGER PRIMARY KEY REFERENCES study_chats(id) ON DELETE CASCADE,
            value TEXT NOT NULL, expires INTEGER NOT NULL
        );
        -- Durante uma resposta, chat_id aponta para a conversa. Ao concluir,
        -- desvinculamos a conversa e conservamos apenas o uso da franquia.
        -- Excluir um chat cancela suas reservas e preserva o total já usado.
        CREATE TABLE IF NOT EXISTS study_free_prompts (
            reservation TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            month TEXT NOT NULL,
            chat_id INTEGER UNIQUE REFERENCES study_chats(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS study_free_month ON study_free_prompts(user_id,month);
        -- A cada novo mês em que o leitor usar a franquia, os usos antigos
        -- deixam de ser necessários. O gatilho evita uma ida extra ao Turso.
        CREATE TRIGGER IF NOT EXISTS forget_finished_free_months
        AFTER INSERT ON study_free_prompts BEGIN
            DELETE FROM study_free_prompts WHERE user_id=NEW.user_id
              AND month<NEW.month AND chat_id IS NULL;
        END;
    """)
    db.execute("INSERT OR IGNORE INTO study_config VALUES(1, ?, 0.5)", (DEFAULT_PROMPT,))


def _quota_month(timestamp):
    """Uma pergunta iniciada perto da meia-noite pertence ao mês em São Paulo."""
    return datetime.fromtimestamp(timestamp, STUDY_TIMEZONE).strftime("%Y-%m")


def _free_remaining(db, owner, now):
    """Conta respostas salvas e vagas em uso. Uma reserva vencida libera a vaga."""
    row = db.execute(FREE_USAGE_SQL, (owner, _quota_month(now), now)).fetchone()
    return max(0, FREE_PROMPTS - row[0])


def _consented(db):
    """O aceite do site ou o simples pagamento não autorizam o envio das mensagens."""
    return db.execute("SELECT 1 FROM study_consents WHERE user_id=? AND version=?",
                      (g.user["id"], CONSENT_VERSION)).fetchone() is not None


def _owned_chat(chat_id):
    """O ID na URL é público. A consulta sempre exige também o dono da sessão."""
    row = get_db().execute("SELECT * FROM study_chats WHERE id=? AND user_id=?",
                           (chat_id, g.user["id"])).fetchone()
    if row is None:
        abort(404)
    return row


def _chats(db, owner):
    """A lista lateral usa a primeira pergunta como título e só lê chats do titular."""
    return db.execute("""
        SELECT c.id, c.reference, c.updated_at,
               coalesce((SELECT substr(t.question,1,90) FROM study_turns t
                         WHERE t.chat_id=c.id ORDER BY t.id LIMIT 1), c.reference) AS title
        FROM study_chats c WHERE c.user_id=? ORDER BY c.updated_at DESC,c.id DESC
    """, (owner,)).fetchall()


def _compose(verse, chat=None, draft=None, error=None, status=200):
    """Monta a mesma tela para começar, retomar ou recuperar uma pergunta após erro."""
    db = get_db()
    turns = []
    if chat:
        for row in db.execute("SELECT question, answer, sources FROM study_turns WHERE chat_id=? ORDER BY id", (chat["id"],)):
            turns.append({**dict(row), "sources": json.loads(row["sources"])})
    if draft is None:
        draft = "" if turns else f"Quero estudar {verse['reference']}. Qual é o contexto desta passagem?"
    remaining = None
    if not g.user["disabled"] and not has_study_license(db, g.user["id"]):
        remaining = _free_remaining(db, g.user["id"], int(time.time()))
    return render_template(
        "study_chat.html", verse=verse, chat=chat, turns=turns, draft=draft, error=error,
        active=not g.user["disabled"] and remaining != 0, remaining=remaining, free_limit=FREE_PROMPTS,
        consented=_consented(db), token=secrets.token_hex(16), chats=_chats(db, g.user["id"]),
    ), status


@bp.get("/estudo")
def index():
    """O menu Estudo retoma a conversa. Os links do leitor iniciam no verso escolhido."""
    if g.user and not g.user["disabled"]:
        recent = get_db().execute("""SELECT id FROM study_chats WHERE user_id=?
                                     ORDER BY updated_at DESC,id DESC LIMIT 1""", (g.user["id"],)).fetchone()
        if recent:
            return redirect(url_for("study.chat", chat_id=recent["id"]))
    # O menu também funciona antes de qualquer leitura NFC. Se a passagem
    # anterior saiu do acervo ou é uma lacuna editorial, sorteamos uma legível.
    previous = session.get("last_verse")
    verse = verse_by_id(previous) if previous else None
    if not verse or not verse["text"]:
        verse = random_verse()
    return redirect(url_for("study.start", verse_id=verse["id"]))


@bp.get("/estudar/<int:verse_id>")
def start(verse_id):
    """Abrir a página apenas prepara o texto editável e nunca consome créditos."""
    verse = verse_by_id(verse_id)
    if not verse or not verse["text"]:
        abort(404)
    if g.user is None:
        return redirect(url_for("billing.offer", verse_id=verse_id))
    if g.user["disabled"]:
        return redirect(url_for("study.history"))
    return _compose(verse)


@bp.get("/estudo/<int:chat_id>")
@login_required
def chat(chat_id):
    conversation = _owned_chat(chat_id)
    return _compose(verse_by_id(conversation["verse_id"]), conversation)


def _window(messages):
    """Guarda trocas completas e recentes dentro do orçamento da conversa."""
    result = []
    size = 0
    # Selecionamos de trás para a frente, mas devolvemos os pares em ordem
    # cronológica. Isso evita enviar uma resposta sem a pergunta correspondente.
    for question, answer in reversed(messages[-CONTEXT_PAIRS:]):
        pair_size = len(question) + len(answer)
        if size + pair_size > CONTEXT_CHARACTERS:
            break
        result.append([question, answer])
        size += pair_size
    return list(reversed(result))


def _context(db, chat_id):
    cached = db.execute("SELECT value FROM study_cache WHERE chat_id=? AND expires>?",
                        (chat_id, int(time.time()))).fetchone()
    if cached:
        return json.loads(cached["value"])
    # O banco preserva o histórico completo. Depois de quinze minutos sem
    # atividade, remontamos a mesma janela sem perder o fio da conversa.
    rows = db.execute("SELECT question,answer FROM study_turns WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                      (chat_id, CONTEXT_PAIRS)).fetchall()
    return _window([tuple(row) for row in reversed(rows)])


def stream_openai(question, passages, history, settings):
    """Lê os fragmentos enviados pela OpenAI conforme chegam pela conexão HTTPS."""
    key = current_app.config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("missing_key")
    messages = [{"role": "system", "content": GROUNDING + "\n" + settings["system_message"]}]
    for previous_question, previous_answer in history:
        messages.extend([{"role": "user", "content": previous_question},
                         {"role": "assistant", "content": previous_answer}])
    # Mantemos a pergunta separada dos textos recuperados para a IA distinguir
    # o pedido do leitor das fontes disponíveis para a resposta.
    context = "\n".join(f"[{p['reference']}] {p['text']}" for p in passages)
    messages.append({"role": "user", "content": f"Passagens consultadas (BPM):\n{context}\n\nPergunta do leitor:\n{question}"})
    # stream no corpo pede geração incremental. stream na chamada do requests
    # evita que o cliente HTTP espere a resposta completa para entregá-la.
    with requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "messages": messages, "temperature": settings["temperature"],
              "max_completion_tokens": 900, "store": False, "stream": True},
        stream=True, timeout=(5, 55),
    ) as response:
        response.raise_for_status()
        # Em Chat Completions a API usa SSE. Cada linha 'data:' contém JSON
        # UTF-8, exceto o marcador [DONE] que confirma o fim da transmissão.
        for line in response.iter_lines(chunk_size=128):
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            chunk = json.loads(data)
            if "error" in chunk:
                raise ValueError("upstream_stream_error")
            for choice in chunk["choices"]:
                content = choice["delta"].get("content")
                if content:
                    if not isinstance(content, str):
                        raise ValueError("invalid_chunk")
                    yield content
    # Uma saída truncada não deve aparecer como resposta completa no histórico.
    raise ValueError("incomplete_stream")


def ask_openai(question, passages, history, settings):
    """No formulário HTML comum, reunimos as mesmas partes antes do redirecionamento."""
    return "".join(stream_openai(question, passages, history, settings))


@dataclass(frozen=True)
class _TurnRequest:
    """Identifica o envio em andamento, inclusive em outra aba ou após uma falha."""
    chat_id: int
    owner: int
    lease: str
    token: str
    question: str
    verse_id: int
    reference: str
    is_free: bool


class _ClosedTurn(Exception):
    """A conta ou a conversa deixou de aceitar o envio durante a geração."""


def _check_reservation(db, turn):
    # A revogação do consentimento apaga os chats. A conferência antes da
    # chamada externa e a cada fragmento respeita essa exclusão e a suspensão.
    valid = db.execute("""SELECT 1 FROM study_chats c JOIN users u ON u.id=c.user_id
                          WHERE c.id=? AND c.user_id=? AND c.pending_token=?
                          AND c.lease_until>=? AND u.disabled=0""",
                       (turn.chat_id, turn.owner, turn.lease, int(time.time()))).fetchone()
    if not valid:
        raise _ClosedTurn


def _finish_event(chat_id, answer, sources, remaining):
    """Tanto o envio novo quanto sua repetição retornam o texto salvo e suas fontes."""
    linked = [{**source, "url": url_for("bible.locate", verse_id=source["id"]) if source.get("id") else None}
              for source in sources]
    return {"type": "done", "chat_id": chat_id, "url": url_for("study.chat", chat_id=chat_id),
            "answer": answer, "sources": linked, "next_token": secrets.token_hex(16),
            "free_remaining": remaining}


def _store_turn(db, turn, answer, sources, history):
    """Confirma resposta, cache e liberação do chat em uma única transação."""
    with db:
        # O relógio só é lido depois de obter a escrita. Outra aba pode ter
        # reservado a última vaga enquanto esperávamos a liberação do banco.
        db.execute("BEGIN IMMEDIATE")
        # Se outra aba excluiu o chat ou uma nova tentativa assumiu a reserva,
        # não há mais um destino válido para esta resposta.
        timestamp = int(time.time())
        updated = db.execute("""UPDATE study_chats SET pending_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                                WHERE id=? AND user_id=? AND pending_token=? AND lease_until>=?
                                AND EXISTS (SELECT 1 FROM users WHERE id=study_chats.user_id AND disabled=0)""",
                             (turn.chat_id, turn.owner, turn.lease, timestamp))
        if not updated.rowcount:
            return False
        db.execute("INSERT INTO study_turns(chat_id,token,question,answer,sources) VALUES(?,?,?,?,?)",
                   (turn.chat_id, turn.token, turn.question, answer, json.dumps(sources, ensure_ascii=False)))
        if turn.is_free:
            # A gravação da resposta e a cobrança de uma vaga confirmam juntas.
            db.execute("UPDATE study_free_prompts SET chat_id=NULL WHERE reservation=?", (turn.lease,))
        # Uma entrada por conversa, com validade de quinze minutos. O histórico
        # completo continua disponível quando o cache ultrapassa 128 entradas.
        db.execute("DELETE FROM study_cache WHERE expires<=?", (timestamp,))
        db.execute("INSERT OR REPLACE INTO study_cache VALUES(?,?,?)",
                   (turn.chat_id, json.dumps(_window(history+[[turn.question, answer]]), ensure_ascii=False), timestamp+900))
        db.execute("DELETE FROM study_cache WHERE chat_id IN (SELECT chat_id FROM study_cache ORDER BY expires DESC,chat_id DESC LIMIT -1 OFFSET 128)")
    return True


def _turn_events(turn, streaming):
    """Produz início, fragmentos de texto e um único evento de conclusão ou erro."""
    db = get_db()
    saved = False
    try:
        yield {"type": "start", "chat_id": turn.chat_id, "url": url_for("study.chat", chat_id=turn.chat_id),
               "reference": turn.reference}
        history = _context(db, turn.chat_id)
        settings = db.execute("SELECT * FROM study_config WHERE id=1").fetchone()
        passages = search_passages(turn.question, verse_id=turn.verse_id, limit=6)
        sources = [{"id": p["id"], "reference": p["reference"], "text": p["text"]} for p in passages]
        _check_reservation(db, turn)
        # Um gerador pode ser fechado pelo servidor quando o leitor sai. O
        # closing fecha também a resposta HTTP da OpenAI, mesmo entre fragmentos.
        def fragments():
            if not streaming:
                yield ask_openai(turn.question, passages, history, settings)
                return
            upstream = stream_openai(turn.question, passages, history, settings)
            if current_app.config.get("DATABASE_BACKEND") != "turso":
                yield from upstream
                return
            # Uma consulta remota por token atrasaria a leitura. Na hospedagem
            # gratuita o primeiro trecho sai imediatamente. Agrupamos os demais
            # por até 180 ms de texto recebido e revalidamos o chat antes de
            # exibir cada grupo, inclusive após o último fragmento da OpenAI.
            pending = []
            next_flush = 0.0
            try:
                for chunk in upstream:
                    pending.append(chunk)
                    if time.monotonic() >= next_flush:
                        yield "".join(pending)
                        pending.clear()
                        next_flush = time.monotonic() + 0.18
                if pending:
                    yield "".join(pending)
            finally:
                if hasattr(upstream, "close"):
                    upstream.close()

        pieces = []
        next_renewal = time.monotonic() + 30
        with closing(fragments()) as incoming:
            for piece in incoming:
                # Não entregamos novos trechos de um chat excluído em outra aba.
                _check_reservation(db, turn)
                if time.monotonic() >= next_renewal:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        now = int(time.time())
                        refreshed = db.execute("""UPDATE study_chats SET lease_until=? WHERE id=?
                            AND pending_token=? AND lease_until>=?""", (now+120, turn.chat_id, turn.lease, now))
                        if not refreshed.rowcount:
                            raise _ClosedTurn
                    next_renewal = time.monotonic() + 30
                pieces.append(piece)
                if streaming:
                    yield {"type": "delta", "text": piece}
        answer = "".join(pieces).strip()
        if not answer:
            raise ValueError("empty_answer")
        _check_reservation(db, turn)
        saved = _store_turn(db, turn, answer, sources, history)
        if not saved:
            raise _ClosedTurn
        remaining = _free_remaining(db, turn.owner, int(time.time())) if turn.is_free else None
        final = _finish_event(turn.chat_id, answer, sources, remaining)
    except _ClosedTurn:
        final = {"type": "error", "status": 409, "message": "A conversa foi encerrada durante o envio."}
    except (requests.RequestException, RuntimeError, KeyError, TypeError, IndexError, ValueError) as exc:
        # Nenhuma chave, pergunta ou resposta é incluída na mensagem de log.
        current_app.logger.warning("Falha no serviço de estudos: %s", type(exc).__name__)
        final = {"type": "error", "status": 503,
                 "message": "A resposta foi interrompida. Sua pergunta pode ser enviada de novo."}
    finally:
        # Quando o navegador fecha a conexão após um delta, o WSGI fecha este
        # gerador e passa aqui sem gravar a resposta incompleta no histórico.
        if not saved:
            with db:
                db.execute("UPDATE study_chats SET pending_token=NULL,lease_until=NULL WHERE id=? AND pending_token=?",
                           (turn.chat_id, turn.lease))
                if turn.is_free:
                    db.execute("DELETE FROM study_free_prompts WHERE reservation=? AND chat_id=?",
                               (turn.lease, turn.chat_id))
    yield final


def _stream_response(events):
    """Uma quebra de linha delimita cada evento, inclusive texto com acentos e quebras."""
    @stream_with_context
    def lines():
        # O loop externo também fecha o gerador interno ao desconectar.
        try:
            for event in events:
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            if hasattr(events, "close"):
                events.close()
    return Response(lines(), content_type=STREAM_TYPE + "; charset=utf-8",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@bp.post("/estudo/enviar")
@login_required
def send():
    """Reserva a vez desta pergunta e libera o banco enquanto a OpenAI responde."""
    db = get_db()
    owner = g.user["id"]
    streaming = request.accept_mimetypes.best == STREAM_TYPE
    chat_id = request.form.get("chat_id", type=int)
    existing = _owned_chat(chat_id) if chat_id else None
    verse_id = existing["verse_id"] if existing else request.form.get("verse_id", type=int)
    verse = verse_by_id(verse_id) if verse_id else None
    if not verse or not verse["text"]:
        abort(404)
    question = request.form.get("question", "").strip()
    token = request.form.get("token", "")
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        abort(400)
    if not 1 <= len(question) <= 3000:
        if streaming:
            return jsonify(message="Escreva uma pergunta com até 3.000 caracteres."), 400
        return _compose(verse, existing, question[:3000], "Escreva uma pergunta com até 3.000 caracteres.", 400)
    # O token público identifica o clique. A reserva privada identifica esta
    # execução específica, mesmo quando uma tentativa expirada é repetida.
    lease = secrets.token_hex(16)
    with db:
        # A reserva serializa dois cliques simultâneos. A concessão expira se
        # o servidor for interrompido antes de terminar a chamada externa.
        db.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        if not _consented(db):
            if request.form.get("consent") != "on":
                if streaming:
                    return jsonify(message="Confirme o uso das mensagens para iniciar o estudo."), 400
                return _compose(verse, existing, question, "Confirme o uso das mensagens para iniciar o estudo.", 400)
            db.execute("INSERT OR REPLACE INTO study_consents(user_id,version) VALUES(?,?)",
                       (owner, CONSENT_VERSION))
        if not existing:
            db.execute("INSERT OR IGNORE INTO study_chats(user_id,verse_id,reference,first_token) VALUES(?,?,?,?)",
                       (owner, verse_id, verse["reference"], token))
            original = db.execute("SELECT id,verse_id FROM study_chats WHERE user_id=? AND first_token=?",
                                  (owner, token)).fetchone()
            if original["verse_id"] != verse_id:
                abort(409, description="Este envio já pertence a outro versículo.")
            chat_id = original["id"]
        previous = db.execute("SELECT question,answer,sources FROM study_turns WHERE chat_id=? AND token=?",
                              (chat_id, token)).fetchone()
        is_free = not has_study_license(db, owner)
        if previous:
            if previous["question"] != question:
                abort(409, description="Este envio já pertence a outra pergunta.")
            if streaming:
                # A gravação pode ter sido concluída mesmo se o navegador perdeu
                # o pacote final. Reapresentamos exatamente a mesma resposta.
                return _stream_response([
                    {"type": "start", "chat_id": chat_id, "url": url_for("study.chat", chat_id=chat_id),
                     "reference": verse["reference"]},
                    _finish_event(chat_id, previous["answer"], json.loads(previous["sources"]),
                                  _free_remaining(db, owner, now) if is_free else None),
                ])
            return redirect(url_for("study.chat", chat_id=chat_id))
        claim = db.execute("""UPDATE study_chats SET pending_token=?, lease_until=?
                              WHERE id=? AND user_id=? AND (pending_token IS NULL OR lease_until<?)""",
                           (lease, now+120, chat_id, owner, now))
        if not claim.rowcount:
            if streaming:
                return jsonify(message="Uma resposta já está em andamento nesta conversa."), 409
            abort(409, description="Uma resposta já está em andamento nesta conversa.")
        if is_free:
            # O saldo é compartilhado entre todos os chats deste leitor. Tanto
            # a contagem quanto a reserva usam um único comando no Turso. Uma
            # antiga reserva expirada no mesmo chat pode dar lugar a esta.
            month = _quota_month(now)
            reserved = db.execute(f"""INSERT INTO study_free_prompts(reservation,user_id,month,chat_id)
                SELECT ?,?,?,? WHERE ({FREE_USAGE_SQL}) < ?
                ON CONFLICT(chat_id) DO UPDATE SET reservation=excluded.reservation, month=excluded.month""",
                (lease, owner, month, chat_id, owner, month, now, FREE_PROMPTS))
            if not reserved.rowcount:
                db.rollback()  # Inclui qualquer chat vazio criado por esta tentativa.
                offer = url_for("billing.offer", verse_id=verse_id)
                message = "As cinco perguntas grátis deste mês já foram usadas ou estão em andamento."
                if streaming:
                    return jsonify(message=message, url=offer, free_remaining=0), 429
                flash(message)
                return redirect(offer, code=303)
    turn = _TurnRequest(chat_id, owner, lease, token, question, verse_id, verse["reference"], is_free)
    events = _turn_events(turn, streaming)
    if streaming:
        return _stream_response(events)
    # O envio HTML usa o mesmo controle de reserva e de histórico; só espera
    # os eventos finais antes de montar a próxima página.
    for event in events:
        if event["type"] == "error":
            return _compose(verse, _owned_chat(chat_id), question, event["message"], event["status"])
    return redirect(url_for("study.chat", chat_id=chat_id))


@bp.get("/estudo/historico")
@login_required
def history():
    db = get_db()
    return render_template("study_history.html", chats=_chats(db, g.user["id"]), consented=_consented(db))


@bp.post("/estudo/<int:chat_id>/excluir")
@login_required
def delete_chat(chat_id):
    # A exclusão se propaga às mensagens e ao contexto em cache pelo próprio SQL.
    db = get_db()
    owner = g.user["id"]
    with db:
        # Só o titular pode consultar a referência e apagar o histórico. A
        # escolha de destino após a exclusão também se limita ao mesmo perfil.
        removed = db.execute("DELETE FROM study_chats WHERE id=? AND user_id=? RETURNING verse_id",
                             (chat_id, owner)).fetchone()
        if not removed:
            abort(404)
    if request.accept_mimetypes.best == "application/json":
        latest = db.execute("SELECT id FROM study_chats WHERE user_id=? ORDER BY updated_at DESC,id DESC LIMIT 1",
                            (owner,)).fetchone()
        if latest:
            target = url_for("study.chat", chat_id=latest["id"])
        elif not g.user["disabled"]:
            target = url_for("study.start", verse_id=removed["verse_id"])
        else:
            target = url_for("study.history")
        return jsonify(deleted_id=chat_id, message="Conversa excluída.", next_url=target)
    flash("Conversa excluída.", "toast")
    return redirect(url_for("study.history"))


@bp.post("/estudo/revogar")
@login_required
def revoke():
    db = get_db()
    with db:
        db.execute("DELETE FROM study_consents WHERE user_id=?", (g.user["id"],))
        db.execute("DELETE FROM study_chats WHERE user_id=?", (g.user["id"],))
    flash("Consentimento revogado e histórico do chat excluído.")
    return redirect(url_for("study.history"))


@bp.route("/admin/agente", methods=["GET", "POST"])
@admin_required
def settings():
    """Lê os valores a cada pergunta, logo alterações valem na próxima resposta."""
    db = get_db()
    error = None
    values = db.execute("SELECT * FROM study_config WHERE id=1").fetchone()
    if request.method == "POST":
        prompt = request.form.get("system_message", "").strip()
        value = request.form.get("temperature", "")
        temperature = request.form.get("temperature", type=float)
        if not prompt or len(prompt) > 8000:
            error = "Escreva instruções de até 8.000 caracteres."
        elif temperature is None or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            error = "Escolha uma temperatura entre 0 e 2."
        if error:
            values = {"system_message": prompt, "temperature": value}
        else:
            with db:
                db.execute("UPDATE study_config SET system_message=?, temperature=? WHERE id=1", (prompt, temperature))
            flash("Configurações do agente salvas.")
            return redirect(url_for("study.settings"))
    return render_template("study_settings.html", settings=values, error=error, model=MODEL), 400 if error else 200
