#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor de Vinted -> resumen diario por Telegram.

Como funciona:
  - Se ejecuta UNA vez al dia, hacia las 23:50 (hora de Madrid).
  - Recorre cada busqueda ordenada por "mas recientes" y va hacia atras
    hasta llegar a los articulos de ayer.
  - Se queda con todo lo publicado desde las 00:00 de hoy.
  - Te manda el recuento por Telegram.

Ojo con un detalle importante y deliberado: lo que se publico hoy pero ya
se ha vendido no aparece en el listado a estas horas, asi que no se cuenta.
El resumen responde a "cuantos hay disponibles ahora mismo", no a
"cuantos se publicaron en total".

No hace falta tocar este archivo. Toda la configuracion esta en busquedas.json.
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("Necesitas Python 3.9 o superior.")
    sys.exit(1)

import requests

# --------------------------------------------------------------------------
# Ajustes generales
# --------------------------------------------------------------------------
ARCHIVO_CONFIG = "busquedas.json"
ARCHIVO_ESTADO = "estado.json"

ZONA = ZoneInfo("Europe/Madrid")

# Solo se envia el resumen si en Madrid son las 23:xx.
# Asi da igual si estamos en horario de verano o de invierno.
HORA_RESUMEN = 23

POR_PAGINA = 96      # Articulos por consulta
MAX_PAGINAS = 12     # Tope de seguridad por busqueda (12 x 96 = 1152 articulos)
MAX_ENLACES = 10     # Enlaces incluidos en el mensaje, por busqueda

NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Poner a "1" para probar a cualquier hora (variable FORZAR en el workflow).
FORZAR = os.environ.get("FORZAR", "").strip() == "1"

# Diagnostico: codigos HTTP devueltos por Vinted en la busqueda actual.
CODIGOS_HTTP = []


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def log(mensaje):
    print("[{}] {}".format(datetime.now(ZONA).strftime("%H:%M:%S"), mensaje), flush=True)


def leer_json(ruta, por_defecto):
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return por_defecto


def escribir_json(ruta, datos):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def escapar(texto):
    return (
        str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def enviar_telegram(texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("ATENCION: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")
        return False

    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)

    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > 3500:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual)

    todo_ok = True
    for trozo in trozos:
        enviado = False
        for intento in range(3):
            try:
                r = requests.post(
                    url,
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": trozo,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=30,
                )
                if r.status_code == 200:
                    enviado = True
                    break
                log("Telegram {}: {}".format(r.status_code, r.text[:200]))
            except requests.RequestException as e:
                log("Fallo al contactar con Telegram: {}".format(e))
            time.sleep(3 * (intento + 1))
        todo_ok = todo_ok and enviado
        time.sleep(0.5)

    return todo_ok


