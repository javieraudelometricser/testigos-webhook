import os
import json
import subprocess
import requests

TOKEN_IG = os.environ.get("IG_ACCESS_TOKEN", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ARCHIVO_ESTADO = "estado_ig.json"


def enviar_alerta(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": texto})


def leer_ultimo_id():
    if os.path.exists(ARCHIVO_ESTADO):
        with open(ARCHIVO_ESTADO) as f:
            return json.load(f).get("ultimo_id")
    return None


def guardar_ultimo_id(nuevo_id):
    with open(ARCHIVO_ESTADO, "w") as f:
        json.dump({"ultimo_id": nuevo_id}, f)


def revisar():
    url = f"https://graph.facebook.com/v21.0/{IG_USER_ID}/media"
    params = {
        "fields": "id,caption,permalink,timestamp",
        "access_token": TOKEN_IG,
        "limit": 1,
    }
    respuesta = requests.get(url, params=params).json()
    posts = respuesta.get("data", [])

    if not posts:
        return

    ultimo_post = posts[0]
    nuevo_id = ultimo_post["id"]
    anterior_id = leer_ultimo_id()

    if anterior_id is None:
        # primera vez que corre, solo guarda sin avisar (para no disparar alerta de todo el historial)
        guardar_ultimo_id(nuevo_id)
        return

    if nuevo_id != anterior_id:
        caption = ultimo_post.get("caption", "(sin descripción)")
        link = ultimo_post.get("permalink", "")
        enviar_alerta(f"📸 Nuevo post en Instagram\n\"{caption}\"\n🔗 {link}")
        guardar_ultimo_id(nuevo_id)


if __name__ == "__main__":
    revisar()