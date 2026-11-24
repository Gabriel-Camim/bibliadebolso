"""Biblioteca pessoal de leituras. O acervo bíblico público continua em corpus.py.

Um favorito representa uma passagem. O histórico, quando autorizado, representa
cada sorteio ou abertura. Assim, sortear duas vezes o mesmo texto mantém dois
horários, mas não duplica o marcador na lista de favoritos.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from time import time

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadData, URLSafeTimedSerializer

from accounts import current_user, login_required
from core import csrf_token, get_db, write_one
from corpus import verse_by_id


bp = Blueprint("library", __name__, url_prefix="/biblioteca")
PAGE_SIZE = 20
DRAW_LIFETIME = 7 * 24 * 60 * 60
BRASILIA = timezone(timedelta(hours=-3))


def init_library(db):
    """Inicialização aditiva. Excluir a conta remove também toda a biblioteca."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS library_history_consent (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            accepted_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS library_history (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            verse_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('draw','browse')),
            occurred_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS library_recent ON library_history(user_id, occurred_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS library_favorites (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            verse_id INTEGER NOT NULL,
            saved_at INTEGER NOT NULL,
            drawn_at INTEGER,
            PRIMARY KEY (user_id, verse_id)
        );
        CREATE INDEX IF NOT EXISTS library_recent_favorites ON library_favorites(user_id, saved_at DESC, verse_id DESC);
    """)


def _record(verse_id, kind, when):
    """Somente contas ativas com o histórico habilitado gravam eventos privados."""
    user = current_user()
    if user is None or user["disabled"]:
        return
    # Uma única instrução confere o aceite e registra a leitura. A cláusula
    # adicional evita aberturas seguidas da mesma passagem, inclusive entre abas.
    sql = """INSERT INTO library_history(user_id,verse_id,kind,occurred_at)
             SELECT user_id, ?, ?, ? FROM library_history_consent WHERE user_id=?"""
    args = (verse_id, kind, when, user["id"])
    if kind == "browse":
        sql += """ AND NOT coalesce((SELECT kind='browse' AND verse_id=?
                    FROM library_history WHERE user_id=?
                    ORDER BY occurred_at DESC,id DESC LIMIT 1), 0)"""
        args += (verse_id, user["id"])
    write_one(sql, args)


def _signer():
    """Usa a chave da aplicação e uma finalidade própria para assinar o sorteio."""
    return URLSafeTimedSerializer(current_app.secret_key, salt="biblioteca.sorteio.v1")


def _session_fingerprint():
    """Vincula o recibo à sessão sem incluir nele o próprio token de formulário.

    O login Google renova a sessão e o token CSRF. Assim, o recibo perde a
    validade ao sair, inclusive se a mesma pessoa voltar a entrar em outra aba.
    """
    return hashlib.sha256(csrf_token().encode("ascii")).hexdigest()


def record_draw(verse):
    """Gera comprovante apenas se a pessoa já estava conectada ao sortear."""
    user = current_user()
    if user is None or user["disabled"]:
        return None
    when = int(time())
    _record(verse["id"], "draw", when)
    return _signer().dumps([verse["id"], when, user["id"], _session_fingerprint()])


def record_browsed(verse_id):
    """Registra a passagem selecionada no navegador, separada do sorteio."""
    _record(verse_id, "browse", int(time()))


def _decode_draw(token):
    """Recusa comprovantes modificados, vencidos ou emitidos para outra conta."""
    try:
        verse_id, when, owner, fingerprint = _signer().loads(token, max_age=DRAW_LIFETIME)
    except (BadData, TypeError, ValueError):
        abort(400, description="O link deste sorteio expirou ou é inválido. Abra novamente a leitura.")
    if owner != current_user()["id"] or not secrets.compare_digest(fingerprint, _session_fingerprint()):
        abort(404)
    return verse_id, when


def is_favorite(verse_id):
    """Consulta o marcador somente no perfil identificado pela sessão."""
    user = current_user()
    if user is None:
        return False
    return get_db().execute("SELECT 1 FROM library_favorites WHERE user_id=? AND verse_id=?",
                            (user["id"], verse_id)).fetchone() is not None


def _save(verse_id, drawn_at=None):
    """Salva uma só vez. Duplo clique e reenvio mantêm as datas do marcador."""
    db = get_db()
    # Um verso marcado pelo navegador pode receber a data quando for sorteado.
    # Se já havia uma data escolhida, preservamos a primeira.
    with db:
        db.execute("""
            INSERT INTO library_favorites(user_id,verse_id,saved_at,drawn_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id,verse_id) DO UPDATE SET drawn_at=COALESCE(drawn_at,excluded.drawn_at)
        """, (current_user()["id"], verse_id, int(time()), drawn_at))
    message = "Versículo salvo nos favoritos."
    # O leitor com JavaScript fica na mesma posição. Só confirma o marcador
    # depois do commit, inclusive quando duas abas salvam o mesmo versículo.
    if request.accept_mimetypes.best == "application/json":
        return jsonify(saved=True, verse_id=verse_id, message=message)
    flash(message, "toast")
    # Sem JavaScript, voltamos à origem por GET. Na página inicial, guardamos
    # o recibo por uma única requisição para não sortear outro texto ao voltar.
    if request.endpoint == "library.favorite_draw":
        session["saved_reading"] = [current_user()["id"], verse_id, request.form["draw_token"]]
        destination = url_for("reader")
    elif request.endpoint == "library.favorite_entry":
        destination = url_for("library.index", aba="historico", pagina=max(1, request.form.get("pagina",1,type=int)))
    else:
        destination = url_for("bible.locate", verse_id=verse_id)
    return redirect(destination, code=303)


@bp.after_request
def private_response(response):
    """Páginas privadas não devem permanecer em caches compartilhados."""
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@bp.app_template_filter("library_date")
def library_date(timestamp):
    """Persistimos segundos UTC. A interface identifica explicitamente o fuso."""
    return datetime.fromtimestamp(timestamp, BRASILIA).strftime("%d/%m/%Y às %H:%M:%S")


@bp.get("")
@login_required
def index():
    """A listagem já nasce limitada ao titular. Buscamos o texto no acervo oficial."""
    tab = request.args.get("aba", "favoritos")
    if tab not in {"favoritos", "historico"}:
        abort(404)
    db = get_db()
    user_id = current_user()["id"]
    # Validamos a página antes do OFFSET, inclusive para URLs editadas à mão.
    page = request.args.get("pagina", default=1, type=int)
    if page < 1 or page > 1_000_000_000:
        abort(404)
    if tab == "favoritos":
        query = """SELECT verse_id, saved_at, drawn_at FROM library_favorites
                   WHERE user_id=? ORDER BY saved_at DESC,verse_id DESC LIMIT ? OFFSET ?"""
    else:
        query = """SELECT h.id,h.verse_id,h.occurred_at,h.kind,f.verse_id IS NOT NULL AS favorite
                   FROM library_history h LEFT JOIN library_favorites f
                   ON f.verse_id=h.verse_id AND f.user_id=h.user_id
                   WHERE h.user_id=? ORDER BY h.occurred_at DESC,h.id DESC LIMIT ? OFFSET ?"""
    # Uma linha adicional informa se existe página seguinte sem contar toda a tabela.
    rows = db.execute(query, (user_id, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)).fetchall()
    entries = [dict(row, verse=verse_by_id(row["verse_id"])) for row in rows[:PAGE_SIZE]]
    consent = db.execute("SELECT 1 FROM library_history_consent WHERE user_id=?", (user_id,)).fetchone()
    return render_template("library.html", tab=tab, entries=entries, page=page,
                           has_more=len(rows) > PAGE_SIZE, history_enabled=bool(consent))


@bp.post("/favoritos")
def favorite_draw():
    """No leitor, tanto o sorteio quanto o salvamento exigem a mesma sessão."""
    if not current_user() or current_user()["disabled"]:
        abort(403)
    token = request.form.get("draw_token", "")
    verse_id, when = _decode_draw(token)
    return _save(verse_id, when)


@bp.post("/favoritos/versiculo/<int:verse_id>")
def favorite_verse(verse_id):
    """O navegador pode marcar uma referência sem inventar a data de um sorteio."""
    if not current_user() or current_user()["disabled"]:
        abort(403)
    verse = verse_by_id(verse_id)
    if not verse or not verse["text"]:
        abort(404)
    return _save(verse_id)


@bp.post("/historico/<int:entry_id>/favoritar")
@login_required
def favorite_entry(entry_id):
    """O horário de geração pertence ao evento escolhido, mesmo em leituras repetidas."""
    row = get_db().execute("SELECT verse_id,kind,occurred_at FROM library_history WHERE id=? AND user_id=?",
                           (entry_id, current_user()["id"])).fetchone()
    if not row:
        abort(404)
    return _save(row["verse_id"], row["occurred_at"] if row["kind"] == "draw" else None)


@bp.post("/favoritos/<int:verse_id>/excluir")
@login_required
def remove_favorite(verse_id):
    """O filtro de usuário é obrigatório mesmo quando duas pessoas marcam o mesmo verso."""
    db = get_db()
    with db:
        db.execute("DELETE FROM library_favorites WHERE user_id=? AND verse_id=?", (current_user()["id"], verse_id))
    flash("Marcador removido dos seus favoritos.")
    return redirect(url_for("library.index", aba="favoritos"), code=303)


@bp.post("/historico/preferencia")
@login_required
def history_preference():
    """O histórico é opcional. Desativar também apaga o que havia sido registrado."""
    choice = request.form.get("choice")
    if choice not in {"enable", "disable"}:
        abort(400)
    user = current_user()
    db = get_db()
    with db:
        if choice == "enable":
            if user["disabled"]:
                abort(403)
            db.execute("INSERT OR IGNORE INTO library_history_consent(user_id,accepted_at) VALUES(?,?)",
                       (user["id"], int(time())))
        else:
            db.execute("DELETE FROM library_history_consent WHERE user_id=?", (user["id"],))
            db.execute("DELETE FROM library_history WHERE user_id=?", (user["id"],))
    flash("Histórico ativado para as próximas leituras." if choice == "enable" else "Histórico apagado e desativado.")
    return redirect(url_for("library.index", aba="historico"), code=303)


@bp.post("/historico/limpar")
@login_required
def clear_history():
    """Preserva a preferência de registro e os favoritos ao limpar o histórico."""
    db = get_db()
    with db:
        db.execute("DELETE FROM library_history WHERE user_id=?", (current_user()["id"],))
    flash("Seu histórico de leitura foi apagado.")
    return redirect(url_for("library.index", aba="historico"), code=303)
