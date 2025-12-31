import asyncio
import json
import os
import sqlite3
import logging
import time
from typing import Dict

import aiohttp
import aioimaplib
import aiosmtplib
from email.message import EmailMessage
from email import message_from_bytes
from email.utils import parseaddr

from ddtrace import tracer

from ReplyOptimizer.db_functions.users_functions import get_session, insert_message_async
from ReplyOptimizer.worker.services.gemini import gemini_answer

USERS_DB = "users.db"

os.makedirs("logs", exist_ok=True)

_session_loggers: Dict[str, logging.Logger] = {}

def get_session_logger(session: str) -> logging.Logger:
    if session in _session_loggers:
        return _session_loggers[session]

    logger_name = f"reply_optimizer.session_{session}"
    session_logger = logging.getLogger(logger_name)

    if session_logger.handlers:
        _session_loggers[session] = session_logger
        return session_logger

    file_handler = logging.FileHandler(f"logs/session_{session[:8]}.log")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | mailbox:%(mailbox)s | thread:%(thread_id)s: %(message)s",
        defaults={"mailbox": "-", "thread_id": "-"}
    ))

    session_logger.setLevel(logging.INFO)
    session_logger.addHandler(file_handler)
    session_logger.propagate = False

    _session_loggers[session] = session_logger
    return session_logger

class DatadogMetrics:
    def __init__(self, api_key: str, site: str = "datadoghq.com"):
        self.api_key = api_key.strip()
        self.site = site.strip().lower()
        self.base_url = f"https://api.{self.site}"
        self.headers = {
            "Content-Type": "application/json",
            "DD-API-KEY": self.api_key
        }
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.headers)
        return self.session

    async def _post(self, endpoint: str, payload: dict):
        url = f"{self.base_url}{endpoint}"
        try:
            async with await self._get_session() as session:
                async with session.post(url, data=json.dumps(payload), timeout=10) as resp:
                    if resp.status in (200, 202, 204):
                        return
                    text = await resp.text()
                    self.logger.warning(f"Datadog API error {resp.status}: {text[:200]}")
        except Exception as e:
            self.logger.warning(f"Datadog send exception: {e}")

    async def submit_metric(
        self,
        metric_name: str,
        value: float,
        metric_type: str = "gauge",
        tags: list[str] | None = None,
        timestamp: int | None = None
    ):
        if timestamp is None:
            timestamp = int(time.time())

        series = {
            "series": [{
                "metric": metric_name,
                "points": [[timestamp, value]],
                "type": metric_type,
                "tags": tags or []
            }]
        }
        await self._post("/api/v1/series", series)

    async def gauge(self, metric_name: str, value: float, tags: list[str] | None = None):
        await self.submit_metric(metric_name, value, "gauge", tags)

    async def count(self, metric_name: str, value: int = 1, tags: list[str] | None = None):
        await self.submit_metric(metric_name, value, "count", tags)

    async def increment(self, metric_name: str, tags: list[str] | None = None):
        await self.count(metric_name, 1, tags)

    async def histogram(self, metric_name: str, value: float, tags: list[str] | None = None):
        await self.submit_metric(metric_name, value, "histogram", tags)

    async def service_check(
        self,
        check_name: str,
        status: int,
        tags: list[str] | None = None,
        message: str = ""
    ):
        payload = {
            "check": check_name,
            "status": status,
            "tags": tags or [],
            "message": message,
            "timestamp": int(time.time())
        }
        await self._post("/api/v1/check_run", payload)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

def _sqlite_fetchone(query, params=()):
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    conn.close()
    return row


def _sqlite_fetchall(query, params=()):
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows

def get_text_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(errors="ignore")
    return msg.get_payload(decode=True).decode(errors="ignore")

