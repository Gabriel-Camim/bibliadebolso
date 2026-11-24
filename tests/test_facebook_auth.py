"""Percorre o login com uma Meta simulada e um banco privado em memória."""
import sqlite3
from urllib.parse import parse_qs, urlsplit
from unittest import TestCase
from unittest.mock import patch

from flask import Flask
import requests

import accounts
import facebook_auth
from core import protect_forms


class FacebookAccess(TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.db.close)
        accounts.init_accounts(self.db)
        self.enterContext(patch("accounts.get_db", return_value=self.db))
        # As duas identidades recebem o mesmo e-mail para demonstrar que uma
        # pessoa só alcança a biblioteca do provedor com que fez o cadastro.
        with self.db:
            self.db.execute("INSERT INTO users(google_sub,email,name) VALUES(?,?,?)",
                            ("123", "nome@example.test", "Usuário Google"))
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="apenas-nos-testes",
                               PUBLIC_BASE_URL="https://leitor.example.test",
                               FACEBOOK_APP_ID="777", FACEBOOK_APP_SECRET="somente-teste",
                               FACEBOOK_GRAPH_VERSION="v27.0", OWNER_GOOGLE_SUB="123")
        self.app.register_blueprint(accounts.bp)
        self.app.register_blueprint(facebook_auth.bp)
        self.app.before_request(protect_forms)
        self.client = self.app.test_client()
        self.exchange = self.enterContext(patch("facebook_auth.requests.post"))
        self.graph = self.enterContext(patch("facebook_auth.requests.get"))
        self.exchange.return_value.json.return_value = {"access_token": "token-de-ensaio"}
        self.graph.return_value.json.return_value = {"id":"123", "name":"Leitora",
                                                    "email":"nome@example.test"}

    def start(self, path="cadastro", **changes):
        # O login renova a sessão. O formulário seguinte recebe um novo
        # token CSRF, assim como as páginas reais de cadastro e de entrada.
        with self.client.session_transaction() as session:
            csrf = session.setdefault("_csrf", "token-de-formulario")
        fields = {"accept_policies":"yes", "next":"/biblia", "csrf_token":csrf, **changes}
        return self.client.post("/auth/facebook/" + path, data=fields)

    def finish(self, start, **changes):
        state = parse_qs(urlsplit(start.location).query)["state"][0]
        return self.client.get("/auth/facebook/callback",
                               query_string={"code":"codigo-unico", "state":state, **changes})

    def test_signup_login_and_provider_isolation(self):
        # O fluxo "entrar" com um e-mail Google conhecido não cria a conta
        # Facebook. O cadastro somente é concluído após o aceite explícito.
        self.assertTrue(self.finish(self.start("entrar")).location.startswith("/cadastro"))
        self.assertTrue(self.start(accept_policies="").location.startswith("/cadastro"))
        with self.client.session_transaction() as session:
            session.update(cookie_choice="essential", last_verse=7)
        start = self.start()
        parts = urlsplit(start.location)
        self.assertEqual((parts.netloc, parts.path), ("www.facebook.com", "/v27.0/dialog/oauth"))
        query = parse_qs(parts.query)
        self.assertEqual(query["redirect_uri"], ["https://leitor.example.test/auth/facebook/callback"])
        self.assertEqual(query["scope"], ["public_profile,email"])
        self.assertNotIn("client_secret", query)
        self.assertEqual(self.finish(start).location, "/biblia")
        with self.client.session_transaction() as session:
            user_id = session["user_id"]
            self.assertEqual(session["last_verse"], 7)
            self.assertEqual(session["cookie_choice"], "essential")
            self.assertNotIn("facebook_flow", session)
            self.assertNotIn("token-de-ensaio", str(dict(session)))
        user = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        self.assertEqual((user["auth_provider"], user["email"]), ("facebook", "nome@example.test"))
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.db.execute("SELECT created FROM account_totals").fetchone()[0], 2)
        # O titular Facebook continua sendo identificado pelo mesmo subject
        # quando entra novamente com endereço de contato alterado ou omitido.
        self.graph.return_value.json.return_value = {"id":"123", "name":"Leitora"}
        self.assertEqual(self.finish(self.start("entrar", next="//outro.example")).location, "/conta")
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], user_id)
        self.assertEqual(self.exchange.call_args.kwargs["data"]["redirect_uri"], query["redirect_uri"][0])
        self.assertEqual(self.graph.call_args.kwargs["headers"]["Authorization"], "Bearer token-de-ensaio")
        self.assertNotIn("access_token", self.graph.call_args.kwargs["params"])
        self.assertIn("appsecret_proof", self.graph.call_args.kwargs["params"])

    def test_invalid_state_expiration_cancel_and_replay(self):
        self.assertEqual(self.start(csrf_token="").status_code, 400)
        start = self.start()
        self.assertEqual(self.finish(start, state="outro").status_code, 400)
        cancelled = self.finish(start, error="access_denied")
        self.assertTrue(cancelled.location.startswith("/cadastro"))
        self.assertEqual(self.finish(start).status_code, 400)
        expired = self.start()
        # A sessão Flask continua válida, mas a tentativa de login já expirou.
        with self.client.session_transaction() as session:
            flow = dict(session["facebook_flow"])
            flow["started"] = 0
            session["facebook_flow"] = flow
        self.assertEqual(self.finish(expired).status_code, 400)
        self.exchange.assert_not_called()

    def test_failure_and_missing_secret_do_not_create_accounts(self):
        self.exchange.side_effect = requests.Timeout()
        self.assertTrue(self.finish(self.start()).location.startswith("/cadastro"))
        self.exchange.side_effect = None
        self.graph.return_value.json.return_value = {"error": "perfil indisponível"}
        self.assertTrue(self.finish(self.start()).location.startswith("/cadastro"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.app.config.update(FACEBOOK_APP_SECRET="")
        with patch.dict("os.environ", {"FACEBOOK_APP_SECRET": ""}):
            start = self.start()
            self.assertTrue(start.location.startswith("/cadastro"))
            self.assertNotIn("facebook.com", start.location)
