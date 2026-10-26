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

# Gran chollo: precio igual o inferior a este porcentaje del límite (0.6 = 60 %).
# Se puede cambiar por disco en discos.json con "gran_chollo": 12 (euros).
UMBRAL_GRAN_CHOLLO = float(os.environ.get("UMBRAL_GRAN_CHOLLO") or 0.6)
# Los avisos normales llegan sin sonido; los grandes chollos, con sonido.
AVISOS_NORMALES_SILENCIOSOS = os.environ.get("AVISOS_NORMALES_SILENCIOSOS", "1") == "1"

# Modo ahorro: si compras algo, el resto del mes solo te avisa de grandes chollos.
MODO_AHORRO = os.environ.get("MODO_AHORRO", "1") == "1"
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]


def hoy():
    """Fecha de hoy en hora de Madrid (GitHub trabaja en UTC)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def compras_del_mes(discos):
    mes = hoy().strftime("%Y-%m")
    return [d for d in discos if str(d.get("comprado", "")).startswith(mes)]


def aviso_modo_ahorro():
    h = hoy()
    siguiente = _MESES[h.month % 12]
    return (f"\n🧘 <b>Modo ahorro</b> hasta el 1 de {siguiente}: "
            "solo te avisaré de los grandes chollos.")


# Contadores para el resumen final
stats = {"ok": 0, "fallos_telegram": 0, "errores_web": {}}


def sin_secretos(texto):
    """Quita los tokens de cualquier texto que vaya al log (el log es público
    si el repositorio lo es). GitHub ya los oculta, pero así no dependemos de ello."""
    texto = str(texto)
    for secreto in (TELEGRAM_TOKEN, DISCOGS_TOKEN, TELEGRAM_CHAT_ID):
        if secreto:
            texto = texto.replace(secreto, "***")
    return texto


def registrar_error(web, detalle):
    detalle = sin_secretos(detalle)
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

def enviar_telegram(mensaje, silencioso=False, responder_a=None):
    """Envía en HTML (más robusto que Markdown: un '_' o '*' en el título
    de un anuncio ya no rompe el mensaje). Con silencioso=True el mensaje
    llega sin sonido ni vibración."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
        "disable_notification": silencioso,
    }
    if responder_a:
        payload["reply_parameters"] = {"message_id": responder_a, "allow_sending_without_reply": True}
    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            stats["ok"] += 1
            print("✅ Mensaje enviado a Telegram.")
            return True
        print(f"❌ Error de Telegram ({res.status_code}): {sin_secretos(res.text)}")
        if res.status_code == 429:  # demasiados mensajes seguidos
            espera = res.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(espera + 1)
            return enviar_telegram(mensaje, silencioso, responder_a)
    except Exception as e:
        print(f"❌ Excepción conectando a Telegram: {sin_secretos(e)}")
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
        print(f"❌ Token NO válido ({r.status_code}): {sin_secretos(r.text)}")
        print("   Pide uno nuevo a @BotFather (/mybots → API Token) y actualiza el secret.")
        sys.exit(1)
    print("✅ Token válido.")   # sin nombre del bot ni chat_id: el log puede ser público

    r = requests.get(f"{api}/getChat", params={"chat_id": TELEGRAM_CHAT_ID}, timeout=15)
    if r.status_code == 200:
        print("✅ El bot puede escribir en tu chat.")
        return

    print(f"❌ El bot NO puede escribir en ese chat_id ({r.status_code}): {sin_secretos(r.text)}")
    print("   Causas típicas: chat_id equivocado, o nunca le has enviado /start al bot,")
    print("   o es un grupo y le falta el '-' (o el '-100') delante del número.")
    print("   👉 Abre Telegram, busca tu bot, pulsa INICIAR / envía /start y vuelve a lanzar el workflow.")

    # Intentamos descubrir el chat_id correcto a partir de los últimos mensajes recibidos
    wh = requests.get(f"{api}/getWebhookInfo", timeout=15).json().get("result", {})
    if wh.get("url"):
        print("   ⚠️ El bot tiene un webhook activo; eso impide leer mensajes con getUpdates.")
    # No se muestran los chat_id ni los nombres de quien escribe al bot (el log puede ser público).
    print("   Para ver tu chat_id: envía /start al bot y abre en el navegador")
    print("   https://api.telegram.org/bot<TU_TOKEN>/getUpdates  →  busca \"chat\":{\"id\": …")
    sys.exit(1)


