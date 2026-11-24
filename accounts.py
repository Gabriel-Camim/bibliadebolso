"""Contas do Bíblia de Bolso.

O cadastro reúne logins por senha, Google e Facebook. O provedor faz parte
da identidade. O navegador recebe uma sessão assinada pelo Flask,
enquanto o cadastro e os aceites ficam no banco da aplicação.
"""

import os
import re
import secrets
from functools import wraps
from urllib.parse import unquote

from authlib.common.errors import AuthlibBaseError
from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint, abort, current_app, flash, g, redirect, render_template,
    request, session, url_for,
)
from requests.exceptions import RequestException
from joserfc.errors import JoseError
import phonenumbers

from core import get_db


bp = Blueprint("accounts", __name__)

# Ao publicar uma alteração material, o operador muda a versão. Na próxima
# entrada, o site pede que a pessoa leia os novos documentos antes de continuar.
POLICY_VERSION = "2026-11-24.2"
PAGE_SIZE = 25
PHONE_HINT = "Informe um celular com DDD. Para outros países, comece com + e o código do país."


def normalized_mobile(value):
    """Confere o plano de numeração e grava uma forma única, como +5511998765432."""
    value = (value or "").strip()
    # O analisador também entende ramais, letras e texto livre. No cadastro
    # aceitamos apenas um número e os separadores comuns de um campo telefônico.
    if not re.fullmatch(r"\+?[0-9() .-]{7,32}", value):
        return None
    try:
        parsed = phonenumbers.parse(value, "BR")
    except phonenumbers.NumberParseException:
        return None
    # Em alguns países, um número válido de celular usa os mesmos prefixos
    # dos fixos. A validação do formato não comprova a posse da linha.
    if phonenumbers.number_type(parsed) not in (
        phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE
    ):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _setting(name):
    """Permite configurar via ambiente no servidor ou pelo app nos testes."""
    return str(current_app.config.get(name) or os.environ.get(name) or "").strip()


def init_accounts(db):
    """Cria cadastro, registro de aceite e o contador histórico de cadastros."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT NOT NULL UNIQUE,
            auth_provider TEXT NOT NULL DEFAULT 'google',
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            phone_e164 TEXT,
            tutorial_seen INTEGER NOT NULL DEFAULT 0 CHECK(tutorial_seen IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS account_consents (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            version TEXT NOT NULL,
            accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, version)
        );

        -- Uma conta removida continua contando como um cadastro histórico,
        -- mas o contador não guarda nome, endereço de e-mail nem ID do Google.
        CREATE TABLE IF NOT EXISTS account_totals (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            created INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO account_totals (id, created)
            SELECT 1, COUNT(*) FROM users;
        CREATE TRIGGER IF NOT EXISTS count_new_account AFTER INSERT ON users
        BEGIN
            UPDATE account_totals SET created = created + 1 WHERE id = 1;
        END;
    """)
    # Antes das outras formas de acesso, todos os cadastros eram do Google.
    # A migração preserva os IDs usados por anotações, faturas e conversas.
    with db:
        db.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "auth_provider" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT NOT NULL DEFAULT 'google'")
        if "phone_e164" not in columns:
            # NULL identifica os cadastros anteriores à coleta de celular.
            db.execute("ALTER TABLE users ADD COLUMN phone_e164 TEXT")
        if "tutorial_seen" not in columns:
            # Quem já tinha cadastro conhece a plataforma. Somente os perfis
            # novos começam com o guia de primeiros passos ainda disponível.
            db.execute("ALTER TABLE users ADD COLUMN tutorial_seen INTEGER NOT NULL DEFAULT 0 CHECK(tutorial_seen IN (0,1))")
            db.execute("UPDATE users SET tutorial_seen=1")


