from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from datetime import datetime
import os, time, threading, json
import requests
import pytz

# ==========================
# ⏰ Timezone (Panama only)
# ==========================
PANAMA_TZ = pytz.timezone("America/Panama")

def now_panama():
    """Return current Panama-local datetime (naive)."""
    return datetime.now(PANAMA_TZ).replace(tzinfo=None)

# ==========================
# 🌐 Flask App
# ==========================
app = Flask(__name__)

# ==========================
# 🔹 Database Configuration
# ==========================
DB_URL = os.environ.get("DATABASE_URL")

def get_connection():
    return psycopg2.connect(
        DB_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor
    )

# ==========================
# 🔄 Keep DB Awake (Render)
# ==========================
def keep_db_awake():
    ping_interval = int(os.environ.get("DB_PING_INTERVAL", 600))
    while True:
        try:
            conn = get_connection()
            conn.cursor().execute("SELECT 1;")
            conn.commit()
            conn.close()
            print("🔁 DB ping OK")
        except Exception as e:
            print("⚠️ DB ping failed:", e)
        time.sleep(ping_interval)

threading.Thread(target=keep_db_awake, daemon=True).start()

# ==========================
# 🌐 WhatsApp Cloud API
# ==========================
WA_GRAPH_VER       = os.environ.get("WA_GRAPH_VER", "v22.0")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_PERM_TOKEN      = os.environ.get("WA_PERM_TOKEN", "")
WA_VERIFY_TOKEN    = os.environ.get("WA_VERIFY_TOKEN", "sardaarjisecret")

def send_whatsapp_reply(to_number: str, text: str):
    try:
        if not (WA_PHONE_NUMBER_ID and WA_PERM_TOKEN):
            print("⚠️ Cloud API credentials missing")
            return

        url = f"https://graph.facebook.com/{WA_GRAPH_VER}/{WA_PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": text}
        }
        headers = {
            "Authorization": f"Bearer {WA_PERM_TOKEN}",
            "Content-Type": "application/json"
        }

        r = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"📤 WhatsApp Send: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print("⚠️ Send failed:", e)

# ==========================
# 📞 Helpers
# ==========================
def normalize_phone(p: str) -> str:
    p = str(p).strip()
    if not p.startswith("+"):
        p = "+" + p
    return p

def log_incoming_message(cur, customer_id, name, phone, text):
    now = now_panama()

    cur.execute("""
        INSERT INTO messages (
            customer_id,
            direction,
            name,
            phone,
            type,
            message,
            status,
            created_at,
            is_read
        )
        VALUES (
            %s,
            'in',
            %s,
            %s,
            'Incoming',
            %s,
            'received',
            %s,
            FALSE
        )
    """, (
        customer_id,
        name or "",
        f"whatsapp:{phone}",
        text,
        now
    ))

    cur.execute("""
        UPDATE customers
        SET last_contacted = %s
        WHERE id = %s
    """, (now, customer_id))

# ==========================
# 🎉 Welcome Messages
# ==========================
WELCOME_EN = (
    "🎉 Welcome to Sardaar Ji Indian Cuisine! "
    "You're now part of our WhatsApp Loyalty Club.\n\n"
    "Earn points every visit and unlock rewards! 🍛✨\n"
    "Reply STOP anytime to unsubscribe."
)

WELCOME_ES = (
    "🎉 ¡Bienvenido a Sardaar Ji Indian Cuisine! "
    "Ahora eres parte de nuestro Club de Lealtad por WhatsApp.\n\n"
    "Gana puntos en cada visita y obtén recompensas! 🍛✨\n"
    "Responde STOP en cualquier momento para darte de baja."
)

def handle_initial_optin(db_phone, text):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT id, language, optin_date FROM customers WHERE phone=%s",
        (db_phone,)
    )
    row = cur.fetchone()

    t = (text or "").lower()
    lang = "es" if ("hola" in t or "espan" in t or "españ" in t) else "en"
    now = now_panama()

    if not row:
        cur.execute("""
            INSERT INTO customers (phone, language, dnc, optin_date, optin_source)
            VALUES (%s, %s, FALSE, %s, 'WhatsApp QR')
            RETURNING id
        """, (db_phone, lang, now))

    elif not row["optin_date"]:
        cur.execute("""
            UPDATE customers
            SET dnc=FALSE, optin_date=%s, optin_source='WhatsApp QR', language=%s
            WHERE phone=%s
        """, (now, lang, db_phone))

    else:
        conn.commit()
        cur.close()
        conn.close()
        return

    conn.commit()
    cur.close()
    conn.close()

    send_whatsapp_reply(db_phone, WELCOME_ES if lang == "es" else WELCOME_EN)

# ===============================
# 🔐 Webhook Verification (GET)
# ===============================
@app.route("/meta/webhook", methods=["GET"])
def meta_webhook_verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == WA_VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

# ===============================
# 📩 Webhook Events (POST)
# ===============================
@app.route("/meta/webhook", methods=["POST"])
def meta_webhook():
    data = request.get_json()
    print("📥 Incoming:", json.dumps(data, indent=2)[:900])

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):

                    # ✅ Ignore non-text messages safely
                    if msg.get("type") != "text":
                        continue

                    raw_number = msg.get("from")
                    text = msg.get("text", {}).get("body", "").strip()
                    upper = text.upper()
                    db_phone = normalize_phone(raw_number)

                    conn = get_connection()
                    cur = conn.cursor()

                    cur.execute("""
                        INSERT INTO customers (phone)
                        VALUES (%s)
                        ON CONFLICT DO NOTHING
                    """, (db_phone,))
                    conn.commit()

                    if upper in ["STOP", "UNSUBSCRIBE", "SALIR"]:
                        cur.execute("""
                            UPDATE customers
                            SET dnc=TRUE, optout_date=%s
                            WHERE phone=%s
                        """, (now_panama(), db_phone))
                        conn.commit()
                        send_whatsapp_reply(
                            db_phone,
                            "❌ Unsubscribed.\nReply START to rejoin.\n\n"
                            "❌ Dado de baja.\nResponde START para volver."
                        )

                    elif upper in ["START", "YES", "SI", "SÍ"]:
                        cur.execute("""
                            UPDATE customers
                            SET dnc=FALSE, optin_date=%s
                            WHERE phone=%s
                        """, (now_panama(), db_phone))
                        conn.commit()
                        send_whatsapp_reply(
                            db_phone,
                            "🎉 Subscription active again!\nReply STOP anytime.\n\n"
                            "🎉 ¡Suscripción reactivada!\nResponde STOP para darte de baja."
                        )

                    else:
                        cur.execute(
                            "SELECT id, name FROM customers WHERE phone=%s",
                            (db_phone,)
                        )
                        cust = cur.fetchone()

                        if cust:
                            log_incoming_message(
                                cur,
                                customer_id=cust["id"],
                                name=cust.get("name"),
                                phone=db_phone,
                                text=text
                            )
                            conn.commit()

                        handle_initial_optin(db_phone, text)

                    cur.close()
                    conn.close()

    except Exception as e:
        print("⚠️ Webhook error:", e)

    return jsonify(success=True), 200

# ========================
# ❤️ Health Check
# ========================
@app.route("/")
def health():
    return "🚀 Meta WhatsApp Webhook READY"