def limite_de(web, item):
    return item.get("max_precio_discogs", item["max_precio"]) if web == "Discogs" else item["max_precio"]


def es_gran_chollo(web, item, res):
    """Gran chollo = precio (con el envío estimado, en Discogs) muy por debajo
    del límite: por defecto, el 60 % o menos. En discos.json se puede fijar
    un umbral propio en euros con "gran_chollo"."""
    precio = res.get("precio_total", res["precio"])
    umbral = item.get("gran_chollo", UMBRAL_GRAN_CHOLLO * limite_de(web, item))
    return round(precio, 2) <= round(umbral, 2)


def formatear(web, emoji, item, res):
    e = html.escape
    limite = limite_de(web, item)
    precio = res.get("precio_total", res["precio"])
    if es_gran_chollo(web, item, res):
        rebaja = round(100 * (1 - precio / limite)) if limite else 0
        cabecera = (f"🚨🔥 <b>¡GRAN CHOLLO en {web.upper()}!</b> 🔥🚨\n"
                    f"<b>{rebaja} % por debajo de tu límite</b>")
    else:
        cabecera = f"{emoji} <b>¡Chollo en {web}!</b>"
    return (
        f"{cabecera}\n\n"
        f"<b>Buscado:</b> {e(item['artista'])} - {e(item['disco'])}\n"
        f"<b>Anuncio:</b> {e(str(res['titulo']))}\n"
        f"<b>Precio:</b> {res['precio']:.2f} € (Límite: {limite} €)\n\n"
        f"🔗 <a href=\"{e(res['link'], quote=True)}\">Ver en {web}</a>"
    )


_NO_VINILO = re.compile(
    r"\b(cd|cds|cdr|dvd|blu-?ray|cassette|casete|cassete|musicassetta|musicasetta|cinta|"
    r"camiseta|t-?shirt|sudadera|poster|póster|libro|funko|taza|chapa|puzzle)\b", re.I)


# Formatos que NO son un LP: singles y EPs de 7", 10", maxis, 45 rpm…
# Ojo: "singles" en plural (p. ej. "The Singles", un recopilatorio) NO se descarta.
_NO_LP = re.compile(
    r"(?<![\d.,])(7|10)\s?(\"|”|″|''|’’|pulgadas|pulg\.?|inch(es)?|in)(?![\w])"
    r"|\b45\s?(rpm|r\.p\.m\.?|vueltas|rev|giri|tours?|t|u/?min|upm|toeren)\b"
    r"|\b(single|sencillo|singolo|maxi|maxisingle|maxi-single|ep|flexi|flexidisc)\b",
    re.I)


def parece_vinilo(titulo, item=None):
    """Descarta CDs, casetes, camisetas, pósters… y también singles/EPs
    (7", 10", 45 rpm, "single", "maxi"…), salvo que en discos.json el disco
    lleve "formato": "single"."""
    titulo = str(titulo)
    if _NO_VINILO.search(titulo):
        return False
    if item is not None and str(item.get("formato", "LP")).lower() == "single":
        return True
    return not _NO_LP.search(titulo)


_NUMEROS = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
            "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten"}
_RELLENO = {"vinilo", "vinyl", "vinile", "vinyle", "lp", "disco", "album"}


