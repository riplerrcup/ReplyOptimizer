import asyncio
import json
import sqlite3
import uuid

USERS_DB = "users.db"

def get_session(session):
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()

    cur.execute('''SELECT gemini_key, datadog_api_key, datadog_site, imap_primary_host, smtp_primary_host, instructions FROM users WHERE session = ?''', (session,))

    row = cur.fetchone()
    if row is None:
        return None

    gemini_key, datadog_api_key, datadog_site, imap_host, smtp_primary_host, instructions = row

    cur.execute("""SELECT imap_host, smtp_host, mail, password FROM imap_accounts WHERE session = ?""", (session,))

    imap_accounts = cur.fetchall()
    if imap_accounts is None:
        imap_accounts_list = []
    else:
        imap_accounts_list = []
        for imap_account in imap_accounts:
            imap_account_host, smtp_account_host, mail, password = imap_account
            imap_accounts_list.append({
                'imap_host': imap_account_host,
                'smtp_host': smtp_account_host,
                "mail": mail,
                "password": password
            })

    conn.commit()
    conn.close()

    return {
        'gemini_key': gemini_key,
        'datadog_api_key': datadog_api_key,
        'datadog_site': datadog_site,
        'imap_primary_host': imap_host,
        "smtp_primary_host": smtp_primary_host,
        'imap_accounts': imap_accounts_list,
        'instructions': instructions
    }

def add_user(gemini_key, datadog_api_key, datadog_site, imap_host, smtp_host, instructions):
    if not all([gemini_key, datadog_api_key, datadog_site, imap_host, smtp_host, instructions]):
        raise ValueError("Not all required parameters entered")
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()

    session = str(uuid.uuid4())

    cur.execute("""INSERT INTO users (session, gemini_key, datadog_api_key, datadog_site, imap_primary_host, smtp_primary_host, instructions) VALUES (?,?,?,?,?,?,?)""",
                (session, gemini_key, datadog_api_key, datadog_site, imap_host, smtp_host, instructions)
                )

    conn.commit()
    conn.close()

    return session

def update_user(session, gemini_key=None, datadog_api_key=None, datadog_site=None, imap_primary_host=None, smtp_primary_host=None, instructions=None, imap_keys=None):
    fields = []
    values = []

    if gemini_key is not None:
        fields.append("gemini_key = ?")
        values.append(gemini_key)

    if datadog_api_key is not None:
        fields.append("datadog_api_key = ?")
        values.append(datadog_api_key)

    if datadog_site is not None:
        fields.append("datadog_site = ?")
        values.append(datadog_site)

    if imap_primary_host is not None:
        fields.append("imap_primary_host = ?")
        values.append(imap_primary_host)

    if smtp_primary_host is not None:
        fields.append("smtp_primary_host = ?")
        values.append(smtp_primary_host)

    if instructions is not None:
        fields.append("instructions = ?")
        values.append(instructions)

    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()

    try:
        if fields:
            values.append(session)
            query = f"""
                UPDATE users
                SET {", ".join(fields)}
                WHERE session = ?
            """
            cur.execute(query, values)

        if imap_keys:
            for account in imap_keys:
                imap_host = account.get("host")
                smtp_host = account.get("smtp_host")
                mail = account.get("email")
                password = account.get("password")

                if not all([imap_host, smtp_host, mail, password]):
                    continue

                cur.execute("""
                    SELECT 1 FROM imap_accounts
                    WHERE session = ? AND mail = ?
                """, (session, mail))

                exists = cur.fetchone()
                if exists:
                    continue

                cur.execute("""
                    INSERT INTO imap_accounts (session, imap_host, smtp_host, mail, password)
                    VALUES (?, ?, ?, ?, ?)
                """, (session, imap_host, smtp_host, mail, password))

        conn.commit()
        conn.close()
        return True

    except sqlite3.Error as e:
        print("DB ERROR:", e)
        conn.rollback()
        return False

    finally:
        conn.close()

async def insert_message_async(session, thread_id, msg_type, from_addr, message, subject, mail):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, insert_message, session, thread_id, msg_type, from_addr, message, subject, mail)

def insert_message(session, thread_id, msg_type, from_addr, message, subject, mail):
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO imap_messages (thread_id, session, type, sender, message, subject, mail)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (thread_id, session, msg_type, from_addr, message, subject, mail))
    conn.commit()
    conn.close()


def get_inbox(session):
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()

    cur.execute("SELECT mail FROM imap_accounts WHERE session = ?", (session,))
    accounts_raw = cur.fetchall()
    accounts = [{"mail": mail[0]} for mail in accounts_raw]

    cur.execute("""
                SELECT msg_id, thread_id, type, sender, message, subject, mail
                FROM (
                    SELECT msg_id, thread_id, type, sender, message, subject, mail, ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY msg_id ASC) AS rn
                    FROM imap_messages
                    WHERE session = ?
                    ) ranked
                WHERE rn = 1
                ORDER BY msg_id DESC
                """, (session,))

    first_messages = cur.fetchall()

    threads = {}
    for msg in first_messages:
        msg_id, thread_id, msg_type, sender, message, subject, mail = msg
        mail_str = str(mail)
        if mail_str not in threads:
            threads[mail_str] = {"messages": []}

        threads[mail_str]["messages"].append({
            "thread_id": thread_id,
            "sender": sender,
            "preview": message[:25] + "..." if len(message) > 25 else message,
            "subject": subject or "(No subject)",
        })

    conn.close()

    return {
        "accounts": accounts,
        "threads": threads
    }

def get_thread(session, thread_id):
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()

    cur.execute("""
        SELECT type, sender, message
        FROM imap_messages
        WHERE thread_id = ? AND session = ?
        ORDER BY msg_id ASC
    """, (thread_id, session))

    thread = cur.fetchall()

    if not thread:
        return None
    else:
        messages = []
        for message in thread:
            message_type, sender, message_text = message
            messages.append({
                "type": message_type,
                "sender": sender,
                "message": message_text
            })

        return messages
