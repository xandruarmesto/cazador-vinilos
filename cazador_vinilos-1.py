import os
import sys
import json
import html
import time
import requests

TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
DISCOGS_TOKEN = (os.environ.get("DISCOGS_TOKEN") or "").strip()  # opcional

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ARCHIVO_VISTOS = "vistos.json"
DIAS_MEMORIA = 60          # se olvidan los anuncios pasados estos días
MAX_AVISOS_POR_WEB = 3     # máximo de anuncios nuevos por disco y web en cada pasada

# Contadores para el resumen final
stats = {"ok": 0, "fallos_telegram": 0, "errores_web": {}}


def registrar_error(web, detalle):
    stats["errores_web"].setdefault(web, []).append(detalle)
    print(f"⚠️  [{web}] {detalle}")


def cargar_lista_discos(filename="discos.json"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error al cargar {filename}: {e}")
        return []


# ---------------------------------------------------------------- Memoria

def cargar_vistos():
    try:
        with open(ARCHIVO_VISTOS, "r", encoding="utf-8") as f:
            vistos = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        vistos = {}
    limite = time.time() - DIAS_MEMORIA * 86400
    return {k: v for k, v in vistos.items() if v.get("ts", 0) >= limite}


def guardar_vistos(vistos):
    with open(ARCHIVO_VISTOS, "w", encoding="utf-8") as f:
        json.dump(vistos, f, ensure_ascii=False, indent=1, sort_keys=True)


def es_nuevo(vistos, res):
    """Nuevo = no avisado nunca, o ha bajado de precio desde el último aviso."""
    previo = vistos.get(res["link"])
    return previo is None or res["precio"] < previo.get("precio", 0)


# ---------------------------------------------------------------- Telegram

def enviar_telegram(mensaje):
    """Envía en HTML (más robusto que Markdown: un '_' o '*' en el título
    de un anuncio ya no rompe el mensaje)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            stats["ok"] += 1
            print("✅ Mensaje enviado a Telegram.")
            return True
        print(f"❌ Error de Telegram ({res.status_code}): {res.text}")
        if res.status_code == 429:  # demasiados mensajes seguidos
            espera = res.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(espera + 1)
            return enviar_telegram(mensaje)
    except Exception as e:
        print(f"❌ Excepción conectando a Telegram: {e}")
    stats["fallos_telegram"] += 1
    return False


def comprobar_telegram():
    """Diagnóstico completo del bot antes de buscar nada."""
    print("\n========== DIAGNÓSTICO TELEGRAM ==========")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Faltan los secrets TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID en GitHub "
              "(Settings → Secrets and variables → Actions → pestaña 'Secrets', no 'Variables').")
        print(f"   TELEGRAM_TOKEN presente: {bool(TELEGRAM_TOKEN)} | TELEGRAM_CHAT_ID presente: {bool(TELEGRAM_CHAT_ID)}")
        sys.exit(1)
    if ":" not in TELEGRAM_TOKEN:
        print("❌ El token no tiene el formato de BotFather (123456789:AA...). ¿Has pegado otra cosa?")
        sys.exit(1)
    if TELEGRAM_TOKEN.lower().startswith("bot"):
        print("❌ El token empieza por 'bot'. Quita ese prefijo: el script ya lo añade.")
        sys.exit(1)

    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    r = requests.get(f"{api}/getMe", timeout=15)
    if r.status_code != 200:
        print(f"❌ Token NO válido ({r.status_code}): {r.text}")
        print("   Pide uno nuevo a @BotFather (/mybots → API Token) y actualiza el secret.")
        sys.exit(1)
    bot = r.json()["result"]["username"]
    print(f"✅ Token válido. Bot: @{bot}")
    print(f"   chat_id configurado: {TELEGRAM_CHAT_ID}")

    r = requests.get(f"{api}/getChat", params={"chat_id": TELEGRAM_CHAT_ID}, timeout=15)
    if r.status_code == 200:
        chat = r.json()["result"]
        nombre = chat.get("title") or chat.get("first_name") or chat.get("username")
        print(f"✅ El bot puede ver ese chat ({chat.get('type')}: {nombre}).")
        return

    print(f"❌ El bot NO puede escribir en ese chat_id ({r.status_code}): {r.text}")
    print("   Causas típicas: chat_id equivocado, o nunca le has enviado /start al bot,")
    print("   o es un grupo y le falta el '-' (o el '-100') delante del número.")
    print(f"   👉 Abre Telegram, busca @{bot}, pulsa INICIAR / envía /start y vuelve a lanzar el workflow.")

    # Intentamos descubrir el chat_id correcto a partir de los últimos mensajes recibidos
    wh = requests.get(f"{api}/getWebhookInfo", timeout=15).json().get("result", {})
    if wh.get("url"):
        print(f"   ⚠️ El bot tiene un webhook activo ({wh['url']}); eso impide leer mensajes con getUpdates.")
    up = requests.get(f"{api}/getUpdates", timeout=15).json()
    chats = {}
    for u in up.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat")
        if c:
            chats[c["id"]] = f"{c.get('type')}: {c.get('title') or c.get('first_name') or c.get('username')}"
    if chats:
        print("   Chats que han escrito recientemente al bot (usa uno de estos como TELEGRAM_CHAT_ID):")
        for cid, desc in chats.items():
            print(f"     • {cid}  ({desc})")
    else:
        print("   El bot no ha recibido ningún mensaje reciente. Envíale /start y relanza.")
    sys.exit(1)


def formatear(web, emoji, item, res):
    e = html.escape
    return (
        f"{emoji} <b>¡Chollo en {web}!</b>\n\n"
        f"<b>Buscado:</b> {e(item['artista'])} - {e(item['disco'])}\n"
        f"<b>Anuncio:</b> {e(str(res['titulo']))}\n"
        f"<b>Precio:</b> {res['precio']:.2f} € (Límite: {item['max_precio']} €)\n\n"
        f"🔗 <a href=\"{e(res['link'], quote=True)}\">Ver en {web}</a>"
    )


def a_precio(valor):
    """Wallapop y Vinted devuelven el precio como número o como
    {'amount': ..., 'currency': ...}. Esto acepta ambos formatos."""
    if isinstance(valor, dict):
        valor = valor.get("amount")
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- Wallapop

def buscar_wallapop(item):
    busqueda = f"vinilo {item['artista']} {item['disco']}"
    url = "https://api.wallapop.com/api/v3/search"
    params = {
        "keywords": busqueda,
        "source": "search_box",
        "order_by": "newest",
        # Centro de España; sin coordenadas la API a veces no devuelve nada
        "latitude": 40.4168,
        "longitude": -3.7038,
    }
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9",
        "Origin": "https://es.wallapop.com",
        "Referer": "https://es.wallapop.com/",
        "X-DeviceOS": "0",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code != 200:
            registrar_error("Wallapop", f"HTTP {r.status_code} buscando '{busqueda}': {r.text[:150]}")
            return []
        data = r.json()
        # Formato actual: data.section.payload.items  /  formato antiguo: search_objects
        objetos = (data.get("data", {}).get("section", {}).get("payload", {}).get("items")
                   or data.get("search_objects")
                   or [])
        encontrados = []
        for obj in objetos:
            it = obj.get("item", obj)  # en el formato antiguo venía envuelto en "item"
            precio = a_precio(it.get("price"))
            slug = it.get("web_slug")
            if slug and 0 < precio <= item["max_precio"]:
                encontrados.append({
                    "titulo": it.get("title", ""),
                    "precio": precio,
                    "link": f"https://es.wallapop.com/item/{slug}",
                })
        print(f"   Wallapop: {len(objetos)} anuncios, {len(encontrados)} por debajo del límite")
        return encontrados
    except Exception as e:
        registrar_error("Wallapop", f"Excepción buscando '{busqueda}': {e!r}")
    return []


# ---------------------------------------------------------------- Vinted

_vinted_session = None


def sesion_vinted():
    """Vinted exige la cookie 'access_token_web' que da la portada.
    Se pide una sola vez y se reutiliza para todas las búsquedas."""
    global _vinted_session
    if _vinted_session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9",
                          "Accept": "application/json, text/plain, */*"})
        r = s.get("https://www.vinted.es", timeout=15)
        if "access_token_web" not in s.cookies:
            registrar_error("Vinted", f"No se obtuvo la cookie de sesión (HTTP {r.status_code}); "
                                      "probablemente Vinted bloquea la IP de GitHub")
        _vinted_session = s
    return _vinted_session


def buscar_vinted(item):
    busqueda = f"vinilo {item['artista']} {item['disco']}"
    try:
        s = sesion_vinted()
        r = s.get("https://www.vinted.es/api/v2/catalog/items",
                  params={"search_text": busqueda, "order": "newest_first", "per_page": 30},
                  timeout=15)
        if r.status_code != 200:
            registrar_error("Vinted", f"HTTP {r.status_code} buscando '{busqueda}': {r.text[:150]}")
            return []
        objetos = r.json().get("items", [])
        encontrados = []
        for obj in objetos:
            precio = a_precio(obj.get("price"))
            url_item = obj.get("url")
            if url_item and 0 < precio <= item["max_precio"]:
                encontrados.append({"titulo": obj.get("title", ""), "precio": precio, "link": url_item})
        print(f"   Vinted: {len(objetos)} anuncios, {len(encontrados)} por debajo del límite")
        return encontrados
    except Exception as e:
        registrar_error("Vinted", f"Excepción buscando '{busqueda}': {e!r}")
    return []


# ---------------------------------------------------------------- Discogs (opcional)

def buscar_discogs(item):
    """La API de Discogs no permite listar anuncios, pero sí da el precio
    más bajo en venta de cada edición. Necesita el secret DISCOGS_TOKEN
    (discogs.com/settings/developers → Generate new token)."""
    if not DISCOGS_TOKEN:
        return []
    headers = {"User-Agent": "CazadorVinilos/1.0",
               "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
    try:
        r = requests.get("https://api.discogs.com/database/search", headers=headers, timeout=15,
                         params={"artist": item["artista"], "release_title": item["disco"],
                                 "format": "Vinyl", "type": "release", "per_page": 10})
        if r.status_code != 200:
            registrar_error("Discogs", f"HTTP {r.status_code}: {r.text[:150]}")
            return []
        encontrados = []
        for rel in r.json().get("results", []):
            time.sleep(1.1)  # límite: 60 peticiones/minuto
            st = requests.get(f"https://api.discogs.com/marketplace/stats/{rel['id']}",
                              headers=headers, params={"curr_abbr": "EUR"}, timeout=15)
            if st.status_code != 200:
                continue
            lp = st.json().get("lowest_price") or {}
            precio = a_precio(lp.get("value"))
            if lp.get("currency") == "EUR" and 0 < precio <= item["max_precio"]:
                encontrados.append({"titulo": rel.get("title", ""), "precio": precio,
                                    "link": f"https://www.discogs.com/sell/release/{rel['id']}"})
        encontrados.sort(key=lambda x: x["precio"])
        print(f"   Discogs: {len(encontrados)} ediciones por debajo del límite")
        return encontrados
    except Exception as e:
        registrar_error("Discogs", f"Excepción: {e!r}")
    return []


# ---------------------------------------------------------------- main

def main():
    print("Iniciando rastreo de vinilos...")
    comprobar_telegram()

    if os.environ.get("TEST_TELEGRAM") == "1":
        enviar_telegram("🧪 Mensaje de prueba del Cazador de Vinilos: Telegram funciona.")

    discos = cargar_lista_discos()
    if not discos:
        print("Lista vacía o inválida.")
        sys.exit(1)

    webs = [("Wallapop", "🎵", buscar_wallapop), ("Vinted", "🛍️", buscar_vinted)]
    if DISCOGS_TOKEN:
        webs.append(("Discogs", "💿", buscar_discogs))
    print(f"Rastreando {len(discos)} discos en {', '.join(w[0] for w in webs)}...")

    vistos = cargar_vistos()
    print(f"Memoria: {len(vistos)} anuncios ya avisados en los últimos {DIAS_MEMORIA} días.")

    try:
        for item in discos:
            print(f"\n🔎 {item['artista']} - {item['disco']} (≤ {item['max_precio']} €)")
            for nombre, emoji, buscar in webs:
                resultados = sorted(buscar(item), key=lambda x: x["precio"])
                nuevos = [r for r in resultados if es_nuevo(vistos, r)]
                if resultados:
                    print(f"   {nombre}: {len(nuevos)} nuevos de {len(resultados)}")
                for res in nuevos[:MAX_AVISOS_POR_WEB]:
                    previo = vistos.get(res["link"])
                    msg = formatear(nombre, emoji, item, res)
                    if previo:
                        msg = "📉 <b>¡Ha bajado de precio!</b> (antes " + f"{previo['precio']:.2f} €)\n" + msg
                    if enviar_telegram(msg):
                        vistos[res["link"]] = {"precio": res["precio"], "ts": time.time()}
                time.sleep(1)  # no saturar las webs
    finally:
        guardar_vistos(vistos)  # se guarda aunque algo falle a mitad

    # ---- Resumen: si algo ha fallado, el workflow sale en ROJO en GitHub
    print("\n========== RESUMEN ==========")
    print(f"Mensajes enviados: {stats['ok']}  |  Fallos de Telegram: {stats['fallos_telegram']}")
    for web, errores in stats["errores_web"].items():
        print(f"{web}: {len(errores)} errores (primero: {errores[0]})")

    fallo_total = [w for w, e in stats["errores_web"].items() if len(e) >= len(discos)]
    if stats["fallos_telegram"] or fallo_total:
        if fallo_total:
            enviar_telegram("⚠️ Cazador de Vinilos: no se ha podido consultar "
                            + ", ".join(fallo_total) + ". Revisa el log de GitHub Actions.")
        sys.exit(1)


if __name__ == "__main__":
    main()
