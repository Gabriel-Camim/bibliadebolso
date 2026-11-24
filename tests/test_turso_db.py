"""Testes de integração do SQL privado com o transporte HTTP do Turso.

Cada requisição passa pela interface Hrana e por um SQLite temporário real.
O pequeno transporte abaixo só implementa os comandos usados no projeto.
Não abre sockets, não depende de um plano Turso e não recebe seus tokens.
"""
import base64
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

import turso_db


def _value(value):
    """Cada célula tem um tipo explícito no JSON; inteiros viajam como texto."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": value}


def _argument(value):
    kind = value["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "blob":
        # O protocolo também aceita base64 sem os sinais de preenchimento.
        return base64.b64decode(value["base64"] + "===")
    return value["value"]


class LocalPipeline:
    """Cada baton em aberto representa uma conexão real e isolada com o arquivo."""
    def __init__(self, path):
        self.path = path
        self.streams = {}
        self.serial = 0
        self.posts = []

    def close(self):
        for db in self.streams.values():
            db.close()

    @staticmethod
    def _sequence(db, sql):
        # SQLite sabe distinguir o final de um statement de um ponto e vírgula
        # interno de um gatilho. O texto precisa ser executado na mesma sessão.
        statement = ""
        for char in sql:
            statement += char
            if char == ";" and sqlite3.complete_statement(statement):
                db.execute(statement)
                statement = ""
        if statement.strip():
            db.execute(statement)

    def post(self, url, *, json, **options):
        self.posts.append((url, json, options))
        baton = json.get("baton")
        if baton is None:
            self.serial += 1
            baton = f"conexao-{self.serial}"
            self.streams[baton] = sqlite3.connect(self.path, isolation_level=None)
        db = self.streams[baton]
        results = []
        for operation in json["requests"]:
            kind = operation["type"]
            entry = {"type": kind}
            try:
                if kind == "execute":
                    stmt = operation["stmt"]
                    cursor = db.execute(stmt["sql"], [_argument(v) for v in stmt.get("args", [])])
                    rows = cursor.fetchall()
                    entry["result"] = {
                        "cols": [{"name": col[0]} for col in cursor.description or []],
                        "rows": [[_value(v) for v in row] for row in rows],
                        "affected_row_count": max(cursor.rowcount, 0),
                        "last_insert_rowid": str(cursor.lastrowid) if cursor.lastrowid else None,
                    }
                elif kind == "sequence":
                    self._sequence(db, operation["sql"])
                elif kind == "get_autocommit":
                    entry["is_autocommit"] = not db.in_transaction
                elif kind == "close":
                    self.streams.pop(baton).close()
                    baton = None
                else:
                    raise AssertionError(f"Operação fora do contrato de teste: {kind}")
            except sqlite3.Error as exc:
                # O servidor de referência publica apenas o código primário
                # nestas respostas, por exemplo SQLITE_CONSTRAINT.
                code = "SQLITE_CONSTRAINT" if isinstance(exc, sqlite3.IntegrityError) else exc.sqlite_errorname
                results.append({"type": "error", "error": {"message": str(exc), "code": code}})
            else:
                results.append({"type": "ok", "response": entry})
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"baton": baton, "base_url": None, "results": results}
        return response


class TursoTransactions(TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.path = Path(folder) / "perfil.db"
        self.transport = LocalPipeline(self.path)
        self.addCleanup(self.transport.close)
        self.post = self.enterContext(patch.object(requests.Session, "post", side_effect=self.transport.post))
        self.conn = turso_db.connect("libsql://leitor.turso.io", "token-somente-do-teste")
        self.addCleanup(self.conn.close)

    def test_rows_preserve_types_names_and_cursor_position(self):
        self.conn.row_factory = sqlite3.Row
        n = 2**60 + 3  # Um float de 64 bits perderia a precisão deste número.
        result = self.conn.execute("SELECT ? AS id, ? AS titulo, ? AS dado, ? AS vazio, ? AS fracao "
                                   "UNION ALL SELECT 2, 'segundo', NULL, NULL, 0",
                                   (n, "Gênesis", b"\x00\xfe", None, 0.25))
        row = result.fetchone()
        self.assertEqual(row[0], n)
        self.assertEqual(dict(row), {"id": n, "titulo":"Gênesis", "dado":b"\x00\xfe", "vazio":None, "fracao":0.25})
        self.assertEqual([r["titulo"] for r in result], ["segundo"])
        self.assertIsNone(result.fetchone())
        self.assertFalse(self.transport.streams)

    def test_trigger_returning_transactions_and_unique_constraint(self):
        self.conn.executescript("""
            CREATE TABLE usuarios(id INTEGER PRIMARY KEY, nome TEXT UNIQUE);
            CREATE TABLE total(n INTEGER);
            INSERT INTO total VALUES(0);
            CREATE TRIGGER novo AFTER INSERT ON usuarios BEGIN
                UPDATE total SET n=n+1;
            END;
        """)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            inserted = self.conn.execute("INSERT INTO usuarios(nome) VALUES(?) RETURNING id", ("saulo",))
            self.assertEqual(inserted.lastrowid, inserted.fetchone()["id"])
            self.assertEqual(inserted.rowcount, 1)
        with self.assertRaises(sqlite3.IntegrityError) as failure:
            # A segunda inclusão falha; a primeira e seu incremento do contador
            # devem ser revertidos pelo mesmo rollback.
            with self.conn:
                self.conn.execute("INSERT INTO usuarios(nome) VALUES(?)", ("rute",))
                self.conn.execute("INSERT INTO usuarios(nome) VALUES(?)", ("saulo",))
        self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_CONSTRAINT_UNIQUE)
        self.assertEqual(self.conn.execute("SELECT n FROM total").fetchone()[0], 1)
        self.assertEqual([r["nome"] for r in self.conn.execute("SELECT nome FROM usuarios")], ["saulo"])
        self.assertFalse(self.transport.streams)

    def test_account_deletion_cascades_in_the_actual_write_session(self):
        self.conn.executescript("""
            CREATE TABLE conta(id INTEGER PRIMARY KEY);
            CREATE TABLE nota(dono INTEGER REFERENCES conta(id) ON DELETE CASCADE);
            INSERT INTO conta VALUES(1); INSERT INTO nota VALUES(1);
        """)
        with self.conn:
            self.conn.execute("DELETE FROM conta WHERE id=1")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM nota").fetchone()[0], 0)
        self.assertTrue(all(url == "https://leitor.turso.io/v3/pipeline" for url, _, _ in self.transport.posts))

    def test_single_write_confirms_foreign_keys_and_obeys_outer_rollback(self):
        self.conn.executescript("""CREATE TABLE perfil(id INTEGER PRIMARY KEY);
            CREATE TABLE leitura(dono INTEGER REFERENCES perfil(id)); INSERT INTO perfil VALUES(1);""")
        before = self.post.call_count
        self.conn.execute_atomic("INSERT INTO leitura VALUES(?)", (1,))
        self.assertEqual(self.post.call_count - before, 2)
        self.assertFalse(self.transport.streams)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute_atomic("INSERT INTO leitura VALUES(?)", (2,))
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute_atomic("INSERT INTO leitura VALUES(?)", (1,))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM leitura").fetchone()[0], 1)

    def test_lost_commit_is_never_resent(self):
        self.conn.execute("CREATE TABLE marcador(nome TEXT)")
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("INSERT INTO marcador(nome) VALUES('estudo')")

        def lost_response(url, *, json, **options):
            # O servidor confirmou e liberou a transação, mas o cliente nunca
            # recebeu a resposta. Não sabemos o resultado pelo transporte.
            self.transport.post(url, json=json, **options)
            raise requests.ConnectionError("Conexão de teste interrompida")

        self.post.side_effect = lost_response
        before = self.post.call_count
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.commit()
        self.assertEqual(self.post.call_count, before+1)
        with closing(sqlite3.connect(self.path)) as check:
            self.assertEqual(check.execute("SELECT count(*) FROM marcador").fetchone()[0], 1)
        # Teardown da requisição também não pode repetir o COMMIT perdido.
        self.conn.close()
        self.assertEqual(self.post.call_count, before+1)

    def test_open_transaction_in_script_is_not_committed_on_close(self):
        self.conn.executescript("CREATE TABLE teste(valor INTEGER); BEGIN; INSERT INTO teste VALUES(1);")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM teste").fetchone()[0], 1)
        self.conn.close()
        with closing(sqlite3.connect(self.path)) as check:
            self.assertEqual(check.execute("SELECT count(*) FROM teste").fetchone()[0], 0)

    def test_flask_restart_preserves_account_and_private_note(self):
        from app import create_app
        config = dict(TESTING=True, SECRET_KEY="somente-para-o-ensaio", DATABASE_BACKEND="turso",
                      TURSO_DATABASE_URL="libsql://leitor.turso.io", TURSO_AUTH_TOKEN="token-de-ensaio",
                      PUBLIC_BASE_URL="http://localhost")
        first = create_app(config).test_client()

        def submit(client, url, **fields):
            with client.session_transaction() as session:
                csrf = session.setdefault("_csrf", "formulario-de-teste")
            return client.post(url, data={**fields, "csrf_token":csrf})

        created = submit(first, "/auth/senha/cadastro", username="leitor", email="leitor@example.test",
                         password="uma-senha-de-teste", phone="(11) 98765-4321", accept_policies="yes")
        self.assertEqual(created.status_code, 303)
        saved = submit(first, "/biblia/marcacoes", book="GEN", chapter=1, start_verse=1,
                       end_verse=1, color="gold", title="Minha leitura", body="Voltar aqui.")
        self.assertEqual(saved.status_code, 201)
        # Outra instância, sem os cookies da anterior, abre o mesmo banco.
        # Ela só enxerga a nota depois de verificar a senha e criar nova sessão.
        before = self.post.call_count
        restarted = create_app(config).test_client()
        # Ao acordar, a Render precisa consultar apenas a versão do esquema.
        self.assertLessEqual(self.post.call_count - before, 2)
        self.assertEqual(restarted.get("/biblia/marcacoes").status_code, 401)
        entered = submit(restarted, "/auth/senha/entrar", login="leitor", password="uma-senha-de-teste")
        self.assertEqual(entered.status_code, 303)
        self.assertEqual(restarted.get("/biblia/marcacoes").json["notes"][0]["body"], "Voltar aqui.")
        self.assertEqual(restarted.get("/saude").status_code, 200)
