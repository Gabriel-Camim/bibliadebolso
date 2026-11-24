"""Entrada e cadastro com o Facebook para o leitor, a biblioteca e o chat.

A Meta autentica a pessoa. A aplicação mantém o perfil e os aceites no banco
privado e usa a mesma sessão que os outros métodos de entrada.
"""
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, abort, flash, redirect, request, session, url_for

import accounts


bp = Blueprint("facebook_auth", __name__, url_prefix="/auth/facebook")
FLOW_LIFETIME = 600


def _version():
    """A versão dos endpoints é configurável quando a Meta atualizar a Graph API."""
    return accounts._setting("FACEBOOK_GRAPH_VERSION") or "v27.0"


def _configured():
    return bool(accounts._setting("FACEBOOK_APP_ID") and accounts._setting("FACEBOOK_APP_SECRET")
                and re.fullmatch(r"v[0-9]+\.[0-9]+", _version()))


@bp.app_context_processor
def facebook_context():
    """As telas de acesso exibem a disponibilidade sem conhecer a chave secreta."""
    return {"facebook_ready": _configured()}


def _callback_url():
    # Usar o endereço público configurado impede que um cabeçalho Host recebido
    # de fora mude o destino do código de autorização.
    base = accounts._setting("PUBLIC_BASE_URL").rstrip("/")
    return base + url_for("facebook_auth.callback")


def _start(signup):
    """Inicia o diálogo da Meta somente após a escolha explícita desse provedor."""
    destination = accounts._safe_destination(request.form.get("next"))
    back = url_for("accounts.signup" if signup else "accounts.login", next=destination)
    if not _configured():
        flash("O acesso pelo Facebook aguarda a configuração do aplicativo Meta.")
        return redirect(back, code=303)
    if signup and request.form.get("accept_policies") != "yes":
        flash("Leia os documentos e marque o aceite para criar sua conta.")
        return redirect(back, code=303)
    phone = accounts.normalized_mobile(request.form.get("phone")) if signup else None
    if signup and not phone:
        flash(accounts.PHONE_HINT)
        return redirect(url_for("accounts.signup", metodo="facebook", next=destination), code=303)

    # Vinculamos o destino, a validade e o aceite ao navegador que iniciou a
    # tentativa. A Meta recebe apenas o valor aleatório e o callback fixo.
    # Para entrar por Facebook, bastam o nome e o identificador do perfil.
    # Se autorizado, o e-mail serve como contato, nunca como chave de acesso.
    state = secrets.token_urlsafe(32)
    session["facebook_flow"] = dict(state=state, started=int(time.time()), next=destination,
                                    policy=accounts.POLICY_VERSION if signup else None, phone=phone)
    params = urlencode(dict(client_id=accounts._setting("FACEBOOK_APP_ID"),
                            redirect_uri=_callback_url(), response_type="code", state=state,
                            scope="public_profile,email"))
    return redirect(f"https://www.facebook.com/{_version()}/dialog/oauth?{params}", code=303)


@bp.post("/entrar")
def login():
    return _start(signup=False)


@bp.post("/cadastro")
def signup():
    return _start(signup=True)


def _identity(code):
    """Troca o código de uso único no servidor e lê só os campos do cadastro."""
    secret = accounts._setting("FACEBOOK_APP_SECRET")
    graph = "https://graph.facebook.com/" + _version()
    # O segredo só viaja no corpo da conexão HTTPS com o endpoint de tokens.
    # Nenhum token vai para cookies ou para o SQLite.
    response = requests.post(graph + "/oauth/access_token", timeout=10, allow_redirects=False,
                             data={"client_id": accounts._setting("FACEBOOK_APP_ID"),
                                   "client_secret": secret, "code": code, "redirect_uri": _callback_url(),
                                   "grant_type": "authorization_code"})
    response.raise_for_status()
    token = response.json()["access_token"]
    if not isinstance(token, str) or not token:
        raise ValueError("Resposta de token inválida.")
    # appsecret_proof também permite usar a opção de segurança da Meta que
    # exige uma prova do segredo nas consultas posteriores ao Graph.
    proof = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    response = requests.get(graph + "/me", timeout=10, allow_redirects=False,
                            headers={"Authorization": "Bearer " + token},
                            params={"fields": "id,name,email", "appsecret_proof": proof})
    response.raise_for_status()
    data = response.json()
    subject = data["id"]
    if not isinstance(subject, str) or not re.fullmatch(r"[0-9]{1,128}", subject):
        raise ValueError("Perfil inválido.")
    # O e-mail pode faltar se a pessoa preferiu não compartilhá-lo. O ID
    # atribuído pela Meta a este app continua sendo suficiente para entrar.
    email, name = data.get("email") or "", data.get("name") or "Leitor"
    if not isinstance(email, str) or not isinstance(name, str):
        raise ValueError("Campos de perfil inválidos.")
    return f"facebook:{accounts._setting('FACEBOOK_APP_ID')}:{subject}", email.strip()[:254], name.strip()[:200] or "Leitor"


@bp.get("/callback")
def callback():
    """Valida a tentativa antes de usar o código ou consultar qualquer conta."""
    flow = session.get("facebook_flow")
    supplied = request.args.get("state", "")
    if not flow or not secrets.compare_digest(flow["state"].encode(), supplied.encode()):
        abort(400, description="Abra a tela de acesso e inicie uma nova entrada pelo Facebook.")
    session.pop("facebook_flow")
    if not 0 <= time.time() - flow["started"] <= FLOW_LIFETIME:
        abort(400, description="A tentativa de entrada expirou. Entre pelo Facebook novamente.")
    destination = accounts._safe_destination(flow["next"])
    back = url_for("accounts.signup" if flow["policy"] else "accounts.login", next=destination)
    if request.args.get("error"):
        flash("A entrada pelo Facebook foi cancelada.")
        return redirect(back)
    code = request.args.get("code", "")
    if not code or not _configured():
        abort(400, description="Inicie novamente a entrada pelo Facebook.")
    try:
        subject, email, display_name = _identity(code)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # Uma exceção do cliente HTTP pode conter dados da requisição. A tela
        # de acesso usa uma mensagem genérica e permite reiniciar o fluxo.
        flash("O Facebook não pôde confirmar a entrada. Tente novamente.")
        return redirect(back)
    return accounts.complete_social_login("facebook", subject, email, display_name,
                                          flow["policy"], destination, flow.get("phone"))
