"""Consulta local à Bíblia Portuguesa Mundial, compartilhada pelo leitor e pelo chat.

O arquivo público contém apenas as Escrituras e seu índice. Não recebe contas,
perguntas ou histórico. O importador no final deste módulo o reconstrói com
Python e SQLite a partir do ZIP VPL disponibilizado pelo eBible.org.
"""

import argparse
import hashlib
import math
import os
import re
import secrets
import sqlite3
import tempfile
import unicodedata
import zipfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree


CORPUS_PATH = Path(__file__).resolve().parent / "data" / "biblia.sqlite3"
SOURCE_URL = "https://ebible.org/Scriptures/porbrbsl_vpl.zip"

# A ordem aqui faz parte do identificador permanente usado nos links do site.
# Uma revisão do texto que acrescente versículos não desloca os IDs seguintes.
BOOKS = {
    "GEN":"Gênesis", "EXO":"Êxodo", "LEV":"Levítico", "NUM":"Números", "DEU":"Deuteronômio",
    "JOS":"Josué", "JDG":"Juízes", "RUT":"Rute", "1SA":"1 Samuel", "2SA":"2 Samuel",
    "1KI":"1 Reis", "2KI":"2 Reis", "1CH":"1 Crônicas", "2CH":"2 Crônicas", "EZR":"Esdras",
    "NEH":"Neemias", "EST":"Ester", "JOB":"Jó", "PSA":"Salmos", "PRO":"Provérbios",
    "ECC":"Eclesiastes", "SNG":"Cantares de Salomão", "ISA":"Isaías", "JER":"Jeremias",
    "LAM":"Lamentações", "EZK":"Ezequiel", "DAN":"Daniel", "HOS":"Oseias", "JOL":"Joel",
    "AMO":"Amós", "OBA":"Obadias", "JON":"Jonas", "MIC":"Miqueias", "NAM":"Naum",
    "HAB":"Habacuque", "ZEP":"Sofonias", "HAG":"Ageu", "ZEC":"Zacarias", "MAL":"Malaquias",
    "TOB":"Tobias", "JDT":"Judite", "ESG":"Ester (Grego)", "WIS":"Sabedoria", "SIR":"Sirácida",
    "BAR":"Baruc", "1MA":"1 Macabeus", "2MA":"2 Macabeus", "1ES":"1 Esdras", "MAN":"Oração de Manassés",
    "PS2":"Salmo 151", "3MA":"3 Macabeus", "2ES":"2 Esdras", "4MA":"4 Macabeus", "DAG":"Daniel (Grego)",
    "MAT":"Mateus", "MRK":"Marcos", "LUK":"Lucas", "JHN":"João", "ACT":"Atos",
    "ROM":"Romanos", "1CO":"1 Coríntios", "2CO":"2 Coríntios", "GAL":"Gálatas", "EPH":"Efésios",
    "PHP":"Filipenses", "COL":"Colossenses", "1TH":"1 Tessalonicenses", "2TH":"2 Tessalonicenses",
    "1TI":"1 Timóteo", "2TI":"2 Timóteo", "TIT":"Tito", "PHM":"Filemom", "HEB":"Hebreus",
    "JAS":"Tiago", "1PE":"1 Pedro", "2PE":"2 Pedro", "1JN":"1 João", "2JN":"2 João",
    "3JN":"3 João", "JUD":"Judas", "REV":"Apocalipse",
}
BOOK_INDEX = {code: index for index, code in enumerate(BOOKS)}

# Palavras gramaticais muito comuns acrescentariam ruído à busca temática.
# Os termos são normalizados só para procurar. O leitor recebe o texto original.
STOPWORDS = set("""a ao aos as com como da das de del dele deles do dos e ela elas ele eles em
entre era eram essa essas esse esses esta estas este estes eu foi foram ha ja la lhe lhes me
mes meu meus minha minhas muito na nas no nos nossa nosso nossos numa num o os ou para pela
pelas pelo pelos por porque qual que quem se seu seus sua suas tambem te tem tendo ti tinha tudo
tua tuas teu teus um uma umas uns vos vossa vosso versiculo versiculos passagem explique explicar
contexto estudo estudar significado significa""".split())


