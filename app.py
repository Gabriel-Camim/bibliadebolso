"""Entrada do bibliadebolso.com. Execute "python app.py" para abrir a versão local."""
import os
import re
import secrets
from ipaddress import ip_address
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parent
# Em produção, as variáveis fornecidas pela hospedagem têm prioridade sobre o .env.
load_dotenv(ROOT / ".env")

# Carregamos os módulos só depois das variáveis locais. Assim um cliente que
# leia a configuração ao ser importado também recebe os valores desta instalação.
import accounts
import facebook_auth
import password_auth
import bible_browser
import bible_jump
import bible_notes
import billing
import core
import corpus
import library
import study


def session_key(directory):
    """Preserva sessões locais após reiniciar. Em produção configure SECRET_KEY."""
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured
    path = directory / "session.key"
    # A instalação cria este arquivo só uma vez, fora do diretório público.
    if not path.exists():
        with NamedTemporaryFile(dir=directory, mode="w", delete=False, encoding="ascii") as draft:
            draft.write(secrets.token_hex(32))
        try:
            # No Windows, rename falha se o destino já existe. Em sistemas
            # Unix, link oferece essa mesma garantia. Só publicamos a chave completa.
            if os.name == "nt":
                os.rename(draft.name, path)
            else:
                os.link(draft.name, path)
        except FileExistsError:
            pass
        finally:
            Path(draft.name).unlink(missing_ok=True)
    return path.read_text(encoding="ascii").strip()


def public_origin(configured, on_render):
    """Resolve a origem que o Google e a Stripe usarão para voltar ao site."""
    local = "http://127.0.0.1:5050"
    chosen = (configured or "").strip().rstrip("/")
    if on_render:
        assigned = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        host = (urlsplit(chosen).hostname or "").rstrip(".")
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost" or host.endswith(".localhost")
        # A URL de desenvolvimento às vezes vem junto na importação do .env.
        # Só a substituímos pela origem HTTPS gerada pela plataforma. Depois
        # de ativar o domínio próprio, a configuração dele terá precedência.
        if not chosen or loopback:
            if re.fullmatch(r"https://[a-z0-9-]+\.onrender\.com", assigned, re.IGNORECASE):
                return assigned
    return chosen or local


