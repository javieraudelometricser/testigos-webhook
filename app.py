import os
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "cambia_esto_luego")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def enviar_alerta(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": texto})


def armar_mensaje(valor):
    item = valor.get("item")
    verb = valor.get("verb", "add")
    post_id = valor.get("post_id", "")
    link = f"https://www.facebook.com/{post_id}" if post_id else "sin link"
    autor = valor.get("from", {}).get("name", "alguien")

    if verb != "add":
        return f"ℹ️ Actividad ({verb}) en Facebook\n🔗 {link}"

    if item == "comment":
        texto = valor.get("message", "(sin texto)")
        return (
            f"💬 Nuevo comentario en Facebook\n"
            f"Autor: {autor}\n"
            f"\"{texto}\"\n"
            f"🔗 {link}"
        )

    if item in ("status", "photo", "video", "link"):
        texto = valor.get("message", "(sin texto)")
        return (
            f"🔔 Nuevo post en Facebook\n"
            f"Página: {autor}\n"
            f"\"{texto}\"\n"
            f"🔗 {link}"
        )

    return f"⚠️ Evento no reconocido ({item})\n🔗 {link}"


@app.route("/webhook", methods=["GET"])
def verificar():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Token invalido", 403


@app.route("/webhook", methods=["POST"])
def recibir():
    datos = request.json
    try:
        for entrada in datos.get("entry", []):
            for cambio in entrada.get("changes", []):
                if cambio.get("field") == "feed":
                    mensaje = armar_mensaje(cambio.get("value", {}))
                    enviar_alerta(mensaje)
    except Exception as e:
        enviar_alerta(f"❌ Error procesando webhook: {e}")
    return "OK", 200


if __name__ == "__main__":
    app.run(port=5000)