class SessionWorker:
    def __init__(self, session: str):
        self.session = session
        self.tasks: Dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self.dd: DatadogMetrics | None = None
        self.running = True
        self.lock = asyncio.Lock()
        self.logger = get_session_logger(session)

    async def sync_accounts(self):
        async with self.lock:
            data = await asyncio.to_thread(get_session, self.session)
            if not data:
                return

            if not self.dd:
                self.dd = DatadogMetrics(data.get("datadog_api_key"), data.get("datadog_site"))
                self.logger.info("Datadog initialized", extra={"session": self.session})

            db_accounts = {a["mail"]: a for a in data["imap_accounts"]}
            current = set(self.tasks.keys())
            desired = set(db_accounts.keys())

            for mail in desired - current:
                stop_event = asyncio.Event()
                acc = db_accounts[mail]

                task = asyncio.create_task(
                    imap_worker(
                        session=self.session,
                        mail=mail,
                        password=acc["password"],
                        imap_host=acc["imap_host"],
                        smtp_host=acc["smtp_host"],
                        stop_event=stop_event,
                        dd=self.dd,
                        logger=self.logger
                    )
                )

                self.tasks[mail] = (task, stop_event)

                await self.dd.increment(
                    "imap.accounts.started",
                    tags=[f"mailbox:{mail}"]
                )

            # stop removed
            for mail in current - desired:
                task, stop_event = self.tasks.pop(mail)
                stop_event.set()
                task.cancel()

                await self.dd.increment(
                    "imap.accounts.stopped",
                    tags=[f"mailbox:{mail}"]
                )

            await self.dd.gauge(
                "imap.accounts.active",
                len(self.tasks)
            )

    async def stop(self):
        self.running = False
        for task, stop_event in self.tasks.values():
            stop_event.set()
            task.cancel()

        await asyncio.gather(*(t for t, _ in self.tasks.values()), return_exceptions=True)

    async def run(self):
        while self.running:
            await self.sync_accounts()
            await asyncio.sleep(15)

async def imap_worker(
    session: str,
    mail: str,
    password: str,
    imap_host: str,
    smtp_host: str,
    stop_event: asyncio.Event,
    dd: DatadogMetrics,
    logger: logging.Logger
):
    host, port = imap_host.split(":")
    smtp_host, smtp_port = smtp_host.split(":")

    while not stop_event.is_set():
        client = None
        try:
            client = aioimaplib.IMAP4_SSL(host, int(port))
            await client.wait_hello_from_server()
            await client.login(mail, password)
            await client.select("INBOX")

            await dd.service_check(
                "imap.health",
                0,
                tags=[f"mailbox:{mail}"]
            )

            logger.info("IMAP connected", extra={"mailbox": mail, "session": session})

            while not stop_event.is_set():
                await asyncio.sleep(30)

                with tracer.trace("imap.search", service="reply-optimizer"):
                    _, data = await client.search("UNSEEN")

                if not data or not data[0]:
                    continue

                for uid in data[0].split():
                    await handle_message(
                        client=client,
                        uid=uid,
                        session=session,
                        mail=mail,
                        smtp_host=smtp_host,
                        smtp_port=int(smtp_port),
                        password=password,
                        dd=dd,
                        logger=logger
                    )

                    await client.store(uid, "+FLAGS", "\\Seen")

                    await dd.increment(
                        "emails.processed",
                        tags=[f"mailbox:{mail}"]
                    )

        except asyncio.CancelledError:
            break

        except Exception:
            logger.exception("IMAP error", extra={"mailbox": mail, "session": session})

            await dd.increment(
                "errors.total",
                tags=["component:imap", f"mailbox:{mail}"]
            )

            await dd.service_check(
                "imap.health",
                2,
                tags=[f"mailbox:{mail}"]
            )

            await asyncio.sleep(5)

        finally:
            if client:
                try:
                    await client.logout()
                except Exception:
                    pass

