"""Testes locais da cobrança. Usam um segredo fictício e não movimentam dinheiro."""

import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit
from unittest.mock import patch

from flask import Flask, g

import billing


class BillingWebhookTest(unittest.TestCase):
    def setUp(self):
        # A instalação pode ter um portal próprio no .env. Cada cenário
        # define aqui se a Stripe deve receber um identificador de portal.
        self.enterContext(patch.dict("os.environ", {"STRIPE_TEST_PORTAL_CONFIGURATION": ""}))
        # Um banco em memória por teste permite simular notificações fora de
        # ordem sem tocar no histórico de uma conta real.
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        billing.init_billing(self.db)
        self.db.execute("INSERT INTO users VALUES (1)")
        self.db.execute("""INSERT INTO billing_orders
            (id,user_id,created,offer_revision,payload) VALUES('pedido',1,0,1,'{}')""")
        self.db.commit()
        self.addCleanup(self.db.close)
        app = Flask(__name__)
        app.config.update(STRIPE_WEBHOOK_SECRET="exemplo-local", STRIPE_SECRET_KEY="sk_test_exemplo",
                          OWNER_GOOGLE_SUB="dono-ficticio",
                          SECRET_KEY="somente-teste", PUBLIC_BASE_URL="https://exemplo.invalid")
        # O login Google pertence aos testes de contas. Aqui simulamos apenas
        # a identidade já verificada, como ela chega ao blueprint da cobrança.
        @app.before_request
        def signed_in():
            g.user = {"id": 1, "disabled": 0, "auth_provider":"google", "google_sub":"dono-ficticio"}
        # O portal devolve o navegador à página da conta. Aqui basta seu
        # endereço, pois o cadastro completo tem uma suíte própria.
        app.add_url_rule("/conta", endpoint="accounts.profile", view_func=lambda: "")
        app.register_blueprint(billing.bp)
        self.client = app.test_client()
        lookup = patch("billing.get_db", return_value=self.db)
        lookup.start()
        self.addCleanup(lookup.stop)

    def deliver(self, event_id, kind, obj, secret="exemplo-local", created=None):
        # A Stripe assina timestamp, ponto e corpo exato da requisição.
        stamp = str(int(time.time()))
        body = json.dumps({"id":event_id, "type":kind, "created":created or int(stamp),
                           "livemode":False, "data":{"object":obj}}).encode()
        signature = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/stripe", data=body, content_type="application/json",
                                headers={"Stripe-Signature":f"t={stamp},v1={signature}"}).status_code

    def test_checkout_uses_server_price_and_resumes(self):
        with self.db:
            self.db.execute("UPDATE billing_offer SET published=1,price_cents=1990")
            self.db.execute("UPDATE billing_orders SET state='expired'")
        form = {"revision":"1", "verse_id":"123", "price":"0.01"}
        fake_session = {"id":"cs_exemplo", "url":"https://checkout.stripe.com/exemplo", "status":"open"}
        with patch("billing._stripe", return_value=fake_session) as api:
            self.assertEqual(self.client.post("/checkout", data=form).status_code, 303)
            fields = api.call_args.args[1]
            self.assertEqual(fields["line_items[0][price_data][unit_amount]"], 1990)
            self.assertEqual(fields["mode"], "subscription")
            # A Stripe vê apenas um identificador de pedido no retorno. A
            # associação com o versículo só acontece no site, após autenticar.
            self.assertNotIn("verse_id", json.dumps(fields))
            cancel = urlsplit(fields["cancel_url"])
            back = self.client.get(cancel.path + "?" + cancel.query)
            self.assertEqual(back.location, "/assinatura?verse_id=123")
            # Um segundo clique apenas consulta a sessão que já existe.
            self.assertEqual(self.client.post("/checkout", data=form).status_code, 303)
            self.assertEqual(api.call_args.args, ("checkout/sessions/cs_exemplo",))
        self.assertEqual(self.db.execute("SELECT count(*) FROM billing_orders WHERE session_id IS NOT NULL").fetchone()[0], 1)
        self.assertFalse(billing.has_subscription(self.db, 1))

    def test_draft_to_study_with_modern_invoice_snapshot(self):
        # Todos os dados deste percurso estão no SQLite em memória. A oferta
        # de R$ 29,90 existe somente no ambiente de teste desta suíte.
        self.client.application.add_url_rule("/estudar/<int:verse_id>", endpoint="study.start",
                                             view_func=lambda verse_id: str(verse_id))
        with self.db:
            self.db.execute("UPDATE billing_orders SET state='expired'")
            self.db.execute("UPDATE billing_offer SET published=0,price_cents=2990,revision=2")
        form = {"verse_id":"123", "revision":"2"}
        with patch("billing._stripe") as provider:
            self.assertEqual(self.client.post("/checkout",data=form).status_code,302)
            provider.assert_not_called()
        with self.db:
            self.db.execute("UPDATE billing_offer SET published=1")
        session_info = {"id":"cs_teste_moderno","status":"open","url":"https://example.invalid/checkout"}
        with patch("billing._stripe",return_value=session_info) as provider:
            self.assertEqual(self.client.post("/checkout",data=form).status_code,303)
            params = provider.call_args.args[1]
        order_id = params["client_reference_id"]
        self.assertEqual(params["subscription_data[metadata][order]"],order_id)
        self.assertEqual(params["line_items[0][price_data][unit_amount]"],2990)
        self.assertEqual(params["line_items[0][price_data][currency]"],"brl")
        returned = "/assinatura/retorno?order=" + order_id
        self.assertEqual(self.deliver("cs1","checkout.session.completed",{
            "id":"cs_teste_moderno","customer":"cus_moderno","subscription":"sub_moderno",
            "metadata":{"order":order_id}}),204)
        # Um redirect bem-sucedido não pode conceder estudo. A página aguarda
        # a fatura e verifica novamente, inclusive se o navegador voltou cedo.
        with patch("billing.render_template",return_value="Aguardando"):
            self.assertIn("tentativa=1",self.client.get(returned).headers["Refresh"])
            self.assertNotIn("Refresh",self.client.get(returned+"&tentativa=12").headers)
        self.assertFalse(billing.has_subscription(self.db,1))
        period_end = int(time.time())+30*86400
        invoice = {"id":"in_moderno","amount_paid":2990,"status":"paid","currency":"brl","customer":"cus_moderno",
                   "parent":{"type":"subscription_details","subscription_details":{
                       "subscription":"sub_moderno","metadata":{"order":order_id}}},
                   "lines":{"data":[{"parent":{"type":"subscription_item_details"},
                                     "period":{"end":period_end}}]}}
        # Basil e versões posteriores podem omitir charge e payments. A
        # liberação depende da fatura assinada e dispensa outra consulta à API.
        with patch("billing._stripe") as provider:
            self.assertEqual(self.deliver("paga","invoice.paid",invoice),204)
            self.assertEqual(self.deliver("paga","invoice.paid",invoice),204)
            provider.assert_not_called()
        self.assertEqual(self.client.get(returned).location,"/estudar/123")
        self.assertEqual(billing.billing_metrics(self.db)["gross_cents"],2990)
        self.assertEqual(billing.billing_metrics(self.db)["paid_count"],1)
        # Um estorno moderno resolve o vínculo reverso pelo PaymentIntent e
        # Invoice Payments, que substituiu a referência direta charge.invoice.
        def reconcile(path, *, version=None):
            if path=="charges/ch_moderno":
                return {"id":"ch_moderno","payment_intent":"pi_moderno"}
            if path.startswith("invoice_payments?"):
                self.assertEqual(version,"2025-03-31.basil")
                self.assertIn("pi_moderno",path)
                return {"data":[{"invoice":"in_moderno"}],"has_more":False}
            self.assertEqual(path,"invoices/in_moderno")
            return invoice
        refund = {"id":"re_moderno","currency":"brl","charge":"ch_moderno","amount":2990,"status":"succeeded"}
        with patch("billing._stripe",side_effect=reconcile):
            self.assertEqual(self.deliver("reembolso","refund.created",refund),204)
        self.assertEqual(billing.billing_metrics(self.db)["adjusted_cents"],0)
        self.assertFalse(billing.has_subscription(self.db,1))

    def test_sandbox_publication_never_opens_live_offer(self):
        form = {"title":"Plano de teste","description":"Estudo em ambiente de testes.",
                "price":"29,90","revision":"1","published":"on","offer_mode":"test"}
        self.assertEqual(self.client.post("/admin/checkout",data=form).status_code,302)
        self.assertEqual(billing.get_offer(self.db)["price_cents"],2990)
        self.assertTrue(billing.get_offer(self.db)["published"])
        with patch("billing._live_mode",return_value=True):
            self.assertFalse(billing.get_offer(self.db)["published"])
            self.assertEqual(billing.get_offer(self.db)["price_cents"],0)
            # Uma aba de administração de teste, ainda aberta após a troca de
            # ambiente, não publica sua oferta no ambiente real simulado.
            self.client.post("/admin/checkout",data=form)
            self.assertFalse(billing.get_offer(self.db)["published"])
            self.client.post("/admin/checkout",data=dict(form,offer_mode="live",price="12,00"))
            self.assertEqual(billing.get_offer(self.db)["price_cents"],1200)
            with patch("billing._stripe") as gateway:
                self.client.post("/checkout",data={"offer_mode":"test","revision":"2"})
                gateway.assert_not_called()
        self.assertEqual(billing.get_offer(self.db)["price_cents"],2990)

    def test_deleting_account_forgets_verse_and_preserves_finance(self):
        # O titular já encerrou sua assinatura. Precisamos guardar a fatura
        # e o valor pago, sem conservar a referência religiosa do estudo.
        with self.db:
            self.db.execute("UPDATE billing_orders SET verse_id=321, state='complete', sub_status='canceled'")
            self.db.execute("INSERT INTO billing_invoices VALUES ('in_1','pedido','ch_1',1990,1)")
            self.db.execute("DELETE FROM users WHERE id=1")
        row = self.db.execute("SELECT user_id, verse_id, payload FROM billing_orders").fetchone()
        self.assertEqual(tuple(row), (None, None, "{}"))
        self.assertEqual(billing.billing_metrics(self.db)["gross_cents"], 1990)

    def test_canceled_customer_can_read_invoices_and_sign_up_again(self):
        with self.db:
            self.db.execute("""UPDATE billing_orders SET state='complete',sub_status='canceled',
                customer='cus_arquivo',subscription='sub_arquivo' WHERE id='pedido'""")
            self.db.execute("""INSERT INTO billing_invoices VALUES
                ('in_arquivo','pedido','ch_arquivo',1990,9999999999)""")
            self.db.execute("UPDATE billing_offer SET published=1,price_cents=1990")
        # Consultar cobranças antigas não altera o contrato nem impede a
        # exclusão da conta, mesmo se o período original ainda não terminou.
        with patch("billing._stripe", return_value={"url":"https://exemplo.invalid/faturas"}) as api:
            response = self.client.post("/assinatura/portal", data={"order":"pedido"})
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.location,"https://exemplo.invalid/faturas")
        api.assert_called_once_with("billing_portal/sessions", {
            "customer":"cus_arquivo","return_url":"https://exemplo.invalid/conta"})
        self.assertFalse(billing.has_subscription(self.db,1))
        self.assertTrue(billing.can_delete_account(1))
        with patch("billing._stripe",return_value={"id":"cs_nova","url":"https://exemplo.invalid/pagar","status":"open"}):
            purchase = self.client.post("/checkout",data={"revision":"1"})
        self.assertEqual(purchase.location,"https://exemplo.invalid/pagar")

    def test_portal_selection_is_private_and_scoped_to_stripe_mode(self):
        with self.db:
            self.db.execute("INSERT INTO users VALUES (2)")
            self.db.execute("""UPDATE billing_orders SET state='complete',sub_status='canceled',
                customer='cus_antigo' WHERE id='pedido'""")
            # Duas contratações compartilham um cliente. O portal desse cliente
            # reúne ambas e o seletor exibe uma única opção para ele.
            self.db.executemany("""INSERT INTO billing_orders
                (id,user_id,livemode,customer,sub_status,created,offer_revision,payload,state)
                VALUES(?,?,?,?,?,?,1,'{}','complete')""", [
                ("mesmo-cliente",1,0,"cus_antigo","canceled",3),
                ("atual",1,0,"cus_novo","active",5),
                ("outro-titular",2,0,"cus_outro","active",7),
                ("producao",1,1,"cus_real","active",9)])
        self.assertEqual([r["id"] for r in billing.portal_orders(self.db,1)],["atual","mesmo-cliente"])
        self.client.application.config["STRIPE_TEST_PORTAL_CONFIGURATION"] = "bpc_teste"
        with patch("billing._stripe", return_value={"url":"https://exemplo.invalid/faturas"}) as api:
            for order_id in ("outro-titular","producao","inexistente"):
                self.assertEqual(self.client.post("/assinatura/portal",data={"order":order_id}).status_code,404)
            api.assert_not_called()
            for order_id, customer in (("pedido","cus_antigo"),("atual","cus_novo")):
                self.assertEqual(self.client.post("/assinatura/portal",data={"order":order_id}).status_code,303)
                self.assertEqual(api.call_args.args[1]["customer"],customer)
                self.assertEqual(api.call_args.args[1]["configuration"],"bpc_teste")
            # No ambiente de produção a seleção se inverte. Um pedido válido
            # do próprio titular em teste não abre uma sessão de produção.
            with patch("billing._live_mode",return_value=True):
                self.assertEqual([r["id"] for r in billing.portal_orders(self.db,1)],["producao"])
                self.assertEqual(self.client.post("/assinatura/portal",data={"order":"atual"}).status_code,404)
                self.assertEqual(self.client.post("/assinatura/portal",data={"order":"producao"}).status_code,303)
                self.assertEqual(api.call_args.args[1]["customer"],"cus_real")
                self.assertNotIn("configuration",api.call_args.args[1])

    def test_invoice_first_retries_and_refunds(self):
        # Mesmo sem checkout.session.completed, os metadados da assinatura
        # ligam o primeiro pagamento a quem iniciou o pedido.
        invoice = {"id":"in_exemplo", "subscription":"sub_exemplo", "currency":"brl",
                   "charge":"ch_exemplo", "customer":"cus_exemplo", "amount_paid":1990,
                   "subscription_details":{"metadata":{"order":"pedido"}},
                   "lines":{"data":[{"type":"subscription","period":{"end":int(time.time()) + 86400}}]}}
        self.assertEqual(self.deliver("e1","invoice.paid",invoice,"segredo-errado"), 400)
        for event_id in ["e1", "e1", "e2"]:
            self.assertEqual(self.deliver(event_id,"invoice.paid",invoice), 204)
        self.assertTrue(billing.has_subscription(self.db, 1))
        self.assertFalse(billing.has_subscription(self.db, 2))
        self.assertEqual(billing.billing_metrics(self.db)["paid_count"], 1)
        # Ao trocar a loja para produção, vendas de teste ficam fora tanto
        # dos indicadores financeiros quanto da autorização para o chat.
        with patch("billing._live_mode", return_value=True):
            self.assertEqual(billing.billing_metrics(self.db)["paid_count"], 0)
            self.assertFalse(billing.has_subscription(self.db, 1))
        # Dois reembolsos parciais distintos anulam o valor integral. A
        # repetição do mesmo reembolso nunca deve descontá-lo duas vezes.
        for event_id, refund, value in [("r1","re_a",1000),("r2","re_a",1000),("r3","re_b",990)]:
            self.assertEqual(self.deliver(event_id, "refund.created", {
                "id":refund, "amount":value, "currency":"brl", "status":"succeeded", "charge":"ch_exemplo"}), 204)
        self.assertEqual(billing.billing_metrics(self.db)["adjusted_cents"], 0)
        self.assertFalse(billing.has_subscription(self.db, 1))

    def test_late_update_after_cancellation(self):
        # Uma atualização antiga, entregue depois da exclusão da assinatura,
        # deve conservar o cancelamento da recorrência.
        sub = {"id":"sub_exemplo", "customer":"cus_exemplo", "status":"canceled",
               "metadata":{"order":"pedido"}}
        self.assertEqual(self.deliver("e3","customer.subscription.deleted",sub), 204)
        sub["status"] = "active"
        self.assertEqual(self.deliver("e4","customer.subscription.updated",sub), 204)
        self.assertEqual(self.db.execute("SELECT sub_status FROM billing_orders").fetchone()[0], "canceled")

    def test_recovering_old_price_requires_new_confirmation(self):
        # O checkout antigo foi criado, mas o servidor caiu antes de gravar o
        # ID retornado. O dono publica um preço novo durante a interrupção.
        with self.db:
            self.db.execute("UPDATE billing_offer SET published=1,revision=2,price_cents=2990")
            self.db.execute("UPDATE billing_orders SET created=?,payload=?", (
                int(time.time()), json.dumps({"recuperar":"via-idempotencia"})))
        sessions = [{"id":"cs_antiga", "url":"https://checkout.stripe.com/antiga", "status":"open"},
                    {"id":"cs_antiga", "status":"expired"}]
        with patch("billing._stripe", side_effect=sessions) as api:
            response = self.client.post("/checkout", data={"revision":"2"})
        self.assertEqual(api.call_args.args, ("checkout/sessions/cs_antiga/expire", {}))
        self.assertEqual(response.location, "/assinatura")
        self.assertEqual(self.db.execute("SELECT state FROM billing_orders").fetchone()[0], "expired")

    def test_first_payment_failure_can_resume_original_checkout(self):
        # Uma falha de cartão pode ser seguida de nova tentativa na mesma
        # sessão. O leitor não deve ser enviado ao portal antes da assinatura.
        with self.db:
            self.db.execute("UPDATE billing_offer SET published=1,price_cents=1990")
            self.db.execute("UPDATE billing_orders SET session_id='cs_aberta'")
        sub = {"id":"sub_tentativa", "customer":"cus_tentativa", "status":"incomplete", "metadata":{"order":"pedido"}}
        invoice = {"id":"in_recusada", "subscription":"sub_tentativa", "currency":"brl", "customer":"cus_tentativa"}
        self.assertEqual(self.deliver("incompleta","customer.subscription.created",sub), 204)
        self.assertEqual(self.deliver("falha","invoice.payment_failed",invoice), 204)
        self.assertEqual(self.db.execute("SELECT state FROM billing_orders").fetchone()[0], "pending")
        reply = {"id":"cs_aberta", "url":"https://checkout.stripe.com/test", "status":"open"}
        with patch("billing._stripe", return_value=reply) as api:
            response = self.client.post("/checkout", data={"revision":"1"})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(api.call_args.args, ("checkout/sessions/cs_aberta",))
        self.assertFalse(billing.has_subscription(self.db, 1))

    def test_conflicting_subscription_updates_in_same_second(self):
        stamp = int(time.time())
        obj = {"id":"sub_estado","customer":"cus_estado","status":"paused","metadata":{"order":"pedido"}}
        self.assertEqual(self.deliver("pausa","customer.subscription.updated",obj,created=stamp), 204)
        obj["status"] = "active"
        # O aviso antigo chegou por último, com timestamp idêntico. A consulta
        # ocorre antes da transação de escrita, sem bloquear os outros leitores.
        def latest(path):
            self.assertFalse(self.db.in_transaction)
            self.assertEqual(path, "subscriptions/sub_estado")
            return {"status":"paused"}
        with patch("billing._stripe", side_effect=latest):
            self.assertEqual(self.deliver("antigo","customer.subscription.updated",obj,created=stamp), 204)
        self.assertEqual(self.db.execute("SELECT sub_status FROM billing_orders").fetchone()[0], "paused")

    def test_refund_before_any_invoice_or_session_webhook(self):
        invoice = {"id":"in_tardia", "subscription":"sub_tardia", "charge":"ch_tardia",
                   "customer":"cus_tardia","currency":"brl","amount_paid":1500,
                   "subscription_details":{"metadata":{"order":"pedido"}},
                   "lines":{"data":[{"type":"subscription","period":{"end":int(time.time())+86400}}]}}
        def lookup(path):
            # A conciliação consulta o provedor fora do bloqueio de escrita.
            self.assertFalse(self.db.in_transaction)
            return {"charges/ch_tardia":{"invoice":"in_tardia"}, "invoices/in_tardia":invoice}[path]
        with patch("billing._stripe", side_effect=lookup):
            self.assertEqual(self.deliver("estorno_antes", "refund.created", {
                "id":"re_tardia", "amount":1500, "currency":"brl", "charge":"ch_tardia", "status":"succeeded"}), 204)
        self.assertEqual(self.deliver("paga_depois", "invoice.paid", invoice), 204)
        self.assertFalse(billing.has_subscription(self.db,1))
        self.assertEqual(billing.billing_metrics(self.db)["adjusted_cents"],0)

    def test_dispute_partial_reinstatement_arrives_first(self):
        # O extrato da contestação pode informar uma devolução parcial dos
        # fundos. A operação é registrada mesmo se chegar antes do débito.
        with self.db:
            self.db.execute("INSERT INTO billing_invoices VALUES('in_disp','pedido','ch_disp',1990,9999999999)")
        dispute = {"id":"dp_1","charge":"ch_disp","currency":"brl","amount":1990,"status":"won",
                   "balance_transactions":[{"currency":"brl","amount":-1990},{"currency":"brl","amount":990}]}
        self.assertEqual(self.deliver("reversao","charge.dispute.funds_reinstated",dispute),204)
        self.assertEqual(billing.billing_metrics(self.db)["adjusted_cents"],990)
        dispute["balance_transactions"] = [{"currency":"brl","amount":-1990}]
        self.assertEqual(self.deliver("debito","charge.dispute.funds_withdrawn",dispute),204)
        self.assertEqual(billing.billing_metrics(self.db)["adjusted_cents"],990)

    def test_two_simultaneous_requests_share_order(self):
        # Duas conexões ao mesmo arquivo reproduzem duas abas em processos
        # distintos. Ambas aguardam a resposta da Stripe para a mesma reserva.
        app = self.client.application
        gate = Barrier(2, timeout=5)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory, "compras.sqlite")
            setup = sqlite3.connect(filename)
            self.db.backup(setup)
            with setup:
                setup.execute("UPDATE billing_offer SET published=1,price_cents=1250")
                setup.execute("UPDATE billing_orders SET state='expired'")
            setup.close()
            def connection():
                if "billing_test_db" not in g:
                    g.billing_test_db = sqlite3.connect(filename)
                    g.billing_test_db.row_factory = sqlite3.Row
                return g.billing_test_db
            @app.teardown_appcontext
            def cleanup(_error):
                if "billing_test_db" in g:
                    g.pop("billing_test_db").close()
            def provider(path, fields, *, idem):
                self.assertEqual(fields["client_reference_id"],idem)
                self.assertEqual(path, "checkout/sessions")
                gate.wait()
                return {"id":"cs_" + idem,"url":"https://checkout.stripe.com/" + idem,"status":"open"}
            def pay(_):
                return app.test_client().post("/checkout",data={"revision":"1"}).location
            with (patch("billing.get_db",side_effect=connection), patch("billing._stripe",side_effect=provider),
                  ThreadPoolExecutor(2) as pool):
                links = list(pool.map(pay, range(2)))
            self.assertEqual(links[0],links[1])
            self.assertTrue(links[0].startswith("https://checkout.stripe.com/"))
            check = sqlite3.connect(filename)
            try:
                self.assertEqual(check.execute("SELECT count(*) FROM billing_orders WHERE state='pending'").fetchone()[0],1)
                self.assertEqual(check.execute("SELECT count(*) FROM billing_events WHERE kind='checkout.started'").fetchone()[0],1)
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
