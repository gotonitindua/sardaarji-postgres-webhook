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

# ==========================
# 🌐 WhatsApp Cloud API
# ==========================
WA_GRAPH_VER       = os.environ.get("WA_GRAPH_VER", "v22.0")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "") # <-- required
WA_PERM_TOKEN      = os.environ.get("WA_PERM_TOKEN", "")      # <-- required
WA_VERIFY_TOKEN    = os.environ.get("WA_VERIFY_TOKEN", "sardaarjisecret")


def send_whatsapp_reply(to_number: str, text: str):
    """Send simple text reply from webhook"""
    try:
        if not (WA_PHONE_NUMBER_ID and WA_PERM_TOKEN):
            print("⚠️ Cloud API not configured")
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
        r = requests.post(url, json=payload, headers=headers, timeout=20)
        print("📤 Reply:", r.status_code, r.text[:150])
    except Exception as e:
        print("⚠️ Reply failed:", e)


def normalize_db_phone(from_number: str) -> str:
    """Ensure +507 format"""
    p = str(from_number).strip()
    if not p.startswith("+"):
        p = "+" + p
    return p


# ===============================
# 🔐 Webhook Verification (GET)
# ===============================
@app.route("/meta/webhook", methods=["GET"])
def meta_webhook_verify():
    if (request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == WA_VERIFY_TOKEN):
        print("🔐 Webhook verified with Meta")
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403


# ===============================
# 📩 Webhook Events (POST)
# ===============================
@app.route("/meta/webhook", methods=["POST"])
def meta_webhook():
    data = request.get_json()
    print("📥 Incoming webhook:", json.dumps(data, indent=2)[:900])

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # 🔹 Handle inbound text messages
                for msg in value.get("messages", []):
                    from_number = msg.get("from")
                    text        = msg.get("text", {}).get("body", "").strip()
                    upper       = text.upper()
                    db_phone    = normalize_db_phone(from_number)

                    conn = get_connection()
                    cur = conn.cursor()

                    # 🔥 Auto-opt-in if new user texts first time
                    cur.execute("""
                        INSERT INTO customers (phone, optin_date, dnc)
                        VALUES (%s, %s, FALSE)
                        ON CONFLICT (phone) DO NOTHING
                    """, (db_phone, datetime.now()))
                    conn.commit()

                    # 🔹 Handle STOP
                    if upper in ["STOP", "UNSUBSCRIBE", "SALIR"]:
                        cur.execute(
                            "UPDATE customers SET dnc=TRUE, optout_date=%s WHERE phone=%s",
                            (datetime.now(), db_phone)
                        )
                        conn.commit()
                        send_whatsapp_reply(
                            from_number,
                            "❌ You’ve been unsubscribed from Sardaar Ji promotions.\n"
                            "Reply START to resubscribe.\n\n"
                            "❌ Has sido dado de baja de Sardaar Ji.\n"
                            "Responde START para suscribirte de nuevo."
                        )

                    # 🔹 Handle START
                    elif upper in ["START", "YES"]:
                        cur.execute(
                            "UPDATE customers SET dnc=FALSE, optin_date=%s WHERE phone=%s",
                            (datetime.now(), db_phone)
                        )
                        conn.commit()
                        send_whatsapp_reply(
                            from_number,
                            "🎉 Welcome back! You are subscribed again to Sardaar Ji updates.\n"
                            "Reply STOP anytime to unsubscribe.\n\n"
                            "🎉 ¡Bienvenido de nuevo! Te has suscrito otra vez a las novedades de Sardaar Ji.\n"
                            "Responde STOP en cualquier momento para darte de baja."
                        )

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
          width: 150px;
          margin-bottom: 20px;
        }
      </style>
    </head>
    <body>
      <div class='container'>
        <!-- ⚠️ If logo still doesn’t show, replace this URL with the
             exact working Cloudinary URL you use on your main site. -->
        <img src='https://res.cloudinary.com/dqf7aonc5/image/upload/v1721445237/sardaar_logo.png'
             alt='Sardaar Ji Logo'
             class='logo'>

        <h1>Join Loyalty Club</h1>

        <!-- English: "Hi! I want to join the Sardaar Ji Loyalty Club and get rewards and offer updates! 🎉" -->
        <a class='btn'
           href="https://wa.me/50767248548?text=Hi!%20I%20want%20to%20join%20the%20Sardaar%20Ji%20Loyalty%20Club%20and%20get%20rewards%20and%20offer%20updates!%20%F0%9F%8E%89">
           English 🇬🇧
        </a>

        <!-- Spanish: "¡Hola! Quiero unirme al Club de Lealtad de Sardaar Ji y recibir recompensas y ofertas! 🎉" -->
        <a class='btn'
           href="https://wa.me/50767248548?text=%C2%A1Hola!%20Quiero%20unirme%20al%20Club%20de%20Lealtad%20de%20Sardaar%20Ji%20y%20recibir%20recompensas%20y%20ofertas!%20%F0%9F%8E%89">
           Español 🇵🇦
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
