from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "BotBITCOIN activo", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    print("Señal recibida:", data)
    return jsonify({"ok": True, "received": data}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
