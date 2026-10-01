import os
import re
import hmac
import html
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

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
# Mensajes individuales por evento. Apagados salvo que en Render se ponga ALERTAS_INDIVIDUALES=1
ALERTAS_INDIVIDUALES = os.environ.get("ALERTAS_INDIVIDUALES", "0") == "1"
CANAL_YOUTUBE = "UCmYx6HZpnFV5LgiOgZ4SWzA"

# Horario de operación (hora de Ciudad de México). Fuera de este horario
# no se mandan resúmenes, pero se sigue contando y todo sale a la hora de inicio.
HORA_INICIO = int(os.environ.get("HORA_INICIO", "7"))
HORA_FIN = int(os.environ.get("HORA_FIN", "24"))  # 24 = medianoche
# Cada cuánto llama el cron a /resumen (solo afecta el título del mensaje)
MINUTOS_RESUMEN = int(os.environ.get("MINUTOS_RESUMEN", "10"))

try:
    from zoneinfo import ZoneInfo
    ZONA = ZoneInfo("America/Mexico_City")
except Exception:
    ZONA = timezone(timedelta(hours=-6))  # CDMX no tiene horario de verano desde 2022


def ahora():
    return datetime.now(ZONA)


def en_horario(momento):
    return HORA_INICIO <= momento.hour < HORA_FIN

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

EMOJIS_REACCION = {
    "like": "👍", "love": "❤️", "care": "🤗", "wow": "😮",
    "haha": "😆", "sorry": "😢", "anger": "😡",
}

# Orden y etiquetas del resumen.
# Cada línea: (tipo, emoji, singular, plural, fija)
# "fija" = se muestra siempre, aunque esté en 0 (placeholder).
SECCIONES = [
    ("facebook", "Facebook", [
        ("reacciones", "👍", "reacción", "reacciones", True),
        ("comentarios", "💬", "comentario", "comentarios", True),
        ("inbox", "📩", "inbox", "inbox", True),  # visión: aún no se recibe (falta Messenger)
        ("posts", "🔔", "post nuevo", "posts nuevos", False),
        ("otros", "⚠️", "evento no reconocido", "eventos no reconocidos", False),
    ]),
    ("instagram", "Instagram", [
        ("comentarios", "💬", "comentario", "comentarios", False),
        ("menciones", "📣", "mención", "menciones", False),
        ("otros", "⚠️", "evento no reconocido", "eventos no reconocidos", False),
    ]),
    ("youtube", "YouTube", [
        ("videos", "🎬", "video nuevo", "videos nuevos", True),
    ]),
]

LIMITE_TELEGRAM = 3800      # Telegram acepta ~4096 caracteres; dejamos margen
LIMITE_TEXTO_COMENTARIO = 150

# Estado en memoria del periodo actual.
# Requiere gunicorn con UN solo worker para que todos compartan el mismo estado.
candado = threading.Lock()


def estado_vacio():
    return {
        "conteos": Counter(),        # {(red, tipo): n}
        "tipos_reaccion": Counter(),  # {"love": n} (Facebook)
        "comentarios": [],            # [(red, autor, texto, link)]
        "inicio": ahora(),            # desde cuándo se está acumulando
        "fuera_de_horario": False,    # True si el periodo cruzó la noche
    }


estado = estado_vacio()


def registrar(red, tipo, reaccion=None, comentario=None):
    with candado:
        estado["conteos"][(red, tipo)] += 1
        if reaccion:
            estado["tipos_reaccion"][reaccion] += 1
        if comentario:
            estado["comentarios"].append((red, *comentario))


