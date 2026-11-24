"""Contas locais por usuário ou e-mail e senha, com cadastro independente do OAuth.

O cadastro público continua em `users` para que favoritos, assinatura e conversas
usem o mesmo ID. Só este módulo lê a tabela privada de credenciais.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from time import time
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.exceptions import TooManyRequests
from werkzeug.security import check_password_hash, generate_password_hash

import accounts
from core import get_db

bp = Blueprint("password_auth", __name__)

# O Werkzeug gera um salt aleatório para cada hash. Fixamos o custo para que
# uma atualização da biblioteca não altere silenciosamente o orçamento do servidor.
HASH_METHOD = "scrypt:32768:8:1"
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32), method=HASH_METHOD)
WINDOW = 900
LOGIN_BY_ACCOUNT = 10
LOGIN_BY_ADDRESS = 120
SIGNUP_BY_ADDRESS = 12
RENDER_BURST = {"signup":24, "login":120}
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,31}")
LOCAL_PART = re.compile(r"[a-zA-Z0-9!#$%&'*+\/=?^_`{|}~.-]+")
DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def init_password_auth(db):
    """Cria as credenciais e o limitador sem modificar contas Google existentes."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS password_credentials (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email_key TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS password_limits (
            bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS password_limit_expiration ON password_limits(expires);
    """)


def _email_key(value):
    """Aceita endereços com + e domínios internacionais; a busca ignora caixa."""
    if len(value) > 254 or value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if (len(local) > 64 or not LOCAL_PART.fullmatch(local) or local.startswith(".")
            or local.endswith(".") or ".." in local):
        return None
    try:
        labels = domain.encode("idna").decode("ascii").lower().split(".")
    except UnicodeError:
        return None
    if len(labels) < 2 or not all(DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    normalized = f"{local.lower()}@{'.'.join(labels)}"
    return normalized if len(normalized) <= 254 else None


def _bucket(kind, value):
    """A janela de tentativas armazena uma chave opaca, sem IP ou e-mail em claro."""
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return hmac.new(secret, f"{kind}\x00{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _origin_limits(action, maximum):
    """Distribui o orçamento entre sessões quando a Render compartilha um proxy."""
    if os.environ.get("RENDER") == "true":
        # A sessão assinada separa leitores legítimos. Um teto geral curto
        # limita o custo de gerar hashes e cadastros mesmo com cookies novos.
        return [(_bucket(action + "-session", session["_csrf"]), maximum, WINDOW),
                (_bucket(action + "-service", "render"), RENDER_BURST[action], 60)]
    return [(_bucket(action + "-address", request.remote_addr or ""), maximum, WINDOW)]


def _consume(limits):
    """Reserva a tentativa antes do hash caro, inclusive com vários workers.

    As entradas vencidas são removidas no próximo uso. Requisições bloqueadas
    não estendem a janela. O limite por conta vale inclusive em outra sessão.
    """
    db = get_db()
    now = int(time())
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM password_limits WHERE expires<=?", (now,))
        for bucket, maximum, _ in limits:
            row = db.execute("SELECT attempts,expires FROM password_limits WHERE bucket=?", (bucket,)).fetchone()
            if row and row["attempts"] >= maximum:
                return max(1, row["expires"] - now)
        for bucket, _, duration in limits:
            db.execute("""INSERT INTO password_limits(bucket,attempts,expires) VALUES(?,1,?)
                          ON CONFLICT(bucket) DO UPDATE SET attempts=attempts+1""", (bucket, now + duration))
    return 0


def _limited(seconds):
    error = TooManyRequests(description="Muitas tentativas em pouco tempo. Aguarde alguns minutos e tente novamente.")
    return render_template("error.html", error=error), 429, {"Retry-After":str(seconds)}


def _return_to(endpoint, message, destination, **params):
    """Redireciona depois do POST sem colocar usuários, e-mails ou senhas na URL."""
    flash(message)
    return redirect(url_for(endpoint, next=destination, **params), code=303)


def _start_session(user_id, version):
    """Uma troca de senha muda a versão e invalida sessões de senha mais antigas."""
    accounts._renew_session()
    session["user_id"] = user_id
    session["password_version"] = version


@bp.post("/auth/senha/cadastro")
def signup():
    """Cria usuário, credencial e aceite juntos. Basta o leitor ter uma sessão CSRF."""
    destination = accounts._safe_destination(request.form.get("next"))
    remaining = _consume(_origin_limits("signup", SIGNUP_BY_ADDRESS))
    if remaining:
        return _limited(remaining)
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    email_key = _email_key(email)
    phone = accounts.normalized_mobile(request.form.get("phone"))
    password = request.form.get("password", "")
    if request.form.get("accept_policies") != "yes":
        message = "Leia e aceite os termos e a política de privacidade para criar a conta."
    elif not USERNAME.fullmatch(username):
        message = "Use um nome de usuário de 3 a 32 caracteres. Comece com uma letra ou número. Você também pode usar ponto, hífen e sublinhado."
    elif not email_key:
        message = "Confira o endereço de e-mail informado."
    elif not phone:
        message = accounts.PHONE_HINT
    elif not 12 <= len(password) <= 128:
        message = "Crie uma senha de 12 a 128 caracteres."
    else:
        message = ""
    if message:
        return _return_to("accounts.signup", message, destination)

    # O identificador interno aleatório cabe na tabela original. Ele não usa
    # o e-mail como prova de identidade e não concede acesso administrativo.
    display_name = request.form.get("name", "").strip()[:200] or username
    hashed = generate_password_hash(password, method=HASH_METHOD)
    db = get_db()
    try:
        with db:
            user_id = db.execute("""INSERT INTO users(google_sub,auth_provider,email,name,phone_e164)
                                    VALUES(?,'password',?,?,?)""",
                                 (f"local:{uuid4().hex}", email, display_name, phone)).lastrowid
            db.execute("""INSERT INTO password_credentials(user_id,username,email_key,password_hash)
                          VALUES(?,?,?,?)""", (user_id, username, email_key, hashed))
            db.execute("INSERT INTO account_consents(user_id,version) VALUES(?,?)", (user_id, accounts.POLICY_VERSION))
    except sqlite3.IntegrityError as error:
        # Uma corrida entre dois cadastros com os mesmos dados termina aqui.
        # O rollback desfaz também o incremento do contador de contas criadas.
        if error.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
            raise
        return _return_to("accounts.signup", "Não foi possível usar esse nome de usuário ou e-mail. Confira os dados ou entre na sua conta.", destination)
    _start_session(user_id, 1)
    return redirect(destination, code=303)


@bp.post("/auth/senha/entrar")
def login():
    """Autentica exclusivamente na credencial local, nunca pelo e-mail do Google."""
    destination = accounts._safe_destination(request.form.get("next"))
    identifier = request.form.get("login", "").strip()
    password = request.form.get("password", "")
    db = get_db()
    if "@" in identifier:
        column, key = "email_key", _email_key(identifier) or ""
    else:
        column, key = "username", identifier.lower() if USERNAME.fullmatch(identifier) else ""
    # Só as duas constantes acima entram no SQL. A entrada da pessoa é sempre
    # um parâmetro. A mesma credencial tem um único limite, mesmo usando o alias.
    user = db.execute(f"""SELECT p.user_id,p.password_hash,p.version,u.disabled,u.phone_e164
                         FROM password_credentials p JOIN users u ON u.id=p.user_id
                         WHERE p.{column}=? AND u.auth_provider='password'""", (key,)).fetchone()
    account_bucket = _bucket("login-account", f"id:{user['user_id']}" if user else f"key:{key}")
    remaining = _consume([(account_bucket, LOGIN_BY_ACCOUNT, WINDOW)] + _origin_limits("login", LOGIN_BY_ADDRESS))
    if remaining:
        return _limited(remaining)
    # Mesmo um usuário inexistente passa pela verificação de um hash válido.
    # Evitamos assim uma resposta rápida que denunciaria quais e-mails existem.
    valid = 0 < len(password) <= 128 and check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
    if not valid or user is None:
        return _return_to("accounts.login", "Usuário, e-mail ou senha incorretos.", destination)
    with db:
        db.execute("DELETE FROM password_limits WHERE bucket=?", (account_bucket,))
        if request.form.get("accept_policies") == "yes":
            db.execute("INSERT OR IGNORE INTO account_consents(user_id,version) VALUES(?,?)",
                       (user["user_id"], accounts.POLICY_VERSION))
        elif user["phone_e164"] and not db.execute("SELECT 1 FROM account_consents WHERE user_id=? AND version=?",
                            (user["user_id"], accounts.POLICY_VERSION)).fetchone():
            return _return_to("accounts.login", "Leia e aceite os documentos atualizados para continuar.", destination, aceite=1)
    _start_session(user["user_id"], user["version"])
    if not user["phone_e164"]:
        return redirect(url_for("accounts.phone", next=destination), code=303)
    if user["disabled"]:
        flash("Os estudos desta conta estão suspensos. Você pode gerenciar a conta e a assinatura.")
        return redirect(url_for("accounts.profile"), code=303)
    return redirect(destination, code=303)


@bp.post("/conta/senha")
@accounts.login_required
def change_password():
    """Exige a senha em uso. A mudança encerra as outras sessões deste login."""
    user = accounts.current_user()
    if user["auth_provider"] != "password":
        abort(403, description="A senha desta conta é gerenciada pelo provedor de login.")
    new = request.form.get("new_password", "")
    old = request.form.get("current_password", "")
    return_path = url_for("accounts.profile")
    if not 12 <= len(new) <= 128 or old == new:
        flash("Escolha uma senha diferente da atual, com 12 a 128 caracteres.")
        return redirect(return_path, code=303)

    bucket = _bucket("change-account", str(user["id"]))
    remaining = _consume([(bucket, LOGIN_BY_ACCOUNT, WINDOW)])
    if remaining:
        return _limited(remaining)
    db = get_db()
    credential = db.execute("SELECT password_hash,version FROM password_credentials WHERE user_id=?", (user["id"],)).fetchone()
    if not credential or not 0 < len(old) <= 128 or not check_password_hash(credential["password_hash"], old):
        flash("Confira a sua senha atual.")
        return redirect(return_path, code=303)
    hashed = generate_password_hash(new, method=HASH_METHOD)
    with db:
        # A versão impede que duas abas usando a senha antiga substituam uma
        # a alteração da outra. Cada gravação bem-sucedida avança a versão.
        changed = db.execute("""UPDATE password_credentials SET password_hash=?,version=version+1
                                WHERE user_id=? AND version=?""",
                             (hashed, user["id"], credential["version"])).rowcount
        if changed:
            db.execute("DELETE FROM password_limits WHERE bucket=?", (bucket,))
    if changed:
        _start_session(user["id"], credential["version"] + 1)
        flash("Senha alterada. Os outros dispositivos precisam entrar novamente.")
    else:
        flash("A senha foi alterada em outra sessão. Entre novamente.")
    return redirect(return_path, code=303)
