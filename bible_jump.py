"""Atalhos do índice para abrir uma referência ou uma folha da edição digital."""
import re
from contextlib import closing

from flask import Blueprint, flash, jsonify, redirect, request, url_for

import bible_browser
import corpus

bp = Blueprint("bible_jump", __name__)

# As abreviaturas são aliases de navegação. A grafia exibida vem sempre da BPM.
# Remover espaços permite escrever tanto "1 Co" quanto "1Co".
def _key(text):
    return corpus._fold(text).replace(" ", "").replace(".", "")


ALIASES = {_key(label): code for label, code in corpus.BOOK_ALIASES.items()}
ALIASES.update({_key(code): code for code in corpus.BOOKS})
ALIASES.update({
    "gn":"GEN", "ex":"EXO", "lv":"LEV", "nm":"NUM", "dt":"DEU", "js":"JOS",
    "jz":"JDG", "rt":"RUT", "1sm":"1SA", "2sm":"2SA", "1rs":"1KI", "2rs":"2KI",
    "1cr":"1CH", "2cr":"2CH", "ed":"EZR", "ne":"NEH", "et":"EST", "sl":"PSA",
    "pv":"PRO", "ec":"ECC", "ct":"SNG", "is":"ISA", "jr":"JER", "lm":"LAM",
    "ez":"EZK", "dn":"DAN", "os":"HOS", "jl":"JOL", "am":"AMO", "ob":"OBA",
    "jn":"JON", "mq":"MIC", "na":"NAM", "hc":"HAB", "sf":"ZEP", "ag":"HAG",
    "zc":"ZEC", "ml":"MAL", "mt":"MAT", "mc":"MRK", "lc":"LUK", "jo":"JHN",
    "at":"ACT", "rm":"ROM", "gl":"GAL", "ef":"EPH", "fp":"PHP", "cl":"COL",
    "1ts":"1TH", "2ts":"2TH", "1tm":"1TI", "2tm":"2TI", "tt":"TIT", "fm":"PHM",
    "hb":"HEB", "tg":"JAS", "1pe":"1PE", "2pe":"2PE", "1jo":"1JN", "2jo":"2JN",
    "3jo":"3JN", "jd":"JUD", "ap":"REV",
})


def _book(label):
    # A acentuação distingue o livro de Jó da abreviatura de João.
    return "JOB" if label.strip().casefold() == "jó" else ALIASES.get(_key(label))


def destination(query):
    """Resolve a entrada no mesmo catálogo que determina as páginas do livro."""
    query = " ".join(query.split())
    if not query or len(query) > 80:
        raise ValueError("Digite uma passagem ou um número de página.")
    books, chapters, pages = bible_browser._contents()
    page = re.fullmatch(r"(?:(?:pagina|pag|p)\.?\s*)?(\d{1,6}|\d{1,3}\.\d{3})", corpus._fold(query))
    if page:
        number = int(page[1].replace(".", ""))
        if not 1 <= number <= len(pages):
            raise ValueError(f"Escolha uma página de 1 a {len(pages):,}.".replace(",", "."))
        return bible_browser._leaf_url(pages[number - 1])

    # Testar o nome completo primeiro preserva livros cujo título contém números,
    # como "Salmo 151". Para os demais, aceitamos capítulo e versículo opcionais.
    book = _book(query)
    if book:
        return url_for("bible.chapter", book_code=book, chapter=books[book][0])
    ref = re.fullmatch(r"(.+?)\s*(\d{1,3})(?:\s*[:,]\s*(\d{1,3}))?", query)
    book = _book(ref[1]) if ref else None
    if not book:
        raise ValueError("Use uma referência como João 3:16, ou o número da página.")
    chapter = int(ref[2])
    if (book, chapter) not in chapters:
        raise ValueError(f"Confira o capítulo escolhido para {corpus.BOOKS[book]}.")
    if ref[3] is None:
        return url_for("bible.chapter", book_code=book, chapter=chapter)
    number = int(ref[3])
    # Algumas linhas da edição reúnem dois versos. O link deve apontar para
    # o início da linha que contém o número procurado.
    with closing(corpus._connect()) as db:
        row = db.execute("""SELECT start_verse FROM verses WHERE book_code=? AND chapter=?
                            AND ? BETWEEN start_verse AND end_verse""", (book, chapter, number)).fetchone()
    if row is None:
        raise ValueError(f"Confira o versículo em {corpus.BOOKS[book]} {chapter}.")
    return bible_browser._verse_url(book, chapter, row["start_verse"])


@bp.get("/biblia/localizar")
def locate():
    """A busca funciona por formulário comum e também mostra erros no índice via JS."""
    wants_json = request.accept_mimetypes.best == "application/json"
    try:
        target = destination(request.args.get("q", ""))
    except ValueError as error:
        if wants_json:
            return jsonify(message=str(error)), 422
        flash(str(error))
        return redirect(url_for("bible.index", indice=1, _anchor="indice-biblico"))
    return jsonify(url=target) if wants_json else redirect(target)
