# Bíblia de Bolso

Aplicação Python para o chaveiro NFC de bibliadebolso.com. A página inicial funciona sem login. Ao estudar um versículo, o site preserva a referência durante a entrada por senha, Google ou Facebook e a contratação da assinatura. Os arquivos Python têm comentários e explicações em português.

## Executar no Windows

Abra o PowerShell na pasta do projeto. Na instalação preparada neste computador, basta executar:

```powershell
.\.venv\Scripts\python.exe app.py
```

Abra **http://127.0.0.1:5050**. O terminal precisa permanecer aberto. Use Ctrl+C para encerrar. Numa nova instalação crie o ambiente e instale as dependências:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use o arquivo `.env.example` como referência para preencher o `.env`. Preserve os valores já configurados. Reinicie o servidor após editar variáveis. As configurações feitas pelo painel valem nas próximas operações, sem reinício.

## Onde cada parte funciona

| Arquivo | O que procurar |
| --- | --- |
| `app.py` | Montagem do site, leitura pelo NFC, sorteio e políticas |
| `core.py` | Conexão SQLite, contadores agregados e proteção dos formulários |
| `corpus.py` | Importação da Bíblia, sorteio e consulta ao índice vetorial |
| `bible_browser.py` | Escolha de livro, capítulo e versículo e destaque da passagem sorteada |
| `bible_jump.py` | Busca do índice por nome, abreviatura bíblica ou número da página |
| `bible_notes.py` | Marca-páginas privados, comentários e cores aplicadas a palavras ou versículos |
| `library.py` | Favoritos por conta, horário dos sorteios e histórico opcional de leitura |
| `accounts.py` | Login, cadastro, aceite dos documentos e autorização do proprietário |
| `study.py` | Referências bíblicas, busca de passagens, histórico e comunicação com a OpenAI |
| `billing.py` | Oferta, assinatura mensal, confirmação e conciliação financeira |
| `stripe_local.py` | Encaminhamento dos avisos da sandbox da Stripe para o computador |
| `templates/` | Conteúdo das páginas em HTML com variáveis do Python |
| `static/` | Folhas de estilo, interações do leitor, fontes e personagens |

## Como o chat encontra e lembra passagens

