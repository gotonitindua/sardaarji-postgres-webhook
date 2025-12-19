from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
from datetime import datetime
import os, time, threading, json
import requests

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

# 🔄 Keep DB Awake (Render free tier)
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
        print(f"📤 WhatsApp Send: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print("⚠️ Send failed:", e)


def normalize_phone(p: str) -> str:
    p = str(p).strip()
    if not p.startswith("+"):
        p = "+" + p
    return p


def log_incoming_message(cur, customer_id, name, phone, text):
    """
    Logs an inbound WhatsApp message into messages table.
    Assumes:
    - cur is an active cursor
    - commit/close handled by caller
    """
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
        datetime.now()
    ))

    cur.execute("""
        UPDATE customers
        SET last_contacted = %s
        WHERE id = %s
    """, (datetime.now(), customer_id))



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


# ======================================================
# 🔥 Handle First Inbound Message (QR Join)
# ======================================================
def handle_initial_optin(db_phone, text):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, language, optin_date, dnc FROM customers WHERE phone=%s", (db_phone,))
    row = cur.fetchone()

    t = (text or "").lower()
    lang = "es" if ("hola" in t or "espan" in t or "españ" in t) else "en"
    now = datetime.now()

    if not row:
        cur.execute("""
            INSERT INTO customers (phone, language, dnc, optin_date, optin_source)
            VALUES (%s, %s, FALSE, %s, 'WhatsApp QR')
            RETURNING id
        """, (db_phone, lang, now))
        cid = cur.fetchone()["id"]

    elif not row["optin_date"]:
        cid = row["id"]
        cur.execute("""
            UPDATE customers
            SET dnc=FALSE, optin_date=%s, optin_source='WhatsApp QR', language=%s
            WHERE id=%s
        """, (now, lang, cid))

    else:
        conn.commit()
        cur.close()
        conn.close()
        return

    conn.commit()
    cur.close()
    conn.close()

    send_whatsapp_reply(db_phone, WELCOME_ES if lang == "es" else WELCOME_EN)
    print("🎯 Sent welcome to", db_phone)


# ===============================
# 🔐 Webhook Verification (GET)
# ===============================
@app.route("/meta/webhook", methods=["GET"])
def meta_webhook_verify():
    if (request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == WA_VERIFY_TOKEN):
        print("🔐 Verified with Meta")
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
                            UPDATE customers SET dnc=TRUE, optout_date=%s WHERE phone=%s
                        """, (datetime.now(), db_phone))
                        conn.commit()
                        send_whatsapp_reply(
                            db_phone,
                            "❌ Unsubscribed.\nReply START to rejoin.\n\n"
                            "❌ Dado de baja.\nResponde START para volver."
                        )

                    elif upper in ["START", "YES", "SI", "SÍ"]:
                        cur.execute("""
                            UPDATE customers SET dnc=FALSE, optin_date=%s WHERE phone=%s
                        """, (datetime.now(), db_phone))
                        conn.commit()
                        send_whatsapp_reply(
                            db_phone,
                            "🎉 Subscription active again!\nReply STOP anytime.\n\n"
                            "🎉 ¡Suscripción reactivada!\nResponde STOP para darte de baja."
                        )


                    else:
                        # Ensure customer exists (same connection)
                        cur.execute(
                            "SELECT id, name FROM customers WHERE phone=%s",
                            (db_phone,)
                        )
                        cust = cur.fetchone()
                    
                        if cust:
                            # 🔥 Log incoming message FIRST
                            log_incoming_message(
                                cur,
                                customer_id=cust["id"],
                                name=cust.get("name"),
                                phone=db_phone,
                                text=text
                            )
                            conn.commit()
                    
                        # Run opt-in logic AFTER logging
                        handle_initial_optin(db_phone, text)


                    cur.close()
                    conn.close()

    except Exception as e:
        print("⚠️ Webhook error:", e)

    return jsonify(success=True), 200


# ========================
# 🎉 QR Join Landing Page
# ========================
@app.route("/join")
def join_page():
    return """
    <!DOCTYPE html>
    <html lang='en'>
    <head>
      <meta charset='UTF-8'>
      <meta name='viewport' content='width=device-width, initial-scale=1.0'>
      <title>Join Loyalty Club</title>
      <style>
        body {
          margin: 0;
          padding: 0;
          background: #111;
          font-family: Arial, sans-serif;
          color: #FFD700;
          text-align: center;
        }
        .container {
          margin-top: 80px;
          padding: 20px;
        }
        h1 {
          font-size: 26px;
          font-weight: bold;
        }
        .btn {
          display: block;
          margin: 20px auto;
          padding: 18px 25px;
          background: #FFD700;
          color: #000;
          text-decoration: none;
          border-radius: 12px;
          font-size: 18px;
          width: 80%;
          max-width: 320px;
          font-weight: bold;
        }
        .tagline {
          margin-top: 10px;
          font-size: 15px;
          line-height: 22px;
          color: #FFD700;
          opacity: 0.9;
        }
        img.logo {
          display: block;
          margin: 0 auto 20px auto;
          width: 180px;
        }
      </style>
    </head>
    <body>
      <div class='container'>
        <img src='/static/sardaar_logo.png' class='logo' alt='Sardaar Ji Logo'>
        <h1>Join Loyalty Club</h1>
        <a class='btn'
           href="https://wa.me/50767248548?text=Hi%21%20I%20want%20to%20join%20the%20Sardaar%20Ji%20Loyalty%20Club%20and%20get%20rewards%20%2B%20offer%20updates%21%20">
           English
        </a>
        <a class='btn'
           href="https://wa.me/50767248548?text=%C2%A1Hola%21%20Quiero%20unirme%20al%20Club%20de%20Lealtad%20de%20Sardaar%20Ji%20y%20recibir%20recompensas%20%2B%20ofertas%21%20">
           Español
        </a>
        <p class='tagline'>
          ⭐ Earn Rewards Every Visit <br>
          ⭐ Gane recompensas en cada visita
        </p>
      </div>
    </body>
    </html>
    """


@app.route("/")
def health():
    return "🚀 Meta WhatsApp Webhook READY"
