"""Marca-páginas e anotações do leitor, armazenados apenas no banco privado.

Uma marca pode identificar um versículo ou um intervalo do mesmo capítulo.
A referência aponta para o acervo público. A cor e o comentário pertencem à
conta que os criou e não são enviados ao chat nem usados nas métricas.
"""
from contextlib import closing
from time import time
from uuid import UUID

from flask import Blueprint, abort, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

import corpus
from accounts import current_user
from core import get_db


bp = Blueprint("bible_notes", __name__, url_prefix="/biblia/marcacoes")
COLORS = {"gold", "sage", "rose", "blue", "none"}
FIELDS = ("id, book, chapter, start_verse, end_verse, start_offset, end_offset, "
          "excerpt, color, title, body, revision, created_at, updated_at")


def init_notes(db):
    """Cria a tabela sem alterar os favoritos e históricos que já existiam."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS bible_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            book TEXT NOT NULL,
            chapter INTEGER NOT NULL,
            start_verse INTEGER NOT NULL,
            end_verse INTEGER NOT NULL CHECK(end_verse >= start_verse),
            start_offset INTEGER,
            end_offset INTEGER,
            excerpt TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            request_id TEXT,
            UNIQUE(user_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS bible_notes_chapter
            ON bible_notes(user_id,book,chapter,start_verse);
    """)
    # Instalações que iniciaram a versão intermediária desta funcionalidade
    # podem já ter notas de verso inteiro. Preservamos suas linhas e IDs.
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = {row[1] for row in db.execute("PRAGMA table_info(bible_notes)")}
        for name, definition in (("start_offset", "INTEGER"), ("end_offset", "INTEGER"),
                                 ("excerpt", "TEXT NOT NULL DEFAULT ''")):
            if name not in existing:
                db.execute(f"ALTER TABLE bible_notes ADD COLUMN {name} {definition}")


@bp.errorhandler(HTTPException)
def json_error(error):
    """O editor pode mostrar o erro e conservar o texto que a pessoa digitou."""
    return jsonify(message=error.description), error.code


@bp.before_request
def require_account():
    """Um perfil suspenso ainda pode consultar e apagar suas próprias notas."""
    user = current_user()
    if user is None:
        abort(401, description="Entre na sua conta para acessar as marcações.")
    if user["disabled"] and request.endpoint in {"bible_notes.create", "bible_notes.edit"}:
        abort(403, description="Esta conta está suspensa para novas marcações e edições.")


def _public(row):
    """Monta a referência exibida na árvore e um link para a passagem."""
    note = dict(row)
    note["book_name"] = corpus.BOOKS[note["book"]]
    numbers = str(note["start_verse"])
    if note["end_verse"] != note["start_verse"]:
        numbers += f"–{note['end_verse']}"
    note["reference"] = f"{note['book_name']} {note['chapter']}:{numbers}"
    note["url"] = url_for("bible.chapter", book_code=note["book"], chapter=note["chapter"],
                          versiculo=note["start_verse"], _anchor=f"v{note['start_verse']}")
    return note


def list_notes(book=None, chapter=None):
    """Lista marcas em ordem bíblica. A sessão é a única fonte do proprietário."""
    user = current_user()
    if user is None:
        return []
    sql = f"SELECT {FIELDS} FROM bible_notes WHERE user_id=?"
    params = [user["id"]]
    if book is not None:
        sql += " AND book=?"
        params.append(book)
    if chapter is not None:
        sql += " AND chapter=?"
        params.append(chapter)
    rows = get_db().execute(sql + " ORDER BY chapter,start_verse,end_verse,id", params)
    # A ordem canônica é definida pelo próprio importador da tradução.
    ordered = sorted(rows, key=lambda row: corpus.BOOK_INDEX[row["book"]])
    return [_public(row) for row in ordered]


def _integer(value, *, upper, message, minimum=1):
    """Formulários enviam números como texto. Também aceitamos inteiros JSON."""
    raw = str(value)
    if not raw.isascii() or not raw.isdecimal() or len(raw) > len(str(upper)):
        abort(422, description=message)
    number = int(raw)
    if not minimum <= number <= upper:
        abort(422, description=message)
    return number


@bp.get("")
def index():
    book = request.args.get("book")
    chapter = request.args.get("chapter")
    if book is not None and book not in corpus.BOOKS:
        abort(422, description="Escolha um livro válido.")
    if chapter is not None:
        if book is None:
            abort(422, description="Escolha também o livro desse capítulo.")
        chapter = _integer(chapter, upper=999, message="Escolha um capítulo válido.")
    return jsonify(notes=list_notes(book, chapter))


def _fields():
    """As mutações aceitam tanto FormData como JSON do mesmo navegador."""
    if not request.is_json:
        return request.form
    values = request.get_json()
    if not isinstance(values, dict):
        abort(422, description="Envie os campos da marcação.")
    return values


def _text(fields, name, limit):
    value = fields.get(name, "")
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        label = "título" if name == "title" else "texto da anotação"
        abort(422, description=f"O {label} deve ter até {limit} caracteres de texto.")
    return value.strip()


