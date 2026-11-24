"""Primeiros passos de leitura. Guardamos só um indicador de guia apresentado."""
from flask import Blueprint, current_app, g, jsonify, request, session

from core import write_one


bp = Blueprint("tutorial", __name__)
SEEN_COOKIE = "biblia_guia_visto"


@bp.before_app_request
def first_reading():
    """Observa a sessão antes que o sorteio da página atual escreva last_verse."""
    if (request.method != "GET" or request.headers.get("X-Bible-Chapter") or
            request.endpoint not in {"reader", "bible.chapter", "accounts.profile", "study.start", "study.chat"}):
        return
    user = g.user
    # O cookie opcional é sempre o mesmo valor. Não identifica um leitor,
    # dispositivo, passagem ou etapa. A preferência por essenciais prevalece.
    browser_seen = session.get("tutorial_seen") or (
        session.get("cookie_choice") != "essential" and request.cookies.get(SEEN_COOKIE) == "1")
    if user:
        if user["tutorial_seen"] or user["disabled"]:
            return
        if browser_seen:
            # Quem conheceu o guia como visitante leva a escolha para o perfil
            # recém-criado, inclusive quando entrar em um segundo computador.
            write_one("UPDATE users SET tutorial_seen=1 WHERE id=? AND tutorial_seen=0", (user["id"],))
            return
    elif browser_seen or session.get("last_verse") or session.get("cookie_choice"):
        # Os dois últimos campos também reconhecem sessões da versão anterior.
        return
    g.offer_tutorial = True


@bp.after_app_request
def note_invitation(response):
    """A resposta de navegação é dona da sessão que contém o versículo sorteado."""
    if g.get("offer_tutorial") and response.status_code == 200 and response.mimetype == "text/html":
        # Se a pessoa pular o tutorial e sortear imediatamente, um POST em
        # segundo plano jamais deve sobrescrever o novo sorteio com o cookie
        # antigo. O próprio HTML inicial já leva este bit na sessão assinada.
        session["tutorial_seen"] = True
    return response


def sync_preference_cookie(response):
    """Persiste a escolha fora da sessão apenas se o leitor permitiu preferências."""
    if session.get("cookie_choice") == "preferences" and session.get("tutorial_seen"):
        response.set_cookie(SEEN_COOKIE, "1", max_age=365*86400, httponly=True,
                            secure=current_app.config["SESSION_COOKIE_SECURE"], samesite="Lax")
    elif session.get("cookie_choice") == "essential" and SEEN_COOKIE in request.cookies:
        response.delete_cookie(SEEN_COOKIE, httponly=True,
                               secure=current_app.config["SESSION_COOKIE_SECURE"], samesite="Lax")
    return response


@bp.post("/tutorial/visto")
def remember():
    """Chamado ao abrir o diálogo de verdade. O mesmo POST pode ser repetido."""
    if g.user and not g.user["tutorial_seen"]:
        write_one("UPDATE users SET tutorial_seen=1 WHERE id=? AND tutorial_seen=0", (g.user["id"],))
    if not session.get("tutorial_seen"):
        # Reabertura manual numa página que não tinha convite automático.
        session["tutorial_seen"] = True
    return sync_preference_cookie(jsonify(ok=True))
