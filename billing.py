"""Assinatura mensal em reais, checkout hospedado e conciliação do faturamento.

Fluxo: o painel publica uma oferta, o leitor abre uma sessão na Stripe e a
Stripe confirma o pagamento por webhook. Cada fatura guarda um único crédito.
Os valores são inteiros em centavos, inclusive no banco e nos cálculos.

Para acompanhar o código, comece por offer(), checkout() e returned(): são
as páginas percorridas pelo leitor. As linhas com @bp.get ou @bp.post ligam
um endereço do site à função seguinte. GET mostra uma página. Os formulários
usam POST para pedir uma alteração. Já webhook() recebe avisos diretamente
da Stripe, mesmo quando o leitor fechou o navegador.

Nas consultas SQL, cada interrogação recebe um valor da tupla seguinte.
Assim, os textos digitados no painel são tratados como dados. “Commit”
confirma em conjunto as alterações de uma transação. “Rollback” as desfaz.
"""

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Blueprint, abort, current_app, flash, g, has_app_context, make_response, redirect, render_template,
    request, url_for,
)

from accounts import admin_required, login_required
from core import get_db


bp = Blueprint("billing", __name__)


def init_billing(db):
    """Cria as tabelas uma vez. A oferta nasce em rascunho, sem preço."""
    # A tabela anterior guarda a oferta de teste da instalação. O primeiro
    # uso em produção começa com outro rascunho. Os nomes SQL são constantes
    # do programa e nunca são recebidos de um formulário.
    for table in ("billing_offer", "billing_offer_live"):
        db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            title TEXT NOT NULL, description TEXT NOT NULL, note TEXT NOT NULL,
            price_cents INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1
        )""")
        db.execute(f"""INSERT OR IGNORE INTO {table} (id,title,description,note)
            VALUES (1,'Estudo guiado','Explore o contexto de cada passagem e guarde suas conversas para continuar a leitura depois.','')""")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS billing_orders (
            id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            verse_id INTEGER, offer_revision INTEGER NOT NULL, created INTEGER NOT NULL,
            livemode INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL, session_id TEXT UNIQUE,
            state TEXT NOT NULL DEFAULT 'pending', customer TEXT,
            subscription TEXT UNIQUE, sub_status TEXT, status_time INTEGER DEFAULT 0
        );
        -- Uma segunda aba pode recuperar a mesma tentativa. O índice protege
        -- inclusive quando há duas instâncias do servidor atendendo o leitor.
        CREATE UNIQUE INDEX IF NOT EXISTS billing_one_pending
            ON billing_orders(user_id,livemode) WHERE state = 'pending';
        -- Ao apagar uma conta, o SQLite anula o vínculo do pedido por meio
        -- da chave estrangeira. A referência religiosa e o corpo usado para
        -- criar o checkout não são necessários para o histórico financeiro.
        CREATE TRIGGER IF NOT EXISTS forget_checkout_context
        AFTER UPDATE OF user_id ON billing_orders WHEN NEW.user_id IS NULL
        BEGIN
            UPDATE billing_orders SET verse_id=NULL, payload='{}' WHERE id=NEW.id;
        END;
        -- Abrange pedidos de contas excluídas antes da criação do gatilho.
        UPDATE billing_orders SET verse_id=NULL, payload='{}'
        WHERE user_id IS NULL AND (verse_id IS NOT NULL OR payload!='{}');
        CREATE TABLE IF NOT EXISTS billing_invoices (
            invoice TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES billing_orders(id),
            charge TEXT, amount INTEGER NOT NULL, period_end INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS billing_invoice_charge ON billing_invoices(charge);
        CREATE TABLE IF NOT EXISTS billing_adjustments (
            id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES billing_orders(id),
            invoice TEXT NOT NULL, kind TEXT NOT NULL, amount INTEGER NOT NULL,
            event_time INTEGER NOT NULL, status TEXT NOT NULL
        );
        -- Guardamos somente dados operacionais. O JSON completo do provedor
        -- pode conter endereço, e-mail e dados desnecessários para o painel.
        CREATE TABLE IF NOT EXISTS billing_events (
            id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES billing_orders(id),
            created INTEGER NOT NULL, kind TEXT NOT NULL, reference TEXT NOT NULL,
            amount INTEGER
        );
        CREATE INDEX IF NOT EXISTS billing_event_time ON billing_events(created);
    """)


def get_offer(db):
    """A vitrine e o checkout sempre usam a mesma oferta publicada pelo dono."""
    return db.execute(f"SELECT * FROM {_offer_table()} WHERE id=1").fetchone()


def _offer_table():
    return "billing_offer_live" if _live_mode() else "billing_offer"


def _form_matches_stripe_mode():
    """Evita que uma aba aberta no teste publique ou confirme um preço real."""
    expected = "live" if _live_mode() else "test"
    # Formulários emitidos antes de separar os ambientes eram de teste.
    return request.form.get("offer_mode", "test") == expected


