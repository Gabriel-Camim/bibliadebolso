"""Verifica quem recebe o guia e a duração da escolha de cada leitor."""
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app import create_app
from core import get_db
from tutorial import SEEN_COOKIE


class ReadingTutorial(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.database = Path(directory) / "perfil.sqlite3"
        # Versão anterior do cadastro. A migração precisa reconhecer esses
        # leitores mesmo quando chegarem sem um único cookie do navegador.
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript("""CREATE TABLE users (
                id INTEGER PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, email TEXT NOT NULL, phone_e164 TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                disabled INTEGER NOT NULL DEFAULT 0, auth_provider TEXT NOT NULL DEFAULT 'google');
                INSERT INTO users(google_sub,name,email,phone_e164)
                    VALUES('antigo','Leitor','old@example.test','+12025550127');""")
        self.app = create_app(dict(TESTING=True, SECRET_KEY="teste-de-guia", DATABASE=self.database))
        self.client = self.app.test_client()

    def post(self, client, url, **data):
        with client.session_transaction() as state:
            token = state["_csrf"]
        return client.post(url, data={**data, "csrf_token": token}, headers={"Accept":"application/json"})

    def test_dismiss_and_replay_leave_verse_and_personal_data_untouched(self):
        page = self.client.get("/")
        self.assertIn(b'data-first="1"', page.data)
        with self.client.session_transaction() as state:
            verse = state["last_verse"]
        self.assertEqual(self.client.post("/tutorial/visto").status_code, 400)
        self.assertTrue(self.post(self.client, "/tutorial/visto").json["ok"])
        self.assertIsNone(self.client.get_cookie(SEEN_COOKIE))
        with self.client.session_transaction() as state:
            self.assertEqual(state["last_verse"], verse)
        page = self.client.get("/biblia/GEN/1?versiculo=3")
        self.assertIn(b'data-first="0"', page.data)
        self.assertIn(b'data-replay-tutorial', page.data)
        self.assertIn(b'aria-current="location"', page.data)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM study_turns").fetchone()[0], 0)

    def test_persistent_guest_choice_requires_optional_cookie_permission(self):
        self.client.get("/")
        self.post(self.client, "/tutorial/visto")
        self.post(self.client, "/cookies", choice="preferences")
        self.assertEqual(self.client.get_cookie(SEEN_COOKIE).value, "1")
        self.client.delete_cookie("session")
        self.assertIn(b'data-first="0"', self.client.get("/").data)
        self.post(self.client, "/cookies", choice="essential")
        self.assertIsNone(self.client.get_cookie(SEEN_COOKIE))

    def test_known_guest_and_payment_screen_do_not_trigger_first_tour(self):
        self.assertIn(b'data-first="0"', self.client.get("/assinatura").data)
        with self.client.session_transaction() as state:
            state["last_verse"] = 12
        self.assertIn(b'data-first="0"', self.client.get("/").data)

    def test_signed_profile_remembers_across_devices_after_first_tour(self):
        with self.client.session_transaction() as state:
            state["user_id"] = 1
        self.assertIn(b'data-first="0"', self.client.get("/").data)
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("""INSERT INTO users(google_sub,name,email,phone_e164)
                    VALUES('novo','Leitor','new@example.test','+12025550128')""")
        new_browser = self.app.test_client()
        with new_browser.session_transaction() as state:
            state["user_id"] = 2
        self.assertIn(b'data-first="1"', new_browser.get("/biblia/GEN/1").data)
        self.post(new_browser, "/tutorial/visto")
        other_device = self.app.test_client()
        with other_device.session_transaction() as state:
            state["user_id"] = 2
        self.assertIn(b'data-first="0"', other_device.get("/").data)
