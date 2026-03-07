from flask import Flask, request
import os

app = Flask(__name__)

@app.route("/")
def home():
    return "BOT ACTIVO"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print(data)
    return {"status": "ok"}