@bp.app_template_filter("brl")
def brl(cents):
    """Formata centavos com vírgula decimal, inclusive estornos negativos."""
    units, remainder = divmod(abs(int(cents)), 100)
    whole = f"{units:,}".replace(",", ".")
    return f"{'-' if cents < 0 else ''}R$ {whole},{remainder:02}"


@bp.app_template_filter("billing_time")
def billing_time(stamp):
    """O horário do painel acompanha São Paulo mesmo em servidores no exterior."""
    try:
        zone = ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        # Windows pode estar sem a base IANA. A sigla visível evita ambiguidade.
        from datetime import timezone
        zone = timezone.utc
    return datetime.fromtimestamp(stamp, zone).strftime("%d/%m/%Y %H:%M %Z")


def has_subscription(db, user_id):
    """O acesso depende de um período pago, confirmado pelo servidor.

    Uma fatura de outro mês não estende o acesso. Se o período corrente foi
    totalmente estornado ou contestado, o valor disponível volta a zero.
    """
    if not user_id:
        return False
    return bool(db.execute("""
        SELECT 1 FROM billing_orders o JOIN billing_invoices i ON i.order_id=o.id
        WHERE o.user_id=? AND o.livemode=? AND coalesce(o.sub_status, '') NOT IN
          ('canceled','unpaid','incomplete_expired','paused') AND i.period_end>?
          AND i.amount + coalesce((SELECT sum(a.amount) FROM billing_adjustments a
                                  WHERE a.invoice=i.invoice),0)>0
        LIMIT 1
    """, (user_id, _live_mode(), int(time.time()))).fetchone())


def _manageable_order(db, user_id):
    """Inclui assinantes inadimplentes para que consigam atualizar o cartão."""
    return db.execute("""
        SELECT * FROM billing_orders WHERE user_id=? AND livemode=? AND customer IS NOT NULL
        AND state='complete'
        AND coalesce(sub_status,'') NOT IN ('canceled','incomplete_expired')
        ORDER BY created DESC LIMIT 1
    """, (user_id, _live_mode())).fetchone()


def portal_orders(db, user_id, order_id=None):
    """Uma opção por cadastro de cobrança deste titular no ambiente atual.

    Cada nova contratação pode criar um cliente diferente na Stripe. Seu
    portal mostra só as faturas daquele cliente. O pedido local permite ao
    leitor escolher entre eles sem expor os identificadores da Stripe.
    """
    sql = """SELECT o.id,o.customer,o.created,o.sub_status FROM billing_orders o
             WHERE o.user_id=? AND o.livemode=? AND o.customer IS NOT NULL
             AND (o.state='complete' OR EXISTS (
                 SELECT 1 FROM billing_invoices i WHERE i.order_id=o.id))"""
    args = [user_id, _live_mode()]
    if order_id is not None:
        # O formulário pode ser modificado. O pedido precisa pertencer ao mesmo
        # titular e ambiente mesmo quando o identificador exato foi informado.
        sql += " AND o.id=?"
        args.append(order_id)
    # Colocamos o contrato atual primeiro e os cadastros anteriores por data.
    sql += """ ORDER BY coalesce(o.sub_status,'') IN ('canceled','incomplete_expired'),
               o.created DESC,o.id DESC"""
    options = []
    seen_customers = set()
    for row in db.execute(sql, args):
        if row["customer"] not in seen_customers:
            options.append(row)
            seen_customers.add(row["customer"])
    return options


def can_delete_account(user_id):
    """Preserva a ligação com a Stripe enquanto pode haver cobrança futura."""
    db = get_db()
    if not _live_mode() and db.execute("""SELECT 1 FROM billing_orders
        WHERE user_id=? AND livemode=1 AND (state='pending' OR
        (subscription IS NOT NULL AND coalesce(sub_status,'') NOT IN ('canceled','incomplete_expired')))
        LIMIT 1""", (user_id,)).fetchone():
        # Se o servidor voltou ao modo teste, ainda pode haver uma cobrança
        # real. O cadastro conserva a ligação necessária ao cancelamento.
        return False
    pending = db.execute("SELECT * FROM billing_orders WHERE user_id=? AND livemode=? AND state='pending'",
                         (user_id, _live_mode())).fetchone()
    # Um corte de rede antes de criar a sessão pode não produzir webhook.
    # Confirmamos a expiração na Stripe antes de liberar a remoção do cadastro.
    if pending and int(time.time()) >= pending["created"] + 35 * 60 and _configured():
        try:
            remote = _recover_attempt(pending)
        except StripeUnavailable:
            return False
        if remote["status"] == "expired":
            db.execute("UPDATE billing_orders SET state='expired' WHERE id=? AND state='pending'",
                       (pending["id"],))
            pending = None
    return not pending and not _manageable_order(db, user_id)


