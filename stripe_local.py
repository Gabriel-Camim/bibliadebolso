"""Encaminha eventos da sandbox ao Flask local. Execute em um segundo terminal.

O checkout hospedado consegue abrir no navegador sem este processo. Já o
aviso de pagamento precisa chegar ao computador para liberar o estudo.
"""
import hmac
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from dotenv import load_dotenv


# Seguimos somente os tipos que o site utiliza para conciliar a assinatura.
EVENTS = (
    "checkout.session.completed", "checkout.session.expired",
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    "invoice.paid", "invoice.payment_failed", "refund.created", "refund.updated", "refund.failed",
    "charge.dispute.funds_withdrawn", "charge.dispute.funds_reinstated",
)


def main():
    """Usa a conta do .env e verifica se o Flask reconhecerá a assinatura."""
    load_dotenv(Path(__file__).with_name(".env"))
    key = os.getenv("STRIPE_SECRET_KEY", "")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    base = urlsplit(os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5050"))
    if not key.startswith(("sk_test_", "rk_test_")) or not secret:
        raise SystemExit("Configure a chave de teste e o segredo de webhook no .env.")
    if base.scheme != "http" or base.hostname not in {"127.0.0.1", "localhost"} or not base.port:
        raise SystemExit("Use PUBLIC_BASE_URL com o endereço HTTP e a porta local do Flask.")

    # O pacote npm no Windows também pode instalar o executável em um
    # subdiretório. Preferimos o binário nativo, sem executar o shell.
    binary = shutil.which("stripe.exe" if os.name == "nt" else "stripe")
    if not binary and os.name == "nt":
        candidate = Path(os.getenv("APPDATA", "")) / "npm/node_modules/@stripe/cli/node_modules/@stripe/cli-win32-x64/bin/stripe.exe"
        if candidate.is_file():
            binary = str(candidate)
    if not binary:
        raise SystemExit("Instale a Stripe CLI para encaminhar os eventos de teste.")

    # A chave segue apenas no ambiente do processo. Não a colocamos nos
    # argumentos nem imprimimos o segredo mostrado pela CLI ao conectar.
    environment = {**os.environ, "STRIPE_API_KEY": key}
    command = [binary, "listen", "--events", ",".join(EVENTS),
               "--forward-to", f"http://{base.hostname}:{base.port}/webhooks/stripe"]
    check = subprocess.run([*command, "--print-secret"], env=environment, capture_output=True, timeout=30)
    if check.returncode != 0 or not hmac.compare_digest(check.stdout.strip(), secret.encode()):
        raise SystemExit("A CLI não confirmou o segredo local. Confira a sandbox e STRIPE_WEBHOOK_SECRET.")
    # A CLI reconecta durante interrupções curtas de rede. Só as mudanças
    # de estado e o código HTTP do destino são úteis a quem opera o site.
    with subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as relay:
        try:
            for raw in relay.stdout:
                if b"Ready!" in raw:
                    print(f"Escutando eventos da sandbox na porta {base.port}.", flush=True)
                elif b"POST" in raw and b"/webhooks/stripe" in raw:
                    # A rota usa 204 para reconhecer a entrega sem devolver
                    # um corpo. Todos os códigos 2xx indicam sucesso à Stripe.
                    ok = re.search(rb"\[2\d\d\]", raw)
                    print("Webhook aceito." if ok else "Confira o retorno do webhook.", flush=True)
        except KeyboardInterrupt:
            relay.terminate()
        return relay.wait()


if __name__ == "__main__":
    raise SystemExit(main())