def _titulo_corto_ok(palabras, disco, artista):
    """Para discos de una sola palabra ("1", "Legend", "Casa") no basta con que
    la palabra aparezca en cualquier parte: tiene que ir pegada al artista
    ("Beatles 1", "Bob Marley - Legend", "Casa Iván Ferreiro").
    Si es un número, solo vale justo después del artista, para que no cuelen
    "1 vinilo de los Beatles 1969" y similares."""
    numero = disco.isdigit()
    sin_relleno = [w for w in palabras if w not in _RELLENO]
    pos_artista = [i for i, w in enumerate(sin_relleno) if w in artista]
    if not pos_artista:
        return False
    ini, fin = min(pos_artista), max(pos_artista)
    if disco in sin_relleno[fin + 1:fin + 2]:
        return True
    if not numero and disco in sin_relleno[max(0, ini - 1):ini]:
        return True
    return False


def _menciona_artista(palabras, item):
    """El anuncio tiene que nombrar al artista: basta su palabra más larga
    (normalmente el apellido: "Marley", "Fitzgerald"), el nombre junto
    ("R.E.M." -> "rem") o sus iniciales ("CCR"). En discos.json se pueden
    añadir otros nombres con "alias_artista": ["CCR", "Creedence"]."""
    t = set(palabras)
    juntas = "".join(palabras)
    for nombre in [item["artista"]] + list(item.get("alias_artista", [])):
        a = _normalizar(nombre)
        if not a:
            continue
        if max(a, key=len) in t or "".join(a) in juntas:
            return True
        if len(a) >= 2 and "".join(w[0] for w in a) in t:
            return True
    return False


def es_relevante(item, titulo):
    """Vinted y Wallapop devuelven cualquier cosa que se parezca a la búsqueda.
    Exigimos que el anuncio nombre el disco (todas sus palabras si es corto,
    al menos el 75 % si es largo) y al artista. Los títulos de una sola
    palabra tienen que ir junto al artista. En discos.json se puede afinar cada disco con:
      "alias":   ["One"]                 otros nombres con los que se anuncia
      "excluir": ["1962", "1967", "box"] palabras que descartan el anuncio"""
    palabras = _normalizar(titulo)
    t = set(palabras)
    artista = _normalizar(item["artista"])

    excluir = {w for x in item.get("excluir", []) for w in _normalizar(x)}
    if t & excluir:
        return False
    if not _menciona_artista(palabras, item):
        return False   # p. ej. "Best of UB40 - Greatest Hits" buscando Queen

    variantes = [item["disco"]] + list(item.get("alias", []))
    if item["disco"].strip() in _NUMEROS:
        variantes.append(_NUMEROS[item["disco"].strip()])

    for variante in variantes:
        disco = _normalizar(variante)
        if not disco:
            continue
        if len(disco) == 1 and disco != artista:
            nombres = set(artista)
            for n in [item["artista"]] + list(item.get("alias_artista", [])):
                a = _normalizar(n)
                nombres |= set(a)
                if len(a) >= 2:
                    nombres.add("".join(w[0] for w in a))  # iniciales: CCR, RHCP…
            if _titulo_corto_ok(palabras, disco[0], nombres):
                return True
            continue
        aciertos = sum(1 for w in disco if w in t)
        necesarios = len(disco) if len(disco) <= 3 else 0.75 * len(disco)
        if aciertos >= necesarios:
            return True
    return False


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
            if slug and 0 < precio <= item["max_precio"] and parece_vinilo(it.get("title", ""), item) \
                    and es_relevante(item, it.get("title", "")):
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
            if 0 < precio <= item["max_precio"] and parece_vinilo(titulo, item) and es_relevante(item, titulo):
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
        quiere_single = str(item.get("formato", "LP")).lower() == "single"

        def formato_ok(rel):
            fmts = {str(f).lower() for f in rel.get("format", [])}
            es_single = bool(fmts & {'7"', '10"', "single", "ep", "maxi-single", "45 rpm"})
            if quiere_single:
                return es_single
            return "lp" in fmts and not es_single

        validos = [rel for rel in resultados
                   if _coincide(item, rel.get("title", "")) and formato_ok(rel)]
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
                    "precio_total": precio + DISCOGS_MARGEN_ENVIO,
                    "link": f"https://www.discogs.com/sell/release/{rel['id']}?sort=price%2Casc",
                })
        encontrados.sort(key=lambda x: x["precio"])
        print(f"   Discogs: {len(resultados)} ediciones encontradas, {len(validos)} del disco correcto, "
              f"{len(encontrados)} bajo el límite con envío")
        return encontrados[:1]  # solo la más barata
    except Exception as e:
        registrar_error("Discogs", f"Excepción: {e!r}")
    return []