def billing_metrics(db):
    """Receita da aplicação antes das tarifas do processador e dos tributos."""
    mode = _live_mode()
    sales = db.execute("""SELECT coalesce(sum(i.amount),0), count(*) FROM billing_invoices i
        JOIN billing_orders o ON o.id=i.order_id WHERE i.amount>0 AND o.livemode=?""", (mode,)).fetchone()
    changes = dict(db.execute("""SELECT a.kind,sum(a.amount) FROM billing_adjustments a
        JOIN billing_orders o ON o.id=a.order_id WHERE o.livemode=? GROUP BY a.kind""", (mode,)).fetchall())
    refunds = -changes.get("refund", 0)
    disputes = changes.get("dispute", 0)
    return dict(gross_cents=sales[0], paid_count=sales[1], refund_cents=refunds,
                dispute_cents=disputes, adjusted_cents=sales[0] - refunds + disputes)


def _config(key):
    """Flask pode injetar valores de teste. Em produção usamos o ambiente."""
    value = current_app.config.get(key) if has_app_context() else None
    return value or os.environ.get(key, "")


def _live_mode():
    """Vendas de teste nunca entram na receita nem liberam estudos em produção."""
    return _config("STRIPE_SECRET_KEY").startswith(("sk_live_", "rk_live_"))


def _origin():
    """Usa uma origem configurada pelo dono ao montar os retornos do checkout."""
    origin = _config("PUBLIC_BASE_URL").rstrip("/")
    parts = urlsplit(origin)
    local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not parts.hostname or parts.username or parts.password or parts.path or
            parts.query or parts.fragment or
            (parts.scheme != "https" and not (local and parts.scheme == "http"))):
        return ""
    return origin


def _configured():
    return bool(_config("STRIPE_SECRET_KEY") and _config("STRIPE_WEBHOOK_SECRET") and _origin())


class StripeUnavailable(Exception):
    """Mensagem interna sem corpo da resposta, credenciais ou dados do cartão."""


def _stripe(path, fields=None, *, idem=None, version="2024-06-20"):
    """Faz uma chamada HTTPS à Stripe. GET consulta e POST cria ou altera.

    O checkout mantém uma versão fixa. A consulta Invoice Payments usa a
    versão Basil, na qual a Stripe passou a oferecer esse recurso.
    Idempotency-Key permite repetir uma criação cujo retorno se perdeu.
    """
    headers = {"Authorization": "Bearer " + _config("STRIPE_SECRET_KEY"),
               "Stripe-Version": version}
    if idem:
        headers["Idempotency-Key"] = idem
    encoded = None if fields is None else urlencode(fields).encode()
    call = Request("https://api.stripe.com/v1/" + path, data=encoded, headers=headers)
    try:
        with urlopen(call, timeout=8) as response:
            return json.load(response)
    except HTTPError as error:
        # Apenas o código HTTP e o ID de suporte entram no log. A página de
        # detalhes da requisição no painel Stripe auxilia a diagnosticar falhas.
        current_app.logger.warning("Stripe recusou %s (HTTP %s, requisição %s)",
                                   path.split("/")[0], error.code, error.headers.get("Request-Id", "sem ID"))
        error.close()
        raise StripeUnavailable from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        current_app.logger.warning("Stripe indisponível em %s (%s)", path.split("/")[0], getattr(error, "code", "rede"))
        raise StripeUnavailable from error


@bp.get("/assinatura")
def offer():
    """Mostra a passagem de origem e a oferta também para leitores sem conta."""
    db = get_db()
    uid = g.user["id"] if g.user else None
    return render_template("billing_offer.html", offer=get_offer(db),
                           verse_id=request.args.get("verse_id", type=int),
                           manageable=_manageable_order(db, uid) if uid else None,
                           ready=_configured(), live=_live_mode())


def _checkout_fields(plan, order_id, now):
    """Monta os detalhes exibidos pelo checkout hospedado.

    O HTML envia apenas a revisão aceita pelo leitor. Preço, periodicidade e
    descrição vêm do banco, impedindo divergência entre a oferta e a cobrança.
    """
    origin = _origin()
    fields = {
        "mode": "subscription", "locale": "pt-BR", "payment_method_types[0]": "card",
        "client_reference_id": order_id, "metadata[order]": order_id,
        "subscription_data[metadata][order]": order_id,
        "success_url": origin + url_for("billing.returned", order=order_id),
        # Somente o identificador aleatório do pedido sai do site. O retorno
        # privado recupera a referência do versículo no banco local.
        "cancel_url": origin + url_for("billing.returned", order=order_id, voltar=1),
        "expires_at": now + 35 * 60,
        "line_items[0][quantity]": 1,
        "line_items[0][price_data][currency]": "brl",
        "line_items[0][price_data][unit_amount]": plan["price_cents"],
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": plan["title"],
        "line_items[0][price_data][product_data][description]": plan["description"],
    }
    if plan["note"]:
        fields["custom_text[submit][message]"] = plan["note"]
    return fields


