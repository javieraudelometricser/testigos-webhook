import requests

CANAL_YOUTUBE = "UCmYx6HZpnFV5LgiOgZ4SWzA"
CALLBACK_URL = "https://testigos-webhook.onrender.com/youtube_callback"

def suscribir():
    hub_url = "https://pubsubhubbub.appspot.com/subscribe"
    topic_url = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={CANAL_YOUTUBE}"

    datos = {
        "hub.callback": CALLBACK_URL,
        "hub.topic": topic_url,
        "hub.verify": "async",
        "hub.mode": "subscribe",
    }

    respuesta = requests.post(hub_url, data=datos)
    print(respuesta.status_code, respuesta.text)

if __name__ == "__main__":
    suscribir()