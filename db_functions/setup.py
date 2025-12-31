import sqlite3

USERS_DB = "users.db"

def init_db():
    conn = sqlite3.connect(USERS_DB)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        session TEXT NOT NULL UNIQUE,
        gemini_key TEXT NOT NULL,
        datadog_api_key TEXT NOT NULL,
        datadog_site TEXT NOT NULL,
        instructions TEXT NOT NULL,
        imap_primary_host TEXT,
        smtp_primary_host TEXT
    );""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS imap_accounts(
        session TEXT NOT NULL,
        imap_host TEXT NOT NULL,
        smtp_host TEXT NOT NULL,
        mail TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        
        FOREIGN KEY (session) REFERENCES users (session)
    );""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS imap_messages(
        msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
        mail TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        session TEXT NOT NULL,
        type TEXT NOT NULL,
        sender TEXT NOT NULL,
        message TEXT NOT NULL,
        subject TEXT,
        FOREIGN KEY (session) REFERENCES users (session)
    );""")

    conn.commit()
    conn.close()