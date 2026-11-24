# O mesmo servidor Python do computador pode ser hospedado em um contêiner.
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 PORT=5050 APP_DATA_DIR=/var/lib/bibliadebolso
WORKDIR /app

# Instalar as dependências primeiro permite reaproveitar essa etapa ao mudar o HTML.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# O usuário do processo só precisa escrever no volume privado da aplicação.
RUN useradd --system --uid 10001 --create-home leitor && mkdir -p /var/lib/bibliadebolso && chown leitor /var/lib/bibliadebolso
USER leitor
VOLUME ["/var/lib/bibliadebolso"]
EXPOSE 5050
CMD ["python", "app.py"]