def _fold(text):
    """Permite, por exemplo, que a pergunta 'fe' encontre 'fé' na edição."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _words(text):
    return [word for word in re.findall("[a-z]{2,}", _fold(text)) if word not in STOPWORDS]


def _connect():
    """mode=ro impede que um caminho incorreto crie acidentalmente uma Bíblia vazia."""
    db = sqlite3.connect(CORPUS_PATH.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _public(row):
    if row is None:
        return None
    record = dict(row)
    record["book"] = BOOKS[record["book_code"]]
    record["reference"] = f"{record['book']} {record['chapter']}:{record['verse']}"
    return record


FIELDS = "id, book_code, chapter, verse, text"


def verse_by_id(verse_id):
    """Retorna um registro identificável ou None se o ID não existe na edição."""
    with closing(_connect()) as db:
        return _public(db.execute(f"SELECT {FIELDS} FROM verses WHERE id=?", (verse_id,)).fetchone())


def random_verse(previous_id=None):
    """Sorteia uniformemente uma passagem com texto, pulando a anterior.

    O importador numera consecutivamente só os registros legíveis. Essa fila
    evita sortear lacunas editoriais e dispensa ordenar a Bíblia inteira.
    """
    with closing(_connect()) as db:
        count = db.execute("SELECT max(position) FROM readings").fetchone()[0]
        prior = db.execute("SELECT position FROM readings WHERE verse_id=?", (previous_id,)).fetchone()
        if prior and count > 1:
            position = 1 + secrets.randbelow(count-1)
            if position >= prior[0]:
                position += 1
        else:
            position = 1 + secrets.randbelow(count)
        row = db.execute(f"""SELECT {FIELDS} FROM verses WHERE id=(
            SELECT verse_id FROM readings WHERE position=?)""", (position,)).fetchone()
        return _public(row)


# Nas perguntas com uma citação explícita, a localização exata tem prioridade.
# Ordenar nomes por comprimento distingue "1 João" de "João", por exemplo.
BOOK_ALIASES = {_fold(name): code for code, name in BOOKS.items()}
BOOK_ALIASES.update({"salmo":"PSA","eclesiastico":"SIR","cantico dos canticos":"SNG"})
REFERENCE_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(name) for name in sorted(BOOK_ALIASES, key=len, reverse=True))
    + r")\s+(\d{1,3})\s*[:,]\s*(\d{1,3})(?!\d)")


def search_passages(query, verse_id=None, limit=5):
    """Retorna citações verificáveis para uma pergunta e para o versículo aberto.

    A lista inclui a passagem aberta, as referências expressas na pergunta,
    até dois vizinhos do mesmo capítulo e os resultados por similaridade.
    """
    limit = max(0, min(int(limit), 30))
    if not limit:
        return []
    records = {}
    with closing(_connect()) as db:
        def remember(row):
            # O dicionário conserva a ordem e evita repetir uma mesma passagem.
            if row is not None and row["text"] and len(records) < limit:
                records[row["id"]] = _public(row)

        anchor = db.execute(f"SELECT {FIELDS} FROM verses WHERE id=?", (verse_id,)).fetchone()
        remember(anchor)
        for match in REFERENCE_PATTERN.finditer(_fold(query or "")):
            name, chapter, number = match.groups()
            remember(db.execute(f"""SELECT {FIELDS} FROM verses WHERE book_code=? AND chapter=?
                AND start_verse<=? AND end_verse>=?""", (BOOK_ALIASES[name], chapter, number, number)).fetchone())
        if anchor:
            # A pesquisa não cruza o limite do capítulo ao compor o contexto.
            for op, direction in (("<", "DESC"), (">", "ASC")):
                remember(db.execute(f"""SELECT {FIELDS} FROM verses WHERE book_code=? AND chapter=?
                    AND id {op} ? AND text<>'' ORDER BY id {direction} LIMIT 1""",
                    (anchor["book_code"], anchor["chapter"], anchor["id"])).fetchone())

        # Cada dimensão do vetor é uma palavra do vocabulário. TF mede a
        # frequência local e IDF dá mais importância aos termos distintivos.
        terms = dict(Counter(_words(query or "")).most_common(100))
        if terms and len(records) < limit:
            marks = ",".join("?" for _ in terms)
            vocab = db.execute(f"SELECT id,word,idf FROM terms WHERE word IN ({marks})", tuple(terms)).fetchall()
            if vocab:
                weights = [(term["id"], (1+math.log(terms[term["word"]])) * term["idf"]) for term in vocab]
                norm = math.sqrt(sum(weight**2 for _, weight in weights))
                # Os dois vetores têm comprimento 1. O produto escalar soma
                # apenas dimensões em comum e equivale à similaridade cosseno.
                pairs = ",".join("(?,?)" for _ in weights)
                args = [item for term_id, weight in weights for item in (term_id, weight/norm)]
                matches = db.execute(f"""WITH query_vector(term_id,weight) AS (VALUES {pairs})
                    SELECT p.verse_id, sum(q.weight*p.weight) AS score FROM query_vector q
                    JOIN postings p ON p.term_id=q.term_id GROUP BY p.verse_id
                    ORDER BY score DESC,p.verse_id LIMIT ?""", (*args, limit + len(records)))
                for match in matches:
                    remember(db.execute(f"SELECT {FIELDS} FROM verses WHERE id=?", (match[0],)).fetchone())
        return list(records.values())


def import_corpus(archive, target=CORPUS_PATH):
    """Constrói e verifica uma cópia nova antes de substituir o banco servido."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        # O XML fornece códigos padronizados e mantém faixas como 28-29.
        root = ElementTree.fromstring(bundle.read("porbrbsl_vpl.xml"))
    if root.tag != "verseFile" or {row.get("b") for row in root} != set(BOOKS):
        raise ValueError("Acervo incompleto ou edição diferente da Bíblia Portuguesa Mundial.")

    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".sqlite3")
    os.close(handle)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript("""
                CREATE TABLE verses(id INTEGER PRIMARY KEY,book_code TEXT NOT NULL,
                    chapter INTEGER NOT NULL,verse TEXT NOT NULL,start_verse INTEGER NOT NULL,
                    end_verse INTEGER NOT NULL,text TEXT NOT NULL,UNIQUE(book_code,chapter,start_verse));
                CREATE TABLE readings(position INTEGER PRIMARY KEY,verse_id INTEGER NOT NULL UNIQUE);
                CREATE TABLE terms(id INTEGER PRIMARY KEY,word TEXT NOT NULL UNIQUE,idf REAL NOT NULL);
                CREATE TABLE postings(term_id INTEGER NOT NULL,verse_id INTEGER NOT NULL,weight REAL NOT NULL,
                    PRIMARY KEY(term_id,verse_id)) WITHOUT ROWID;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            # Cada versículo é um documento, inclusive quando a edição reúne
            # uma faixa. As lacunas são preservadas literalmente na tabela.
            frequencies = Counter()
            readable = 0
            for element in root:
                code, chapter, label = element.attrib["b"], int(element.attrib["c"]), element.attrib["v"]
                if not re.fullmatch(r"\d+(?:-\d+)?", label):
                    raise ValueError(f"Numeração desconhecida em {code} {chapter}:{label}")
                start, end = int(label.split("-")[0]), int(label.split("-")[-1])
                if not (1 <= chapter < 1000 and 1 <= start <= end < 1000):
                    raise ValueError(f"Coordenada fora da faixa em {code} {chapter}:{label}")
                verse_id = BOOK_INDEX[code]*1_000_000 + (chapter-1)*1000 + start
                text = "".join(element.itertext()).strip()
                db.execute("INSERT INTO verses VALUES(?,?,?,?,?,?,?)", (verse_id,code,chapter,label,start,end,text))
                if text:
                    readable += 1
                    db.execute("INSERT INTO readings VALUES(?,?)",(readable,verse_id))
                    frequencies.update(set(_words(text)))
            if not readable:
                raise ValueError("O arquivo recebido não contém texto para leitura.")

            # Primeira passagem: quantidade de documentos que contêm cada
            # palavra. Segunda: pesos normalizados de cada documento.
            vocabulary = {}
            for term_id, word in enumerate(sorted(frequencies), 1):
                idf = 1 + math.log((1+readable)/(1+frequencies[word]))
                vocabulary[word] = (term_id, idf)
                db.execute("INSERT INTO terms VALUES(?,?,?)",(term_id,word,idf))
            for verse_id, text in db.execute("SELECT id,text FROM verses WHERE text<>'' ORDER BY id"):
                weights = [(vocabulary[word][0], (1+math.log(count))*vocabulary[word][1])
                           for word, count in Counter(_words(text)).items()]
                norm = math.sqrt(sum(weight**2 for _, weight in weights))
                db.executemany("INSERT INTO postings VALUES(?,?,?)",
                               ((term_id,verse_id,weight/norm) for term_id,weight in weights))
            with open(archive, "rb") as source:
                digest = hashlib.file_digest(source,"sha256").hexdigest()
            metadata = {"edition":"Bíblia Portuguesa Mundial", "source":SOURCE_URL, "license":"Domínio público",
                        "archive_sha256":digest, "records":str(len(root)), "readable":str(readable),
                        "books":str(len(BOOKS)), "index":"TF-IDF com normalização L2 por versículo"}
            db.executemany("INSERT INTO metadata VALUES(?,?)", metadata.items())
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("O SQLite detectou uma falha de integridade no novo acervo.")
        os.replace(temporary,target)
        return metadata
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    # Exemplo: python corpus.py instance/porbrbsl_vpl.zip
    parser = argparse.ArgumentParser(description="Importar o ZIP VPL porbrbsl para o acervo local.")
    parser.add_argument("zip",type=Path)
    parser.add_argument("--saida",type=Path,default=CORPUS_PATH)
    options = parser.parse_args()
    result = import_corpus(options.zip, options.saida)
    print(f"{result['records']} registros em {result['books']} livros. {result['readable']} textos para leitura.")
