"""Percorre o navegador pelas URLs reais, usando o acervo instalado e um perfil temporário."""

from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app import create_app
from bible_browser import _contents
from core import get_db


class PrintedPages(HTMLParser):
    """Observa as páginas e os links que o leitor recebe, sem simular o índice."""
    def __init__(self, html):
        super().__init__()
        self.pages = []
        self.current = None
        self.links = {}
        self.ids = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-page-number" in attrs:
            number = int(attrs["data-page-number"])
            self.pages.append(number)
            if "leaf-current" in attrs["class"].split():
                self.current = number
        if "data-book-turn" in attrs:
            self.links[attrs["class"].split()[-1], attrs["data-book-turn"]] = attrs["href"]
        if tag == "li" and "id" in attrs:
            self.ids.append(attrs["id"])


class BibleBrowserFlow(TestCase):
    def setUp(self):
        # Cada execução começa com métricas, preferências e usuários independentes.
        folder = self.enterContext(TemporaryDirectory())
        self.app = create_app({"TESTING": True, "SECRET_KEY": "somente-teste",
                               "DATABASE": Path(folder) / "leitor.db"})
        self.client = self.app.test_client()

    def test_selection_and_draw_link_reach_the_same_chapter_and_study(self):
        # O sorteio fornece um ID e os seletores usam livro, capítulo e verso.
        # Os dois caminhos precisam destacar a mesma linha da mesma edição.
        from_draw = self.client.get("/biblia/versiculo/57002016")
        from_picker = self.client.get("/biblia?livro=JHN&capitulo=3&versiculo=16")
        canonical = "/biblia/JHN/3?versiculo=16#v16"
        self.assertEqual(from_draw.location, canonical)
        self.assertEqual(from_picker.location, canonical)
        page = self.client.get(canonical).get_data(as_text=True)
        self.assertIn('id="v16" class="bible-selected"', page)
        self.assertIn('id="v15"', page)
        printed = PrintedPages(page)
        self.assertIn(printed.current, printed.pages)
        # Quando a passagem termina uma folha, a próxima folha deve abrir o
        # versículo seguinte. O leitor não carrega todo o capítulo escondido.
        if 'id="v17"' not in page:
            following = self.client.get(printed.links["leaf-single", "next"]).get_data(as_text=True)
            self.assertIn('id="v17"', following)
        self.assertIn("Porque Deus amou o mundo", page)
        self.assertIn('href="/estudar/57002016"', page)
        self.assertNotIn("Favoritar versículo", page)
        self.assertNotIn("/biblioteca/favoritos/versiculo/", page)

    def test_first_last_and_cross_book_navigation(self):
        # Os links vizinhos respeitam os extremos da edição e cruzam a fronteira entre livros.
        first = self.client.get("/biblia", follow_redirects=True)
        self.assertEqual(first.request.path, "/biblia/GEN/1")
        printed = PrintedPages(first.get_data(as_text=True))
        self.assertEqual(printed.pages, [1, 2])
        self.assertEqual(printed.current, 1)
        self.assertNotIn(("leaf-double", "previous"), printed.links)
        self.assertNotIn(("leaf-single", "previous"), printed.links)
        last = PrintedPages(self.client.get("/biblia/REV/22?versiculo=21").get_data(as_text=True))
        self.assertNotIn(("leaf-double", "next"), last.links)
        self.assertNotIn(("leaf-single", "next"), last.links)
        genesis = PrintedPages(self.client.get("/biblia/GEN/50?versiculo=26").get_data(as_text=True))
        exodus = PrintedPages(self.client.get("/biblia?livro=EXO", follow_redirects=True).get_data(as_text=True))
        self.assertEqual(genesis.links["leaf-single", "next"], "/biblia/EXO/1")
        self.assertEqual(exodus.current, genesis.current + 1)
        previous = PrintedPages(self.client.get(exodus.links["leaf-single", "previous"]).get_data(as_text=True))
        self.assertEqual(previous.current, genesis.current)

    def test_page_turn_and_long_chapter_bookmarks(self):
        first = PrintedPages(self.client.get("/biblia/GEN/1").get_data(as_text=True))
        next_mobile = PrintedPages(self.client.get(first.links["leaf-single", "next"]).get_data(as_text=True))
        next_desktop = PrintedPages(self.client.get(first.links["leaf-double", "next"]).get_data(as_text=True))
        self.assertEqual(next_mobile.current, 2)
        self.assertEqual(next_desktop.pages, [3, 4])
        linked = self.client.get("/biblia/PSA/119?versiculo=176").get_data(as_text=True)
        self.assertIn('id="v176" class="bible-selected"', linked)
        self.assertNotIn('id="v1"', linked)
        self.assertEqual(self.client.get("/biblia/GEN/1?folha=999").status_code, 404)
        self.assertEqual(self.client.get("/biblia/indice/XYZ/1").status_code, 404)
        self.assertIn({"number":28,"label":"28-29"}, self.client.get("/biblia/indice/4MA/8").json)

    def test_complete_index_global_count_and_mobile_destinations(self):
        by_book, _, leaves = _contents()
        # A numeração corresponde à edição física simulada: uma tela pequena
        # pode mostrar somente a segunda folha de uma dupla, com o mesmo total.
        last = self.client.get("/biblia/REV/22?versiculo=21").get_data(as_text=True)
        total = f"{len(leaves):,}".replace(",", ".")
        self.assertIn(f"Página {total} de {total}", last)
        self.assertEqual(PrintedPages(last).current, len(leaves))
        self.assertIn("<h1>Bíblia Sagrada</h1>", last)
        for book, chapters in by_book.items():
            self.assertIn(f'data-tree-book="{book}"', last)
            self.assertTrue(chapters)
        # Um link na conta ou no menu geral precisa preservar sua seção
        # depois do redirecionamento para o início do livro.
        for section in ("1", "notas"):
            response = self.client.get("/biblia?indice=" + section)
            self.assertEqual(response.location, f"/biblia/GEN/1?indice={section}#indice-biblico")
        self.assertIn('aria-label="Apocalipse 22:21" aria-current="location"', last)

    def test_turn_preserves_index_until_chapter_changes(self):
        headers = {"X-Bible-Chapter": "GEN/1"}
        page = self.client.get("/biblia/GEN/1?folha=2", headers=headers).get_data(as_text=True)
        self.assertEqual(PrintedPages(page).current, 2)
        self.assertNotIn('id="indice-biblico"', page)
        next_chapter = self.client.get("/biblia/GEN/2", headers=headers).get_data(as_text=True)
        self.assertIn('id="indice-biblico"', next_chapter)

    def test_grouped_verses_and_editorial_gaps(self):
        # 4 Macabeus 8 tem uma única linha para 28 e 29. O link do 29 deve
        # selecionar aquela linha sem oferecer um ID inexistente para estudo.
        joined = self.client.get("/biblia/4MA/8?versiculo=29", follow_redirects=True)
        self.assertEqual(joined.request.args["versiculo"], "28")
        self.assertIn('id="v28" class="bible-selected"', joined.get_data(as_text=True))
        self.assertIn('href="/estudar/52007028"', joined.get_data(as_text=True))

        gap = self.client.get("/biblia/SIR/1?versiculo=5").get_data(as_text=True)
        self.assertIn("Sem texto nesta edição.", gap)
        self.assertNotIn('href="/estudar/43000005"', gap)

    def test_selected_verse_in_private_history_and_favorites(self):
        # Só o passo de login é substituído. Abertura, consentimento e
        # favorito passam pelos mesmos formulários e rotas do navegador.
        with self.app.app_context():
            db = get_db()
            with db:
                user_id = db.execute(
                    "INSERT INTO users(google_sub,email,name,phone_e164) VALUES(?,?,?,'+5511987654321')",
                    ("teste-local", "teste@example.test", "Leitor")
                ).lastrowid
                second_id = db.execute(
                    "INSERT INTO users(google_sub,email,name,phone_e164) VALUES(?,?,?,'+5511987654321')",
                    ("outro-leitor", "outro@example.test", "Outro leitor")
                ).lastrowid
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["_csrf"] = "somente-teste"
        form = {"csrf_token": "somente-teste"}
        selected = "/biblia/JHN/3?versiculo=16"
        self.client.post("/biblioteca/historico/preferencia", data={**form, "choice": "enable"})
        self.client.head("/biblia/JHN/3?versiculo=17")
        for _ in range(2):
            self.assertEqual(self.client.get(selected).status_code, 200)
        result = self.client.post("/biblioteca/favoritos/versiculo/57002016", data=form)
        self.assertEqual(result.status_code, 303)
        self.assertIn("Nos meus favoritos", self.client.get(selected).get_data(as_text=True))
        with self.app.app_context():
            rows = get_db().execute("SELECT verse_id,kind FROM library_history WHERE user_id=?", (user_id,))
            self.assertEqual([tuple(row) for row in rows], [(57002016, "browse")])
        # Um segundo perfil autenticado pode salvar a mesma passagem em sua
        # própria conta. Ao abri-la, seu marcador ainda está desativado.
        second = self.app.test_client()
        with second.session_transaction() as session:
            session["user_id"] = second_id
        other_page = second.get(selected).get_data(as_text=True)
        self.assertNotIn("Nos meus favoritos", other_page)
        self.assertIn("Favoritar versículo", other_page)
        other_history = second.get("/biblioteca?aba=historico").get_data(as_text=True)
        self.assertNotIn("João 3:16", other_history)
        anonymous = self.app.test_client()
        public_page = anonymous.get(selected).get_data(as_text=True)
        self.assertNotIn("Nos meus favoritos", public_page)
        self.assertNotIn("Favoritar versículo", public_page)
        self.assertNotIn("/biblioteca/favoritos/versiculo/", public_page)

    def test_unknown_references(self):
        for path in ("/biblia?livro=XYZ", "/biblia?livro=GEN&capitulo=x", "/biblia/GEN/0",
                     "/biblia/GEN/51", "/biblia/GEN/1?versiculo=999", "/biblia/versiculo/999999999"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
