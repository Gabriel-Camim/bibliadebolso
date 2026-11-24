"""Verifica os atalhos com os textos e as páginas da edição instalada."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app import create_app
from bible_browser import _contents


class BibleJump(TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.client = create_app(dict(TESTING=True, SECRET_KEY="teste",
                                      DATABASE=Path(folder) / "jump.db")).test_client()

    def test_reference_names_and_accented_abbreviations(self):
        cases = {"João 3:16": "/biblia/JHN/3?versiculo=16#v16",
                 "Jo 3,16": "/biblia/JHN/3?versiculo=16#v16",
                 "Jó 3:16": "/biblia/JOB/3?versiculo=16#v16",
                 "1 Co 13:4": "/biblia/1CO/13?versiculo=4#v4",
                 "Salmo 151": "/biblia/PS2/1", "Salmos": "/biblia/PSA/1",
                 "Êxodo 2": "/biblia/EXO/2"}
        for query, url in cases.items():
            with self.subTest(query=query):
                result = self.client.get("/biblia/localizar", query_string={"q":query})
                self.assertEqual(result.location, url)

    def test_pages_and_invalid_references(self):
        last = _contents()[2][-1]
        total = f"{last.number:,}".replace(",", ".")
        headers = {"Accept":"application/json"}
        result = self.client.get("/biblia/localizar", query_string={"q":"Página " + total}, headers=headers)
        self.assertEqual(result.json["url"], f"/biblia/{last.book}/{last.chapter}?folha={last.local}")
        for query in ("Página 0", str(last.number+1), "João 3:170", "João 99", "um lugar"):
            with self.subTest(query=query):
                error = self.client.get("/biblia/localizar", query_string={"q":query}, headers=headers)
                self.assertEqual(error.status_code, 422)
                self.assertTrue(error.json["message"])