def _recover_attempt(order):
    """Recupera uma tentativa antiga mesmo se a primeira resposta se perdeu.

    Depois do vencimento não é possível abrir a sessão original novamente.
    Consultamos as sessões daquele intervalo para confirmar que a tentativa
    não foi paga enquanto o servidor estava sem rede.
    """
    if order["session_id"]:
        return _stripe("checkout/sessions/" + order["session_id"])
    fields = {"created[gte]": order["created"] - 300,
              "created[lte]": order["created"] + 35 * 60, "limit": 100}
    while True:
        page = _stripe("checkout/sessions?" + urlencode(fields))
        for item in page["data"]:
            if item.get("client_reference_id") == order["id"]:
                return item
        if not page.get("has_more"):
            return {"id": None, "status": "expired"}
        fields["starting_after"] = page["data"][-1]["id"]


@bp.post("/checkout")
@login_required
def checkout():
    """Cria ou retoma uma única sessão aberta para esta conta."""
    db = get_db()
    uid = g.user["id"]
    verse_id = request.form.get("verse_id", type=int)
    back = url_for("billing.offer", verse_id=verse_id)
    if not _configured():
        flash("As assinaturas ainda estão em preparação.")
        return redirect(back)
    # A transação curta apenas reserva a tentativa. O acesso à rede ocorre
    # depois do commit, permitindo que outros leitores continuem usando o site.
    db.execute("BEGIN IMMEDIATE")
    try:
        plan = get_offer(db)
        if (not _form_matches_stripe_mode() or not plan["published"]
                or request.form.get("revision", type=int) != plan["revision"]):
            db.rollback()
            flash("A oferta mudou. Confira os detalhes antes de continuar.")
            return redirect(back)
        if _manageable_order(db, uid):
            db.rollback()
            flash("Sua conta já possui uma assinatura em acompanhamento.")
            return redirect(back)
        mode = _live_mode()
        order = db.execute("SELECT * FROM billing_orders WHERE user_id=? AND livemode=? AND state='pending'",
                           (uid, mode)).fetchone()
        if not order:
            token = uuid.uuid4().hex
            now = int(time.time())
            payload = _checkout_fields(plan, token, now)
            db.execute("""INSERT INTO billing_orders
                (id,user_id,verse_id,offer_revision,created,payload,livemode) VALUES(?,?,?,?,?,?,?)""",
                       (token, uid, verse_id, plan["revision"], now, json.dumps(payload), mode))
            order = db.execute("SELECT * FROM billing_orders WHERE id=?", (token,)).fetchone()
        db.commit()
    except Exception:
        db.rollback()
        raise
    try:
        # A Stripe exige ao menos 30 minutos para a expiração de uma sessão
        # nova. Passada essa janela de criação, reconciliamos a tentativa
        # original em vez de reenviar parâmetros que ela recusaria.
        if order["session_id"] or int(time.time()) >= order["created"] + 6 * 60:
            checkout_session = _recover_attempt(order)
        else:
            checkout_session = _stripe("checkout/sessions", json.loads(order["payload"]), idem=order["id"])
        # A resposta de uma criação pode ter se perdido. Mesmo que o POST
        # idempotente a recupere agora, jamais devolvemos a URL de outra revisão.
        # Conferimos de novo após a rede: o dono pode ter editado a oferta.
        current_offer = get_offer(db)
        if checkout_session["status"] == "open" and (
                not current_offer["published"] or order["offer_revision"] != current_offer["revision"]):
            checkout_session = _stripe("checkout/sessions/" + checkout_session["id"] + "/expire", {})
    except StripeUnavailable:
        flash("O checkout está temporariamente indisponível. Tente novamente.")
        return redirect(back)
    state = checkout_session["status"]
    # Uma notificação pode ter chegado antes da resposta HTTP da Stripe.
    # Um retorno antigo 'open' nunca volta uma operação concluída a pendente.
    with db:
        db.execute("""UPDATE billing_orders SET session_id=?,
            state=CASE WHEN state='pending' THEN ? ELSE state END,
            customer=coalesce(?,customer), subscription=coalesce(?,subscription) WHERE id=?""",
            (checkout_session["id"], "pending" if state=="open" else state,
             checkout_session.get("customer"), checkout_session.get("subscription"), order["id"]))
        if state == "open":
            # O funil já mostra um checkout iniciado enquanto a Stripe aguarda
            # o cartão. Esta operação tem valor nulo e não entra na receita.
            db.execute("""INSERT OR IGNORE INTO billing_events
                (id,order_id,created,kind,reference) VALUES(?,?,?,?,?)""",
                       ("local/" + checkout_session["id"], order["id"], order["created"],
                        "checkout.started", checkout_session["id"]))
    if state == "open":
        return redirect(checkout_session["url"], code=303)
    if state == "complete":
        return redirect(url_for("billing.returned", order=order["id"]))
    flash("A sessão anterior expirou. Confira o plano para iniciar outra.")
    return redirect(back)


