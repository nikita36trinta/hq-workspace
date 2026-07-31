"""Аккаунты, пароли и сессии NutriPlan.

Почему SQLite, а не json-файлы, как всё остальное в проекте
-----------------------------------------------------------
Остальные хранилища читают-меняют-пишут файл целиком. На этом уже сгорели
counters.json (бился при параллельных заходах) и идемпотентность платежей
(битый файл молча возвращал пустой словарь, и каждый вебхук снова считался
первым). Сессии — самая горячая по записи сущность из всех, что появятся в
продукте: запись на каждый вход и на каждое продление. Класть их в json —
значит гарантированно повторить ту же историю, только уже с доступом к
оплаченному.

SQLite лежит в stdlib, живёт одним файлом в том же DATA_DIR и переживает
пересборку контейнера. Процесс один (uvicorn без --workers), крон тикает
HTTP-запросом в тот же процесс — то есть писатель ровно один, и WAL здесь
не компромисс, а точное попадание.

Чего тут намеренно НЕТ
----------------------
Роли, права, смена почты, вход через соцсети, двухфакторка. Ничего этого в
продукте нет и не планируется; каждая такая заготовка — код, который никто
не проверяет и который однажды выстрелит.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── параметры, которые может понадобиться крутить в бою ────────────────────
SESSION_DAYS = int(os.getenv("NUTRI_SESSION_DAYS", "180"))
COOKIE_NAME = "np_sid"
CODE_TTL_SEC = 600          # 10 минут на ввод кода из письма
CODE_MAX_ATTEMPTS = 5       # столько попыток на один код, потом он сгорает
CODE_RESEND_SEC = 60        # пауза между письмами на один адрес
PW_MIN = 8
PW_MAX = 128                # длиннее не нужно, а scrypt на мегабайте — это DoS

# Лимиты на восстановление. Пороги подобраны так, чтобы живой человек в них не
# упирался: три попытки в час — это «не пришло, ещё раз, ещё раз».
FORGOT_PER_EMAIL_HOUR = 3
FORGOT_PER_EMAIL_DAY = 10
FORGOT_PER_IP_HOUR = 15
FORGOT_GLOBAL_DAY = int(os.getenv("NUTRI_FORGOT_MAIL_DAILY_CAP", "300"))

# Подбор пароля. Блокируем ФОРМУ ПАРОЛЯ, но не вход кодом из письма — иначе
# любой желающий заблокирует чужой аккаунт, отправив пять неверных паролей.
PW_TRIES = 5
PW_TRIES_WINDOW = 900       # 15 минут

# scrypt из stdlib: ноль зависимостей, ноль вопросов про доступность из РФ.
# n=2**14 — это 16 МиБ памяти на один хеш (~25 мс). Не 2**15 сознательно:
# контейнер прода ограничен по памяти, а десяток одновременных проверок на
# 2**15 дал бы 320 МиБ пика. Стойкость к перебору всё равно кратно выше pbkdf2.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32, "maxmem": 64 * 1024 * 1024}
_PW_SEMA = threading.Semaphore(4)   # не больше четырёх хешей одновременно

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    e = norm_email(email)
    return bool(e) and len(e) <= 254 and bool(_EMAIL_RE.match(e))


# ── хранилище ──────────────────────────────────────────────────────────────
_DB_LOCK = threading.Lock()
_conns: dict[int, sqlite3.Connection] = {}


class Auth:
    """Всё состояние входа. Экземпляр создаётся один раз в app.py."""

    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    # соединение на поток: sqlite3 не любит, когда одно делят между потоками,
    # а sync-роуты FastAPI живут в пуле на 40 потоков
    def _conn(self) -> sqlite3.Connection:
        key = threading.get_ident()
        c = _conns.get(key)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA foreign_keys=ON")
            _conns[key] = c
        return c

    @contextmanager
    def _tx(self):
        c = self._conn()
        with _DB_LOCK:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def _init(self) -> None:
        # Без _tx: executescript сам коммитит начатую транзакцию, и выход из
        # контекста падает на «no transaction is active». DDL идемпотентен
        # (IF NOT EXISTS), так что своя транзакция ему и не нужна.
        with _DB_LOCK:
            self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
              id         INTEGER PRIMARY KEY,
              email      TEXT NOT NULL UNIQUE COLLATE NOCASE,
              created    TEXT NOT NULL,
              pw_hash    TEXT,          -- NULL = пароль ещё не задан
              pw_salt    TEXT,
              pw_algo    TEXT,
              pw_updated TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions(
              sid        TEXT PRIMARY KEY,
              account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
              created    TEXT NOT NULL,
              last_seen  TEXT NOT NULL,
              expires    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_sessions_acc ON sessions(account_id);
            -- Код живёт по адресу, а не по аккаунту: письмо уходит только тем,
            -- у кого аккаунт есть, но сам код удобнее искать по тому, что ввёл
            -- человек.
            CREATE TABLE IF NOT EXISTS codes(
              email      TEXT PRIMARY KEY COLLATE NOCASE,
              code_hash  TEXT NOT NULL,
              purpose    TEXT NOT NULL,
              created    TEXT NOT NULL,
              expires    TEXT NOT NULL,
              attempts   INTEGER NOT NULL DEFAULT 0,
              sent_at    TEXT NOT NULL
            );
            -- Лимиты в базе, а не в памяти процесса: память обнуляется каждым
            -- деплоем, то есть лимит обходится ожиданием выкладки.
            CREATE TABLE IF NOT EXISTS hits(
              bucket TEXT NOT NULL,
              key    TEXT NOT NULL,
              ts     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_hits ON hits(bucket, key, ts);
            """)

    # ── лимиты ─────────────────────────────────────────────────────────────
    def hit_count(self, bucket: str, key: str, window_sec: int) -> int:
        since = _iso(_now() - timedelta(seconds=window_sec))
        r = self._conn().execute(
            "SELECT COUNT(*) n FROM hits WHERE bucket=? AND key=? AND ts>=?",
            (bucket, key, since)).fetchone()
        return int(r["n"])

    def hit_add(self, bucket: str, key: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO hits(bucket,key,ts) VALUES(?,?,?)",
                      (bucket, key, _iso(_now())))
            # чистим на записи, чтобы не заводить отдельную уборку
            c.execute("DELETE FROM hits WHERE ts < ?", (_iso(_now() - timedelta(days=2)),))

    def rate_ok(self, bucket: str, key: str, limit: int, window_sec: int) -> bool:
        """True — можно. Счётчик увеличивается ТОЛЬКО при разрешении: иначе
        поток отказов сам себя удерживает в заблокированном состоянии."""
        if self.hit_count(bucket, key, window_sec) >= limit:
            return False
        self.hit_add(bucket, key)
        return True

    # ── аккаунты ───────────────────────────────────────────────────────────
    def account(self, email: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM accounts WHERE email=?", (norm_email(email),)).fetchone()

    def account_by_id(self, aid: int) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()

    def ensure_account(self, email: str) -> int:
        """Завести аккаунт, если его нет. Зовётся в момент оплаты — человек
        ничего для этого не делает и никакого экрана не видит."""
        e = norm_email(email)
        if not e:
            raise ValueError("пустой email")
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO accounts(email,created) VALUES(?,?)", (e, _iso(_now())))
        r = self.account(e)
        return int(r["id"])

    def has_password(self, email: str) -> bool:
        r = self.account(email)
        return bool(r and r["pw_hash"])

    # ── пароли ─────────────────────────────────────────────────────────────
    @staticmethod
    def password_problem(pw: str, email: str = "") -> str:
        """Пустая строка = пароль годится. Иначе — что сказать человеку.

        Требований к составу намеренно нет (NIST SP 800-63B): «обязательно
        цифра и спецсимвол» даёт Parol123! у всех подряд, а не стойкость.
        Проверяем длину и очевидную угадываемость."""
        pw = pw or ""
        if len(pw) < PW_MIN:
            return f"Пароль короче {PW_MIN} символов"
        if len(pw) > PW_MAX:
            return f"Пароль длиннее {PW_MAX} символов"
        low = pw.lower()
        local = norm_email(email).split("@")[0]
        if local and len(local) >= 4 and local in low:
            return "Пароль не должен повторять адрес почты"
        if low in _COMMON:
            return "Такой пароль слишком часто используют — придумай другой"
        if len(set(pw)) <= 2:
            return "Слишком простой пароль"
        return ""

    @staticmethod
    def _hash(pw: str, salt: bytes) -> str:
        with _PW_SEMA:
            return hashlib.scrypt(pw.encode("utf-8"), salt=salt, **_SCRYPT).hex()

    def set_password(self, email: str, pw: str) -> None:
        salt = secrets.token_bytes(16)
        h = self._hash(pw, salt)
        with self._tx() as c:
            c.execute(
                "UPDATE accounts SET pw_hash=?,pw_salt=?,pw_algo=?,pw_updated=? WHERE email=?",
                (h, salt.hex(), "scrypt-n14", _iso(_now()), norm_email(email)))

    def check_password(self, email: str, pw: str) -> bool:
        r = self.account(email)
        if not r or not r["pw_hash"] or not r["pw_salt"]:
            # Считаем впустую, чтобы по времени ответа нельзя было отличить
            # «нет такого аккаунта» от «неверный пароль».
            self._hash(pw or "x", b"\x00" * 16)
            return False
        h = self._hash(pw or "", bytes.fromhex(r["pw_salt"]))
        return hmac.compare_digest(h, r["pw_hash"])

    # ── коды из письма ─────────────────────────────────────────────────────
    @staticmethod
    def _code_hash(email: str, code: str) -> str:
        return hashlib.sha256(f"{norm_email(email)}|{code}".encode()).hexdigest()

    def issue_code(self, email: str, purpose: str) -> tuple[str, int]:
        """Выдать код. Возвращает (код или '', сколько секунд ждать до повтора).

        Если живой код уже есть — НЕ выдаём новый, а просим подождать. Иначе
        кнопка «отправить ещё раз» превращается в усилитель: один нажимающий —
        сколько угодно писем.
        """
        e = norm_email(email)
        now = _now()
        r = self._conn().execute("SELECT * FROM codes WHERE email=?", (e,)).fetchone()
        if r:
            try:
                sent = datetime.fromisoformat(r["sent_at"])
                exp = datetime.fromisoformat(r["expires"])
            except ValueError:
                sent, exp = now - timedelta(days=1), now - timedelta(days=1)
            if exp > now:
                wait = int((sent + timedelta(seconds=CODE_RESEND_SEC) - now).total_seconds())
                if wait > 0:
                    return "", wait
        code = f"{secrets.randbelow(10 ** 6):06d}"
        with self._tx() as c:
            c.execute(
                "INSERT INTO codes(email,code_hash,purpose,created,expires,attempts,sent_at) "
                "VALUES(?,?,?,?,?,0,?) ON CONFLICT(email) DO UPDATE SET "
                "code_hash=excluded.code_hash,purpose=excluded.purpose,created=excluded.created,"
                "expires=excluded.expires,attempts=0,sent_at=excluded.sent_at",
                (e, self._code_hash(e, code), purpose, _iso(now),
                 _iso(now + timedelta(seconds=CODE_TTL_SEC)), _iso(now)))
        return code, 0

    def check_code(self, email: str, code: str, purpose: str) -> str:
        """Пустая строка = код верный (и погашен). Иначе — что показать."""
        e = norm_email(email)
        r = self._conn().execute("SELECT * FROM codes WHERE email=?", (e,)).fetchone()
        if not r or r["purpose"] != purpose:
            return "Код не найден или устарел — запроси новый"
        try:
            if datetime.fromisoformat(r["expires"]) <= _now():
                with self._tx() as c:
                    c.execute("DELETE FROM codes WHERE email=?", (e,))
                return "Код устарел — запроси новый"
        except ValueError:
            return "Код не найден или устарел — запроси новый"
        if int(r["attempts"]) >= CODE_MAX_ATTEMPTS:
            with self._tx() as c:
                c.execute("DELETE FROM codes WHERE email=?", (e,))
            return "Слишком много попыток — запроси новый код"
        ok = hmac.compare_digest(self._code_hash(e, (code or "").strip()), r["code_hash"])
        if not ok:
            with self._tx() as c:
                c.execute("UPDATE codes SET attempts=attempts+1 WHERE email=?", (e,))
            left = CODE_MAX_ATTEMPTS - int(r["attempts"]) - 1
            return ("Неверный код — запроси новый" if left <= 0
                    else f"Неверный код. Осталось попыток: {left}")
        with self._tx() as c:
            c.execute("DELETE FROM codes WHERE email=?", (e,))
        return ""

    # ── сессии ─────────────────────────────────────────────────────────────
    def open_session(self, account_id: int) -> str:
        sid = secrets.token_urlsafe(32)
        now = _now()
        with self._tx() as c:
            c.execute("INSERT INTO sessions(sid,account_id,created,last_seen,expires) "
                      "VALUES(?,?,?,?,?)",
                      (sid, account_id, _iso(now), _iso(now),
                       _iso(now + timedelta(days=SESSION_DAYS))))
        return sid

    def session_account(self, sid: str) -> sqlite3.Row | None:
        """Аккаунт по куке. Заодно продлеваем — но не чаще раза в сутки, чтобы
        не писать в базу на каждый чих."""
        if not sid:
            return None
        r = self._conn().execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not r:
            return None
        now = _now()
        try:
            if datetime.fromisoformat(r["expires"]) <= now:
                self.close_session(sid)
                return None
            stale = (now - datetime.fromisoformat(r["last_seen"])).total_seconds() > 86400
        except ValueError:
            self.close_session(sid)
            return None
        if stale:
            with self._tx() as c:
                c.execute("UPDATE sessions SET last_seen=?,expires=? WHERE sid=?",
                          (_iso(now), _iso(now + timedelta(days=SESSION_DAYS)), sid))
        return self.account_by_id(int(r["account_id"]))

    def close_session(self, sid: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM sessions WHERE sid=?", (sid,))

    def close_all_sessions(self, account_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))


# Топ угадываемых паролей. Список короткий намеренно: он ловит первый эшелон
# (эти пароли есть в любом словаре для перебора), а полноценная проверка по
# базе утечек требовала бы внешнего запроса — ставить вход в зависимость от
# доступности чужого API нельзя, прод и так ходит наружу через прокси.
_COMMON = frozenset("""
password passw0rd password1 password123 qwerty qwerty123 qwertyui 123456 1234567
12345678 123456789 1234567890 111111 000000 iloveyou princess admin welcome
monkey login abc123 letmein dragon baseball football master sunshine shadow
superman qazwsx michael computer jennifer jordan hunter trustno1 whatever
йцукен йцукенг пароль пароль123 привет любовь солнышко наташа сергей андрей
максим человек россия москва спартак зенит динамо котенок солнце
nutriplan nutriplan1 nutriplan123 питание здоровье диета
""".split())
