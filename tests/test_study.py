"""Exercita o fluxo de um assinante sem usar créditos nem dados de uma conta real."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app import create_app
from core import get_db
from study import FREE_PROMPTS, _context, _free_remaining, _quota_month, _window


class StudyFlow(TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.app = create_app({"TESTING": True, "SECRET_KEY": "isolado",
                               "OWNER_GOOGLE_SUB": "dono", "DATABASE": Path(folder) / "test.db"})
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            with db:
                db.executemany("INSERT INTO users(google_sub,email,name,phone_e164) VALUES(?,?,?,'+5511987654321')",
                               [("dono", "dono@example.test", "Dono"), ("leitor", "leitor@example.test", "Leitor")])
        # Nos testes da conversa, a confirmação financeira é substituída
        # por uma decisão local. A própria cobrança tem testes separados.
        self.paid = self.enterContext(patch("study.has_study_license", return_value=True))
        self.api = self.enterContext(patch("study.ask_openai", return_value="Consulte Gênesis 1:1."))
        self.login(1)

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["_csrf"] = "protecao"

    def send(self, **extra):
        data = {"csrf_token": "protecao", "token": "1" * 32, "verse_id": 1, "question": "Explique a criação."}
        return self.client.post("/estudo/enviar", data={**data, **extra})

    def stream(self, **extra):
        """Deixa o corpo aberto para observar um fragmento antes da resposta final."""
        data = {"csrf_token": "protecao", "token": "1"*32, "verse_id": 1, "question": "Explique a criação."}
        return self.client.post("/estudo/enviar", data={**data, **extra},
                                headers={"Accept": "application/x-ndjson"}, buffered=False)

    def test_live_fragments_commit_once_and_replay(self):
        # O falso provedor só é consultado quando o navegador começa a ler.
        # Cada yield ocorre antes da gravação final, como na conexão HTTPS.
        with patch("study.stream_openai", return_value=iter(["## ", "Criação", "\nO começo."])) as api:
            reply = self.stream(consent="on")
            self.assertEqual(reply.mimetype, "application/x-ndjson")
            iterator = iter(reply.response)
            start = json.loads(next(iterator))
            self.assertEqual(start["type"], "start")
            self.assertEqual(json.loads(next(iterator)), {"type": "delta", "text": "## "})
            with self.app.app_context():
                self.assertEqual(get_db().execute("SELECT COUNT(*) FROM study_turns").fetchone()[0], 0)
            tail = [json.loads(line) for line in iterator]
            reply.close()
            self.assertEqual([e["type"] for e in tail], ["delta", "delta", "done"])
            self.assertEqual(tail[-1]["answer"], "## Criação\nO começo.")
            self.assertEqual(len(tail[-1]["next_token"]), 32)
            self.assertTrue(tail[-1]["sources"][0]["url"].startswith("/biblia/"))
            again = self.stream()
            replay = [json.loads(line) for line in again.response]
            again.close()
            self.assertEqual([e["type"] for e in replay], ["start", "done"])
            self.assertEqual(replay[-1]["answer"], tail[-1]["answer"])
            api.assert_called_once()
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM study_turns").fetchone()[0], 1)

    def test_resume_private_chat_and_format_saved_answer(self):
        self.api.return_value = "**O começo**\n\nLeia **com atenção**.\n\n- Dia\n- Noite\n\n<texto>"
        finished = self.send(consent="on")
        self.assertEqual(self.client.get("/estudo").location, finished.location)
        saved = self.client.get(finished.location).get_data(as_text=True)
        for fragment in ("<h3>O começo</h3>", "<strong>com atenção</strong>",
                         "<ul><li>Dia</li><li>Noite</li></ul>", "&lt;texto&gt;"):
            self.assertIn(fragment, saved)
        self.assertNotIn("**O começo**", saved)
        # O botão ligado a um verso abre um rascunho, mesmo com conversa salva.
        draft = self.client.get("/estudar/1").get_data(as_text=True)
        self.assertIn('data-current=""', draft)
        self.assertNotIn("<h3>O começo</h3>", draft)
        self.login(2)
        self.assertNotEqual(self.client.get("/estudo").location, finished.location)

    def test_study_menu_uses_last_readable_verse_or_draws_a_new_one(self):
        self.paid.return_value = False
        with patch("study.random_verse", return_value={"id": 2}) as draw:
            self.assertEqual(self.client.get("/estudo").location, "/estudar/2")
            with self.client.session_transaction() as session:
                session["last_verse"] = 1
            self.assertEqual(self.client.get("/estudo").location, "/estudar/1")
            with self.client.session_transaction() as session:
                session["last_verse"] = 99999999
            self.assertEqual(self.client.get("/estudo").location, "/estudar/2")
            self.assertEqual(draw.call_count, 2)
        self.api.assert_not_called()

    def test_closed_connection_releases_generation_and_preserves_complete_history(self):
        # Fechamos a resposta logo após a primeira parte. Isso simula o WSGI
        # encerrando a requisição quando a pessoa sai da página.
        closed = []
        def incoming(*_args):
            try:
                yield "Início incompleto"
                yield "Fim"
            finally:
                closed.append(True)
        with patch("study.stream_openai", side_effect=incoming):
            reply = self.stream(consent="on")
            chunks = iter(reply.response)
            self.assertEqual(json.loads(next(chunks))["type"], "start")
            self.assertEqual(json.loads(next(chunks))["type"], "delta")
            # Outra aba, enquanto o primeiro envio está reservado, recebe 409.
            conflict = self.stream(chat_id=1, token="3"*32)
            self.assertEqual(conflict.status_code, 409)
            reply.close()
        self.assertEqual(closed, [True])
        with self.app.app_context():
            db = get_db()
            self.assertIsNone(db.execute("SELECT pending_token FROM study_chats").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM study_turns").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM study_cache").fetchone()[0], 0)
        self.assertEqual(self.send().status_code, 302)

    def test_interrupted_provider_does_not_save_partial_answer(self):
        # Resposta SSE válida até o último bloco, mas sem o terminador [DONE].
        # Os dois streams estão habilitados, na API e no cliente HTTP local.
        http = MagicMock()
        http.__enter__.return_value = http
        http.iter_lines.return_value = iter([b'data: {"choices":[{"delta":{"content":"parcial"}}]}'])
        with patch("study.requests.post", return_value=http) as api:
            self.app.config["OPENAI_API_KEY"] = "somente-teste"
            reply = self.stream(consent="on")
            events = [json.loads(line) for line in reply.response]
            reply.close()
            self.assertEqual([e["type"] for e in events], ["start", "delta", "error"])
            self.assertTrue(api.call_args.kwargs["stream"])
            self.assertTrue(api.call_args.kwargs["json"]["stream"])
            http.__exit__.assert_called_once()
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM study_turns").fetchone()[0], 0)
        self.assertEqual(self.send().status_code, 302)

    def test_deleting_chat_in_another_tab_stops_next_delta(self):
        # A exclusão precisa interromper também uma resposta em andamento e
        # jamais sugerir uma conversa de outro perfil como próximo destino.
        with patch("study.stream_openai", return_value=iter(["A", "B"])):
            reply = self.stream(consent="on")
            stream = iter(reply.response)
            chat_id = json.loads(next(stream))["chat_id"]
            self.assertEqual(json.loads(next(stream))["text"], "A")
            deleted = self.client.post(f"/estudo/{chat_id}/excluir", data={"csrf_token": "protecao"},
                                       headers={"Accept": "application/json"})
            self.assertEqual(deleted.json["deleted_id"], chat_id)
            self.assertEqual(deleted.json["next_url"], "/estudar/1")
            self.assertEqual([json.loads(line)["type"] for line in stream], ["error"])
            reply.close()
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) FROM study_cache").fetchone()[0], 0)

    def test_consent_cache_and_private_history(self):
        self.assertEqual(self.client.get("/estudar/1").status_code, 200)
        self.assertEqual(self.send().status_code, 400)
        self.api.assert_not_called()
        first = self.send(consent="on")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(self.send().location, first.location)
        self.api.assert_called_once()
        self.assertIn("Consulte Gênesis", self.client.get(first.location).get_data(as_text=True))
        self.assertEqual(self.send(chat_id=1, token="2"*32, question="E o contexto?").status_code, 302)
        self.assertEqual(self.api.call_args.args[2], [["Explique a criação.", "Consulte Gênesis 1:1."]])
        self.login(2)
        self.assertEqual(self.client.get(first.location).status_code, 404)
        self.assertEqual(self.client.post("/estudo/1/excluir", data={"csrf_token":"protecao"}).status_code, 404)
        self.login(1)
        self.paid.return_value = False
        self.assertEqual(self.client.get("/estudo/historico").status_code, 200)
        self.assertEqual(self.client.get(first.location).status_code, 200)
        # Ao terminar a licença, o leitor também pode usar as perguntas grátis.
        self.assertEqual(self.send(chat_id=1, token="3"*32).location, first.location)
        self.assertEqual(self.client.post("/estudo/revogar", data={"csrf_token":"protecao"}).status_code, 302)
        with self.app.app_context():
            for table in ("study_chats", "study_turns", "study_cache", "study_consents"):
                self.assertEqual(get_db().execute("SELECT count(*) FROM "+table).fetchone()[0], 0)

    def test_monthly_grant_retries_deletion_and_licenses(self):
        self.paid.return_value = False
        self.assertIn("data-free-allowance", self.client.get("/estudar/1").get_data(as_text=True))
        for i in range(1, FREE_PROMPTS + 1):
            args = {"chat_id": 1, "token": str(i)*32} if i > 1 else {"consent":"on"}
            self.assertEqual(self.send(**args).status_code, 302)
        # A resposta do último pedido pode ter se perdido na rede. Mesmo sem
        # saldo o cliente recupera esse resultado, sem nova geração da OpenAI.
        retry = self.stream(chat_id=1, token="5"*32)
        self.assertEqual([json.loads(line) for line in retry.response][-1]["free_remaining"], 0)
        retry.close()
        denied = self.stream(token="6"*32)
        self.assertEqual((denied.status_code, denied.json["url"]), (429, "/assinatura?verse_id=1"))
        self.assertEqual(self.api.call_count, 5)
        self.assertEqual(self.client.get("/estudo/1").status_code, 200)
        self.assertEqual(self.client.post("/estudo/1/excluir", data={"csrf_token":"protecao"}).status_code, 302)
        self.assertEqual(self.send(token="7"*32).status_code, 303)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT count(*) FROM study_chats").fetchone()[0], 0)
            self.assertEqual(_free_remaining(db, 1, int(time.time())), 0)
            self.assertEqual(_free_remaining(db, 2, int(time.time())), 5)
        self.paid.return_value = True
        self.assertEqual(self.send(token="8"*32).status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM study_free_prompts").fetchone()[0], 5)

    def test_free_slot_reserved_during_stream_and_returned_on_disconnect(self):
        self.paid.return_value = False
        for i in range(1, 5):
            self.assertEqual(self.send(token=str(i)*32, consent="on").status_code, 302)
        with patch("study.stream_openai", return_value=iter(["Trecho"])):
            reply = self.stream(token="5"*32)
            chunks = iter(reply.response)
            self.assertEqual(json.loads(next(chunks))["type"], "start")
            self.assertEqual(json.loads(next(chunks))["type"], "delta")
            self.assertEqual(self.stream(token="6"*32).status_code, 429)
            reply.close()
        self.api.side_effect = RuntimeError("openai_indisponivel")
        self.assertEqual(self.send(token="7"*32).status_code, 503)
        self.api.side_effect = None
        self.assertEqual(self.send(token="7"*32).status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM study_free_prompts").fetchone()[0], 5)

    def test_free_month_changes_at_midnight_in_sao_paulo_and_ignores_stale_leases(self):
        before = int(datetime(2026, 12, 1, 2, 59, 59, tzinfo=timezone.utc).timestamp())
        self.assertEqual((_quota_month(before), _quota_month(before+1)), ("2026-11", "2026-12"))
        self.paid.return_value = False
        with patch("study.time.time", return_value=before):
            self.assertEqual(self.send(consent="on").status_code, 302)
        with self.app.app_context():
            db = get_db()
            with db:
                db.execute("UPDATE study_chats SET pending_token='parou',lease_until=? WHERE id=1", (before-1,))
                db.execute("INSERT INTO study_free_prompts VALUES('parou',1,'2026-11',1)")
            self.assertEqual(_free_remaining(db, 1, before), 4)
            self.assertEqual(_free_remaining(db, 1, before+1), 5)

    def test_settings_affect_next_answer_and_owner_only(self):
        self.assertEqual(self.client.get("/admin/agente").status_code, 200)
        data = {"csrf_token":"protecao", "system_message":"Escreva em frases curtas.", "temperature":"0.75"}
        self.assertEqual(self.client.post("/admin/agente", data={**data,"temperature":"nan"}).status_code, 400)
        self.assertEqual(self.client.post("/admin/agente", data=data).status_code, 302)
        self.assertEqual(self.send(consent="on").status_code, 302)
        settings = self.api.call_args.args[3]
        self.assertEqual(settings["system_message"], data["system_message"])
        self.assertEqual(settings["temperature"], 0.75)
        self.login(2)
        self.assertEqual(self.client.get("/admin/agente").status_code, 403)

    def test_context_survives_expiry_and_obeys_both_limits(self):
        """A janela fria precisa reproduzir ordem e conteúdo da janela quente."""
        self.send(consent="on")
        turns = [(f"Questão {i}", f"Resposta {i}") for i in range(14)]
        with self.app.app_context():
            db = get_db()
            with db:
                db.executemany("""INSERT INTO study_turns(chat_id,token,question,answer,sources)
                                  VALUES(1,?,?,?,'[]')""",
                               [(f"turno-{i}", q, a) for i, (q, a) in enumerate(turns)])
                db.execute("UPDATE study_cache SET expires=0 WHERE chat_id=1")
            self.assertEqual(_context(db, 1), [list(pair) for pair in turns[-12:]])
        # Uma conversa com respostas extensas atinge o teto de caracteres
        # antes do número de pares. A pergunta e sua resposta ficam juntas.
        large = [("Pergunta", "a" * 8000) for _ in range(6)]
        self.assertEqual(_window(large), [list(pair) for pair in large[-3:]])

    def test_transient_failure_keeps_draft_and_releases_turn(self):
        self.api.side_effect = RuntimeError("falha sintética")
        failed = self.send(consent="on")
        self.assertEqual(failed.status_code, 503)
        self.assertIn("Explique a criação.", failed.get_data(as_text=True))
        self.api.side_effect = None
        self.assertEqual(self.send(chat_id=1).status_code, 302)
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM study_turns").fetchone()[0], 1)