@bp.get("/assinatura/retorno")
@login_required
def returned():
    """O redirecionamento do navegador nunca concede acesso por si só."""
    db = get_db()
    order = db.execute("SELECT * FROM billing_orders WHERE id=? AND user_id=? AND livemode=?",
                       (request.args.get("order"), g.user["id"], _live_mode())).fetchone()
    if not order:
        abort(404)
    if request.args.get("voltar") == "1":
        # Voltar do checkout não cancela uma assinatura nem muda o pedido.
        # Apenas retoma a página que a própria pessoa abriu antes de pagar.
        return redirect(url_for("billing.offer", verse_id=order["verse_id"]))
    paid = has_subscription(db, g.user["id"])
    if paid and order["verse_id"]:
        return redirect(url_for("study.start", verse_id=order["verse_id"]))
    response = make_response(render_template("billing_return.html", paid=paid))
    # O navegador por vezes chega ao retorno alguns segundos antes da fatura.
    # Consultamos apenas o banco local, com tempo de espera limitado. Ao receber
    # o webhook, a próxima consulta leva o leitor ao estudo do mesmo versículo.
    attempt = max(0, request.args.get("tentativa", default=0, type=int))
    if not paid and order["state"] != "expired" and order["sub_status"] not in FINAL_STATUSES and attempt < 12:
        retry = url_for("billing.returned", order=order["id"], tentativa=attempt + 1)
        response.headers["Refresh"] = "3; url=" + retry
    return response


@bp.post("/assinatura/portal")
@login_required
def portal():
    """Abre o portal do titular, inclusive para consultar faturas anteriores."""
    order_id = request.form.get("order")
    orders = portal_orders(get_db(), g.user["id"], order_id)
    if order_id is not None and not orders:
        abort(404)
    if not orders or not _configured():
        return redirect(url_for("billing.offer"))
    # Formulários antigos sem o campo "order" abrem o cadastro mais recente,
    # com preferência pela assinatura vigente. O acesso aos estudos continua
    # dependendo da fatura paga e da vigência, verificadas em has_subscription.
    try:
        fields = {"customer": orders[0]["customer"], "return_url": _origin() + url_for("accounts.profile")}
        # Configurações do portal pertencem ao respectivo ambiente da Stripe.
        # O sandbox pode selecionar sua própria configuração. O portal padrão
        # da conta continua valendo quando o campo não for preenchido.
        if not _live_mode():
            sandbox_portal = _config("STRIPE_TEST_PORTAL_CONFIGURATION").strip()
            if sandbox_portal:
                fields["configuration"] = sandbox_portal
        result = _stripe("billing_portal/sessions", fields)
    except StripeUnavailable:
        flash("O portal de cobrança está indisponível. Tente novamente.")
        return redirect(url_for("accounts.profile"))
    return redirect(result["url"], code=303)


def _signed_event():
    """Confere o HMAC dos bytes originais antes de interpretar o JSON.

    A Stripe pode enviar duas assinaturas v1 durante a troca de segredo. O
    relógio da máquina precisa estar correto para a tolerância de 5 minutos.
    """
    secret = _config("STRIPE_WEBHOOK_SECRET")
    if not secret:
        abort(503)
    raw = request.get_data()
    parts = [item.strip().split("=", 1) for item in request.headers.get("Stripe-Signature", "").split(",")]
    stamps = [part[1] for part in parts if len(part)==2 and part[0]=="t"]
    signatures = [part[1] for part in parts if len(part)==2 and part[0]=="v1"]
    if (len(stamps) != 1 or not re.fullmatch(r"[0-9]{1,12}", stamps[0])
            or abs(time.time() - int(stamps[0])) > 300):
        abort(400)
    digest = hmac.new(secret.encode(), stamps[0].encode() + b"." + raw, hashlib.sha256).hexdigest()
    if not any(re.fullmatch("[0-9a-f]{64}", sig) and hmac.compare_digest(sig, digest)
               for sig in signatures):
        abort(400)
    try:
        event = json.loads(raw)
        obj = event["data"]["object"]
        if not (isinstance(obj, dict) and isinstance(event["id"], str)
                and isinstance(event["type"], str) and isinstance(event["created"], int)):
            abort(400)
    except (KeyError, TypeError, ValueError, UnicodeError):
        abort(400)
    # Testes de pagamento e faturamento real usam bancos/ambientes separados.
    if event.get("livemode") is not _live_mode():
        abort(400)
    return event


def _id(obj):
    """Campos expandíveis da Stripe podem chegar como ID ou como objeto."""
    return obj.get("id") if isinstance(obj, dict) else obj


def _invoice_subscription(invoice):
    """Os webhooks anteriores a Basil guardam estes dados em outro nível."""
    details = (invoice.get("parent") or {}).get("subscription_details") or invoice.get("subscription_details") or {}
    return _id(details.get("subscription") or invoice.get("subscription")), (details.get("metadata") or {}).get("order")


