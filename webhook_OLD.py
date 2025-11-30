from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import psycopg2
import psycopg2.extras
from datetime import datetime
import os, time, threading

app = Flask(__name__)

# ✅ Postgres connection URL comes from Render environment
DB_URL = os.environ.get("DATABASE_URL")

def get_connection():
    return psycopg2.connect(
        DB_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor
    )

# 🔄 Background thread to keep DB awake
def keep_db_awake():
    ping_interval = int(os.environ.get("DB_PING_INTERVAL", 600))  # default 10 min
    while True:
        try:
            conn = psycopg2.connect(DB_URL, sslmode="require")
            cur = conn.cursor()
            cur.execute("SELECT 1;")
            conn.commit()
            cur.close()
            conn.close()
            print("✅ DB pinged, still awake")
        except Exception as e:
            print("⚠️ DB ping failed:", e)
        time.sleep(ping_interval)

# Start keep-alive thread when app launches
threading.Thread(target=keep_db_awake, daemon=True).start()

@app.route("/twilio/inbound", methods=["POST"])
def inbound():
    from_number = request.form.get("From", "").replace("whatsapp:", "")
    body = request.form.get("Body", "").strip().upper()

    conn = get_connection()
    cur = conn.cursor()

    if body in ["STOP", "UNSUBSCRIBE", "SALIR"]:
        cur.execute(
            "UPDATE customers SET dnc=TRUE, optout_date=%s WHERE phone=%s",
            (datetime.now(), from_number)
        )
        resp = MessagingResponse()
        resp.message("✅ ❌ You’ve been unsubscribed from Sardaar Ji promotions. Reply START to resubscribe. / ❌ Has sido dado de baja de Sardaar Ji. Responde START para suscribirte de nuevo..")
    elif body in ["START", "YES"]:
        cur.execute(
            "UPDATE customers SET dnc=FALSE, optin_date=%s WHERE phone=%s",
            (datetime.now(), from_number)
        )
        resp = MessagingResponse()
        resp.message("🎉 Welcome back! You are subscribed again.")
    else:
        resp = MessagingResponse()
        resp.message("🤖 Thanks for your message!")

    conn.commit()
    cur.close()
    conn.close()
    return str(resp)

@app.route("/twilio/status", methods=["POST"])
def status():
    print("📩 Status Callback:", request.form.to_dict())
    return ("", 200)

@app.route("/")
def health():
    return "✅ Postgres Webhook running"