@bp.record_once
def configure_google(state):
    """Cada instância Flask tem um cliente OAuth próprio, inclusive nos testes."""
    app = state.app
    oauth = OAuth(app)
    # O documento de descoberta do Google anuncia chaves e URLs oficiais. A
    # biblioteca confere assinatura, emissor, destinatário, validade e nonce.
    oauth.register(
        name="google",
        client_id=app.config.get("GOOGLE_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=app.config.get("GOOGLE_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    app.extensions["accounts_google"] = oauth


def _safe_destination(value):
    """Aceita apenas caminhos deste site para voltar ao versículo após entrar."""
    if not value or len(value) > 1800:
        return "/conta"
    # Alguns navegadores tratam a barra invertida como barra normal. Também
    # verificamos a forma decodificada para manter o redirecionamento interno.
    decoded = unquote(value)
    if (not decoded.startswith("/") or decoded.startswith("//") or "\\" in decoded
            or any(ord(char) < 32 for char in decoded)):
        return "/conta"
    return value


def current_user():
    """Retorna a linha do cadastro, carregada uma vez por requisição."""
    return getattr(g, "user", None)


def _renew_session():
    """Recomeça a sessão de acesso preservando as escolhas do leitor anônimo."""
    # O aceite de cookies pertence ao navegador. Entrar ou sair da conta não
    # deve reativar cookies opcionais nem fazer reaparecer o pedido de aceite.
    cookie_choice = session.get("cookie_choice")
    last_verse = session.get("last_verse")
    saw_tutorial = session.get("tutorial_seen")
    session.clear()
    if cookie_choice in {"essential", "preferences"}:
        session["cookie_choice"] = cookie_choice
    if isinstance(last_verse, int):
        session["last_verse"] = last_verse
    if saw_tutorial:
        session["tutorial_seen"] = True


def _is_owner(user):
    """O 'sub' identifica uma conta Google mesmo quando o e-mail é alterado."""
    owner = _setting("OWNER_GOOGLE_SUB")
    return bool(user and owner and not user["disabled"]
                and user["auth_provider"] == "google" and user["google_sub"] == owner)


@bp.before_app_request
def load_account():
    """Revalida a conta no banco mesmo que o navegador traga uma sessão antiga."""
    g.user = None
    # Entregamos esses recursos sem tocar no cookie de autenticação. Assim o
    # cache público de um arquivo estático pode servir todos os visitantes.
    if request.endpoint in {"static", "health", "billing.webhook"}:
        return
    user_id = session.get("user_id")
    if user_id is None:
        return
    db = get_db()
    if "password_version" in session:
        # Um único acesso ao banco remoto lê a conta e valida a versão da
        # senha. Uma troca de senha invalida imediatamente as sessões antigas.
        g.user = db.execute("""SELECT u.* FROM users u JOIN password_credentials p ON p.user_id=u.id
                               WHERE u.id=? AND u.auth_provider='password' AND p.version=?""",
                            (user_id, session["password_version"])).fetchone()
    else:
        g.user = db.execute("SELECT * FROM users WHERE id=? AND auth_provider!='password'", (user_id,)).fetchone()
    if g.user is None:
        _renew_session()
        return
    if not g.user["phone_e164"] and request.endpoint and request.endpoint not in {
        # Mesmo quem prefere encerrar a conta pode consultar a política,
        # gerenciar a cobrança e apagar os dados já fornecidos.
        "accounts.phone", "accounts.profile", "accounts.delete_account", "accounts.logout",
        "accounts.login", "accounts.signup", "accounts.google_callback",
        "facebook_auth.login", "facebook_auth.signup", "facebook_auth.callback",
        "password_auth.login", "password_auth.signup", "password_auth.change_password",
        "billing.portal", "information", "cookies", "tutorial.remember",
    }:
        destination = request.full_path.rstrip("?") if request.method == "GET" else "/conta"
        return redirect(url_for("accounts.phone", next=_safe_destination(destination)), code=303)


@bp.app_context_processor
def account_context():
    """Permite ao cabeçalho mostrar os controles adequados a cada pessoa."""
    return {"is_admin": _is_owner(current_user())}


def login_required(view):
    """Protege uma página privada e guarda seu caminho durante a autenticação."""
    @wraps(view)
    def checked(*args, **kwargs):
        user = current_user()
        if user is None:
            # Após o login fazemos GET. Um formulário POST anterior não deve
            # ser reenviado automaticamente, especialmente no checkout.
            next_url = request.full_path.rstrip("?") if request.method == "GET" else "/conta"
            return redirect(url_for("accounts.login", next=next_url))
        if user["disabled"] and request.endpoint not in {
            "accounts.profile", "accounts.phone", "accounts.delete_account", "billing.portal", "password_auth.change_password",
            # Mesmo após suspensão, o titular pode ler/apagar dados antigos
            # e revogar o consentimento. O envio de perguntas tem outra rota.
            "study.history", "study.chat", "study.delete_chat", "study.revoke",
            "library.index", "library.clear_history", "library.remove_favorite", "library.history_preference",
        }:
            abort(403, description="O acesso aos estudos está suspenso.")
        return view(*args, **kwargs)
    return checked


def admin_required(view):
    """Confere o proprietário no servidor em toda operação administrativa."""
    @wraps(view)
    @login_required
    def checked(*args, **kwargs):
        if not _is_owner(current_user()):
            abort(403, description="Esta página é exclusiva do proprietário.")
        return view(*args, **kwargs)
    return checked


def _access_page(mode):
    """O cadastro pede aceite explícito. A entrada reutiliza o aceite vigente."""
    destination = _safe_destination(request.values.get("next"))
    signup = mode == "signup"
    ready = bool(_setting("GOOGLE_CLIENT_ID") and _setting("GOOGLE_CLIENT_SECRET"))
    if request.method == "GET":
        return render_template("account_access.html", signup=signup, next_url=destination,
                               google_ready=ready)
    if not ready:
        flash("A entrada pelo Google ficará disponível após a configuração do site.")
        return render_template("account_access.html", signup=signup, next_url=destination,
                               google_ready=False), 503
    if signup and request.form.get("accept_policies") != "yes":
        flash("Leia os documentos e marque o aceite para criar a conta.")
        return render_template("account_access.html", signup=True, next_url=destination,
                               google_ready=True), 400
    phone = normalized_mobile(request.form.get("phone")) if signup else None
    if signup and not phone:
        flash(PHONE_HINT)
        return redirect(url_for("accounts.signup", metodo="google", next=destination), code=303)

    # Vinculamos o aceite e o destino à tentativa de login. O valor aleatório
    # também é verificado pelo Authlib na volta do Google (proteção CSRF).
    state = secrets.token_urlsafe(32)
    session["google_flow"] = {
        "state": state, "next": destination, "policy": POLICY_VERSION if signup else None, "phone": phone,
    }
    public_url = _setting("PUBLIC_BASE_URL").rstrip("/") or "http://localhost:5000"
    callback = public_url + url_for("accounts.google_callback")
    client = current_app.extensions["accounts_google"].google
    try:
        return client.authorize_redirect(callback, state=state)
    except (AuthlibBaseError, RequestException):
        session.pop("google_flow", None)
        flash("O Google está indisponível. Tente entrar novamente em instantes.")
        return redirect(url_for("accounts.signup" if signup else "accounts.login", next=destination))


@bp.route("/entrar", methods=["GET", "POST"])
def login():
    """Entrada de quem já tem um cadastro."""
    return _access_page("login")


@bp.route("/cadastro", methods=["GET", "POST"])
def signup():
    """Tela de criação de conta e de renovação do aceite dos documentos."""
    return _access_page("signup")


@bp.get("/auth/google/callback")
def google_callback():
    """Recebe o código do Google e cria uma sessão local após validar a identidade."""
    flow = session.get("google_flow")
    if not flow or not secrets.compare_digest(
        flow["state"].encode(), request.args.get("state", "").encode()
    ):
        abort(400, description="A tentativa de entrada expirou. Abra a tela Entrar novamente.")
    session.pop("google_flow")
    destination = _safe_destination(flow["next"])
    if request.args.get("error"):
        flash("A entrada com o Google foi interrompida.")
        return redirect(url_for("accounts.login", next=destination))

    client = current_app.extensions["accounts_google"].google
    try:
        token = client.authorize_access_token()
    except (AuthlibBaseError, JoseError, RequestException, ValueError):
        flash("A identidade não pôde ser confirmada. Inicie uma nova entrada pelo Google.")
        return redirect(url_for("accounts.login", next=destination))
    # Usamos somente os dados do ID token validado. Não armazenamos o access
    # token, o refresh token nem os dados de identificação recebidos por URL.
    identity = token.get("userinfo") or {}
    if not identity.get("sub") or not identity.get("email") or identity.get("email_verified") is not True:
        abort(403, description="O cadastro requer uma conta Google com e-mail verificado.")
    email = identity["email"]
    display_name = (identity.get("name") or email).strip()[:200]
    if str(identity["sub"]).startswith(("local:", "facebook:")):
        abort(403, description="O identificador recebido não é válido para uma conta Google.")
    return complete_social_login("google", identity["sub"], email, display_name,
                                 flow.get("policy"), destination, flow.get("phone"))


def complete_social_login(provider, subject_key, email, display_name, accepted_policy, destination, phone=None):
    """Conclui um callback validado. A identidade é do provedor, nunca do e-mail."""
    db = get_db()
    # A transação une cadastro e aceite. Duas abas não criam duas contas.
    with db:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute("SELECT * FROM users WHERE auth_provider=? AND google_sub=?",
                          (provider, subject_key)).fetchone()
        if accepted_policy != POLICY_VERSION:
            accepted = user and db.execute(
                "SELECT 1 FROM account_consents WHERE user_id = ? AND version = ?",
                (user["id"], POLICY_VERSION),
            ).fetchone()
            if not accepted and (user is None or user["phone_e164"]):
                flash("Para continuar, leia e aceite os documentos do cadastro.")
                return redirect(url_for("accounts.signup", metodo=provider, next=destination))
        if user is None:
            # Uma tentativa OAuth aberta antes da atualização ainda pode
            # voltar. O novo cadastro só é gravado com o celular válido.
            if not phone:
                flash(PHONE_HINT)
                return redirect(url_for("accounts.signup", metodo=provider, next=destination))
            user_id = db.execute(
                "INSERT INTO users (auth_provider, google_sub, email, name, phone_e164) VALUES (?, ?, ?, ?, ?)",
                (provider, subject_key, email, display_name, phone),
            ).lastrowid
        else:
            user_id = user["id"]
            db.execute("UPDATE users SET email=?,name=?,phone_e164=COALESCE(phone_e164,?) WHERE id=?",
                       (email, display_name, phone, user_id))
        if accepted_policy == POLICY_VERSION:
            db.execute("INSERT OR IGNORE INTO account_consents (user_id, version) VALUES (?, ?)",
                       (user_id, POLICY_VERSION))

    # Descarta a sessão anônima e os dados transitórios do protocolo antes de
    # associar o navegador ao cadastro; o formulário seguinte recebe novo CSRF.
    _renew_session()
    session["user_id"] = user_id
    if user and not (user["phone_e164"] or phone):
        return redirect(url_for("accounts.phone", next=destination))
    if user and user["disabled"]:
        flash("Seus estudos estão suspensos. Você ainda pode gerenciar a assinatura e a conta.")
        return redirect(url_for("accounts.profile"))
    return redirect(destination)


@bp.post("/sair")
def logout():
    """Sai deste navegador. A aplicação principal valida o CSRF do formulário."""
    _renew_session()
    return redirect("/")


@bp.route("/conta/celular", methods=["GET", "POST"])
@login_required
def phone():
    """Completa o cadastro antigo ou corrige o celular do próprio titular."""
    user = current_user()
    destination = _safe_destination(request.values.get("next"))
    # Nunca retornamos a esta mesma tela depois de completar os dados.
    if destination.split("?", 1)[0] == request.path:
        destination = url_for("accounts.profile")
    completing = not user["phone_e164"]
    if request.method == "GET":
        return render_template("account_phone.html", user=user, completing=completing, next_url=destination)
    number = normalized_mobile(request.form.get("phone"))
    if not number or (completing and request.form.get("accept_policies") != "yes"):
        flash(PHONE_HINT if not number else "Leia e aceite os documentos para concluir o cadastro.")
        return render_template("account_phone.html", user=user, completing=completing, next_url=destination), 400
    db = get_db()
    with db:
        db.execute("UPDATE users SET phone_e164=? WHERE id=?", (number, user["id"]))
        if completing:
            db.execute("INSERT OR IGNORE INTO account_consents(user_id,version) VALUES(?,?)",
                       (user["id"], POLICY_VERSION))
    flash("Celular salvo.", "toast")
    return redirect(destination, code=303)


@bp.get("/conta")
@login_required
def profile():
    """Mostra os dados que a pessoa pode consultar sobre a própria conta."""
    from billing import portal_orders

    user = current_user()
    return render_template("account_profile.html", user=user,
                           billing_orders=portal_orders(get_db(), user["id"]))


@bp.post("/conta/excluir")
@login_required
def delete_account():
    """Apaga o cadastro, os aceites e as conversas vinculadas à chave do usuário."""
    from billing import can_delete_account

    if request.form.get("confirmation") != "EXCLUIR":
        flash("Digite EXCLUIR para confirmar a remoção da conta.")
        return redirect(url_for("accounts.profile"))
    user_id = current_user()["id"]
    db = get_db()
    # Histórico contábil fica desvinculado (ON DELETE SET NULL); mensagens e
    # aceites são apagados em cascata. Cobranças recorrentes precisam terminar
    # antes de remover a conta, para o cliente manter acesso ao cancelamento.
    # A primeira consulta pode precisar confirmar a expiração de uma sessão
    # na Stripe. Esperamos a rede antes de reservar a escrita no banco local.
    with db:
        allowed = can_delete_account(user_id)
    if allowed:
        with db:
            db.execute("BEGIN IMMEDIATE")
            # Uma nova tentativa de compra pode ter começado em outra aba
            # desde a primeira consulta. A segunda leitura fecha essa janela.
            allowed = can_delete_account(user_id)
            if allowed:
                db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if not allowed:
        flash("Encerre a assinatura no portal ou aguarde o checkout pendente expirar para excluir a conta.")
        return redirect(url_for("accounts.profile"))
    _renew_session()
    flash("Sua conta e suas conversas foram excluídas.")
    return redirect("/")


@bp.get("/admin")
@admin_required
def admin_home():
    """Mostra totais e uma página de cadastros por vez, sem abrir conversas."""
    from billing import admin_license_status, billing_metrics

    db = get_db()
    totals = db.execute(
        "SELECT COALESCE(SUM(visits), 0) AS visits, COALESCE(SUM(verses), 0) AS verses FROM daily_metrics"
    ).fetchone()
    created = db.execute("SELECT created FROM account_totals WHERE id = 1").fetchone()["created"]
    search = request.args.get("q", "").strip()[:200]
    # instr() encontra a consulta literalmente e o parâmetro ? mantém a
    # entrada do visitante separada do SQL. Os resultados são paginados.
    matches = "WHERE instr(lower(name), lower(?)) > 0 OR instr(lower(email), lower(?)) > 0"
    count = db.execute("SELECT COUNT(*) FROM users " + matches, (search, search)).fetchone()[0]
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(pages, max(1, request.args.get("page", 1, type=int)))
    users = db.execute(
        "SELECT * FROM users " + matches + " ORDER BY id DESC LIMIT ? OFFSET ?",
        (search, search, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return render_template(
        "admin_home.html", totals=totals, created=created, sales=billing_metrics(db),
        users=users, page=page, pages=pages, search=search, count=count,
        licenses=admin_license_status(db, users),
    )


@bp.post("/admin/contas/<int:user_id>/acesso")
@admin_required
def set_access(user_id):
    """Suspende ou reativa estudos. O titular mantém acesso ao portal financeiro."""
    action = request.form.get("action")
    if action not in {"suspender", "reativar"}:
        abort(400)
    if current_user()["id"] == user_id:
        abort(400, description="O proprietário precisa manter acesso ao painel.")
    db = get_db()
    with db:
        updated = db.execute("UPDATE users SET disabled = ? WHERE id = ?",
                             (int(action == "suspender"), user_id))
        if not updated.rowcount:
            abort(404)
    flash("Acesso atualizado.")
    return redirect(url_for("accounts.admin_home", q=request.form.get("q", "")[:200],
                            page=request.form.get("page", 1, type=int)))
