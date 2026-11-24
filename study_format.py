"""Formata as respostas salvas para leitura com JavaScript ou sem ele.

O guia usa um subconjunto de Markdown: títulos, ênfase, listas e citações.
A resposta do modelo é sempre texto. Somente este módulo escolhe as tags HTML.
"""
import re
from itertools import groupby

from markupsafe import Markup, escape


# As marcas de código têm precedência: os asteriscos dentro de crases são literais.
INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|__(.+?)__|\*([^*]+?)\*")


def _inline(line):
    """Escapa cada trecho, inclusive o conteúdo entre as marcas de negrito."""
    result = []
    position = 0
    for match in INLINE.finditer(line):
        result.append(escape(line[position:match.start()]))
        tag = ("code", "strong", "strong", "em")[match.lastindex - 1]
        result.append(Markup(f"<{tag}>") + escape(match[match.lastindex]) + Markup(f"</{tag}>"))
        position = match.end()
    result.append(escape(line[position:]))
    return Markup("").join(result)


def _block(line):
    """Converte uma linha em tipo e conteúdo. As listas serão agrupadas depois."""
    line = line.strip()
    if not line:
        return "", ""
    heading = re.match(r"^#{1,6}\s+(.+)$", line)
    if heading:
        return "h3", heading[1]
    if re.fullmatch(r"\*\*[^*]+\*\*|__[^_]+__", line):
        return "h3", line[2:-2]
    if re.match(r"^[-*+]\s+", line):
        return "ul", re.sub(r"^[-*+]\s+", "", line)
    if re.match(r"^\d+[.)]\s+", line):
        return "ol", re.sub(r"^\d+[.)]\s+", "", line)
    if line.startswith(">"):
        return "blockquote", line[1:].strip()
    return "p", line


def study_markdown(text):
    """Produz HTML seguro e sem dependência de CSS para o significado do texto."""
    blocks = (_block(line) for line in (text or "").splitlines())
    result = []
    for tag, group in groupby(blocks, key=lambda block: block[0]):
        lines = [_inline(content) for _, content in group]
        if tag in {"ul", "ol"}:
            items = Markup("").join(Markup("<li>") + line + Markup("</li>") for line in lines)
            result.append(Markup(f"<{tag}>") + items + Markup(f"</{tag}>"))
        elif tag == "h3":
            result.extend(Markup("<h3>") + line + Markup("</h3>") for line in lines)
        elif tag:
            result.append(Markup(f"<{tag}>") + Markup(" ").join(lines) + Markup(f"</{tag}>"))
    return Markup("").join(result)
