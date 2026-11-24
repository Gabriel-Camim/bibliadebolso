"""Regressões do acesso privado. O provedor Google é simulado, sem usar a rede."""

import sqlite3
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from flask import Flask, redirect

import accounts


class AccountAccessTest(unittest.TestCase):
    def setUp(self):
        # Cada teste ganha um banco novo. Nenhuma alteração atinge cadastros
        # da instalação local ou contas do Google de pessoas reais.
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(self.db.close)
        accounts.init_accounts(self.db)
        self.enterContext(patch("accounts.get_db", return_value=self.db))

        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True, SECRET_KEY="teste-local", OWNER_GOOGLE_SUB="dono",
            GOOGLE_CLIENT_ID="exemplo", GOOGLE_CLIENT_SECRET="exemplo",
            PUBLIC_BASE_URL="https://exemplo.invalid",
        )
        self.app.register_blueprint(accounts.bp)
        self.client = self.app.test_client()

        @self.app.get("/teste-admin")
        @accounts.admin_required
        def owner_only():
            return "", 200

    def google_return(self, subject, path="/cadastro", destination="/conta", verified=True):
        """Simula a ida e a volta do OAuth com a mesma sessão e o mesmo state."""
        google = self.app.extensions["accounts_google"].google
        claims = {"sub": subject, "email": "contato@example.invalid",
                  "email_verified": verified, "name": "Pessoa de teste"}
        with (
            patch.object(google, "authorize_redirect",
                         side_effect=lambda callback, state: redirect("/google?state=" + state)),
            patch.object(google, "authorize_access_token", return_value={"userinfo": claims}),
        ):
            response = self.client.post(path, data={"next": destination, "accept_policies": "yes",
                                                    "phone": "(11) 98765-4321"})
            state = parse_qs(urlsplit(response.location).query)["state"][0]
            return self.client.get("/auth/google/callback", query_string={"code": "teste", "state": state})

    def test_signup_requires_identity_and_policy(self):
        # Entrar não cria cadastro implicitamente, e um e-mail não verificado
        # não pode reivindicar a conta de outro usuário.
        self.assertEqual(self.client.get("/auth/google/callback?state=antigo").status_code, 400)
        self.assertTrue(self.google_return("leitor", "/entrar").location.startswith("/cadastro"))
        self.assertEqual(self.google_return("leitor", verified=False).status_code, 403)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        # Uma preferência de privacidade escolhida anonimamente permanece
        # válida após trocar a sessão pelo login reconhecido pelo servidor.
        with self.client.session_transaction() as session:
            session["cookie_choice"] = "essential"
            session["last_verse"] = 7
        self.assertEqual(self.google_return("leitor", destination="/assinatura?verse_id=7").location,
                         "/assinatura?verse_id=7")
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("cookie_choice"), "essential")
            self.assertEqual(session.get("last_verse"), 7)
        self.assertEqual(self.google_return("leitor", path="/entrar").location, "/conta")
        self.assertEqual(self.db.execute("SELECT phone_e164 FROM users").fetchone()[0], "+5511987654321")
        self.assertEqual(self.db.execute("SELECT created FROM account_totals").fetchone()[0], 1)

    def test_admin_checks_google_subject_and_suspension(self):
        # As identidades simuladas têm o mesmo e-mail de propósito. Apenas o
        # sub cadastrado pelo proprietário libera a área administrativa.
        self.assertEqual(self.client.get("/teste-admin").status_code, 302)
        self.google_return("leitor")
        self.assertEqual(self.client.get("/teste-admin").status_code, 403)
        self.assertEqual(self.google_return("dono", destination="//externo.invalid").location, "/conta")
        self.assertEqual(self.client.get("/teste-admin").status_code, 200)
        with self.db:
            self.db.execute("UPDATE users SET disabled = 1 WHERE google_sub = 'dono'")
        self.assertEqual(self.client.get("/teste-admin").status_code, 403)

    def test_authorization_request_uses_pkce_and_nonce(self):
        # Aqui só a descoberta do endereço do provedor é simulada. O Authlib
        # real gera o desafio PKCE, o nonce e o parâmetro de proteção state.
        google = self.app.extensions["accounts_google"].google
        metadata = {"authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"}
        with patch.object(google, "load_server_metadata", return_value=metadata):
            missing = self.client.post("/cadastro", data={"accept_policies": "yes", "phone": ""})
            self.assertEqual(urlsplit(missing.location).path, "/cadastro")
            response = self.client.post("/cadastro", data={"accept_policies": "yes", "phone": "(11) 98765-4321"})
        params = parse_qs(urlsplit(response.location).query)
        self.assertEqual(params["redirect_uri"], ["https://exemplo.invalid/auth/google/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertTrue(params["nonce"] and params["state"])
        self.assertNotIn("98765", response.location)

    def test_existing_google_user_completes_phone_before_returning_to_study(self):
        with self.db:
            self.db.execute("""INSERT INTO users(google_sub,email,name)
                               VALUES('antigo','antigo@example.invalid','Perfil antigo')""")
        destination = "/estudo?verse_id=7"
        reply = self.google_return("antigo", path="/entrar", destination=destination)
        self.assertEqual(urlsplit(reply.location).path, "/conta/celular")
        self.assertEqual(parse_qs(urlsplit(reply.location).query)["next"], [destination])
        saved = self.client.post("/conta/celular", data={"phone":"11 98765-4321", "accept_policies":"yes",
                                                         "next":destination})
        self.assertEqual(saved.location, destination)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT phone_e164 FROM users").fetchone()[0], "+5511987654321")

    def test_upgrade_keeps_legacy_google_identity_and_private_foreign_keys(self):
        # Este é o formato de um banco criado quando só existia login Google.
        # A migração precisa preservar o cadastro e qualquer dado ligado ao ID.
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.row_factory = sqlite3.Row
        legacy.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE users (id INTEGER PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
                email TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT, disabled INTEGER DEFAULT 0);
            INSERT INTO users(id,google_sub,email,name) VALUES(7,'dono','dono@example.invalid','Leitor');
            CREATE TABLE saved (owner INTEGER REFERENCES users(id));
            INSERT INTO saved VALUES(7);
        """)
        accounts.init_accounts(legacy)
        accounts.init_accounts(legacy)
        user = legacy.execute("SELECT users.* FROM saved JOIN users ON users.id=saved.owner").fetchone()
        self.assertIsNone(user["phone_e164"])
        self.assertEqual((user["id"], user["auth_provider"], user["google_sub"]), (7, "google", "dono"))
        with self.app.app_context():
            self.assertTrue(accounts._is_owner(user))
