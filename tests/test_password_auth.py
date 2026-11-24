"""Exercita as rotas públicas de autenticação com sessões e banco descartáveis."""
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from werkzeug.security import check_password_hash

import accounts
import password_auth
from app import create_app
from core import get_db


class PasswordAccess(TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.app = create_app(dict(TESTING=True, SECRET_KEY="apenas-teste",
                                   OWNER_GOOGLE_SUB="subject-google-dono",
                                   DATABASE=Path(folder) / "acesso.sqlite3"))
        self.client = self.app.test_client()
        self.password = "uma frase longa com 🌿"
        self.data = dict(username="leitor.local", email="Leitor+Site@Example.test",
                         phone="(11) 98765-4321",
                         password=self.password, accept_policies="yes", next="/biblioteca")

    def post(self, endpoint, values=None, client=None):
        client = client or self.client
        # O token da sessão anterior é descartado ao trocar de identidade.
        # Cada envio simula um novo formulário emitido pela mesma sessão.
        with client.session_transaction() as cookie:
            csrf = cookie.setdefault("_csrf", "csrf-formulario-local")
        return client.post(endpoint, data={**(values or {}), "csrf_token":csrf})

    def signup(self, **changes):
        return self.post("/auth/senha/cadastro", {**self.data, **changes})

    def login(self, client=None, **changes):
        values = dict(login="leitor.local", password=self.password, next="/biblioteca")
        return self.post("/auth/senha/entrar", {**values, **changes}, client)

    def test_signup_and_login_by_both_identifiers(self):
        with self.client.session_transaction() as s:
            s["cookie_choice"] = "essential"
            s["last_verse"] = 7
        result = self.signup()
        self.assertEqual(result.location, "/biblioteca")
        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT * FROM users").fetchone()
            credential = db.execute("SELECT * FROM password_credentials").fetchone()
            self.assertEqual(user["auth_provider"], "password")
            self.assertEqual(user["phone_e164"], "+5511987654321")
            self.assertEqual(user["id"], credential["user_id"])
            self.assertEqual(credential["email_key"], "leitor+site@example.test")
            self.assertTrue(check_password_hash(credential["password_hash"], self.password))
            self.assertIn("scrypt:", credential["password_hash"])
            self.assertNotIn(self.password, credential["password_hash"])
            self.assertEqual(db.execute("SELECT version FROM account_consents").fetchone()[0],
                             accounts.POLICY_VERSION)
        for login in ("LEITOR.LOCAL", "LEITOR+SITE@example.test"):
            self.post("/sair")
            self.assertEqual(self.login(login=login).location, "/biblioteca")
            self.assertEqual(self.client.get("/admin").status_code, 403)
            with self.client.session_transaction() as s:
                self.assertEqual(s["user_id"], user["id"])
                self.assertEqual(s["last_verse"], 7)
                self.assertEqual(s["cookie_choice"], "essential")
                self.assertNotEqual(s.get("_csrf"), "csrf-formulario-local")

    def test_signup_and_uniqueness_are_atomic(self):
        # Recusar um cadastro inválido ou repetido não cria registros pessoais
        # nem altera o contador histórico. Dois aliases locais são exclusivos.
        self.assertEqual(self.client.post("/auth/senha/cadastro", data=self.data).status_code, 400)
        for changes in (dict(accept_policies=""), dict(username="a"), dict(email="inválido"), dict(password="curta"),
                        dict(phone=""), dict(phone="11 3333-4444"), dict(phone="banana")):
            self.assertEqual(urlsplit(self.signup(**changes).location).path, "/cadastro")
        self.signup()
        self.post("/sair")
        self.signup(username="LEITOR.LOCAL", email="outra@example.test", password="uma senha diferente")
        self.signup(username="outra_pessoa", email="leitor+site@EXAMPLE.test")
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT created FROM account_totals").fetchone()[0], 1)
        with self.client.session_transaction() as s:
            self.assertNotIn("user_id", s)

    def test_legacy_social_profile_is_not_claimed_by_its_email(self):
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("INSERT INTO users(google_sub,email,name) VALUES(?,?,?)",
                           ("subject-google-dono", self.data["email"], "Dono original"))
        self.signup()
        with self.app.app_context():
            rows = get_db().execute("SELECT id,name,auth_provider FROM users ORDER BY id").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["name"], "Dono original")
            self.assertEqual(rows[0]["auth_provider"], "google")
        with self.client.session_transaction() as s:
            self.assertEqual(s["user_id"], rows[1]["id"])
        self.assertEqual(self.client.get("/admin").status_code, 403)

    def test_old_account_keeps_its_library_destination_and_can_correct_phone(self):
        self.signup()
        destination = "/biblioteca?aba=historico"
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE users SET phone_e164=NULL")
                db.execute("DELETE FROM account_consents")
        # Inclusive o cookie emitido antes da migração passa pela tela de
        # conclusão, sem perder os parâmetros da página que tentou abrir.
        before = urlsplit(self.client.get(destination).location)
        self.assertEqual(before.path, "/conta/celular")
        self.assertEqual(parse_qs(before.query)["next"], [destination])
        self.assertEqual(self.client.get("/privacidade").status_code, 200)
        self.post("/sair")
        via_login = self.login(next=destination)
        self.assertEqual(via_login.location, before.geturl())
        self.assertIn('type="tel"', self.client.get(via_login.location).get_data(as_text=True))
        form = dict(next=destination, phone="+1 202 555 0123")
        self.assertEqual(self.post("/conta/celular", form).status_code, 400)
        self.assertEqual(self.post("/conta/celular", {**form, "phone":"444", "accept_policies":"yes"}).status_code, 400)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT phone_e164 FROM users").fetchone()[0])
        self.assertEqual(self.post("/conta/celular", {**form, "accept_policies":"yes"}).location, destination)
        self.assertEqual(self.client.get(destination).status_code, 200)
        self.assertIn("+12025550123", self.client.get("/conta").get_data(as_text=True))
        corrected = self.post("/conta/celular", dict(phone=self.data["phone"], next="//fora.invalid"))
        self.assertEqual(corrected.location, "/conta")
        self.assertIn("+5511987654321", self.client.get("/conta").get_data(as_text=True))

    def test_only_owner_sees_contact_details_of_other_profiles(self):
        self.signup()
        owner_client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            with db:
                owner_id = db.execute("""INSERT INTO users(google_sub,email,name,phone_e164)
                    VALUES('subject-google-dono','dono@example.test','Responsável','+12025550123')""").lastrowid
        with owner_client.session_transaction() as cookie:
            cookie["user_id"] = owner_id
        admin = owner_client.get("/admin")
        self.assertEqual(admin.status_code, 200)
        for detail in ("leitor.local", self.data["email"], "+5511987654321", "Licença de estudo"):
            self.assertIn(detail, admin.get_data(as_text=True))
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertNotIn("+12025550123", self.client.get("/conta").get_data(as_text=True))

    def test_public_stylesheet_skips_account_lookup_even_while_logged_in(self):
        self.signup()
        with patch("accounts.get_db", side_effect=AssertionError("CSS consultou o cadastro")):
            with self.client.get("/static/site.css") as stylesheet:
                self.assertEqual(stylesheet.status_code, 200)
                self.assertNotIn("Cookie", stylesheet.vary)

    def test_limit_survives_switch_of_identifier_and_expires(self):
        self.signup()
        self.post("/sair")
        with patch.object(password_auth, "LOGIN_BY_ACCOUNT", 2), patch.object(password_auth, "time") as clock:
            clock.return_value = 100_000
            for name in (self.data["username"], self.data["email"]):
                self.assertEqual(urlsplit(self.login(login=name, password="senha errada").location).path, "/entrar")
            blocked = self.login()
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers["Retry-After"], str(password_auth.WINDOW))
            clock.return_value += password_auth.WINDOW + 1
            self.assertEqual(self.login().location, "/biblioteca")

    def test_local_session_refreshes_policy_without_creating_new_account(self):
        self.signup()
        self.post("/sair")
        with patch.object(accounts, "POLICY_VERSION", "revisao-seguinte"):
            renew = self.login()
            self.assertEqual(parse_qs(urlsplit(renew.location).query)["aceite"], ["1"])
            with self.client.session_transaction() as s:
                self.assertNotIn("user_id", s)
            self.assertEqual(self.login(accept_policies="yes").location, "/biblioteca")
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM account_consents").fetchone()[0], 2)
            self.assertEqual(get_db().execute("SELECT created FROM account_totals").fetchone()[0], 1)

    def test_render_ingress_separates_browsers_and_persists_only_opaque_keys(self):
        with (patch.dict(os.environ, {"RENDER":"true"}), patch.object(password_auth, "SIGNUP_BY_ADDRESS", 1),
              patch.dict(password_auth.RENDER_BURST, {"signup":2})):
            for index in range(3):
                client = self.app.test_client()
                with client.session_transaction() as s:
                    s["_csrf"] = f"sessao-{index}"
                data = {**self.data, "username":f"leitor{index}", "email":f"leitor{index}@example.test",
                        "csrf_token":f"sessao-{index}"}
                reply = client.post("/auth/senha/cadastro", data=data,
                                    environ_overrides={"REMOTE_ADDR":"10.0.0.7"})
                if index < 2:
                    self.assertEqual(reply.location, "/biblioteca")
                else:
                    self.assertEqual(reply.status_code, 429)
        with self.app.app_context():
            rows = get_db().execute("SELECT bucket FROM password_limits").fetchall()
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertRegex(row["bucket"], r"^[0-9a-f]{64}$")

    def test_changing_password_revokes_other_browsers(self):
        self.signup()
        other = self.app.test_client()
        self.login(client=other)
        new = "nova frase bem comprida 🌿"
        self.post("/conta/senha", dict(current_password="incorreta", new_password=new))
        self.assertEqual(other.get("/biblioteca").status_code, 200)
        self.post("/conta/senha", dict(current_password=self.password, new_password=new))
        self.assertEqual(self.client.get("/biblioteca").status_code, 200)
        self.assertEqual(urlsplit(other.get("/biblioteca").location).path, "/entrar")
        self.assertEqual(urlsplit(self.login(client=other).location).path, "/entrar")
        self.assertEqual(self.login(client=other, password=new, next="//fora.invalid").location, "/conta")
        self.post("/conta/excluir", dict(confirmation="EXCLUIR"))
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT * FROM password_credentials").fetchone())