def _order_for_invoice(db, invoice):
    """Vincula uma fatura ao pedido tanto no formato antigo quanto no novo."""
    sub, token = _invoice_subscription(invoice)
    if not sub:
        return None
    order = db.execute("SELECT * FROM billing_orders WHERE (subscription=? OR id=?) AND livemode=?",
                       (sub, token, _live_mode())).fetchone()
    if not order and not token:
        # A fatura pode ser a primeira notificação entregue. Os metadados
        # inseridos no checkout permitem reconhecê-la mesmo nessa situação.
        subscription = _stripe("subscriptions/" + sub)
        token = (subscription.get("metadata") or {}).get("order")
        order = db.execute("SELECT * FROM billing_orders WHERE id=? AND livemode=?",
                           (token, _live_mode())).fetchone()
    return order


def _order_for_charge(db, charge_id, payment_intent=None):
    """Localiza o pedido de um reembolso ou contestação.

    O ajuste pode chegar antes da confirmação da fatura. Nesse caso a consulta
    ao provedor encontra a referência que a fatura usará posteriormente.
    """
    if not charge_id:
        return None, None
    saved = db.execute("""SELECT i.invoice,i.order_id FROM billing_invoices i JOIN billing_orders o
        ON o.id=i.order_id WHERE i.charge=? AND o.livemode=?""", (charge_id, _live_mode())).fetchone()
    if saved:
        return saved["order_id"], saved["invoice"]
    charge = _stripe("charges/" + charge_id)
    invoice_id = _id(charge.get("invoice"))
    if not invoice_id:
        # A partir de Basil, Charge.invoice saiu da API. O vínculo oficial
        # informa a qual fatura o PaymentIntent do cartão foi creditado.
        intent = _id(payment_intent) or _id(charge.get("payment_intent"))
        if intent:
            params = {"payment[type]":"payment_intent", "payment[payment_intent]":intent,
                      "status":"paid", "limit":100}
            page = _stripe("invoice_payments?" + urlencode(params), version="2025-03-31.basil")
            invoices = {_id(item["invoice"]) for item in page["data"] if item.get("invoice")}
            # Uma cobrança mensal deste checkout é alocada a uma única fatura.
            # Se o provedor não confirmar uma alocação única, pedimos reentrega
            # em vez de aplicar um ajuste arbitrário à conta de um leitor.
            if page.get("has_more") or len(invoices) > 1:
                raise StripeUnavailable
            invoice_id = next(iter(invoices), None)
    if not invoice_id:
        return None, None
    invoice = _stripe("invoices/" + invoice_id)
    order = _order_for_invoice(db, invoice)
    return (order["id"], invoice_id) if order else (None, None)


def _subscription_period(invoice):
    """Usa o fim da linha recorrente, não a data de emissão da fatura."""
    lines = (invoice.get("lines") or {}).get("data", [])
    periods = [line["period"]["end"] for line in lines if
               (line.get("type") == "subscription" or
                (line.get("parent") or {}).get("type") == "subscription_item_details")
               and (line.get("period") or {}).get("end")]
    return max(periods, default=0)


SUBSCRIPTION_EVENTS = {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}
INVOICE_EVENTS = {"invoice.paid", "invoice.payment_failed"}
ADJUSTMENT_EVENTS = {"refund.created", "refund.updated", "refund.failed",
                     "charge.dispute.funds_withdrawn", "charge.dispute.funds_reinstated"}
FINAL_STATUSES = {"canceled", "incomplete_expired"}


def _prepare_event(db, event):
    """Faz consultas externas antes de obter o bloqueio de escrita do SQLite.

    Uma conciliação que espera a rede deixa as outras páginas e os demais
    pagamentos livres para gravar no banco enquanto a resposta não chega.
    """
    kind = event["type"]
    obj = event["data"]["object"]
    if kind in INVOICE_EVENTS and obj.get("currency") == "brl":
        order = _order_for_invoice(db, obj)
        charge = _id(obj.get("charge"))
        # O evento assinado invoice.paid contém valor e período suficientes
        # para liberar o estudo. Eventos modernos podem omitir charge: em caso
        # de estorno, o conciliador localiza o vínculo pelo Invoice Payment.
        return {"order": order, "charge": charge}
    if kind in ADJUSTMENT_EVENTS and obj.get("currency") == "brl":
        order_id, invoice = _order_for_charge(db, _id(obj.get("charge")), obj.get("payment_intent"))
        return {"order_id": order_id, "invoice": invoice}
    if kind in SUBSCRIPTION_EVENTS and obj["status"] not in FINAL_STATUSES:
        row = db.execute("SELECT sub_status,status_time FROM billing_orders WHERE subscription=? AND livemode=?",
                         (obj["id"], _live_mode())).fetchone()
        if (row and row["status_time"] == event["created"] and row["sub_status"] not in FINAL_STATUSES
                and row["sub_status"] != obj["status"]):
            # Os timestamps têm resolução de segundos. Com duas mudanças no
            # mesmo segundo, a Stripe informa o estado mais recente do objeto.
            return {"status": _stripe("subscriptions/" + obj["id"])["status"]}
    return {}


