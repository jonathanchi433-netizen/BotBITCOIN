from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "BOT ACTIVO", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print("Señal recibida:", data, flush=True)
    return jsonify({"ok": True, "received": data}), 200