# --------------------------------------------------------------------------
# Vinted
# --------------------------------------------------------------------------
def crear_sesion(dominio):
    sesion = requests.Session()
    sesion.headers.update(
        {
            "User-Agent": NAVEGADOR,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    )
    try:
        sesion.get("https://{}/".format(dominio), timeout=30)
    except requests.RequestException as e:
        log("  No he podido abrir {}: {}".format(dominio, e))
    return sesion


def parsear_url_busqueda(url):
    """
    Convierte la URL copiada de Vinted en (dominio, parametros).
    Los filtros (marca, talla, estado, precio...) ya viajan dentro de la URL.
    """
    trozos = urlparse(url)
    dominio = trozos.netloc or "www.vinted.es"

    de_lista = {
        "catalog", "brand_ids", "size_ids", "status_ids", "color_ids",
        "material_ids", "video_game_platform_ids", "country_ids", "city_ids",
    }

    parametros = {}
    for clave, valores in parse_qs(trozos.query).items():
        limpia = clave.replace("[]", "")
        if limpia in ("page", "per_page", "order", "time"):
            continue
        if limpia in de_lista or len(valores) > 1:
            parametros[limpia + "[]"] = valores
        else:
            parametros[limpia] = valores[0]

    parametros["order"] = "newest_first"
    parametros["per_page"] = POR_PAGINA
    return dominio, parametros


def consultar_pagina(sesion, dominio, parametros, pagina):
    parametros = dict(parametros)
    parametros["page"] = pagina
    url = "https://{}/api/v2/catalog/items".format(dominio)

    for intento in range(1, 5):
        try:
            r = sesion.get(
                url,
                params=parametros,
                headers={"Referer": "https://{}/catalog".format(dominio)},
                timeout=30,
            )
        except requests.RequestException as e:
            log("    Fallo de red (intento {}): {}".format(intento, e))
            time.sleep(4 * intento)
            continue

        CODIGOS_HTTP.append(r.status_code)

        if r.status_code == 200:
            try:
                return r.json().get("items", [])
            except ValueError:
                log("    Respuesta no valida de Vinted.")
                return None

        if r.status_code in (401, 403):
            log("    Codigo {} - renuevo la sesion.".format(r.status_code))
            sesion.cookies.clear()
            try:
                sesion.get("https://{}/".format(dominio), timeout=30)
            except requests.RequestException:
                pass
            time.sleep(5 * intento)
            continue

        if r.status_code == 429:
            log("    Demasiadas peticiones, espero.")
            time.sleep(15 * intento)
            continue

        log("    Codigo inesperado {}.".format(r.status_code))
        time.sleep(4 * intento)

    return None


def momento_publicacion(item):
    """Devuelve la fecha de publicacion en segundos, o None si no se puede saber."""
    candidatos = []

    foto = item.get("photo") or {}
    alta = foto.get("high_resolution") or {}
    candidatos += [alta.get("timestamp"), foto.get("timestamp")]
    candidatos += [item.get("created_at_ts"), item.get("photo_timestamp")]

    for valor in candidatos:
        if valor is None:
            continue
        if isinstance(valor, (int, float)) and valor > 1_000_000_000:
            return float(valor)
        if isinstance(valor, str):
            texto = valor.strip()
            if texto.isdigit() and len(texto) >= 10:
                return float(texto[:10])
            try:
                fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
                return fecha.timestamp()
            except ValueError:
                continue
    return None


def resumir_articulo(item, dominio, momento):
    precio = item.get("price")
    if isinstance(precio, dict):
        texto_precio = "{} {}".format(
            precio.get("amount", "?"), precio.get("currency_code", "EUR")
        )
    elif precio is not None:
        texto_precio = "{} EUR".format(precio)
    else:
        texto_precio = "?"

    return {
        "id": str(item.get("id")),
        "titulo": item.get("title") or "(sin titulo)",
        "precio": texto_precio,
        "marca": item.get("brand_title") or "",
        "talla": item.get("size_title") or "",
        "estado": item.get("status") or "",
        "hora": datetime.fromtimestamp(momento, ZONA).strftime("%H:%M"),
        "url": item.get("url") or "https://{}/items/{}".format(dominio, item.get("id")),
    }


def recoger_de_hoy(busqueda, inicio_del_dia):
    """
    Recorre la busqueda de mas nuevo a mas viejo y devuelve
    (articulos_de_hoy, hubo_error, se_agoto_el_tope).
    """
    nombre = busqueda.get("nombre", "Sin nombre")
    url = busqueda.get("url", "")
    if not url:
        return {
            "nombre": nombre, "articulos": [], "error": True, "agotado": False,
            "examinados": 0, "sin_fecha": 0, "codigos": [],
        }

    dominio, parametros = parsear_url_busqueda(url)
    sesion = crear_sesion(dominio)
    tope_paginas = int(busqueda.get("max_paginas", MAX_PAGINAS))

    log("  '{}'".format(nombre))

    del CODIGOS_HTTP[:]
    encontrados = {}
    hubo_error = False
    se_agoto = False
    sin_fecha = 0
    examinados = 0

    for pagina in range(1, tope_paginas + 1):
        articulos = consultar_pagina(sesion, dominio, parametros, pagina)

        if articulos is None:
            hubo_error = True
            break
        if not articulos:
            break

        examinados += len(articulos)
        de_hoy_en_pagina = 0
        for item in articulos:
            momento = momento_publicacion(item)
            if momento is None:
                sin_fecha += 1
                continue
            if momento >= inicio_del_dia:
                de_hoy_en_pagina += 1
                resumen = resumir_articulo(item, dominio, momento)
                encontrados[resumen["id"]] = resumen

        log(
            "    pagina {}: {} de {} son de hoy".format(
                pagina, de_hoy_en_pagina, len(articulos)
            )
        )

        # Ordenado por mas recientes: una pagina entera sin nada de hoy
        # significa que ya hemos pasado la medianoche hacia atras.
        if de_hoy_en_pagina == 0:
            break
        if len(articulos) < POR_PAGINA:
            break
        if pagina == tope_paginas:
            se_agoto = True

        time.sleep(random.uniform(2.0, 4.0))

    if sin_fecha:
        log("    ({} articulos sin fecha legible, descartados)".format(sin_fecha))

    lista = sorted(encontrados.values(), key=lambda a: a["hora"], reverse=True)
    log("    total de hoy: {} (de {} anuncios revisados)".format(len(lista), examinados))
    log("    codigos HTTP recibidos: {}".format(sorted(set(CODIGOS_HTTP)) or "ninguno"))
    return {
        "nombre": nombre, "articulos": lista, "error": hubo_error,
        "agotado": se_agoto, "examinados": examinados, "sin_fecha": sin_fecha,
        "codigos": sorted(set(CODIGOS_HTTP)),
    }


# --------------------------------------------------------------------------
# Mensaje
# --------------------------------------------------------------------------
def construir_mensaje(resultados, fecha_texto):
    total = sum(len(r["articulos"]) for r in resultados)

    lineas = [
        "<b>Resumen Vinted — {}</b>".format(fecha_texto),
        "",
        "Publicados hoy y aun disponibles: <b>{}</b>".format(total),
        "",
    ]

    for resultado in resultados:
        articulos = resultado["articulos"]
        lineas.append("<b>{}</b>: {}".format(escapar(resultado["nombre"]), len(articulos)))

        for articulo in articulos[:MAX_ENLACES]:
            detalles = " · ".join(
                x for x in (articulo["marca"], articulo["talla"], articulo["estado"]) if x
            )
            lineas.append(
                '   {} <a href="{}">{}</a> — {}{}'.format(
                    articulo["hora"],
                    articulo["url"],
                    escapar(articulo["titulo"][:55]),
                    escapar(articulo["precio"]),
                    " ({})".format(escapar(detalles)) if detalles else "",
                )
            )
        if len(articulos) > MAX_ENLACES:
            lineas.append("   ... y {} mas".format(len(articulos) - MAX_ENLACES))

        if not articulos and resultado.get("examinados"):
            lineas.append(
                "   <i>Revisados {} anuncios; ninguno publicado hoy.</i>".format(
                    resultado["examinados"]
                )
            )
        if not resultado.get("examinados"):
            lineas.append(
                "   <i>[!] Vinted no ha devuelto ningun anuncio "
                "(codigos: {}). Revisa la URL o puede ser un bloqueo.</i>".format(
                    ", ".join(str(c) for c in resultado.get("codigos", [])) or "sin respuesta"
                )
            )
        if resultado.get("sin_fecha"):
            lineas.append(
                "   <i>[!] {} anuncios sin fecha legible.</i>".format(resultado["sin_fecha"])
            )
        if resultado["error"]:
            lineas.append(
                "   <i>[!] Vinted no ha respondido bien (codigos: {}).</i>".format(
                    ", ".join(str(c) for c in resultado.get("codigos", [])) or "sin respuesta"
                )
            )
        if resultado["agotado"]:
            lineas.append(
                "   <i>[!] Tope de paginas alcanzado. Afina los filtros "
                "o sube 'max_paginas'.</i>"
            )
        lineas.append("")

    return "\n".join(lineas)


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------
def main():
    configuracion = leer_json(ARCHIVO_CONFIG, None)
    if not configuracion or not configuracion.get("busquedas"):
        log("No encuentro busquedas en {}.".format(ARCHIVO_CONFIG))
        sys.exit(1)

    ahora = datetime.now(ZONA)
    hoy = ahora.strftime("%Y-%m-%d")

    # Guardia horaria: el workflow lanza varios intentos por si GitHub
    # se retrasa, pero solo debe salir un mensaje al dia.
    if not FORZAR:
        if ahora.hour != HORA_RESUMEN:
            log("En Madrid son las {}. Aun no toca. Salgo.".format(ahora.strftime("%H:%M")))
            return
        estado = leer_json(ARCHIVO_ESTADO, {})
        if estado.get("ultimo_envio") == hoy:
            log("El resumen de hoy ya se envio. Salgo.")
            return

    inicio_del_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    log("Recogiendo lo publicado desde las 00:00 de hoy ({})...".format(hoy))

    resultados = []
    for busqueda in configuracion["busquedas"]:
        resultados.append(recoger_de_hoy(busqueda, inicio_del_dia))
        time.sleep(random.uniform(3.0, 5.0))

    mensaje = construir_mensaje(resultados, ahora.strftime("%d/%m/%Y"))

    if enviar_telegram(mensaje):
        log("Resumen enviado.")
        if not FORZAR:
            escribir_json(ARCHIVO_ESTADO, {"ultimo_envio": hoy})
    else:
        log("No he podido enviar el resumen. Se reintentara en el proximo intento.")
        sys.exit(1)


if __name__ == "__main__":
    main()
