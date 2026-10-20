import os
import json
import urllib.parse
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def cargar_lista_discos(filename="discos.json"):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error al cargar {filename}: {e}")
        return []

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            print("✅ Mensaje enviado a Telegram correctamente.")
        else:
            print(f"❌ Error de Telegram ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"❌ Excepción conectando a Telegram: {e}")

def buscar_wallapop(item):
    """Consulta los artículos de Wallapop buscando por texto libre sin restricciones estrictas de categoría."""
    busqueda = f"vinilo {item['artista']} {item['disco']}"
    url = f"https://api.wallapop.com/api/v3/general/search?keywords={urllib.parse.quote(busqueda)}&order_by=newest"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'X-Device-OS': '0'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            sections = data.get('search_objects', [])
            
            encontrados = []
            for obj in sections:
                item_data = obj.get('item', {})
                titulo = item_data.get('title', '')
                precio = float(item_data.get('price', 0))
                item_id = item_data.get('id')
                slug = item_data.get('web_slug')
                
                if item_id and slug and precio > 0 and precio <= item['max_precio']:
                    url_item = f"https://es.wallapop.com/item/{slug}"
                    encontrados.append({
                        'titulo': titulo,
                        'precio': precio,
                        'link': url_item
                    })
            return encontrados
    except Exception as e:
        print(f"Error al buscar '{busqueda}' en Wallapop: {e}")
    return []

def buscar_vinted(item):
    busqueda = f"vinilo {item['artista']} {item['disco']}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'es-ES,es;q=0.9',
    }
    
    session = requests.Session()
    session.headers.update(headers)
    
    try:
        session.get("https://www.vinted.es", timeout=10)
        url_api = f"https://www.vinted.es/api/v2/catalog/items?search_text={busqueda}&order=newest_first"
        response = session.get(url_api, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            items = data.get('items', [])
            
            encontrados = []
            for obj in items:
                precio_raw = obj.get('price', {})
                precio = float(precio_raw.get('amount')) if isinstance(precio_raw, dict) else float(precio_raw or 0)
                
                titulo = obj.get('title', '')
                url_item = obj.get('url')
                
                if precio > 0 and precio <= item['max_precio'] and url_item:
                    encontrados.append({
                        'titulo': titulo,
                        'precio': precio,
                        'link': url_item
                    })
            return encontrados
    except Exception as e:
        print(f"Error al buscar '{busqueda}' en Vinted: {e}")
    return []

def main():
    print("Iniciando rastreo de vinilos...")
    
    discos_deseados = cargar_lista_discos()
    if not discos_deseados:
        print("Lista vacía o inválida.")
        return

    print(f"Rastreando {len(discos_deseados)} discos en Wallapop y Vinted...")
    
    for item in discos_deseados:
        # 1. Búsqueda en Wallapop
        resultados_wallapop = buscar_wallapop(item)
        for res in resultados_wallapop[:1]:
            mensaje = (
                f"🎵 *¡Chollo en Wallapop!*\n\n"
                f"*Buscado:* {item['artista']} - {item['disco']}\n"
                f"*Anuncio:* {res['titulo']}\n"
                f"*Precio:* {res['precio']}€ (Límite: {item['max_precio']}€)\n\n"
                f"🔗 [Ver en Wallapop]({res['link']})"
            )
            enviar_telegram(mensaje)

        # 2. Búsqueda en Vinted
        resultados_vinted = buscar_vinted(item)
        for res in resultados_vinted[:1]:
            mensaje = (
                f"🛍️ *¡Chollo en Vinted!*\n\n"
                f"*Buscado:* {item['artista']} - {item['disco']}\n"
                f"*Anuncio:* {res['titulo']}\n"
                f"*Precio:* {res['precio']}€ (Límite: {item['max_precio']}€)\n\n"
                f"🔗 [Ver en Vinted]({res['link']})"
            )
            enviar_telegram(mensaje)

if __name__ == "__main__":
    main()