O banco público `data/biblia.sqlite3` acompanha o projeto. A edição BPM do eBible.org traz 81 livros e 38.058 registros. Desses, 38.029 têm texto para sorteio e consulta. As demais referências continuam no acervo conforme a fonte. O indexador transforma cada passagem legível em um vetor de pesos de palavras (TF-IDF). A recuperação combina referência explícita, vizinhos no capítulo e similaridade de vocabulário. A técnica tem limitações para sinônimos sem palavras em comum. Para reconstruir a partir do [ZIP VPL da BPM](https://ebible.org/Scriptures/porbrbsl_vpl.zip), execute `python corpus.py caminho/para/porbrbsl_vpl.zip`.

A API recebe a pergunta, as passagens e até doze pares recentes de mensagens, limitados a 32 mil caracteres de histórico. O cache chave/valor local guarda uma janela por conversa por 15 minutos e comporta até 128 conversas. Se a entrada expirar, o site remonta o contexto a partir do histórico privado. A configuração da OpenAI usa `gpt-4.1-mini` e `store=false`. O painel altera a temperatura e a mensagem de sistema nas consultas seguintes. O histórico completo pertence à conta e pode ser apagado, inclusive quando a assinatura termina.

O Flask repassa à tela os fragmentos de texto enviados pela OpenAI. Os títulos e destaques recebem formatação durante a escrita e quando a conversa é reaberta. O servidor grava a pergunta e a resposta juntas após a conclusão. O menu **Estudo guiado** retoma a conversa mais recente do leitor. **Estudar com a IA**, dentro da Bíblia, abre um rascunho sobre a passagem selecionada. No celular, o botão **Abrir conversas** mostra o histórico lateral.

Cada conta gratuita pode concluir cinco perguntas de estudo por mês, somando todas as conversas. A franquia renova no primeiro dia do mês, no horário de São Paulo. Falhas e respostas interrompidas antes da conclusão liberam a vaga. Apagar uma conversa preserva a contagem daquele mês. Uma assinatura ativa ou licença manual dá acesso ao estudo além dessa franquia.

Em **Navegar até o versículo**, o leitor abre o capítulo no ponto da passagem. A Bíblia exibe duas páginas no computador e uma no celular. Use as setas, o índice de livros ou um gesto lateral no celular para navegar. Após entrar, cada perfil pode marcar favoritos na própria página e ativar o histórico de leitura na **Biblioteca**. O favorito de um sorteio feito durante aquela sessão mantém o horário original.

### Índice e marca-páginas

O índice **Explorar** fica ao lado do livro no computador. No celular, abra **Menu do site → Índice da Bíblia**. Expanda um livro e um capítulo para escolher o versículo, ou digite, por exemplo, `Salmo 23:1`, `1 Co 13:4` ou `página 120`. A numeração conta as folhas da edição digital inteira e permanece estável ao alterar a fonte ou o tamanho da tela.

Para guardar uma frase, entre na conta, selecione as palavras impressas e use **Marcar seleção**. Para marcar versos inteiros, toque em um versículo, escolha **Anotar** e ajuste o intervalo. O índice **Marca-páginas** agrupa suas anotações por livro e capítulo. Você pode buscar pelo título, texto da anotação ou referência. As marcações ficam no banco privado da aplicação, separadas dos dados enviados ao chat.

## Configurar a entrada e o acesso ao painel

**Usuário e senha.** O cadastro em `/cadastro` funciona sem serviços externos. A pessoa informa e-mail e celular e escolhe um nome de usuário e uma senha de 12 a 128 caracteres. Pode entrar com o nome ou o e-mail e trocar a senha em **Minha conta** usando a senha atual. A troca encerra as demais sessões desse login. O site ainda não envia e-mails de recuperação. Cadastros por métodos diferentes têm perfis separados, mesmo quando o e-mail coincide.

O celular também é solicitado ao concluir o cadastro por provedor externo. Contas anteriores completam esse campo na próxima entrada. O proprietário encontra o celular junto ao nome e e-mail no painel de contas.

**Facebook Login.** Em Meta for Developers, adicione o caso de uso de autenticação com Facebook, configure o domínio e registre o retorno exato `https://bibliadebolso.com/auth/facebook/callback` (ou o endereço HTTPS real do deploy). Informe a URL pública da política de privacidade e use `/privacidade#contato` como página de instruções de exclusão. Configure `FACEBOOK_APP_ID` e `FACEBOOK_APP_SECRET` no ambiente do servidor. Em modo de desenvolvimento, teste com uma pessoa que tenha função no aplicativo Meta. Para liberar o acesso público, conclua as etapas aplicáveis no painel da Meta. [Documentação do fluxo de login](https://developers.facebook.com/documentation/facebook-login/guides/advanced/manual-flow).

**Google e administração.**

1. No Google Cloud, configure o consentimento OAuth e um cliente do tipo **Aplicativo da Web**. Para a configuração local sugerida, cadastre a URI de retorno exata `http://127.0.0.1:5050/auth/google/callback`. Para produção, use `https://bibliadebolso.com/auth/google/callback`.
2. Coloque o identificador em `GOOGLE_CLIENT_ID` e o segredo em `GOOGLE_CLIENT_SECRET`. Em modo de testes do Google, inclua sua conta entre os usuários permitidos.
3. Cadastre-se em `/cadastro`, entre em **Minha conta**, copie o identificador Google exibido ali e configure `OWNER_GOOGLE_SUB`. Após reiniciar, essa identidade terá acesso a `/admin`. O cadastro inicial por si só não concede administração.
4. Em **Configurar agente**, edite a instrução de sistema e a temperatura. Em **Checkout e vendas**, configure e publique a oferta.
5. Em **Contas**, abra a licença da pessoa. Conceda o acesso com uma data final opcional, altere a validade ou revogue a concessão. A licença vale no ambiente selecionado (teste ou produção). Use o checkout com cartão de teste quando quiser exercitar também o registro de uma venda e sua fatura.

## Configurar a assinatura na Stripe

1. Use uma conta Stripe habilitada para o Brasil e comece pelo ambiente de **teste**. O site cobra mensalmente em reais por cartão via Stripe Checkout e Billing.
2. Na sandbox **3D Lobby Print**, copie uma chave de servidor de teste para `STRIPE_SECRET_KEY` no `.env`. Uma chave restrita usa o prefixo `rk_test_`. Ela precisa poder criar, consultar e expirar sessões de Checkout, criar sessões do Portal do Cliente e consultar assinaturas, faturas e cobranças. Para preços definidos na própria sessão, valide também as permissões relativas a produtos e preços na sandbox. Configure o portal padrão dessa mesma conta com **histórico de faturas**, alteração de cartão e cancelamento. O site cria produto e preço na sessão de checkout, a partir da oferta definida pelo proprietário. [Chaves da API](https://docs.stripe.com/keys) · [Portal do Cliente](https://docs.stripe.com/customer-management).
3. No Workbench, crie um destino de **eventos snapshot da conta**, compatível com a versão **2024-06-20**, para `https://bibliadebolso.com/webhooks/stripe`. Configure `STRIPE_WEBHOOK_SECRET` com o segredo desse destino. Para testes locais, encaminhe os mesmos eventos ao endereço `http://127.0.0.1:5050/webhooks/stripe` pela Stripe CLI e use o segredo fornecido pela CLI.
4. Assine estes eventos:

   - `checkout.session.completed`, `checkout.session.expired`
   - `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`
   - `invoice.paid`, `invoice.payment_failed`
   - `refund.created`, `refund.updated`, `refund.failed`
   - `charge.dispute.funds_withdrawn`, `charge.dispute.funds_reinstated`

5. Em `/admin/checkout`, informe nome, descrição, preço mensal em reais e mensagem junto ao botão de pagamento. Marque a publicação quando quiser abrir a oferta. Alterações de preço aplicam-se às novas assinaturas.
6. Faça uma compra de teste e acompanhe o retorno ao versículo. Confira a fatura no painel, teste renovação, estorno e cancelamento. Antes de vender, ative a conta na Stripe e configure chaves e destino de webhook de produção. As receitas de teste ficam separadas.

O faturamento bruto soma as faturas pagas geradas pelo site. A receita ajustada considera os reembolsos e os movimentos de contestação. As taxas do processador, tributos e repasses bancários são conciliados no extrato da Stripe. O histórico lista as operações recebidas por webhook.

### Primeiro teste no computador

Se o `.env` já contém as chaves de teste, mantenha `python app.py` e `python stripe_local.py` em dois terminais enquanto testa. O segundo comando usa a chave da sandbox do site, verifica o segredo de assinatura e entrega os avisos ao Flask. Para manter uma configuração própria do Portal do Cliente em teste, informe o seu ID em `STRIPE_TEST_PORTAL_CONFIGURATION`. A oferta de produção começa em rascunho e tem preço e publicação próprios.

Em um novo PowerShell, execute `stripe login`, conclua a autorização na sandbox correta e confira a conta com `stripe whoami`. Em seguida, mantenha este comando aberto enquanto testa:

```powershell
stripe listen --events checkout.session.completed,checkout.session.expired,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed,refund.created,refund.updated,refund.failed,charge.dispute.funds_withdrawn,charge.dispute.funds_reinstated --forward-to http://127.0.0.1:5050/webhooks/stripe
```

Copie o segredo `whsec_…` mostrado pela CLI para `STRIPE_WEBHOOK_SECRET` no `.env` e reinicie o Python. Abra o checkout pelo próprio site com um usuário de teste. Conclua o pagamento com um [cartão de teste da Stripe](https://docs.stripe.com/testing). Confira que a CLI recebeu uma resposta HTTP 2xx, que o chat abriu no versículo escolhido, que o painel somou uma fatura e que o assinante consegue consultá-la pelo portal. O login do plugin autoriza as ferramentas do chat. O Flask lê as credenciais do ambiente do servidor.

### Plano para vender o chaveiro avulso

O planejador da Stripe indicou Checkout hospedado também para o produto físico. Essa parte é uma proposta de ampliação. Hoje o checkout do site oferece a assinatura mensal. Para a venda do chaveiro, use uma sessão própria com `mode=payment`, preço em BRL definido no servidor e endereço de entrega. Defina preço, frete e destinos atendidos antes de abrir a oferta. Para disponibilizar uma fatura paga da compra avulsa, habilite `invoice_creation.enabled` nessa sessão. A assinatura já gera suas faturas pelo Billing.

Mantenha um pedido físico identificado no banco e marque-o como pronto para envio uma única vez, após confirmar o pagamento no webhook. Se o meio de pagamento tiver confirmação posterior, aguarde o evento de sucesso assíncrono. No painel, concilie cada venda física uma vez, mesmo quando ela também tiver uma fatura. O processo de expedição é independente do acesso ao chat. [Checkout e entrega de pedidos](https://docs.stripe.com/checkout/fulfillment) · [Faturas após o pagamento](https://docs.stripe.com/receipts).

## Privacidade e colocação no ar

O cookie essencial mantém a sessão e protege formulários. O consentimento opcional permite salvar a aparência de leitura no navegador. O painel conta aberturas e sorteios por dia sem associá-los a visitantes. No chat, o consentimento para envio de mensagens ao provedor de IA é específico. O histórico fica vinculado à conta e pode ser excluído.

Para lançar, defina `CONTROLLER_NAME` e `PRIVACY_CONTACT`, revise a política segundo a operação real e estabeleça os prazos dos backups, registros financeiros, atendimento aos titulares e contratos com operadores. As conversas podem revelar convicções religiosas. Inclua a retenção e a localização dos dados na hospedagem e nos fornecedores nessa revisão.

### Publicar a versão de teste gratuita na Render

O `render.yaml` cria um serviço Python no plano Free. O acervo bíblico vai no projeto, em `data/biblia.sqlite3`. O banco privado de contas, notas, conversas e cobranças fica no plano Free do Turso. A Render pode desligar o processo após 15 minutos sem visitas. A primeira abertura pelo NFC depois desse intervalo pode levar cerca de um minuto. Os serviços têm cotas de uso e a OpenAI cobra pelo uso da API. [Render Free](https://render.com/docs/free) · [Turso Free](https://turso.tech/pricing).

1. No Turso, crie um banco libSQL dedicado a este site. Copie a URL e gere um token de leitura e escrita limitado a esse banco. A versão hospedada inicia com as tabelas vazias. O cadastro e os dados de desenvolvimento continuam no computador. [Painel Turso](https://app.turso.tech/).
2. No painel da Render, selecione o repositório com os arquivos do projeto e o plano **Free**. Se usar **New → Blueprint**, a Render lê o `render.yaml` e gera a chave de sessão. Se já abriu **New → Web Service**, preencha os campos da tabela abaixo na mesma tela e defina uma `SECRET_KEY` aleatória e estável em **Environment**. O deploy pelo painel usa sua sessão de login da Render. [Painel da Render](https://dashboard.render.com/).
3. Em **Environment**, selecione `DATABASE_BACKEND=turso` e preencha `TURSO_DATABASE_URL` e `TURSO_AUTH_TOKEN`. Se importou o `.env` local, remova `PUBLIC_BASE_URL` para usar o endereço HTTPS atribuído pela Render. Configure o proprietário, o canal de privacidade e as credenciais do Google e da OpenAI. No Google Cloud, autorize a URL HTTPS do serviço seguida de `/auth/google/callback`. Pode deixar `FACEBOOK_APP_ID` e `FACEBOOK_APP_SECRET` em branco até liberar o Facebook Login na Meta. Depois de ativá-lo, autorize a URL do serviço seguida de `/auth/facebook/callback`. Os segredos pertencem às variáveis de ambiente e o banco privado pertence ao Turso.
4. Para testar pagamentos, use a **sandbox da Stripe**. Cadastre um destino de eventos HTTPS em `/webhooks/stripe`, selecione os eventos listados na seção de testes do checkout e use o segredo de assinatura gerado para esse destino. No painel do site, publique a oferta de teste de R$ 29,90 por mês.
5. Confira `/saude`, crie uma conta de teste e salve uma passagem. Após reiniciar o serviço, entre novamente e abra a passagem salva. Ao conectar `bibliadebolso.com` nas configurações da Render e do DNS, atualize `PUBLIC_BASE_URL`, os retornos dos provedores e o endereço do webhook para o domínio definitivo. Grave **https://bibliadebolso.com/** na tag depois de confirmar a leitura pelo domínio.

Campos da tela **New Web Service** para o repositório com `app.py` na raiz:

| Campo | Valor |
| --- | --- |
| Language | Python 3 |
| Branch | `main` quando essa branch contém o projeto |
| Root Directory | Deixe vazio |
| Build Command | `python -m pip install -r requirements.txt` |
| Start Command | `python app.py` |
| Compute | Free |
| Health Check Path | `/saude` |

O servidor escuta na porta fornecida pela Render em `0.0.0.0`. A URL HTTPS pública é usada para os cookies, login e retorno de pagamento. Escolha uma região da Render próxima do seu banco Turso. [Serviços Web na Render](https://render.com/docs/web-services).

O desenvolvimento local continua a usar SQLite. O Dockerfile também permite hospedar o aplicativo por conta própria com `DATABASE_BACKEND=sqlite`, um volume persistente em `/var/lib/bibliadebolso` e uma `SECRET_KEY` estável. A pasta `static/` é pública. As credenciais, backups e dados de usuários são privados.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Os testes de integração usam bancos temporários e simulam os provedores externos para permitir repetição sem cobranças.