# ---------------------------------------------------------------- Comprados
#
# Para quitar un disco de la búsqueda cuando lo compras:
#   · en Telegram, RESPONDE a cualquier aviso de ese disco con la palabra "comprado"
#     (o escribe "/comprado Artista Disco", vale aproximado: "nirvana nevermin"),
#     y en la siguiente pasada se marca solo;
#   · o en discos.json añade a mano "comprado": "2026-10-22" a ese disco.
# Los discos comprados se quedan en la lista (como registro) pero ya no se buscan.
# Para volver a buscarlo, borra la línea "comprado" o responde "descomprado".
# Puedes añadir lo que pagaste: "comprado 15€".
# Otras órdenes para el bot:
#   /registrar Artista - Disco 12€   apunta como comprado un disco que no estaba
#                                    en la lista (p. ej. uno que cayó en un pack)
#   /amestar Artista - Disco - 15    añade un disco a la búsqueda (también /buscar)
#   /llista                          lista los discos que se buscan (/llista compraos: los comprados)
#   /axuda                           recuerda todas las órdenes

def guardar_discos(discos, filename="discos.json"):
    def fmt(o):
        lineas = [f"    {json.dumps(k, ensure_ascii=False)}: "
                  f"{json.dumps(v, ensure_ascii=False).replace(chr(34) + ',' + chr(34), chr(34) + ', ' + chr(34))}"
                  for k, v in o.items()]
        return "  {\n" + ",\n".join(lineas) + "\n  }"
    import unicodedata

    def clave(texto):
        return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    discos.sort(key=lambda d: (clave(d["artista"]), clave(d["disco"])))  # orden alfabético
    with open(filename, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(fmt(d) for d in discos) + "\n]\n")


def _separar_precio(texto, exigir_euro=False):
    """Separa un precio escrito al final: 'Nirvana - Nevermind 15€' -> ('Nirvana - Nevermind', 15.0).
    Un número sin € solo cuenta como precio si no es el propio título ("Beatles - 1")
    y, con exigir_euro=True, solo si lleva € o "eur"."""
    m = re.search(r"\s+(\d+(?:[.,]\d{1,2})?)\s*(€|eur|euros?)?\s*$", texto, re.I)
    if not m:
        return texto, None
    resto = texto[:m.start()].strip()
    # "Los Piratas - Ultrasónica - 15": el último guion separa el precio,
    # siempre que quede "Artista - Disco" delante (si no, "Beatles - 1" es el título)
    if resto.endswith("-"):
        sin_guion = resto[:-1].strip()
        if " - " in sin_guion:
            valor = float(m.group(1).replace(",", "."))
            return sin_guion, int(valor) if valor.is_integer() else valor
    valor = float(m.group(1).replace(",", "."))
    valor = int(valor) if valor.is_integer() else valor   # 25 en vez de 25.0 en discos.json
    if m.group(2):
        return resto, valor
    if exigir_euro or resto.endswith("-"):
        return texto, None
    return resto, valor