def enviar_alerta(texto, html_mode=False):
    """Manda un mensaje al grupo. Devuelve True si Telegram lo aceptó."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "disable_web_page_preview": True}
    if html_mode:
        datos["parse_mode"] = "HTML"
    try:
        r = requests.post(url, data=datos, timeout=10)
        if r.ok or not html_mode:
            return r.ok
        # Si Telegram rechazó el HTML, se reintenta como texto plano
        plano = html.unescape(re.sub(r"<[^>]+>", "", texto))
        return requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": plano}, timeout=10).ok
    except requests.RequestException:
        return False


# ---------- Clasificación y mensajes individuales ----------

def link_facebook(valor):
    """Arma el link al post (y al comentario, si aplica).

    1) Si Meta manda el permalink oficial del post, se usa ese.
    2) Si no, se arma con el formato /{pagina}/posts/{post}, porque
       facebook.com/{pagina_post} no siempre abre bien.
    """
    post_id = valor.get("post_id", "")
    comment_id = valor.get("comment_id", "")
    permalink = (valor.get("post") or {}).get("permalink_url", "")

    if permalink:
        base = permalink
    elif "_" in post_id:
        pagina, post = post_id.split("_", 1)
        base = f"https://www.facebook.com/{pagina}/posts/{post}"
    elif post_id:
        base = f"https://www.facebook.com/{post_id}"
    else:
        return ""

    if comment_id:
        base += ("&" if "?" in base else "?") + f"comment_id={comment_id.split('_')[-1]}"
    return base


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


def armar_mensaje(valor):
    item = valor.get("item")
    verb = valor.get("verb", "add")
    link = link_facebook(valor) or "sin link"
    autor = valor.get("from", {}).get("name", "alguien")

    if verb == "remove":
        return None
    if item == "reaction":
        tipo = valor.get("reaction_type", "like")
        return f"{EMOJIS_REACCION.get(tipo, '👍')} Nueva reacción ({tipo}) en Facebook\nAutor: {autor}\n🔗 {link}"
    if item == "comment":
        texto = valor.get("message", "(sin texto)")
        return f"💬 Nuevo comentario en Facebook\nAutor: {autor}\n\"{texto}\"\n🔗 {link}"
    if item in ("status", "photo", "video", "link"):
        texto = valor.get("message", "(sin texto)")
        return f"🔔 Nuevo post en Facebook\nPágina: {autor}\n\"{texto}\"\n🔗 {link}"
    return f"⚠️ Evento no reconocido ({item})\n🔗 {link}"


def armar_mensaje_instagram(field, valor):
    if field == "comments":
        autor = valor.get("from", {}).get("username", "alguien")
        texto = valor.get("text", "(sin texto)")
        media_id = valor.get("media", {}).get("id", "")
        return f"📸 Nuevo comentario en Instagram\nAutor: {autor}\n\"{texto}\"\n🔗 Post: {media_id}"
    if field == "mentions":
        media_id = valor.get("media_id", "")
        if valor.get("comment_id"):
            return f"📸 Te mencionaron en un comentario de Instagram\n🔗 Post: {media_id}"
        return f"📸 Te mencionaron en un post de Instagram\n🔗 Post: {media_id}"
    return f"⚠️ Evento de Instagram no reconocido ({field})"


# ---------- Resumen ----------

def recortar(texto, n):
    texto = " ".join((texto or "").split())
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def armar_resumen(snap, titulo=None):
    """Arma el resumen en HTML de Telegram. Devuelve None si no hubo actividad."""
    conteos = snap["conteos"]
    if not any(conteos.values()):
        return None

    esc = html.escape
    bloques = []
    for red, nombre_red, tipos in SECCIONES:
        lineas = []
        for tipo, emoji, singular, plural, fija in tipos:
            n = conteos.get((red, tipo), 0)
            if not (n or fija):
                continue
            linea = f"{emoji} {n} {singular if n == 1 else plural}"
            if red == "facebook" and tipo == "reacciones" and n:
                desglose = " · ".join(
                    f"{EMOJIS_REACCION.get(t, '👍')} {c}"
                    for t, c in snap["tipos_reaccion"].most_common()
                )
                linea += f"  ({desglose})"
            lineas.append(esc(linea))
        if lineas:
            bloques.append(f"<b>{esc(nombre_red)}</b>\n" + "\n".join(lineas))

    titulo = titulo or f"📊 <b>Resumen (últimos {MINUTOS_RESUMEN} min)</b>"
    texto = titulo + "\n\n" + "\n\n".join(bloques)

    comentarios = snap["comentarios"]
    if comentarios:
        nombres_red = {"facebook": "FB", "instagram": "IG"}
        items = []
        largo = len(texto) + 60
        for i, (red, autor, cuerpo, link) in enumerate(comentarios):
            item = f"💬 <b>{esc(autor)}</b> ({nombres_red.get(red, red)}): \"{esc(recortar(cuerpo, LIMITE_TEXTO_COMENTARIO))}\""
            if link:
                item += f'\n<a href="{esc(link, quote=True)}">Ver post</a>'
            if largo + len(item) > LIMITE_TELEGRAM:
                items.append(f"… y {len(comentarios) - i} más")
                break
            items.append(item)
            largo += len(item) + 2
        texto += "\n\n<blockquote expandable>Detalle de comentarios\n\n" + "\n\n".join(items) + "</blockquote>"

    return texto


@app.route("/webhook", methods=["POST"])
def recibir():
    datos = request.json
    try:
        objeto = datos.get("object")
        for entrada in datos.get("entry", []):
            for cambio in entrada.get("changes", []):
                field = cambio.get("field")
                valor = cambio.get("value", {})

                if objeto == "instagram":
                    tipo = clasificar_instagram(field)
                    comentario = None
                    if tipo == "comentarios":
                        comentario = (
                            valor.get("from", {}).get("username", "alguien"),
                            valor.get("text", "(sin texto)"),
                            "",
                        )
                    registrar("instagram", tipo, comentario=comentario)
                    if ALERTAS_INDIVIDUALES:
                        enviar_alerta(armar_mensaje_instagram(field, valor))

                elif objeto == "page" and field == "feed":
                    tipo = clasificar_facebook(valor)
                    if not tipo:
                        continue
                    reaccion = valor.get("reaction_type", "like") if tipo == "reacciones" else None
                    comentario = None
                    if tipo == "comentarios":
                        comentario = (
                            valor.get("from", {}).get("name", "alguien"),
                            valor.get("message", "(sin texto)"),
                            link_facebook(valor),
                        )
                    registrar("facebook", tipo, reaccion=reaccion, comentario=comentario)
                    if ALERTAS_INDIVIDUALES:
                        enviar_alerta(armar_mensaje(valor))
    except Exception as e:
        enviar_alerta(f"❌ Error procesando webhook: {e}")
    return "OK", 200


@app.route("/webhook", methods=["GET"])
def verificar():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Token invalido", 403


@app.route("/resumen", methods=["GET", "POST"])
def resumen():
    global estado
    token = request.args.get("token", "")
    if not RESUMEN_TOKEN or not hmac.compare_digest(token, RESUMEN_TOKEN):
        return "No autorizado", 403

    momento = ahora()

    # Fuera de horario: no se manda nada, se sigue acumulando para el resumen nocturno
    if not en_horario(momento):
        with candado:
            estado["fuera_de_horario"] = True
        return "Fuera de horario", 200

    # Tomar el estado y reiniciar de inmediato, para no perder eventos que lleguen mientras se envía
    with candado:
        snap = estado
        estado = estado_vacio()

    if snap["fuera_de_horario"]:
        titulo = (f"🌙 <b>Resumen nocturno ({snap['inicio']:%H:%M} – {momento:%H:%M})</b>")
    else:
        titulo = None

    texto = armar_resumen(snap, titulo)
    if texto is None:
        return "Sin actividad", 200

    if not enviar_alerta(texto, html_mode=True):
        # Si Telegram falló, se regresa el estado para incluirlo en el siguiente resumen
        with candado:
            estado["conteos"].update(snap["conteos"])
            estado["tipos_reaccion"].update(snap["tipos_reaccion"])
            estado["comentarios"][:0] = snap["comentarios"]
            estado["inicio"] = snap["inicio"]
            estado["fuera_de_horario"] = estado["fuera_de_horario"] or snap["fuera_de_horario"]
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
        <code>{html.escape(code)}</code>
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
            if ALERTAS_INDIVIDUALES:
                titulo = entrada.find("atom:title", NS).text
                link_el = entrada.find("atom:link[@rel='alternate']", NS)
                link = link_el.get("href") if link_el is not None else "sin link"
                enviar_alerta(f"🔔 Nuevo video en YouTube\n\"{titulo}\"\n🔗 {link}")
    except Exception as e:
        enviar_alerta(f"❌ Error procesando YouTube: {e}")
    return "OK", 200


if __name__ == "__main__":
    app.run(port=5000)