def _record_adjustment(db, key, order_id, invoice, category, amount, stamp, status):
    """Substitui a fotografia de um movimento sem somá-lo em duplicidade."""
    db.execute("""INSERT INTO billing_adjustments
        (id,order_id,invoice,kind,amount,event_time,status) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET amount=excluded.amount,event_time=excluded.event_time,
        status=excluded.status WHERE excluded.event_time>=billing_adjustments.event_time
        AND NOT (billing_adjustments.status IN ('succeeded','failed','canceled')
        AND excluded.status IN ('pending','requires_action'))
        AND NOT (billing_adjustments.status IN ('failed','canceled')
        AND excluded.status='succeeded')""",
               (key, order_id, invoice, category, amount, stamp, status or ""))


def _process_event(db, event, prepared):
    """Aplica uma notificação a uma operação deste site, dentro de transação.

    Uma mesma conta Stripe pode ter outros produtos. Aceitamos somente pedidos
    que começaram neste banco. Cada fatura e cada estorno têm chaves próprias.
    """
    kind = event["type"]
    obj = event["data"]["object"]
    reference = obj["id"]
    stamp = event["created"]
    amount = None
    order_id = None
    if kind in ("checkout.session.completed", "checkout.session.expired"):
        token = (obj.get("metadata") or {}).get("order")
        order = db.execute("SELECT id FROM billing_orders WHERE id=? AND livemode=?",
                           (token, _live_mode())).fetchone()
        if order:
            order_id = order["id"]
            state = "complete" if kind.endswith(".completed") else "expired"
            db.execute("""UPDATE billing_orders SET state=CASE WHEN state='complete'
                    THEN state ELSE ? END,session_id=?,customer=coalesce(?,customer),
                    subscription=coalesce(?,subscription) WHERE id=?""",
                       (state, reference, _id(obj.get("customer")), _id(obj.get("subscription")), order_id))
    elif kind in SUBSCRIPTION_EVENTS:
        token = (obj.get("metadata") or {}).get("order")
        order = db.execute("SELECT * FROM billing_orders WHERE (id=? OR subscription=?) AND livemode=?",
                           (token, reference, _live_mode())).fetchone()
        if order:
            order_id = order["id"]
            status = obj["status"]
            if (order["status_time"] == stamp and order["sub_status"] not in FINAL_STATUSES
                    and status not in FINAL_STATUSES and status != order["sub_status"]):
                # Se outro processo gravou a mudança durante _prepare_event,
                # a próxima entrega já conseguirá consultar o estado atualizado.
                if "status" not in prepared:
                    raise StripeUnavailable
                status = prepared["status"]
            # canceled e incomplete_expired são terminais na Stripe. Uma
            # entrega atrasada nunca pode reativar esta assinatura.
            db.execute("""UPDATE billing_orders SET subscription=?,customer=?,sub_status=?,
                status_time=?,state=CASE WHEN ?='incomplete' THEN state
                     WHEN ?='incomplete_expired' THEN 'expired' ELSE 'complete' END
                WHERE id=? AND status_time<=?
                AND coalesce(sub_status,'') NOT IN ('canceled','incomplete_expired')""",
                       (reference, _id(obj["customer"]), status, stamp, status, status, order_id, stamp))
    elif kind in INVOICE_EVENTS and obj.get("currency")=="brl":
        order = prepared["order"]
        if order:
            order_id = order["id"]
            sub, _ = _invoice_subscription(obj)
            # Uma primeira cobrança recusada ainda pode ser corrigida na mesma
            # sessão. Somente a aprovação conclui o pedido por esta via.
            db.execute("""UPDATE billing_orders SET subscription=?, customer=coalesce(customer,?),
                state=CASE WHEN ?='invoice.paid' THEN 'complete' ELSE state END WHERE id=?""",
                       (sub, _id(obj.get("customer")), kind, order_id))
            if kind == "invoice.paid":
                amount = obj["amount_paid"]
                db.execute("""INSERT OR IGNORE INTO billing_invoices
                    (invoice,order_id,charge,amount,period_end) VALUES(?,?,?,?,?)""",
                           (reference, order_id, prepared["charge"], amount, _subscription_period(obj)))
    elif kind in ADJUSTMENT_EVENTS and obj.get("currency")=="brl":
        order_id, invoice = prepared["order_id"], prepared["invoice"]
        if order_id:
            if kind.startswith("refund."):
                # Um reembolso pendente ou que falhou não reduz a receita.
                status = obj.get("status")
                amount = -obj["amount"] if status=="succeeded" else 0
                key, category = reference, "refund"
            else:
                # Cada movimento de contestação tem sua própria direção.
                # Valores positivos representam a devolução dos fundos ao site.
                status = obj.get("status", "")
                withdrawn = kind.endswith("withdrawn")
                movements = obj.get("balance_transactions") or []
                # A restituição pode ser parcial. O extrato da contestação
                # identifica cada movimento e separa o principal das tarifas.
                # Usamos o grupo de movimentos na direção indicada pelo evento.
                transfers = [tx["amount"] for tx in movements if isinstance(tx, dict)
                             and tx.get("currency") == "brl" and tx.get("amount")]
                debit = sum(cents for cents in transfers if cents < 0)
                credit = sum(cents for cents in transfers if cents > 0)
                amount = (debit or -obj["amount"]) if withdrawn else (credit or obj["amount"])
                if not withdrawn:
                    # Se a devolução dos fundos chega antes do débito, os dois
                    # movimentos aparecem juntos no extrato da contestação.
                    # Sem o extrato, aguardamos a notificação de débito.
                    debit_key = "charge.dispute.funds_withdrawn/" + reference
                    if debit:
                        _record_adjustment(db, debit_key, order_id, invoice, "dispute", debit, stamp, status)
                    elif not db.execute("SELECT 1 FROM billing_adjustments WHERE id=?", (debit_key,)).fetchone():
                        raise StripeUnavailable
                key, category = kind + "/" + reference, "dispute"
            _record_adjustment(db, key, order_id, invoice, category, amount, stamp, status)
    if order_id:
        db.execute("""INSERT INTO billing_events (id,order_id,created,kind,reference,amount)
            VALUES(?,?,?,?,?,?)""", (event["id"], order_id, stamp, kind, reference, amount))