def alta_desde_telegram(orden, texto, discos, por_nombre, message_id):
    """/registrar Artista - Disco [precio]  -> lo apunta como comprado (fuera de lista)
       /buscar    Artista - Disco precio    -> lo añade a la búsqueda con ese límite"""
    cuerpo = texto.split(" ", 1)[1].strip() if " " in texto else ""
    cuerpo, precio = _separar_precio(cuerpo)
    if " - " not in cuerpo:
        ejemplo = "/registrar Bob Dylan - Blonde on Blonde 12€" if orden == "registrar" \
            else f"/{orden} Los Piratas - Ultrasónica - 15"
        enviar_telegram(f"✍️ Escríbelo así, con un guion entre artista y disco:\n{ejemplo}",
                        responder_a=message_id)
        return False
    artista, titulo = (x.strip() for x in cuerpo.split(" - ", 1))
    existente = por_nombre.get(f"{artista} - {titulo}".lower())
    if orden in ("buscar", "amestar"):
        if not precio:
            enviar_telegram("💶 Dime también el precio máximo, p. ej.:\n"
                            f"/{orden} {html.escape(artista)} - {html.escape(titulo)} - 20",
                            responder_a=message_id)
            return False
        if existente:
            existente["max_precio"] = precio
            existente.pop("comprado", None)
            aviso = f"🔁 Ya estaba en la lista: ahora lo busco con límite de {precio:g} €."
        else:
            nuevo = {"artista": artista, "disco": titulo, "max_precio": precio}
            discos.append(nuevo)
            por_nombre[f"{artista} - {titulo}".lower()] = nuevo
            aviso = f"🔎 Añadido a la búsqueda con límite de {precio:g} €."
    else:  # registrar
        d = existente or {"artista": artista, "disco": titulo, "max_precio": 0, "fuera_de_lista": True}
        d["comprado"] = hoy().isoformat()
        if precio is not None:
            d["precio_pagado"] = precio
        if not existente:
            discos.append(d)
            por_nombre[f"{artista} - {titulo}".lower()] = d
        aviso = ("📀 Apuntado en tu registro de comprados." + (f" Pagado: {precio:g} €." if precio else "")
                 + (aviso_modo_ahorro() if MODO_AHORRO else ""))
    enviar_telegram(f"<b>{html.escape(artista)} - {html.escape(titulo)}</b>\n{aviso}",
                    responder_a=message_id)
    return True


AYUDA_BOT = (
    "🎵 <b>Órdenes del Cazador de Vinilos</b>\n\n"
    "/llista — discos que estoy buscando\n"
    "/llista compraos — discos que ya compraste\n"
    "/amestar Artista - Disco - 15 — añadir un disco a la búsqueda (o cambiar su límite)\n"
    "comprado — respondiendo a un aviso; también «comprado 15» con lo que pagaste\n"
    "/comprado Artista Disco — marcar como comprado (vale aproximado)\n"
    "/registrar Artista - Disco 12€ — apuntar una compra que no estaba en la lista\n"
    "descomprado — respondiendo a la confirmación, para volver a buscarlo\n\n"
    "⏱️ Leo los mensajes en cada pasada, así que la respuesta puede tardar un rato."
)


def enviar_lista(discos, texto, message_id):
    """/llista -> discos que se buscan; /llista compraos -> los comprados."""
    e = html.escape
    comprados = any(w in texto.lower() for w in ("comprad", "comprao", "mercad", "mercao"))
    if comprados:
        sel = [d for d in discos if d.get("comprado")]
        titulo = f"📀 <b>Comprados ({len(sel)})</b>"
        linea = lambda d: (f"• {e(d['artista'])} – <i>{e(d['disco'])}</i> · {e(str(d['comprado']))}"
                           + (f" · {d['precio_pagado']:g} €" if d.get("precio_pagado") is not None else ""))
    else:
        sel = [d for d in discos if not d.get("comprado")]
        titulo = f"🔎 <b>Estoy buscando {len(sel)} discos</b>"
        linea = lambda d: f"• {e(d['artista'])} – <i>{e(d['disco'])}</i> · ≤ {d['max_precio']:g} €"
    if not sel:
        enviar_telegram(titulo + "\n\n(ninguno)", responder_a=message_id)
        return
    import unicodedata
    clave = lambda t: unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()
    sel = sorted(sel, key=lambda d: (clave(d["artista"]), clave(d["disco"])))
    # Telegram admite 4096 caracteres por mensaje: se trocea si hace falta
    trozos, actual = [], titulo + "\n"
    for d in sel:
        l = "\n" + linea(d)
        if len(actual) + len(l) > 3800:
            trozos.append(actual)
            actual = ""
        actual += l
    trozos.append(actual)
    for i, t in enumerate(trozos):
        enviar_telegram(t, responder_a=message_id if i == 0 else None)


