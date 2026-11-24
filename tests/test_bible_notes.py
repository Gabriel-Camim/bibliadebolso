"""Testa o editor real usando contas e arquivos temporários, sem dados pessoais."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4

import corpus
from app import create_app
from core import get_db


class BibleNotes(TestCase):
    path = "/biblia/marcacoes"

    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.app = create_app(dict(TESTING=True, SECRET_KEY="teste", DATABASE=Path(directory) / "perfil.db"))
        with self.app.app_context():
            conn = get_db()
            with conn:
                conn.executemany("INSERT INTO users(google_sub,email,name,phone_e164) VALUES(?,?,?,'+5511987654321')",
                                 [("1", "a@example.invalid", "A"), ("2", "b@example.invalid", "B")])
        self.a = self.client_for(1)
        self.b = self.client_for(2)
        self.guest = self.client_for(None)
        self.content = dict(book="GEN", chapter=1, start_verse=1, end_verse=2, color="sage",
                            title="Primeira leitura", body="Reler amanhã.")

    def client_for(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_csrf"] = "sessao-de-teste"
            if user_id is not None:
                session["user_id"] = user_id
        return client

    def submit(self, client, path=None, **changes):
        return client.post(path or self.path, data={**self.content, "csrf_token":"sessao-de-teste", **changes})

    def test_owner_reads_edits_deletes_and_filters_marks(self):
        created = self.submit(self.a)
        self.assertEqual(created.status_code, 201)
        note = created.json["note"]
        self.assertEqual(note["reference"], "Gênesis 1:1–2")
        self.assertEqual(note["url"], "/biblia/GEN/1?versiculo=1#v1")
        self.assertIsNone(note["start_offset"])
        self.assertIsNone(note["end_offset"])
        self.assertGreater(len(note["excerpt"]), 0)
        self.assertNotIn("user_id", note)
        self.assertEqual(self.a.get(f"{self.path}?book=GEN&chapter=1").json["notes"], [note])
        self.assertEqual(self.a.get(f"{self.path}?book=GEN&chapter=2").json["notes"], [])
        self.assertEqual(self.b.get(self.path).json["notes"], [])
        resource = f"{self.path}/{note['id']}"
        self.assertEqual(self.submit(self.b, resource).status_code, 404)
        self.assertEqual(self.submit(self.b, resource + "/excluir").status_code, 404)
        edited = self.submit(self.a, resource, color="rose", title="Novo", body="", revision=1)
        self.assertEqual(edited.json["note"]["revision"], 2)
        self.assertEqual(edited.json["note"]["color"], "rose")
        self.assertEqual(self.submit(self.a, resource, revision=1).status_code, 409)
        self.assertEqual(self.submit(self.a, resource + "/excluir", revision=1).status_code, 409)
        self.assertEqual(self.submit(self.a, resource + "/excluir", revision=2).json["deleted_id"], note["id"])
        self.assertEqual(self.a.get(self.path).json["notes"], [])

    def test_selection_of_words_and_inclusive_verse_groups(self):
        verse = corpus.verse_by_id(1)["text"]
        start = verse.index("Deus")
        response = self.submit(self.a, end_verse=1, start_offset=start, end_offset=start+4)
        self.assertEqual(response.json["note"]["excerpt"], "Deus")
        second = corpus.verse_by_id(2)["text"]
        response = self.submit(self.a, start_offset=len(verse)-6, end_offset=8)
        self.assertEqual(response.json["note"]["excerpt"], verse[-6:].strip() + " " + second[:8].strip())
        # O acervo também contém um número impresso que cobre dois versículos.
        with closing(corpus._connect()) as db:
            group = db.execute("""SELECT * FROM verses WHERE end_verse>start_verse AND text<>''
                                  ORDER BY id LIMIT 1""").fetchone()
        response = self.submit(self.a, book=group["book_code"], chapter=group["chapter"],
                               start_verse=group["end_verse"], end_verse=group["end_verse"])
        note = response.json["note"]
        self.assertEqual((note["start_verse"], note["end_verse"]), (group["start_verse"], group["end_verse"]))
        self.assertTrue(note["url"].endswith(f"#v{group['start_verse']}"))

    def test_invalid_data_does_not_create_ghost_highlights(self):
        invalid = [
            {"book":"AAA"}, {"chapter":999}, {"start_verse":3}, {"end_verse":998}, {"color":"red"},
            {"title":"t"*101}, {"body":"x"*3001}, {"start_offset":1},
            {"start_offset":999, "end_offset":1}, {"start_offset":5, "end_offset":4, "end_verse":1},
            {"end_offset":1}, {"request_id":"sem-uuid"}
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                result = self.submit(self.a, **changes)
                self.assertEqual(result.status_code, 422)
                self.assertTrue(result.json["message"])
        self.assertEqual(self.a.get(self.path).json["notes"], [])

    def test_json_csrf_and_idempotent_retries_per_account(self):
        self.assertEqual(self.guest.get(self.path).status_code, 401)
        self.assertEqual(self.submit(self.guest).status_code, 401)
        self.assertEqual(self.a.post(self.path, json=self.content).status_code, 400)
        payload = {**self.content, "request_id":str(uuid4())}
        # Duas tentativas do mesmo envio voltam para a mesma marcação. Uma
        # seleção intencionalmente repetida pode usar um novo identificador.
        headers = {"X-CSRF-Token":"sessao-de-teste"}
        first = self.a.post(self.path, json=payload, headers=headers)
        again = self.a.post(self.path, json=payload, headers=headers)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json["note"], again.json["note"])
        self.assertEqual(self.b.post(self.path, json=payload, headers=headers).status_code, 201)

    def test_suspension_preserves_removal_and_deleting_account_cascades(self):
        own = self.submit(self.a).json["note"]
        self.submit(self.b)
        self.submit(self.a, start_verse=2, end_verse=2)
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE users SET disabled=1 WHERE id=1")
        self.assertEqual(len(self.a.get(self.path).json["notes"]), 2)
        self.assertEqual(self.submit(self.a).status_code, 403)
        self.assertEqual(self.submit(self.a, f"{self.path}/{own['id']}").status_code, 403)
        self.assertEqual(self.submit(self.a, f"{self.path}/{own['id']}/excluir").status_code, 200)
        self.a.post("/conta/excluir", data={"csrf_token":"sessao-de-teste", "confirmation":"EXCLUIR"})
        with self.app.app_context():
            owners = [row[0] for row in get_db().execute("SELECT user_id FROM bible_notes")]
        self.assertEqual(owners, [2])
