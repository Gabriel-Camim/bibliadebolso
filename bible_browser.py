"""Índice e paginação da Bíblia Portuguesa Mundial exibida como um livro aberto."""
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from itertools import groupby
from operator import itemgetter

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, url_for

import corpus
import library

bp = Blueprint("bible", __name__, url_prefix="/biblia")


@dataclass(frozen=True)
class Leaf:
    """Uma página numerada da edição digital, com versículos sempre inteiros."""
    number: int
    book: str
    chapter: int
    first: int
    last: int
    local: int  # Posição da folha dentro deste capítulo.


@lru_cache(maxsize=1)
def _contents():
    """Calcula um índice pequeno e imutável a partir do acervo público.

    Mantemos na memória apenas os limites de cada folha. Para montar uma
    abertura, lemos do disco o texto das duas páginas que a compõem.
    """
    by_book = {code: [] for code in corpus.BOOKS}
    by_chapter = {}
    leaves = []
    with closing(corpus._connect()) as db:
        # A chave primária da edição já segue a ordem livro, capítulo e verso.
        rows = db.execute("""SELECT book_code,chapter,start_verse,end_verse,length(text) AS size
                              FROM verses ORDER BY id""")
        for (book, chapter), lines in groupby(rows, itemgetter("book_code", "chapter")):
            by_book[book].append(chapter)
            chapter_leaves = []
            first = last = None
            used = 0
            for verse in lines:
                # A abertura de capítulo reserva espaço para a capitular e
                # o título. Cada número de verso também ocupa espaço no texto.
                cost = max(24, verse["size"]) + 12
                budget = 530 if not chapter_leaves else 780
                if first is not None and used + cost > budget:
                    leaf = Leaf(len(leaves) + 1, book, chapter, first, last, len(chapter_leaves) + 1)
                    leaves.append(leaf)
                    chapter_leaves.append(leaf)
                    first, used = None, 0
                if first is None:
                    first = verse["start_verse"]
                last = verse["end_verse"]
                used += cost
            leaf = Leaf(len(leaves) + 1, book, chapter, first, last, len(chapter_leaves) + 1)
            leaves.append(leaf)
            chapter_leaves.append(leaf)
            by_chapter[(book, chapter)] = chapter_leaves
    return by_book, by_chapter, leaves


def _number(value):
    """Valida a localização sem converter cadeias arbitrariamente longas."""
    if not value or len(value) > 3 or not value.isascii() or not value.isdecimal() or int(value) < 1:
        abort(404)
    return int(value)


def _verse_url(book_code, chapter, number):
    return url_for("bible.chapter", book_code=book_code, chapter=chapter,
                   versiculo=number, _anchor=f"v{number}")


def _leaf_url(leaf):
    """A mudança de página não seleciona um versículo nem registra um sorteio."""
    return url_for("bible.chapter", book_code=leaf.book, chapter=leaf.chapter,
                   folha=leaf.local if leaf.local > 1 else None)


@bp.get("")
def index():
    """Transforma a busca de uma passagem em um endereço permanente."""
    by_book, _, leaves = _contents()
    book = request.args.get("livro", leaves[0].book)
    chapters = by_book.get(book)
    if not chapters:
        abort(404)
    chapter = _number(request.args["capitulo"]) if "capitulo" in request.args else chapters[0]
    if chapter not in chapters:
        abort(404)
    if request.args.get("versiculo"):
        return redirect(_verse_url(book, chapter, _number(request.args["versiculo"])))
    # O menu do site também pode abrir direto na aba de anotações. O fragmento
    # identifica o mesmo painel em um telefone e em uma janela de computador.
    view = request.args.get("indice")
    if view in {"1", "notas"}:
        return redirect(url_for("bible.chapter", book_code=book, chapter=chapter,
                                indice=view, _anchor="indice-biblico"))
    return redirect(url_for("bible.chapter", book_code=book, chapter=chapter))


@bp.get("/versiculo/<int:verse_id>")
def locate(verse_id):
    """Tanto o NFC quanto a biblioteca usam o ID permanente do versículo."""
    verse = corpus.verse_by_id(verse_id)
    if not verse:
        abort(404)
    return redirect(_verse_url(verse["book_code"], verse["chapter"], int(verse["verse"].split("-")[0])))