async def handle_message(
    client,
    uid,
    session,
    mail,
    smtp_host,
    smtp_port,
    password,
    dd: DatadogMetrics,
    logger: logging.Logger
):
    with tracer.trace("imap.fetch", service="reply-optimizer"):
        _, msg_data = await client.fetch(uid, "(RFC822)")

    msg = message_from_bytes(msg_data[0][1])
    text = get_text_body(msg)

    _, to_email = parseaddr(msg.get("From", ""))
    subject = msg.get("Subject", "")

    msg_id = msg.get("Message-ID")
    in_reply_to = msg.get("In-Reply-To")

    if in_reply_to:
        row = await asyncio.to_thread(
            _sqlite_fetchone,
            "SELECT session FROM imap_messages WHERE thread_id = ?",
            (in_reply_to,)
        )
        thread_id = in_reply_to if row else msg_id
        is_new = not bool(row)
    else:
        thread_id = msg_id
        is_new = True

    conversation = []
    if not is_new:
        conversation = await asyncio.to_thread(
            _sqlite_fetchall,
            """
            SELECT sender, message, type
            FROM imap_messages
            WHERE thread_id = ?
            ORDER BY msg_id ASC
            """,
            (thread_id,)
        )

    with tracer.trace("gemini.answer", service="reply-optimizer"):
        body, status = await asyncio.to_thread(
            gemini_answer, text, conversation, session
        )

    await send_reply(
        session=session,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        mail=mail,
        password=password,
        to=to_email,
        subject=f"Re: {subject}",
        first_subject = subject,
        body=body,
        thread_id=thread_id,
        original_text=text,
        status=status,
        dd=dd,
        logger=logger
    )

async def send_reply(
    session,
    smtp_host,
    smtp_port,
    mail,
    password,
    to,
    subject,
    first_subject,
    body,
    thread_id,
    original_text,
    status,
    dd: DatadogMetrics,
    logger: logging.Logger
):
    thread_tag = f"thread:{thread_id}"

    await insert_message_async(
        session, thread_id, "incoming", to, original_text, first_subject, mail
    )

    await dd.increment(
        "gemini.outcome",
        tags=[f"status:{status}", f"mailbox:{mail}", thread_tag]
    )

    if not body:
        await dd.increment(
            "errors.by_type",
            tags=[f"type:{status}", f"mailbox:{mail}"]
        )
        logger.warning(
            "LLM returned no body",
            extra={
                "session": session,
                "mailbox": mail,
                "thread_id": thread_id,
                "status": status
            }
        )
        return

    if isinstance(body, dict) and "meta" in body:
        meta = body["meta"]
        await dd.histogram("gemini.prompt.chars", meta.get("prompt_chars", 0))
        await dd.histogram("gemini.response.chars", meta.get("response_chars", 0))

        body = body["body"]

    await insert_message_async(
        session, thread_id, "outcoming", mail, body, subject, mail
    )

    msg = EmailMessage()
    msg["From"] = mail
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    for attempt in (1, 2):
        try:
            with tracer.trace(
                "smtp.send",
                service="reply-optimizer",
                resource=mail
            ):
                await aiosmtplib.send(
                    msg,
                    hostname=smtp_host,
                    port=smtp_port,
                    start_tls=True,
                    username=mail,
                    password=password,
                    timeout=10
                )

            await dd.increment(
                "smtp.sent",
                tags=[f"mailbox:{mail}", thread_tag]
            )

            await dd.service_check(
                "smtp.health",
                0,
                tags=[f"mailbox:{mail}"]
            )

            logger.info(
                "Reply sent",
                extra={
                    "session": session,
                    "mailbox": mail,
                    "thread_id": thread_id,
                    "attempt": attempt
                }
            )
            break

        except Exception:
            await dd.increment(
                "errors.total",
                tags=["component:smtp", f"mailbox:{mail}"]
            )

            if attempt == 2:
                await dd.service_check(
                    "smtp.health",
                    2,
                    tags=[f"mailbox:{mail}"]
                )
                logger.exception(
                    "SMTP send failed",
                    extra={
                        "session": session,
                        "mailbox": mail,
                        "thread_id": thread_id
                    }
                )
                raise

            await asyncio.sleep(1)

class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, SessionWorker] = {}

    def _load_sessions(self):
        conn = sqlite3.connect(USERS_DB)
        cur = conn.cursor()
        cur.execute("SELECT session FROM users")
        rows = cur.fetchall()
        conn.close()
        return {r[0] for r in rows}

    async def sync_sessions(self):
        sessions = await asyncio.to_thread(self._load_sessions)

        for s in sessions - self.sessions.keys():
            worker = SessionWorker(s)
            self.sessions[s] = worker
            asyncio.create_task(worker.run())

        for s in list(self.sessions.keys() - sessions):
            worker = self.sessions.pop(s)
            await worker.stop()

async def main():
    manager = SessionManager()

    while True:
        await manager.sync_sessions()
        await asyncio.sleep(30)