@bp.post("/webhooks/stripe")
def webhook():
    """Responde 2xx só depois de persistir a operação ou reconhecer duplicata."""
    event = _signed_event()
    db = get_db()
    try:
        if db.execute("SELECT 1 FROM billing_events WHERE id=?", (event["id"],)).fetchone():
            return "", 204
        prepared = _prepare_event(db, event)
        # Atomicidade importa aqui: uma falha antes do commit permite que a
        # Stripe tente novamente. Duas notificações iguais contam uma só venda.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM billing_events WHERE id=?", (event["id"],)).fetchone():
            _process_event(db, event, prepared)
        db.commit()
    except StripeUnavailable:
        db.rollback()
        return "", 503
    except Exception:
        db.rollback()
        raise
    return "", 204


@bp.get("/admin/checkout")
@bp.post("/admin/checkout")
@admin_required
def admin_checkout():
    """Administra detalhes enviados ao checkout e pagina todas as operações."""
    db = get_db()
    if request.method == "POST":
        if not _form_matches_stripe_mode():
            flash("O ambiente da cobrança mudou. Confira a oferta antes de salvar.")
            return redirect(url_for("billing.admin_checkout"))
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        note = request.form.get("note", "").strip()
        raw = request.form.get("price", "").strip().replace(",", ".")
        publish = request.form.get("published") == "on"
        if not (title and len(title)<=120 and description and len(description)<=500 and len(note)<=400):
            flash("Informe um título de até 120 caracteres, descrição de até 500 e instrução de até 400.")
            return redirect(url_for("billing.admin_checkout"))
        if not re.fullmatch(r"[0-9]{1,5}(\.[0-9]{1,2})?", raw or "0"):
            flash("Use um preço em reais com até duas casas decimais.")
            return redirect(url_for("billing.admin_checkout"))
        whole, _, fraction = (raw or "0").partition(".")
        cents = int(whole)*100 + int(fraction.ljust(2,"0"))
        if publish and cents < 50:
            flash("Para publicar, informe um valor mensal de pelo menos R$ 0,50.")
            return redirect(url_for("billing.admin_checkout"))
        with db:
            updated = db.execute(f"""UPDATE {_offer_table()} SET title=?,description=?,note=?,
                price_cents=?,published=?,revision=revision+1 WHERE id=1 AND revision=?""",
                (title, description, note, cents, publish, request.form.get("revision", type=int)))
        flash("Oferta salva." if updated.rowcount else "A oferta foi alterada em outra aba. Confira antes de salvar.")
        return redirect(url_for("billing.admin_checkout"))
    page = max(1, min(request.args.get("page", 1, type=int), 10000000))
    events = db.execute("""SELECT e.*,o.user_id FROM billing_events e JOIN billing_orders o
        ON e.order_id=o.id WHERE o.livemode=? ORDER BY e.created DESC,e.rowid DESC LIMIT 31 OFFSET ?""",
                        (_live_mode(), (page-1)*30)).fetchall()
    return render_template("admin_billing.html", offer=get_offer(db), ready=_configured(),
                           metrics=billing_metrics(db), events=events[:30], page=page,
                           more=len(events)>30, labels=EVENT_LABELS, live=_live_mode())


# Os nomes do provedor ficam no banco e o painel os apresenta em português.
EVENT_LABELS = {
    "checkout.started": "Checkout iniciado",
    "checkout.session.completed": "Checkout concluído", "checkout.session.expired": "Checkout expirado",
    "customer.subscription.created": "Assinatura criada", "customer.subscription.updated": "Assinatura atualizada",
    "customer.subscription.deleted": "Assinatura encerrada", "invoice.paid": "Fatura paga",
    "invoice.payment_failed": "Pagamento recusado", "refund.created": "Reembolso criado",
    "refund.updated": "Reembolso atualizado", "refund.failed": "Falha no reembolso",
    "charge.dispute.funds_withdrawn": "Contestação debitada",
    "charge.dispute.funds_reinstated": "Contestação revertida",
}