def buscar_aproximado(texto, discos):
    """Encuentra el disco aunque lo escribas a medias o con erratas:
    "nirvana nevermin", "Nevermind", "police synchronicity"…
    Devuelve (disco, None) si está claro, (None, [candidatos]) si duda
    y (None, None) si no se parece a ninguno."""
    from difflib import SequenceMatcher
    q = " ".join(_normalizar(texto))
    if not q:
        return None, None
    # Solo el artista ("nirvana", "the police"): preguntamos cuál de sus discos
    del_artista = [d for d in discos if " ".join(_normalizar(d["artista"])) == q]
    if del_artista:
        return (del_artista[0], None) if len(del_artista) == 1 else (None, del_artista)
    puntuados = []
    for d in discos:
        completo = " ".join(_normalizar(f"{d['artista']} {d['disco']}"))
        solo_disco = " ".join(_normalizar(d["disco"]))
        p = max(SequenceMatcher(None, q, completo).ratio(),
                SequenceMatcher(None, q, solo_disco).ratio())
        puntuados.append((p, d))
    puntuados.sort(key=lambda x: -x[0])
    mejor, d1 = puntuados[0]
    segundo = puntuados[1][0] if len(puntuados) > 1 else 0
    if mejor < 0.7:
        return None, None
    if mejor - segundo < 0.08:
        return None, [d for p, d in puntuados[:4] if p >= mejor - 0.08]
    return d1, None


