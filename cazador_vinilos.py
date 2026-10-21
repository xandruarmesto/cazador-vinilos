import os
import sys
import json
import html
import re
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
    limite = item.get("max_precio_discogs", item["max_precio"]) if web == "Discogs" else item["max_precio"]
    return (
        f"{emoji} <b>¡Chollo en {web}!</b>\n\n"
        f"<b>Buscado:</b> {e(item['artista'])} - {e(item['disco'])}\n"
        f"<b>Anuncio:</b> {e(str(res['titulo']))}\n"
        f"<b>Precio:</b> {res['precio']:.2f} € (Límite: {limite} €)\n\n"
        f"🔗 <a href=\"{e(res['link'], quote=True)}\">Ver en {web}</a>"
    )


_NO_VINILO = re.compile(
    r"\b(cd|cds|cdr|dvd|blu-?ray|cassette|casete|cassete|musicassetta|musicasetta|cinta|"
    r"camiseta|t-?shirt|sudadera|poster|póster|libro|funko|taza|chapa|puzzle)\b", re.I)


def parece_vinilo(titulo):
    """Descarta CDs, casetes, camisetas, pósters… que salen al buscar 'vinilo X'."""
    return not _NO_VINILO.search(str(titulo))


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
            if slug and 0 < precio <= item["max_precio"] and parece_vinilo(it.get("title", "")):
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
#
# Vinted bloquea las peticiones que "no parecen" un navegador. La librería
# requests se delata por su huella TLS, así que aquí usamos curl_cffi, que
# imita a Chrome. Si no está instalada, se intenta igualmente con requests.

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

_vinted = {"session": None, "bloqueado": False}


def _nueva_sesion_vinted():
    if cffi_requests:
        s = cffi_requests.Session(impersonate="chrome")
    else:
        print("   (curl_cffi no instalado: Vinted probablemente bloqueará la petición)")
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
    s.headers.update({"Accept-Language": "es-ES,es;q=0.9"})
    r = s.get("https://www.vinted.es/", timeout=20)
    cookies = s.cookies.get_dict() if hasattr(s.cookies, "get_dict") else dict(s.cookies)
    if "access_token_web" in cookies:
        print(f"   Vinted: sesión obtenida (HTTP {r.status_code}).")
    else:
        registrar_error("Vinted", f"Sin cookie de sesión al abrir la portada (HTTP {r.status_code}). "
                                  f"Cookies recibidas: {sorted(cookies)}. Inicio de la respuesta: "
                                  f"{r.text[:120]!r}")
    return s


_RE_VINTED = re.compile(
    r'href="/items/(\d+-[^"?]*)\?referrer=catalog"[^>]*?title="([^"]*)"')
_RE_EUROS = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s?€")


def buscar_vinted(item):
    """Vinted retiró /api/v2/catalog/items (da 404). Ahora los resultados
    vienen dentro de la página del catálogo: cada anuncio es un enlace
    /items/... con un title del tipo
    'Nirvana Nevermind, Estado: Muy bueno, 15.00 €, 16.45 €'
    (precio del vendedor y precio con la protección al comprador)."""
    if _vinted["bloqueado"]:
        return []
    busqueda = f"vinilo {item['artista']} {item['disco']}"
    try:
        if _vinted["session"] is None:
            _vinted["session"] = _nueva_sesion_vinted()
        r = _vinted["session"].get(
            "https://www.vinted.es/catalog",
            params={"search_text": busqueda, "order": "newest_first"},
            headers={"Referer": "https://www.vinted.es/"},
            timeout=30)
        if r.status_code != 200:
            registrar_error("Vinted", f"HTTP {r.status_code} buscando '{busqueda}': {r.text[:150]!r}")
            if r.status_code == 403:
                _vinted["bloqueado"] = True
                print("   Vinted bloquea esta IP (403). Se omite Vinted en el resto de la pasada.")
            return []

        vistos_aqui, encontrados, total = set(), [], 0
        for ruta, titulo_attr in _RE_VINTED.findall(r.text):
            if ruta in vistos_aqui:
                continue
            vistos_aqui.add(ruta)
            total += 1
            texto = html.unescape(titulo_attr)
            precios = _RE_EUROS.findall(texto)
            if not precios:
                continue
            precio = a_precio(precios[0].replace(",", "."))
            titulo = re.split(r",\s*(?:Estado|Marca|Talla):", texto)[0]
            if 0 < precio <= item["max_precio"] and parece_vinilo(titulo):
                encontrados.append({"titulo": titulo, "precio": precio,
                                    "link": f"https://www.vinted.es/items/{ruta}"})
        if total == 0 and "/items/" not in r.text:
            registrar_error("Vinted", f"La página de '{busqueda}' no trae anuncios "
                                      "(¿ha vuelto a cambiar el formato de Vinted?)")
        print(f"   Vinted: {total} anuncios, {len(encontrados)} por debajo del límite")
        return encontrados
    except Exception as e:
        registrar_error("Vinted", f"Excepción buscando '{busqueda}': {e!r}")
    return []


