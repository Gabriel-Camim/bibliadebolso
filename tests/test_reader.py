"""Testes do caminho real do chaveiro ao estudo. Usam uma sessão e um banco descartáveis."""
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app import create_app, serve_site


class ReaderFlow(TestCase):
    def setUp(self):
        # Usamos o mesmo acervo bíblico da instalação. Apenas o banco de usuários,
        # preferências e métricas é substituído, para que os testes sejam isolados.
        folder = self.enterContext(TemporaryDirectory())
        self.database = Path(folder) / "site.db"
        self.app = create_app(dict(TESTING=True, SECRET_KEY="chave-do-teste", DATABASE=self.database))
        self.client = self.app.test_client()

    def test_render_without_database_credentials_stops_before_creating_private_files(self):
        # Um deploy com variável faltando precisa falhar visivelmente. Aceitar o
        # primeiro cadastro no disco temporário faria o leitor perdê-lo ao reiniciar.
        private = self.database.parent / "sem-disco"
        with patch.dict(os.environ, {"RENDER":"true", "APP_DATA_DIR":str(private),
                                    "SECRET_KEY":"teste", "DATABASE_BACKEND":"",
                                    "TURSO_DATABASE_URL":"", "TURSO_AUTH_TOKEN":""}):
            with self.assertRaisesRegex(RuntimeError, "TURSO_DATABASE_URL"):
                create_app()
        self.assertFalse(private.exists())

    def test_health_probe_keeps_visit_metrics_and_cookie_untouched(self):
        response = self.client.get("/saude")
        self.assertEqual((response.status_code, response.get_data(as_text=True)), (200, "ok"))
        self.assertNotIn("Set-Cookie", response.headers)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0], 0)

    def test_render_replaces_development_origins_and_preserves_the_domain(self):
        assigned = "https://leitor-de-teste.onrender.com"
        with patch.dict(os.environ, {"RENDER":"true", "RENDER_EXTERNAL_URL":assigned}):
            for source in ("http://127.0.0.1:5050", "http://localhost", "http://[::1]:8000",
                           "https://bibliadebolso.com"):
                with self.subTest(source=source):
                    app = create_app(dict(TESTING=True, SECRET_KEY="teste", DATABASE=self.database,
                                          PUBLIC_BASE_URL=source))
                    expected = source if source == "https://bibliadebolso.com" else assigned
                    self.assertEqual(app.config["PUBLIC_BASE_URL"], expected)
                    self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_render_refuses_sqlite_on_ephemeral_disk_before_writing(self):
        empty = self.database.parent / "temporario"
        with patch.dict(os.environ, {"RENDER":"true", "DATABASE_BACKEND":"sqlite", "APP_DATA_DIR":str(empty)}):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_BACKEND=turso"):
                create_app()
        self.assertFalse(empty.exists())

    def test_waitress_receives_the_render_port_and_the_correct_local_default(self):
        # Substituímos apenas o servidor para conferir os argumentos reais
        # da entrada python app.py sem abrir portas nos testes.
        with patch("app.create_app", return_value=self.app), patch("waitress.serve") as serve, patch("builtins.print"):
            with patch.dict(os.environ, {"RENDER":"true", "PORT":"12000", "BIND_HOST":"127.0.0.1"}):
                serve_site()
                serve.assert_called_with(self.app, host="0.0.0.0", port=12000)
            with patch.dict(os.environ, {"RENDER":"", "PORT":"", "BIND_HOST":""}):
                serve_site()
                serve.assert_called_with(self.app, host="127.0.0.1", port=5050)

    def test_refresh_button_and_study_keep_the_current_reference(self):
        first = self.client.get("/")
        self.assertEqual(first.status_code, 200)
        self.assertIn("bibliadebolso.com", first.get_data(as_text=True))
        with self.client.session_transaction() as saved:
            initial = saved["last_verse"]
        self.client.get("/")
        with self.client.session_transaction() as saved:
            second = saved["last_verse"]
            token = saved["_csrf"]
        self.assertNotEqual(initial, second)
        # O servidor sorteia somente após receber um token correspondente à sessão.
        self.assertEqual(self.client.post("/leitor/novo").status_code, 400)
        generated = self.client.post("/leitor/novo", headers={"X-CSRF-Token":token})
        self.assertEqual(generated.status_code, 200)
        with self.client.session_transaction() as saved:
            third = saved["last_verse"]
        self.assertNotEqual(second, third)
        self.assertEqual(generated.json["study_url"], f"/estudar/{third}")
        landing = self.client.get(generated.json["study_url"], follow_redirects=True)
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(landing.request.path, "/assinatura")
        self.assertEqual(landing.request.args["verse_id"], str(third))
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT sum(visits), sum(verses) FROM daily_metrics").fetchone(), (2, 3))
        finally:
            db.close()

    def test_cookie_choice_can_be_changed_without_login(self):
        self.client.get("/")
        with self.client.session_transaction() as saved:
            token = saved["_csrf"]
        for choice in ("preferences", "essential"):
            reply = self.client.post("/cookies", data={"choice":choice, "csrf_token":token},
                                     headers={"Accept":"application/json"})
            self.assertEqual(reply.json, {"choice":choice})
        self.assertEqual(self.client.get("/privacidade").status_code, 200)

    def test_public_draw_has_navigation_but_no_favorite(self):
        """A página pública e o botão de novo sorteio obedecem à mesma regra."""
        self.assertEqual(self.client.head("/").status_code, 200)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Navegar até o versículo", html)
        self.assertNotIn("Favoritar", html)
        self.assertNotIn("favorite-receipt", html)
        with self.client.session_transaction() as saved:
            csrf = saved["_csrf"]
        drawn = self.client.post("/leitor/novo", headers={"X-CSRF-Token": csrf}).json
        self.assertIsNone(drawn["draw_token"])
        chapter = self.client.get(drawn["bible_url"], follow_redirects=True)
        self.assertIn(drawn["reference"], chapter.get_data(as_text=True))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT sum(visits),sum(verses) FROM daily_metrics").fetchone(), (1, 2))
