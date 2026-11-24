"""Recursos compartilhados entre leitor, login, estudos e cobranças.

Cada requisição ganha uma conexão própria. Assim as alterações de um usuário
não são misturadas com a transação em andamento de outro usuário.
"""
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import abort, current_app, g, request, session


def get_db():
    """Cada requisição tem a própria conexão ao banco privado, local ou remoto."""
    if "db" not in g:
        if current_app.config["DATABASE_BACKEND"] == "turso":
            from turso_db import connect
            db = connect(current_app.config["TURSO_DATABASE_URL"], current_app.config["TURSO_AUTH_TOKEN"])
        else:
            db = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
            db.row_factory = sqlite3.Row  # Permite ler colunas pelo nome, como pessoa["email"].
            db.execute("PRAGMA foreign_keys=ON")  # Aplica as regras de exclusão em cascata.
        g.db = db
    return g.db


def close_db(_error):
    """Libera a conexão mesmo se o visitante sair ou a página apresentar erro."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def write_one(sql, parameters):
    """Confirma uma instrução autônoma no banco ativo da requisição."""
    db = get_db()
    if db.in_transaction:
        return db.execute(sql, parameters)
    if current_app.config.get("DATABASE_BACKEND") == "turso":
        return db.execute_atomic(sql, parameters)
    with db:
        return db.execute(sql, parameters)


def csrf_token():
    """O navegador devolve este valor no formulário para confirmar sua origem."""
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def protect_forms():
    """O webhook usa a assinatura da Stripe. Os formulários usam o token de sessão."""
    if request.method in {"GET", "HEAD", "OPTIONS"} or request.endpoint == "billing.webhook":
        return
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
        abort(400, description="A sessão do formulário expirou. Atualize a página e tente novamente.")


def record_reading(*, opened):
    """Soma totais por dia, sem persistir IP, identificador de tag ou lista de leitores."""
    today = datetime.now(timezone(timedelta(hours=-3))).date().isoformat()
    # O incremento acontece dentro do banco para não perder contagens simultâneas.
    write_one("""
        INSERT INTO daily_metrics(day, visits, verses) VALUES (?, ?, 1)
        ON CONFLICT(day) DO UPDATE SET visits=visits+excluded.visits, verses=verses+1
    """, (today, int(opened)))