def create_app(config=None):
    """Monta os módulos e permite usar um banco temporário durante os testes."""
    app = Flask(__name__)
    options = config or {}
    private = Path(os.getenv("APP_DATA_DIR") or (ROOT / "instance")).resolve()
    # A Render informa a URL HTTPS do serviço antes mesmo de configurar o domínio.
    # A mesma origem precisa ser usada pelo cookie e pelos retornos dos provedores.
    on_render = os.getenv("RENDER") == "true"
    public_url = public_origin(options.get("PUBLIC_BASE_URL") or os.getenv("PUBLIC_BASE_URL"), on_render)
    # O disco da Render gratuita é temporário. Exigimos o banco remoto nesse
    # ambiente para que uma falha de configuração não crie contas descartáveis.
    backend = options.get("DATABASE_BACKEND") or os.getenv("DATABASE_BACKEND")
    if "DATABASE" in options and "DATABASE_BACKEND" not in options:
        backend = "sqlite"  # Bancos temporários passados pelos testes são locais.
    backend = backend or ("turso" if on_render else "sqlite")
    if backend not in {"sqlite", "turso"}:
        raise ValueError("DATABASE_BACKEND deve ser sqlite ou turso.")
    secret = options.get("SECRET_KEY") or os.getenv("SECRET_KEY")
    if backend == "sqlite":
        # Uma pasta gravável não prova que ela sobreviverá ao próximo deploy.
        # Em um serviço Render com disco pago, APP_DATA_DIR pode apontar
        # diretamente para o ponto de montagem escolhido no painel.
        if on_render and not ("DATABASE" in options and options.get("TESTING")):
            if not os.getenv("APP_DATA_DIR") or private == Path(private.anchor) or not private.is_mount():
                raise RuntimeError("No plano Render Free, defina DATABASE_BACKEND=turso. "
                                   "Para SQLite na Render, APP_DATA_DIR deve apontar para o disco montado.")
        private.mkdir(parents=True, exist_ok=True)
        secret = secret or session_key(private)
    elif not secret:
        raise RuntimeError("Configure SECRET_KEY permanente no serviço que usa Turso.")
    app.config.update(
        SECRET_KEY=secret, DATABASE_BACKEND=backend, DATABASE=str(private / "site.sqlite3"),
        TURSO_DATABASE_URL=os.getenv("TURSO_DATABASE_URL", ""),
        TURSO_AUTH_TOKEN=os.getenv("TURSO_AUTH_TOKEN", ""),
        PUBLIC_BASE_URL=public_url, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=urlsplit(public_url).scheme == "https",
        SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=1024*1024,
        PRIVACY_CONTACT=os.getenv("PRIVACY_CONTACT", ""),
        CONTROLLER_NAME=os.getenv("CONTROLLER_NAME", ""),
    )
    if config:
        app.config.update(config)
        app.config["PUBLIC_BASE_URL"] = public_url
    if backend == "turso" and not all(app.config[k] for k in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")):
        raise RuntimeError("Configure TURSO_DATABASE_URL e TURSO_AUTH_TOKEN no servidor.")

    # Cada blueprint agrupa páginas de um assunto. A proteção de formulários
    # também cobre as rotas administrativas e as que apagam dados.
    app.before_request(core.protect_forms)
    app.teardown_appcontext(core.close_db)
    app.jinja_env.globals["csrf_token"] = core.csrf_token
    for blueprint in (accounts.bp, facebook_auth.bp, password_auth.bp, billing.bp, study.bp,
                      bible_browser.bp, library.bp, bible_notes.bp, bible_jump.bp):
        app.register_blueprint(blueprint)

    with app.app_context():
        db = core.get_db()
        if app.config["DATABASE_BACKEND"] == "sqlite":
            db.execute("PRAGMA journal_mode=WAL")  # No Turso, a hospedagem gerencia o journal.
        db.execute("""CREATE TABLE IF NOT EXISTS daily_metrics(
            day TEXT PRIMARY KEY, visits INTEGER NOT NULL, verses INTEGER NOT NULL)""")
        accounts.init_accounts(db)
        password_auth.init_password_auth(db)
        bible_notes.init_notes(db)
        library.init_library(db)
        billing.init_billing(db)
        study.init_study(db)
        db.commit()

    def draw_verse(opened):
        """Emite o recibo do sorteio junto com o texto que o leitor vai ver."""
        verse = corpus.random_verse(session.get("last_verse"))
        session["last_verse"] = verse["id"]
        core.record_reading(opened=opened)
        # Para um leitor já autenticado, o recibo comprova de qual sorteio
        # veio o favorito e a hora em que a leitura foi gerada.
        return verse, library.record_draw(verse)

    @app.get("/saude")
    def health():
        """A hospedagem confere a conexão SQLite sem contar uma visita ou sortear um verso."""
        ready = core.get_db().execute("SELECT 1 FROM account_totals WHERE id=1").fetchone()
        return ("ok", 200) if ready else ("indisponível", 503)

    @app.get("/")
    def reader():
        # A tag NFC contém somente a URL. O servidor sorteia a cada abertura.
        if request.method == "HEAD":
            return "", 200
        resume = session.pop("saved_reading", None)
        user = accounts.current_user()
        if resume and user and user["id"] == resume[0]:
            verse, receipt = corpus.verse_by_id(resume[1]), resume[2]
        else:
            verse, receipt = draw_verse(opened=True)
        return render_template("reader.html", verse=verse, draw_token=receipt,
                               saved=bool(receipt) and library.is_favorite(verse["id"]))

    @app.post("/leitor/novo")
    def next_verse():
        verse, receipt = draw_verse(opened=False)
        return jsonify(text=verse["text"], reference=verse["reference"], verse_id=verse["id"],
                       saved=bool(receipt) and library.is_favorite(verse["id"]),
                       draw_token=receipt,
                       bible_url=url_for("bible.locate", verse_id=verse["id"]),
                       study_url=url_for("study.start", verse_id=verse["id"]))

    @app.get("/sobre")
    @app.get("/privacidade")
    @app.get("/termos")
    def information():
        return render_template("information.html", page=request.path.strip("/"))

    @app.post("/cookies")
    def cookies():
        # A leitura mantém contadores anônimos. Só a preferência de aparência
        # usa armazenamento opcional, habilitado após escolha explícita.
        choice = request.form.get("choice")
        if choice not in {"essential", "preferences"}:
            abort(400)
        session["cookie_choice"] = choice
        target = request.form.get("back", "/")
        # Voltamos apenas a uma página local conhecida, sem redirecionamento externo.
        safe = target in {"/","/sobre","/privacidade","/termos","/cadastro","/entrar","/conta","/assinatura","/estudo"}
        if request.accept_mimetypes.best == "application/json":
            return jsonify(choice=choice)
        return redirect(target if safe else "/")

    @app.after_request
    def response_headers(response):
        # O navegador deve pedir um novo versículo após o refresh e não deve
        # guardar uma cópia em cache das páginas privadas.
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(HTTPException)
    def readable_error(error):
        return render_template("error.html", error=error), error.code

    return app


def serve_site():
    """Inicia o mesmo servidor usado no PowerShell e na Render."""
    from waitress import serve
    on_render = os.getenv("RENDER") == "true"
    # O proxy da Render conecta pela interface pública do contêiner. No PC,
    # o padrão é restrito ao próprio computador, salvo configuração explícita.
    host = "0.0.0.0" if on_render else (os.getenv("BIND_HOST") or "127.0.0.1")
    port = int(os.getenv("PORT") or ("10000" if on_render else "5050"))
    print(f"Servidor escutando em {host}:{port}", flush=True)
    serve(create_app(), host=host, port=port)


if __name__ == "__main__":
    serve_site()
