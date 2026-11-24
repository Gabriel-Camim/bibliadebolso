"""A parte da interface sqlite3 usada pelo site, sobre libSQL/Hrana 3.

O servidor mantém a conexão SQL de uma transação e devolve um identificador
rotativo (baton) a cada resposta. Nunca reconstruímos a transação no cliente.
Referência: github.com/tursodatabase/libsql/blob/main/docs/HRANA_3_SPEC.md
"""
import base64
import re
import sqlite3
from urllib.parse import urlsplit, urlunsplit

import requests


def _base_url(value, original=None):
    """Normaliza a URL e restringe os destinos que podem receber o bearer token."""
    parts = urlsplit(value)
    scheme = "https" if original is None and parts.scheme == "libsql" else parts.scheme
    if (scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("O banco remoto exige uma URL HTTPS ou libsql válida.")
    if original:
        initial = urlsplit(original)
        # O Turso pode redirecionar um stream a um nó da mesma infraestrutura.
        same_service = (initial.hostname.endswith(".turso.io") and parts.hostname.endswith(".turso.io")
                        and parts.port in (None, 443) and initial.port in (None, 443))
        if parts.netloc.lower() != initial.netloc.lower() and not same_service:
            raise ValueError("Destino de continuação do banco inesperado.")
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _pack(value):
    """O protocolo usa strings decimais para inteiros de 64 bits, inclusive IDs."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise OverflowError("O inteiro excede o intervalo SQLite.")
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    raise sqlite3.ProgrammingError("Tipo de parâmetro SQL não suportado.")


def _unpack(cell):
    kind = cell["type"]
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind in ("float", "text"):
        return cell["value"]
    if kind == "blob":
        raw = cell["base64"]
        return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    raise ValueError("Tipo de coluna desconhecido.")


def _execute(sql, params=()):
    statement = {"sql": sql}
    if isinstance(params, dict):
        statement["named_args"] = [{"name": name, "value": _pack(value)} for name, value in params.items()]
    elif params:
        statement["args"] = [_pack(value) for value in params]
    return {"type": "execute", "stmt": statement}


def _sql_error(error):
    """Preserva o tipo e o código estendido usado no cadastro concorrente."""
    code = error.get("code") or "SQLITE_ERROR"
    message = error.get("message") or "Operação de banco recusada."
    if code == "SQLITE_CONSTRAINT" and "UNIQUE constraint failed" in message:
        code = "SQLITE_CONSTRAINT_UNIQUE"
    cls = sqlite3.IntegrityError if code.startswith("SQLITE_CONSTRAINT") else sqlite3.OperationalError
    exc = cls(message)
    exc.sqlite_errorname = code
    exc.sqlite_errorcode = getattr(sqlite3, code, sqlite3.SQLITE_ERROR)
    return exc


class Row(tuple):
    """Como sqlite3.Row, itera pelos valores e aceita índices, nomes e dict(row)."""
    def __new__(cls, values, columns):
        row = super().__new__(cls, values)
        row.columns = columns
        return row

    def keys(self):
        return list(self.columns)

    def __getitem__(self, key):
        if isinstance(key, str):
            for index, name in enumerate(self.columns):
                if name == key or (name.isascii() and key.isascii() and name.lower() == key.lower()):
                    return super().__getitem__(index)
            raise IndexError("Nome de coluna desconhecido.")
        return super().__getitem__(key)


class Cursor:
    """O HTTP já trouxe as linhas; fetchone/fetchall e o iterador dividem a posição."""
    def __init__(self, result=None, factory=sqlite3.Row, write=False):
        result = result or {}
        columns = tuple(col["name"] for col in result.get("cols", []))
        self.description = tuple((name, None, None, None, None, None, None) for name in columns) or None
        self.rowcount = int(result.get("affected_row_count", 0)) if write else -1
        last_id = result.get("last_insert_rowid")
        self.lastrowid = int(last_id) if last_id is not None else None
        self._columns = columns
        self._factory = factory
        self._rows = iter(result.get("rows", []))

    def __iter__(self):
        return self

    def __next__(self):
        values = tuple(_unpack(value) for value in next(self._rows))
        if self._factory is None:
            return values
        if self._factory is sqlite3.Row:
            return Row(values, self._columns)
        return self._factory(self, values)

    def fetchone(self):
        return next(self, None)

    def fetchall(self):
        return list(self)


def _verb(sql):
    # Não dividimos o SQL em ponto e vírgula: gatilhos e literais o admitem.
    # Só precisamos da primeira palavra para a abertura implícita de transação.
    match = re.match(r"(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*([a-z]+)", sql, re.I | re.S)
    return match[1].upper() if match else ""


class Connection:
    def __init__(self, url, token, timeout):
        self._origin = _base_url(url)
        if not token:
            raise ValueError("Informe o token do banco remoto.")
        self._base = self._origin
        self._baton = None
        self._replication_index = 0
        self._closed = False
        self._transaction = False
        self._timeout = timeout
        self.row_factory = sqlite3.Row
        self._http = requests.Session()
        self._http.headers.update({"Authorization": "Bearer " + token})

    @property
    def in_transaction(self):
        return self._transaction

    def _discard(self):
        self._closed = True
        self._transaction = False
        self._baton = None
        self._http.close()

    def _send(self, operations, *, close=False):
        """Uma tentativa apenas: repetir um POST após timeout pode duplicar escritas."""
        if self._closed:
            raise sqlite3.ProgrammingError("A conexão do banco está encerrada.")
        if close:
            operations = [*operations, {"type": "close"}]
        # Pedimos ao nó que espere o último índice observado por esta conexão.
        # Uma leitura depois de um commit em outro stream já enxerga a escrita.
        if self._replication_index:
            for operation in operations:
                if operation["type"] in ("execute", "sequence"):
                    target = operation.get("stmt", operation)
                    target["replication_index"] = str(self._replication_index)
        try:
            result = self._http.post(self._base + "/v3/pipeline",
                                     json={"baton": self._baton, "requests": operations},
                                     timeout=self._timeout, allow_redirects=False)
            if result.status_code != 200:
                raise ValueError("Status do transporte inesperado.")
            payload = result.json()
            replies = payload["results"]
            baton = payload["baton"]
            if len(replies) != len(operations):
                raise ValueError("Resposta parcial do banco.")
            if close and baton is not None:
                raise ValueError("O banco não confirmou o encerramento do stream.")
            if not close and not isinstance(baton, str):
                raise ValueError("O stream do banco expirou.")
            self._baton = baton
            if close:
                self._base = self._origin
                self._transaction = False
            elif payload.get("base_url"):
                self._base = _base_url(payload["base_url"], self._origin)
            for op, item in zip(operations, replies):
                if item["type"] == "error":
                    raise _sql_error(item["error"])
                if item["type"] != "ok" or item["response"]["type"] != op["type"]:
                    raise ValueError("A resposta do banco não corresponde à operação.")
                index = item["response"].get("result", {}).get("replication_index")
                if index is not None:
                    self._replication_index = max(self._replication_index, int(index))
            return [item["response"] for item in replies]
        except (requests.RequestException, KeyError, TypeError, ValueError):
            # Sem a próxima baton, o estado é indeterminado. Nunca continuamos
            # a mesma transação em outro stream. O serviço expira o anterior.
            self._discard()
            raise sqlite3.OperationalError("O banco remoto não confirmou a operação.") from None

    @staticmethod
    def _check_keys(reply):
        """A exclusão de perfis depende de ON DELETE CASCADE em cada novo stream."""
        flag = Cursor(reply["result"], None).fetchone()
        if flag != (1,):
            raise sqlite3.OperationalError("O banco remoto não confirmou as chaves estrangeiras.")

    def _open_checked(self, begin=None):
        # Só enviamos a primeira escrita de negócio após a confirmação deste
        # PRAGMA. O mesmo stream permanece aberto até o commit, rollback ou close.
        setup = [_execute("PRAGMA foreign_keys=ON")]
        if begin:
            setup.append(_execute(begin))
        setup.append(_execute("PRAGMA foreign_keys"))
        try:
            replies = self._send(setup)
            self._check_keys(replies[-1])
        except Exception:
            if self._baton:
                self._send([], close=True)
            raise
        self._transaction = begin is not None

    def execute(self, sql, parameters=()):
        statement = _execute(sql, parameters)
        verb = _verb(sql)
        writes = verb in {"DELETE", "INSERT", "UPDATE", "REPLACE"}
        if verb == "BEGIN":
            if self.in_transaction:
                raise sqlite3.OperationalError("Já existe uma transação aberta.")
            self._open_checked(sql)
            return Cursor()
        if verb in {"COMMIT", "END", "ROLLBACK"}:
            reply = self._send([statement], close=True)[0]
        elif self.in_transaction or writes:
            if not self.in_transaction:
                self._open_checked("BEGIN DEFERRED")
            reply = self._send([statement])[0]
        elif verb in {"SELECT", "PRAGMA", "EXPLAIN"}:
            # Leitura avulsa. Uma única ida ao servidor e nenhum stream parado
            # aguardando a próxima página ou a resposta de outra API.
            replies = self._send(
                [_execute("PRAGMA foreign_keys=ON"), _execute("PRAGMA foreign_keys"), statement],
                close=True,
            )
            self._check_keys(replies[1])
            reply = replies[2]
        else:
            # DDL avulsa tem autocommit, como o sqlite3 padrão.
            self._open_checked()
            reply = self._send([statement], close=True)[0]
        return Cursor(reply["result"], self.row_factory, writes)

    def executemany(self, sql, parameter_sets):
        result = Cursor(write=True)
        for params in parameter_sets:
            result.rowcount += self.execute(sql, params).rowcount
        return result

    def executescript(self, sql):
        """Instala as sequências DDL da aplicação com o parser do próprio libSQL."""
        self.commit()  # A conexão sqlite3 em modo legado também faz este commit.
        self._open_checked()
        try:
            replies = self._send([{"type": "sequence", "sql": sql}, {"type": "get_autocommit"}])
            in_autocommit = replies[1]["is_autocommit"]
            if type(in_autocommit) is not bool:
                raise sqlite3.OperationalError("Estado de transação desconhecido.")
            # O próprio script pode deixar um BEGIN aberto para SQL posterior.
            self._transaction = not in_autocommit
        finally:
            if self._baton and not self.in_transaction:
                self._send([], close=True)
        return Cursor()

    def commit(self):
        if self._closed:
            raise sqlite3.ProgrammingError("A conexão do banco está encerrada.")
        if self.in_transaction:
            self.execute("COMMIT")

    def rollback(self):
        if self.in_transaction:
            self.execute("ROLLBACK")

    def close(self):
        if self._closed:
            return
        try:
            if self._baton:
                # Fechar um stream reverte transações não confirmadas no servidor.
                self._send([], close=True)
        finally:
            self._discard()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.commit()
        else:
            self.rollback()


def connect(url, auth_token, timeout=15):
    """Retorna uma conexão privada por requisição Flask, sem manter réplica no disco."""
    return Connection(url, auth_token, timeout)