def _verse_numbers(db, book, chapter):
    """Lê os números impressos. Há grupos como 28–29 e lacunas na fonte."""
    return [dict(row) for row in db.execute("""SELECT start_verse AS number,verse AS label
                            FROM verses WHERE book_code=? AND chapter=? ORDER BY start_verse""", (book, chapter))]


@bp.get("/indice/<book_code>/<int:chapter>")
def passage_numbers(book_code, chapter):
    """O índice carrega somente os números do capítulo escolhido no seletor."""
    if (book_code, chapter) not in _contents()[1]:
        abort(404)
    with closing(corpus._connect()) as db:
        return jsonify(_verse_numbers(db, book_code, chapter))


@bp.get("/<book_code>/<int:chapter>")
def chapter(book_code, chapter):
    """Abre a dupla de folhas que contém a página ou referência solicitada."""
    by_book, by_chapter, leaves = _contents()
    chapters = by_chapter.get((book_code, chapter))
    if not chapters:
        abort(404)

    selected = None
    saved = False
    with closing(corpus._connect()) as db:
        numbers = _verse_numbers(db, book_code, chapter)
        if "versiculo" in request.args:
            number = _number(request.args["versiculo"])
            selected = db.execute("""SELECT id,verse,start_verse,text FROM verses
                                WHERE book_code=? AND chapter=? AND start_verse<=? AND end_verse>=?""",
                                  (book_code, chapter, number, number)).fetchone()
            if selected is None:
                abort(404)
            if selected["start_verse"] != number:
                return redirect(_verse_url(book_code, chapter, selected["start_verse"]))
            current = next(leaf for leaf in chapters if leaf.first <= number <= leaf.last)
            if g.user and selected["text"]:
                # Virar uma folha é uma navegação. Apenas selecionar um verso
                # gera uma abertura no histórico privado de quem a autorizou.
                if request.method == "GET":
                    library.record_browsed(selected["id"])
                saved = library.is_favorite(selected["id"])
        else:
            local = _number(request.args.get("folha", "1"))
            if local > len(chapters):
                abort(404)
            current = chapters[local - 1]

        # Na sequência 1-2, 3-4, ... o celular sempre mostra a folha pedida,
        # inclusive quando a referência ficou do lado direito no desktop.
        left = ((current.number - 1) // 2) * 2
        spread = []
        for leaf in leaves[left:left + 2]:
            verses = db.execute("""SELECT id,verse,start_verse,text FROM verses WHERE book_code=? AND chapter=?
                                   AND start_verse BETWEEN ? AND ? ORDER BY start_verse""",
                                (leaf.book, leaf.chapter, leaf.first, leaf.last)).fetchall()
            spread.append((leaf, verses))

    def page_link(index):
        return _leaf_url(leaves[index]) if 0 <= index < len(leaves) else None

    # Dentro de um mesmo capítulo já estão na tela os 81 livros, o menu e
    # o editor. Cada virada devolve apenas as folhas. Um link comum e a
    # mudança de capítulo ainda recebem a página completa com o novo índice.
    fragment = request.headers.get("X-Bible-Chapter") == f"{book_code}/{chapter}"
    template = "bible_spread.html" if fragment else "bible_browser.html"
    page_title = f"{corpus.BOOKS[book_code]} {chapter}"
    if selected:
        page_title += f":{selected['verse']}"
    return render_template(template, books=corpus.BOOKS, by_book=by_book, book_code=book_code,
                           page_title=page_title + " · bibliadebolso.com",
                           chapter=chapter, numbers=numbers, current=current, spread=spread,
                           selected=selected, saved=saved, verse_url=_verse_url, total_pages=len(leaves),
                           previous_spread=page_link(left - 2), next_spread=page_link(left + 2),
                           previous_leaf=page_link(current.number - 2), next_leaf=page_link(current.number),
                           number_url=url_for("bible.passage_numbers", book_code="BOOK", chapter=999))
