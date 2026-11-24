"""Fluxos públicos e privados da biblioteca com sessões independentes e banco temporário."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from app import create_app
from core import get_db
from corpus import verse_by_id


class PersonalLibrary(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.app = create_app(dict(TESTING=True, SECRET_KEY="somente-no-teste",
                                   DATABASE=Path(directory) / "test.sqlite3"))
        with self.app.app_context():
            db = get_db()
            with db:
                db.executemany("INSERT INTO users(google_sub,email,name,phone_e164) VALUES(?,?,?,'+5511987654321')",
                               [("a", "a@example.invalid", "A"), ("b", "b@example.invalid", "B")])
        self.first = self.client_for(1)
        self.second = self.client_for(2)
        self.visitor = self.client_for(None)
        # A mesma referência repetida permite verificar se guardamos cada sorteio
        # no histórico e apenas um marcador nos favoritos.
        self.verse = verse_by_id(1)
        self.enterContext(patch("corpus.random_verse", return_value=self.verse))

    def client_for(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            if user_id is not None:
                session["user_id"] = user_id
            session["_csrf"] = f"csrf-de-teste-{user_id}"
        return client

    def post(self, client, path, **fields):
        """O teste passa pela validação real de CSRF, inclusive como visitante."""
        with client.session_transaction() as session:
            fields["csrf_token"] = session["_csrf"]
        return client.post(path, data=fields)

    def rows(self, query):
        with self.app.app_context():
            return [tuple(row) for row in get_db().execute(query)]

    def draw(self, client, at):
        with patch("library.time", return_value=at):
            response = self.post(client, "/leitor/novo")
        self.assertEqual(response.status_code, 200)
        return response.json.get("draw_token")

    def test_session_ownership_and_time_of_generation(self):
        token = self.draw(self.first, 1_700_000_000)
        self.assertTrue(token)
        # Nem a URL nem o formulário transferem a leitura a um segundo perfil.
        self.assertEqual(self.second.get(f"/biblioteca/salvar/{token}").status_code, 404)
        self.assertEqual(self.post(self.second, "/biblioteca/favoritos", draw_token=token).status_code, 404)
        self.assertEqual(self.post(self.first, "/biblioteca/favoritos", draw_token="falso").status_code, 400)
        self.assertEqual(self.first.post("/biblioteca/favoritos", data={"draw_token": token}).status_code, 400)
        with patch("library.time", return_value=1_700_086_400):
            for _ in range(2):
                self.assertEqual(self.post(self.first, "/biblioteca/favoritos", draw_token=token).status_code, 303)
        self.assertEqual(self.rows("SELECT user_id,verse_id,drawn_at,saved_at FROM library_favorites"),
                         [(1, 1, 1_700_000_000, 1_700_086_400)])
        page = self.first.get("/biblioteca").get_data(as_text=True)
        self.assertIn("Gênesis 1:1", page)
        self.assertIn("14/11/2023 às 19:13:20", page)
        self.assertIn("/biblia/versiculo/1", page)
        self.assertIn("/estudar/1", page)
        self.assertNotIn("Gênesis 1:1", self.second.get("/biblioteca").get_data(as_text=True))
        self.post(self.second, "/biblioteca/favoritos/1/excluir")
        self.assertEqual(self.rows("SELECT user_id FROM library_favorites"), [(1,)])
        # O cookie de sessão novo da própria pessoa também invalida o comprovante antigo.
        with self.first.session_transaction() as session:
            session["_csrf"] = "sessao-renovada-depois-do-login"
        self.assertEqual(self.post(self.first, "/biblioteca/favoritos", draw_token=token).status_code, 404)

    def test_anonymous_cannot_stage_or_save_a_favorite(self):
        self.assertIsNone(self.draw(self.visitor, 1_700_000_000))
        token = self.draw(self.first, 1_700_000_000)
        self.assertEqual(self.visitor.get(f"/biblioteca/salvar/{token}").status_code, 404)
        self.assertEqual(self.post(self.visitor, "/biblioteca/favoritos", draw_token=token).status_code, 403)
        self.assertEqual(self.post(self.visitor, "/biblioteca/favoritos/versiculo/1").status_code, 403)
        self.assertEqual(self.visitor.get("/biblioteca/favoritos/versiculo/1").status_code, 405)
        self.assertIn("/entrar", self.visitor.get("/biblioteca").location)
        self.assertEqual(self.rows("SELECT user_id FROM library_history"), [])
        self.assertEqual(self.rows("SELECT user_id FROM library_favorites"), [])

    def test_favorite_preserves_reading_and_draw_count(self):
        token = self.draw(self.first, 1_700_000_000)
        fields = {"csrf_token": "csrf-de-teste-1", "draw_token": token}
        response = self.first.post("/biblioteca/favoritos", data=fields,
                                   headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["saved"])
        self.assertEqual(response.json["verse_id"], self.verse["id"])
        self.assertNotIn("Location", response.headers)
        # Num navegador sem scripts o formulário volta ao mesmo sorteio.
        response = self.first.post("/biblioteca/favoritos", data=fields)
        self.assertEqual(response.location, "/")
        self.assertIn("Nos meus favoritos", self.first.get("/").get_data(as_text=True))
        self.assertEqual(self.rows("SELECT sum(verses) FROM daily_metrics"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM library_favorites"), [(1,)])
        self.first.get("/")
        self.assertEqual(self.rows("SELECT sum(verses) FROM daily_metrics"), [(2,)])

    def test_ribbon_on_nfc_page_and_bible_index_share_one_private_bookmark(self):
        # Os dois atalhos apontam para a mesma marca por perfil. O leitor
        # recebe os estados de favorito e de marca-página separadamente.
        self.assertNotIn("data-draw-bookmark", self.visitor.get("/").get_data(as_text=True))
        html = self.first.get("/").get_data(as_text=True)
        self.assertIn("data-draw-bookmark", html)
        self.assertIn('action="/biblia/marcacoes/rapida"', html)
        verse = self.post(self.first, "/leitor/novo").json
        self.assertFalse(verse["bookmarked"])
        mark = self.first.post("/biblia/marcacoes/rapida", data={
            "book":verse["book"], "chapter":verse["chapter"], "start_verse":verse["start_verse"],
            "csrf_token":"csrf-de-teste-1",
        }, headers={"Accept":"application/json"})
        self.assertEqual(mark.status_code, 201)
        self.assertTrue(mark.json["note"]["quick"])
        same = self.post(self.first, "/leitor/novo").json
        self.assertTrue(same["bookmarked"])
        self.assertFalse(same["saved"])
        self.assertIn("indice=notas", same["bookmark_index_url"])
        self.assertEqual(self.first.get(same["bookmark_index_url"]).status_code, 200)
        self.assertEqual(self.first.get("/biblia/marcacoes").json["notes"][0]["id"], mark.json["note"]["id"])
        self.assertFalse(self.post(self.second, "/leitor/novo").json["bookmarked"])

    def test_history_consent_isolation_and_revocation(self):
        self.draw(self.first, 1_700_000_000)
        self.assertEqual(self.rows("SELECT user_id FROM library_history"), [])
        for client in (self.first, self.second):
            self.post(client, "/biblioteca/historico/preferencia", choice="enable")
        self.draw(self.first, 1_700_000_000)
        self.draw(self.first, 1_700_000_001)
        self.draw(self.second, 1_700_000_002)
        result = self.rows("SELECT id,user_id,kind,occurred_at FROM library_history ORDER BY id")
        self.assertEqual(result, [(1, 1, "draw", 1_700_000_000), (2, 1, "draw", 1_700_000_001),
                                  (3, 2, "draw", 1_700_000_002)])
        self.assertEqual(self.post(self.second, "/biblioteca/historico/1/favoritar").status_code, 404)
        self.post(self.first, "/biblioteca/historico/2/favoritar")
        self.post(self.first, "/biblioteca/historico/limpar")
        self.assertEqual(self.rows("SELECT user_id FROM library_history"), [(2,)])
        self.assertEqual(self.rows("SELECT drawn_at FROM library_favorites"), [(1_700_000_001,)])
        self.draw(self.first, 1_700_000_003)
        self.post(self.first, "/biblioteca/historico/preferencia", choice="disable")
        self.draw(self.first, 1_700_000_004)
        self.assertEqual(self.rows("SELECT user_id FROM library_history"), [(2,)])
        self.assertEqual(self.rows("SELECT user_id FROM library_history_consent"), [(2,)])
        self.post(self.first, "/biblioteca/favoritos/1/excluir")
        self.assertEqual(self.rows("SELECT user_id FROM library_favorites"), [])

    def test_deleting_account_removes_its_library_only(self):
        for client in (self.first, self.second):
            self.post(client, "/biblioteca/historico/preferencia", choice="enable")
            self.draw(client, 1_700_000_000)
            self.post(client, "/biblioteca/favoritos/versiculo/1")
        self.assertEqual(self.rows("SELECT drawn_at FROM library_favorites"), [(None,), (None,)])
        self.post(self.first, "/conta/excluir", confirmation="EXCLUIR")
        self.assertEqual(self.rows("SELECT user_id FROM library_history"), [(2,)])
        self.assertEqual(self.rows("SELECT user_id FROM library_favorites"), [(2,)])
        self.assertEqual(self.rows("SELECT user_id FROM library_history_consent"), [(2,)])
