import os
import hmac
import threading
import requests
import xml.etree.ElementTree as ET
from flask import Flask, request, Response

app = Flask(__name__)


@app.route("/")
def inicio():
    return "Servicio activo", 200


VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "cambia_esto_luego")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RESUMEN_TOKEN = os.environ.get("RESUMEN_TOKEN", "")
CANAL_YOUTUBE = "UCmYx6HZpnFV5LgiOgZ4SWzA"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# Orden y etiquetas del resumen: (red, título, [(tipo, emoji, texto)])
SECCIONES = [
    ("facebook", "Facebook", [
        ("reacciones", "👍", "reacciones"),
        ("comentarios", "💬", "comentarios"),
        ("posts", "🔔", "posts nuevos"),
        ("otros", "⚠️", "eventos no reconocidos"),
    ]),
    ("instagram", "Instagram", [
        ("comentarios", "💬", "comentarios"),
        ("menciones", "📣", "menciones"),
        ("otros", "⚠️", "eventos no reconocidos"),
    ]),
    ("youtube", "YouTube", [
        ("videos", "🎬", "videos nuevos"),
    ]),
]

# Contadores en memoria: {(red, tipo): cantidad}
# Requiere gunicorn con UN solo worker para que todos compartan los mismos contadores.
contadores = {}
candado = threading.Lock()


def registrar(red, tipo):
    with candado:
        clave = (red, tipo)
        contadores[clave] = contadores.get(clave, 0) + 1


def enviar_alerta(texto):
    """Manda un mensaje al grupo. Devuelve True si Telegram lo aceptó."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": texto}, timeout=10)
        return r.ok
    except requests.RequestException:
        return False


def clasificar_facebook(valor):
    item = valor.get("item")
    verb = valor.get("verb", "add")

    # Quitar reacciones o borrar contenido no se cuenta (a propósito)
    if verb == "remove":
        return None
    if item == "reaction":
        return "reacciones"
    if item == "comment":
        return "comentarios"
    if item in ("status", "photo", "video", "link"):
        return "posts"
    return "otros"


def clasificar_instagram(field):
    if field == "comments":
        return "comentarios"
    if field == "mentions":
        return "menciones"
    return "otros"


def armar_resumen(conteos):
    """Arma el texto del resumen. Devuelve None si no hubo actividad."""
    bloques = []
    for red, titulo, tipos in SECCIONES:
        lineas = []
        for tipo, emoji, texto in tipos:
            n = conteos.get((red, tipo), 0)
            if n:
                lineas.append(f"{emoji} {n} {texto}")
        if lineas:
            bloques.append(titulo + "\n" + "\n".join(lineas))

    if not bloques:
        return None
    return "📊 Resumen (últimos 5 min)\n\n" + "\n\n".join(bloques)


@app.route("/webhook", methods=["POST"])
def recibir():
    datos = request.json
    try:
        objeto = datos.get("object")
        for entrada in datos.get("entry", []):
            for cambio in entrada.get("changes", []):
                field = cambio.get("field")
                if objeto == "instagram":
                    registrar("instagram", clasificar_instagram(field))
                elif objeto == "page" and field == "feed":
                    tipo = clasificar_facebook(cambio.get("value", {}))
                    if tipo:
                        registrar("facebook", tipo)
    except Exception as e:
        # Los errores se siguen avisando al momento
        enviar_alerta(f"❌ Error procesando webhook: {e}")
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verificar():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Token invalido", 403


@app.route("/resumen", methods=["GET", "POST"])
def resumen():
    token = request.args.get("token", "")
    if not RESUMEN_TOKEN or not hmac.compare_digest(token, RESUMEN_TOKEN):
        return "No autorizado", 403

    # Tomar los conteos y reiniciar de inmediato, para no perder eventos que lleguen mientras se envía
    with candado:
        conteos = dict(contadores)
        contadores.clear()

    texto = armar_resumen(conteos)
    if texto is None:
        return "Sin actividad", 200

    if not enviar_alerta(texto):
        # Si Telegram falló, se regresan los conteos para incluirlos en el siguiente resumen
        with candado:
            for clave, n in conteos.items():
                contadores[clave] = contadores.get(clave, 0) + n
        return "Error al enviar a Telegram", 502

    return "Resumen enviado", 200


@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code")
    if not code:
        return "No se recibió código de autorización", 400
    return f"""
        <h2>Autorización recibida</h2>
        <p>Copia este código y mándaselo a Javier:</p>
        <code>{code}</code>
    """


@app.route("/privacy")
def privacidad():
    return """
    <h2>Política de Privacidad - Alertas Afore</h2>
    <p>Esta aplicación es una herramienta interna de uso exclusivo para el equipo
    de la agencia y sus clientes autorizados...</p>
    <p>Contacto: javier.audelo@metricser.com</p>
    """, 200


@app.route("/youtube_callback", methods=["GET"])
def youtube_verificar():
    challenge = request.args.get("hub.challenge")
    if challenge:
        return Response(challenge, mimetype="text/plain")
    return "OK", 200


@app.route("/youtube_callback", methods=["POST"])
def youtube_recibir():
    datos = request.data
    try:
        root = ET.fromstring(datos)
        entrada = root.find("atom:entry", NS)
        if entrada is not None:
            registrar("youtube", "videos")
    except Exception as e:
        enviar_alerta(f"❌ Error procesando YouTube: {e}")
    return "OK", 200


if __name__ == "__main__":
    app.run(port=5000)
