from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from db import get_connection
from datetime import datetime

app = Flask(__name__)

@app.route("/twilio/inbound", methods=["POST"])
def inbound():
    from_number = request.form.get("From", "")
    if from_number.startswith("whatsapp:"):
        from_number = from_number.replace("whatsapp:", "")


    conn = get_connection()
    cur = conn.cursor()

    if body in ["STOP", "UNSUBSCRIBE", "SALIR"]:
        cur.execute("UPDATE customers SET dnc=TRUE, optout_date=%s WHERE phone=%s",
                    (datetime.now(), from_number))
        resp = MessagingResponse()
        resp.message("✅ You have been unsubscribed.")
    elif body in ["START", "YES"]:
        cur.execute("UPDATE customers SET dnc=FALSE, optin_date=%s WHERE phone=%s",
                    (datetime.now(), from_number))
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
    return "✅ Webhook running"