def procesar_comandos(discos):
    """Lee los mensajes que le has escrito al bot desde la última pasada.
    Devuelve True si ha cambiado algo en discos.json."""
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        r = requests.get(f"{api}/getUpdates", params={"timeout": 0}, timeout=20)
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"⚠️  No se pudieron leer los mensajes de Telegram: {sin_secretos(repr(e))}")
        return False
    if not updates:
        return False

    por_nombre = {f"{d['artista']} - {d['disco']}".lower(): d for d in discos}
    cambiado = False
    for u in updates:
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue  # solo obedecemos a tu chat
        texto = (m.get("text") or "").strip()
        orden = texto.split()[0].lower().lstrip("/").split("@")[0] if texto else ""
        if orden in ("llista", "lista", "listado"):
            enviar_lista(discos, texto, m.get("message_id"))
            continue
        if orden in ("axuda", "ayuda", "help", "start"):
            enviar_telegram(AYUDA_BOT, responder_a=m.get("message_id"))
            continue
        if orden in ("registrar", "buscar", "amestar"):
            cambiado |= alta_desde_telegram(orden, texto, discos, por_nombre, m.get("message_id"))
            continue
        if orden not in ("comprado", "descomprado"):
            continue
        # "comprado 15" (respondiendo a un aviso) o "/comprado Nirvana Nevermind 15€"
        texto, pagado = _separar_precio(texto, exigir_euro=len(texto.split()) > 2)
        # ¿A qué disco se refiere? Primero, al aviso al que responde…
        nombre = None
        citado = (m.get("reply_to_message") or {}).get("text") or ""
        mt = re.search(r"Buscado:\s*(.+)", citado)
        if mt:
            nombre = mt.group(1).strip()
        # …y si no, a lo escrito detrás de la orden: "/comprado Nirvana - Nevermind"
        if not nombre and " " in texto:
            nombre = texto.split(" ", 1)[1].strip()
        disco = por_nombre.get((nombre or "").lower())
        if not disco and nombre:
            disco, dudas = buscar_aproximado(nombre, discos)
            if dudas:
                opciones = "\n".join(f"• /{orden} {html.escape(d['artista'])} - {html.escape(d['disco'])}"
                                      for d in dudas)
                enviar_telegram(f"🤔 «{html.escape(nombre)}» podría ser varios discos. "
                                f"¿Cuál de estos?\n{opciones}", responder_a=m.get("message_id"))
                continue
        if not disco:
            enviar_telegram(f"❓ No encuentro «{html.escape(nombre or texto)}» en la lista. "
                            "Responde «comprado» a un aviso o escribe /comprado Artista Disco.\n"
                            "Si es un disco que no estaba en la lista (p. ej. de un pack), usa:\n"
                            "/registrar Artista - Disco 12€",
                            responder_a=m.get("message_id"))
            continue
        nombre = f"{disco['artista']} - {disco['disco']}"
        if orden == "comprado":
            disco["comprado"] = hoy().isoformat()
            if pagado is not None:
                disco["precio_pagado"] = pagado
            enviar_telegram(f"✅ <b>{html.escape(nombre)}</b> marcado como comprado. "
                            "Ya no lo buscaré. ¡Que lo disfrutes!"
                            + (aviso_modo_ahorro() if MODO_AHORRO else ""),
                            responder_a=m.get("message_id"))
        else:
            disco.pop("comprado", None)
            disco.pop("precio_pagado", None)
            enviar_telegram(f"🔁 Vuelvo a buscar <b>{html.escape(nombre)}</b>.",
                            responder_a=m.get("message_id"))
        cambiado = True

    # Confirmar a Telegram que ya hemos leído estos mensajes
    try:
        requests.get(f"{api}/getUpdates", params={"offset": updates[-1]["update_id"] + 1, "timeout": 0}, timeout=20)
    except Exception:
        pass
    return cambiado


# ---------------------------------------------------------------- main

def main():
    print("Iniciando rastreo de vinilos...")
    comprobar_telegram()

    if os.environ.get("TEST_TELEGRAM") == "1":
        enviar_telegram("🧪 Mensaje de prueba del Cazador de Vinilos: Telegram funciona.")

    todos = cargar_lista_discos()
    if not todos:
        print("Lista vacía o inválida.")
        sys.exit(1)
    if procesar_comandos(todos):
        guardar_discos(todos)
        print("📝 discos.json actualizado con los cambios pedidos por Telegram.")
    discos = [d for d in todos if not d.get("comprado")]
    compras = compras_del_mes(todos)
    ahorro = MODO_AHORRO and bool(compras)
    if ahorro:
        print(f"🧘 Modo ahorro: {len(compras)} compra(s) este mes; solo se avisa de grandes chollos.")
    callados = 0
    print(f"Discos comprados (no se buscan): {len(todos) - len(discos)}")

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
                    gran = es_gran_chollo(nombre, item, res)
                    if ahorro and not gran:
                        callados += 1   # no se marca como visto: si sigue a la venta, llegará el mes que viene
                        continue
                    if enviar_telegram(msg, silencioso=AVISOS_NORMALES_SILENCIOSOS and not gran):
                        vistos[res["link"]] = {"precio": res["precio"], "ts": time.time()}
                time.sleep(1)  # no saturar las webs
    finally:
        guardar_vistos(vistos)  # se guarda aunque algo falle a mitad

    # ---- Resumen: si algo ha fallado, el workflow sale en ROJO en GitHub
    print("\n========== RESUMEN ==========")
    print(f"Mensajes enviados: {stats['ok']}  |  Fallos de Telegram: {stats['fallos_telegram']}"
          + (f"  |  Avisos normales retenidos por el modo ahorro: {callados}" if ahorro else ""))
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