# ---------------------------------------------------------------- Discogs (opcional)

# Discogs no deja saber desde qué país vende cada vendedor (ni por la API ni,
# de forma fiable, desde la web), así que filtramos de otra manera:
#  1. Se suma un margen de envío estimado al precio más bajo antes de compararlo.
#  2. Solo cuentan las ediciones cuyo título coincide de verdad con el disco
#     (evita recopilatorios, singles y ediciones de otros discos).
#  3. Se ignoran ediciones con muy pocas copias a la venta (suelen ser caras o raras).
#  4. Solo se avisa de la edición más barata de cada disco.
# En discos.json puedes poner "max_precio_discogs" a un disco concreto
# para usar un límite distinto solo en Discogs (o 0 para no buscarlo allí).
DISCOGS_MARGEN_ENVIO = float(os.environ.get("DISCOGS_MARGEN_ENVIO") or 12)
DISCOGS_MIN_COPIAS = 2


def _normalizar(texto):
    import unicodedata, re
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\(\d+\)", " ", t)          # "Nirvana (2)" -> "Nirvana"
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\bthe\b", " ", t).split()


def _coincide(item, titulo_discogs):
    """Discogs devuelve 'Artista - Título'. Exige que el artista y el título
    buscados estén en el sitio que les corresponde."""
    if " - " not in titulo_discogs:
        return False
    artista_d, disco_d = titulo_discogs.split(" - ", 1)
    a_buscado, d_buscado = _normalizar(item["artista"]), _normalizar(item["disco"])
    a_d, d_d = _normalizar(artista_d), _normalizar(disco_d)
    if not set(a_buscado) <= set(a_d):
        return False
    # El título buscado debe aparecer seguido dentro del de la edición, sin que
    # sea un lote de varios discos ("Nevermind / In Utero") ni añada demasiado.
    if " / " in disco_d or len(d_d) > len(d_buscado) + 3:
        return False
    n = len(d_buscado)
    return any(d_d[i:i + n] == d_buscado for i in range(len(d_d) - n + 1))


def buscar_discogs(item):
    """La API de Discogs no permite listar anuncios, pero sí da el precio
    más bajo en venta de cada edición. Necesita el secret DISCOGS_TOKEN
    (discogs.com/settings/developers → Generate new token)."""
    if not DISCOGS_TOKEN:
        return []
    limite = item.get("max_precio_discogs", item["max_precio"])
    if not limite:
        return []
    headers = {"User-Agent": "CazadorVinilos/1.0",
               "Authorization": f"Discogs token={DISCOGS_TOKEN}"}
    try:
        r = requests.get("https://api.discogs.com/database/search", headers=headers, timeout=15,
                         params={"artist": item["artista"], "release_title": item["disco"],
                                 "format": "Vinyl", "type": "release", "per_page": 15})
        if r.status_code != 200:
            registrar_error("Discogs", f"HTTP {r.status_code}: {r.text[:150]}")
            return []
        resultados = r.json().get("results", [])
        validos = [rel for rel in resultados if _coincide(item, rel.get("title", ""))]
        encontrados = []
        for rel in validos:
            time.sleep(1.1)  # límite: 60 peticiones/minuto
            st = requests.get(f"https://api.discogs.com/marketplace/stats/{rel['id']}",
                              headers=headers, params={"curr_abbr": "EUR"}, timeout=15)
            if st.status_code != 200:
                continue
            datos = st.json()
            if (datos.get("num_for_sale") or 0) < DISCOGS_MIN_COPIAS:
                continue
            lp = datos.get("lowest_price") or {}
            precio = a_precio(lp.get("value"))
            if lp.get("currency") == "EUR" and 0 < precio and precio + DISCOGS_MARGEN_ENVIO <= limite:
                extra = " · ".join(x for x in [rel.get("country"), str(rel.get("year") or "")] if x)
                encontrados.append({
                    "titulo": f"{rel.get('title', '')} ({extra}) · {datos.get('num_for_sale')} en venta "
                              f"· con envío estimado ≈ {precio + DISCOGS_MARGEN_ENVIO:.0f} €",
                    "precio": precio,
                    "link": f"https://www.discogs.com/sell/release/{rel['id']}?sort=price%2Casc",
                })
        encontrados.sort(key=lambda x: x["precio"])
        print(f"   Discogs: {len(resultados)} ediciones encontradas, {len(validos)} del disco correcto, "
              f"{len(encontrados)} bajo el límite con envío")
        return encontrados[:1]  # solo la más barata
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