def _validated(fields):
    """Confere ambos os extremos no acervo, inclusive versículos agrupados.

    Se a edição imprime 28–29 como um único texto, selecionar o verso 29
    marca o bloco inteiro. Um número inexistente não cria referências órfãs.
    """
    book = fields.get("book")
    color = fields.get("color", "gold")
    if not isinstance(book, str) or book not in corpus.BOOKS:
        abort(422, description="Escolha um livro válido.")
    if not isinstance(color, str) or color not in COLORS:
        abort(422, description="Escolha uma das cores disponíveis.")
    chapter = _integer(fields.get("chapter"), upper=999, message="Escolha um capítulo válido.")
    start = _integer(fields.get("start_verse"), upper=999, message="Escolha o versículo inicial.")
    end = _integer(fields.get("end_verse", start), upper=999, message="Escolha o versículo final.")
    if end < start:
        abort(422, description="O versículo final deve vir após o inicial ou ser igual a ele.")
    with closing(corpus._connect()) as db:
        rows = db.execute("""SELECT start_verse,end_verse,text FROM verses
                             WHERE book_code=? AND chapter=? AND end_verse>=? AND start_verse<=?
                               AND text<>'' ORDER BY start_verse""", (book, chapter, start, end)).fetchall()
    if not rows or rows[0]["start_verse"] > start or rows[-1]["end_verse"] < end:
        abort(422, description="Selecione versículos com texto nesse capítulo.")
    texts = [row["text"] for row in rows]
    start_offset, end_offset = fields.get("start_offset"), fields.get("end_offset")
    if start_offset in (None, "") and end_offset in (None, ""):
        start_offset = end_offset = None
    else:
        # Os índices contam pontos de código Unicode. A extremidade final é
        # exclusiva, assim como em texto[inicio:fim]. Em múltiplos versículos,
        # início pertence ao primeiro texto e fim pertence ao último.
        error = "Selecione novamente as palavras da passagem."
        start_offset = _integer(start_offset, minimum=0, upper=len(texts[0]), message=error)
        end_offset = _integer(end_offset, minimum=0, upper=len(texts[-1]), message=error)
        if len(texts) == 1 and start_offset >= end_offset:
            abort(422, description=error)
        texts[-1] = texts[-1][:end_offset]
        texts[0] = texts[0][start_offset:]
    # Guardamos um pequeno trecho bíblico para o índice. Abrir o menu então
    # requer uma só leitura do banco privado, mesmo com muitas marcações.
    excerpt = " ".join(part.strip() for part in texts if part.strip())
    if not excerpt:
        abort(422, description="Selecione uma passagem que contenha texto.")
    if len(excerpt) > 200:
        excerpt = excerpt[:199].rstrip() + "…"
    return (book, chapter, rows[0]["start_verse"], rows[-1]["end_verse"],
            start_offset, end_offset, excerpt, color,
            _text(fields, "title", 100), _text(fields, "body", 3000))


@bp.post("")
def create():
    fields = _fields()
    content = _validated(fields)
    user_id = current_user()["id"]
    token = fields.get("request_id")
    if token is not None:
        try:
            token = str(UUID(str(token)))
        except ValueError:
            abort(422, description="Reabra o formulário para iniciar outra marcação.")
    now = int(time())
    db = get_db()
    with db:
        # Se a conexão cair depois da gravação, repetir o mesmo formulário
        # recupera o registro original. Outras marcas no mesmo trecho são válidas.
        row = db.execute(f"""INSERT INTO bible_notes
            (user_id,book,chapter,start_verse,end_verse,start_offset,end_offset,excerpt,
             color,title,body,created_at,updated_at,request_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,request_id) DO NOTHING
            RETURNING {FIELDS}""", (user_id, *content, now, now, token)).fetchone()
        created = row is not None
        if not created:
            row = db.execute(f"SELECT {FIELDS} FROM bible_notes WHERE user_id=? AND request_id=?",
                             (user_id, token)).fetchone()
    return jsonify(note=_public(row), message="Marcação salva."), 201 if created else 200


def _owner_filter(note_id, fields):
    """A revisão enviada pelo editor evita sobrescrever alterações de outra aba."""
    if note_id > 2**63 - 1:
        abort(404, description="Marcação não encontrada.")
    where = "id=? AND user_id=?"
    params = [note_id, current_user()["id"]]
    if "revision" in fields:
        where += " AND revision=?"
        params.append(_integer(fields["revision"], upper=2**63-1, message="Versão da marcação inválida."))
    return where, params


def _missing_or_changed(note_id):
    # Uma nota de outro perfil dá a mesma resposta que um ID inexistente.
    own = get_db().execute("SELECT 1 FROM bible_notes WHERE id=? AND user_id=?",
                           (note_id, current_user()["id"])).fetchone()
    if own:
        abort(409, description="A marcação mudou em outra aba. Reabra-a para ver a versão atual.")
    abort(404, description="Marcação não encontrada.")


@bp.post("/<int:note_id>")
def edit(note_id):
    fields = _fields()
    content = _validated(fields)
    where, params = _owner_filter(note_id, fields)
    db = get_db()
    with db:
        row = db.execute(f"""UPDATE bible_notes
            SET book=?,chapter=?,start_verse=?,end_verse=?,start_offset=?,end_offset=?,excerpt=?,
                color=?,title=?,body=?,
                updated_at=?,revision=revision+1 WHERE {where} RETURNING {FIELDS}""",
                         (*content, int(time()), *params)).fetchone()
        if row is None:
            _missing_or_changed(note_id)
    return jsonify(note=_public(row), message="Marcação atualizada.")


@bp.post("/<int:note_id>/excluir")
def delete(note_id):
    where, params = _owner_filter(note_id, _fields())
    db = get_db()
    with db:
        if not db.execute(f"DELETE FROM bible_notes WHERE {where}", params).rowcount:
            _missing_or_changed(note_id)
    return jsonify(deleted_id=note_id, message="Marcação excluída.")
