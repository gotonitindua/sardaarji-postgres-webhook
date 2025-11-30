from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from datetime import datetime
import os, time, threading, json
import requests

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

threading.Thread(target=keep_db_awake, daemon=True).start()


# ============================
# 🌐 WhatsApp Cloud API config
# ============================
WA_GRAPH_VER       = os.environ.get("WA_GRAPH_VER", "v22.0")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_PERM_TOKEN      = os.environ.get("WA_PERM_TOKEN", "")
WA_VERIFY_TOKEN    = os.environ.get("WA_VERIFY_TOKEN", "sardaarjisecret")  # pick any secret, also set in Meta UI


def send_whatsapp_reply(to_number: str, text: str):
    """
    Minimal Cloud API sender used ONLY by webhook to reply to STOP/START etc.
    Uses the same test number / WA_PHONE_NUMBER_ID as your Streamlit app.
    """
    try:
        if not (WA_PHONE_NUMBER_ID and WA_PERM_TOKEN):
            print("⚠️ Cloud API not configured for webhook replies")
            return

        # Meta sends numbers without '+', but we want E.164 digits only
        to = str(to_number).strip()
        url = f"https://graph.facebook.com/{WA_GRAPH_VER}/{WA_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WA_PERM_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        print("📤 Reply send status:", r.status_code, r.text[:200])
    except Exception as e:
        print("⚠️ Failed to send reply:", e)


def normalize_db_phone(from_number: str) -> str:
    """
    Meta 'from' looks like '50766701248' (no +).
    Your DB stores phones as '+507........'.
    This helper adds '+' if missing.
    """
    p = str(from_number).strip().replace(" ", "")
    if not p.startswith("+"):
        p = "+" + p
    return p


# =========================================
# 🔔 Meta Webhook – Verification (GET)
# =========================================
@app.route("/meta/webhook", methods=["GET"])
def meta_webhook_verify():
    mode      = request.args.get("hub.mode")
    challenge = request.args.get("hub.challenge")
    token     = request.args.get("hub.verify_token")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        print("✅ Webhook verified with Meta")
        return challenge, 200
    else:
        print("❌ Webhook verification failed")
        return "Forbidden", 403


# =========================================
# 🔔 Meta Webhook – Messages & Status (POST)
# =========================================
@app.route("/meta/webhook", methods=["POST"])
def meta_webhook():
    data = request.get_json()
    print("📥 Incoming webhook JSON:", json.dumps(data, indent=2)[:1000])

    try:
        entry_list = data.get("entry", [])
        for entry in entry_list:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})

                # 1) Handle message statuses (delivered, read, failed)
                for status in value.get("statuses", []):
                    message_id = status.get("id")
                    status_str = status.get("status")  # sent, delivered, read, failed
                    error_msg = ""

                    if status_str == "failed":
                        # Meta may send an errors array
                        errs = status.get("errors", [])
                        if errs:
                            error_msg = json.dumps(errs)[:150]

                    if message_id:
                        try:
                            conn = get_connection()
                            cur = conn.cursor()
                            cur.execute(
                                "UPDATE messages SET status=%s, error=%s WHERE sid=%s",
                                (status_str, error_msg, message_id)
                            )
                            conn.commit()
                            cur.close()
                            conn.close()
                            print(f"✅ Updated message {message_id} → {status_str}")
                        except Exception as e:
                            print("⚠️ Failed to update message status:", e)

                # 2) Handle inbound messages (STOP / START / SALIR)
                for msg in value.get("messages", []):
                    from_number = msg.get("from")  # e.g. "50766701248"
                    text_body   = msg.get("text", {}).get("body", "").strip()
                    upper       = text_body.upper()

                    db_phone = normalize_db_phone(from_number)

                    # Connect to DB
                    conn = get_connection()
                    cur = conn.cursor()

                    if upper in ["STOP", "UNSUBSCRIBE", "SALIR"]:
                        cur.execute(
                            "UPDATE customers SET dnc=TRUE, optout_date=%s WHERE phone=%s",
                            (datetime.now(), db_phone)
                        )
                        conn.commit()
                        cur.close()
                        conn.close()

                        reply = (
                            "✅ ❌ You’ve been unsubscribed from Sardaar Ji promotions. "
                            "Reply START to resubscribe.\n\n"
                            "❌ Has sido dado de baja de Sardaar Ji. "
                            "Responde START para suscribirte de nuevo."
                        )
                        send_whatsapp_reply(from_number, reply)

                    elif upper in ["START", "YES"]:
                        cur.execute(
                            "UPDATE customers SET dnc=FALSE, optin_date=%s WHERE phone=%s",
                            (datetime.now(), db_phone)
                        )
                        conn.commit()
                        cur.close()
                        conn.close()

                        reply = "🎉 Welcome back! You are subscribed again to Sardaar Ji updates."
                        send_whatsapp_reply(from_number, reply)

                    else:
                        # Optional: generic reply or just ignore
                        cur.close()
                        conn.close()
                        # send_whatsapp_reply(from_number, "🤖 Thanks for your message!")
    except Exception as e:
        print("⚠️ Error parsing webhook:", e)

    return jsonify(success=True), 200


@app.route("/")
def health():
    return "✅ Meta WhatsApp Webhook running"
