"""Genera noticias.json para el dashboard a partir de feeds RSS de Google News.

- Solo acepta noticias con pubDate de los últimos DIAS_CORTE días.
- Descarta lo que ya se mostró (historial en noticias_vistas.json, 30 días).
- Máximo 2 por fuente y hasta MAX_POR_SECCION por sección.
Solo usa la librería estándar de Python.
"""
import json
import re
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "noticias.json"
HISTORIAL = RAIZ / "noticias_vistas.json"

DIAS_CORTE = 7
DIAS_HISTORIAL = 30
MAX_POR_SECCION = 6
MAX_POR_FUENTE = 2

ES = "&hl=es-419&gl=CO&ceid=CO:es-419"
EN = "&hl=en-US&gl=US&ceid=US:en"
BASE = "https://news.google.com/rss/search?q="

FEEDS = {
    "noticias_co": [
        BASE + "videovigilancia+OR+CCTV+OR+%22control+de+acceso%22+Colombia+when:7d" + ES,
        BASE + "%22seguridad+electr%C3%B3nica%22+OR+biometr%C3%ADa+OR+antidrones+Colombia+when:7d" + ES,
        BASE + "%22c%C3%A1maras+de+seguridad%22+OR+%22reconocimiento+facial%22+OR+%22centro+de+monitoreo%22+Colombia+when:7d" + ES,
        BASE + "site:tecnoseguro.com+when:7d" + ES,
    ],
    "noticias_mundo": [
        BASE + "%22video+surveillance%22+OR+%22access+control%22+when:7d" + EN,
        BASE + "counter-drone+OR+C-UAS+OR+%22physical+security%22+AI+when:7d" + EN,
        BASE + "site:securityinfowatch.com+when:7d" + EN,
        BASE + "site:sourcesecurity.com+when:7d" + EN,
    ],
}

# Palabras que delatan tiendas, catálogos u ofertas
EXCLUIR = re.compile(
    r"\b(comprar|precio|oferta|descuento|tienda|cat[aá]logo|buy now|deal|discount|coupon|% off)\b",
    re.I,
)

# El título debe contener al menos uno de estos términos (se compara sin tildes)
RELEVANTE = {
    "noticias_co": re.compile(
        r"\b(videovigilancia|video ?vigilancia|camaras? de (seguridad|vigilancia|videovigilancia)|"
        r"cctv|control de acceso|biometri\w*|reconocimiento facial|seguridad electronica|"
        r"antidron\w*|anti ?drones?|contra drones|sistemas? anti\w* drones?|ciberseguridad|"
        r"centro de (monitoreo|comando)|c4|lectura de placas|lpr|alarmas?|analitica de video|"
        r"seguridad (privada|integrada|inteligente)|smart ?city|ciudad inteligente|"
        r"hikvision|dahua|axis|genetec|milestone|hanwha|verkada|motorola solutions|tecnoseguro)\b"
    ),
    "noticias_mundo": re.compile(
        r"\b(surveillance|cctv|access control|camera|cameras|video (analytics|security|management)|"
        r"vms|biometric\w*|facial recognition|counter.?drone|counter.?uas|c.?uas|anti.?drone|"
        r"drone detection|physical security|security (platform|systems?|industry)|"
        r"intrusion|perimeter|alarm|lidar|license plate|lpr|smart city|"
        r"hikvision|dahua|axis|genetec|milestone|hanwha|verkada|avigilon|motorola solutions|"
        r"honeywell|bosch|johnson controls|lenel|hid|dedrone|d.?fend)\b"
    ),
}
# Páginas índice, comparadores y reportes de mercado pagados (se comparan sin tildes)
BASURA = re.compile(
    r"^(browse|compare|search|view|see|latest|all|products?|news)\b|\bcompare \S+ (with|vs)\b|"
    r"\bmarket (size|insights|report|research|analysis|forecast|share|growth|outlook|trends)\b|"
    r"\bcagr\b|\bforecast (to|till|by) 20\d\d\b"
)
# Crónica roja: notas de crímenes donde las cámaras solo aparecen como testigo
CRONICA = re.compile(
    r"\b(captar\w*|capto|grabaron|quedo (grabado|registrado)|registraron el momento|asalto|"
    r"asesina\w*|sicari\w*|homicidio|balacera|hurto|atraco|robo|muert[oa]s?|viral)\b"
)
MIN_PALABRAS = 5

FUENTES_DEL_SECTOR = {"tecnoseguro", "securityinfowatch", "sourcesecurity", "security info watch",
                      "sourcesecurity.com", "securityinfowatch.com", "tecnoseguro.com"}

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
BOGOTA = timezone(timedelta(hours=-5))


def normalizar(titulo: str) -> list[str]:
    t = unicodedata.normalize("NFKD", titulo.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", t).split()  # lista de palabras


def clave(titulo: str) -> str:
    return " ".join(normalizar(titulo))


def parecidos(a: str, b: str) -> bool:
    """Misma noticia contada por dos medios: >=60% de palabras en común."""
    pa = {w for w in normalizar(a) if len(w) > 3}
    pb = {w for w in normalizar(b) if len(w) > 3}
    if not pa or not pb:
        return False
    return len(pa & pb) / min(len(pa), len(pb)) >= 0.6


def acortar(titulo: str, n: int = 14) -> str:
    palabras = titulo.split()
    if len(palabras) <= n:
        return titulo
    return " ".join(palabras[:n]).rstrip(" ,.:;-–—") + "…"


def leer_feed(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-noticias)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raiz = ET.fromstring(r.read())
    items = []
    for it in raiz.iter("item"):
        titulo = (it.findtext("title") or "").strip()
        fuente = (it.findtext("source") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        if not (titulo and link and pub):
            continue  # sin fecha real = descartada
        try:
            fecha = parsedate_to_datetime(pub).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if fuente and titulo.endswith(" - " + fuente):
            titulo = titulo[: -len(" - " + fuente)].strip()
        items.append({"titulo": titulo, "fuente": fuente or "—", "url": link, "_fecha": fecha})
    return items


def main() -> None:
    ahora = datetime.now(timezone.utc)
    corte = ahora - timedelta(days=DIAS_CORTE)
    hoy_iso = ahora.astimezone(BOGOTA).date().isoformat()

    historial = []
    if HISTORIAL.exists():
        historial = json.loads(HISTORIAL.read_text(encoding="utf-8"))
    limite_hist = (ahora - timedelta(days=DIAS_HISTORIAL)).date().isoformat()
    historial = [h for h in historial if h.get("f", "") >= limite_hist]
    vistos_t = {h["t"] for h in historial}
    vistos_u = {h["u"] for h in historial}

    resultado = {}
    errores = []
    elegidos_global = []  # evita que una noticia salga en las dos secciones
    for seccion, urls in FEEDS.items():
        candidatos = []
        for url in urls:
            try:
                candidatos += leer_feed(url)
            except Exception as e:  # un feed caído no tumba los demás
                errores.append(f"{seccion}: {type(e).__name__}")

        candidatos.sort(key=lambda x: x["_fecha"], reverse=True)
        elegidos, por_fuente = [], {}
        for n in candidatos:
            if n["_fecha"] < corte:
                continue
            if clave(n["titulo"]) in vistos_t or n["url"] in vistos_u:
                continue
            k = clave(n["titulo"])
            if EXCLUIR.search(n["titulo"]) or BASURA.search(k) or len(k.split()) < MIN_PALABRAS:
                continue
            if seccion == "noticias_co" and CRONICA.search(k):
                continue
            if (n["fuente"].lower() not in FUENTES_DEL_SECTOR
                    and not RELEVANTE[seccion].search(clave(n["titulo"]))):
                continue
            if any(parecidos(n["titulo"], e["titulo"]) for e in elegidos + elegidos_global):
                continue
            if por_fuente.get(n["fuente"], 0) >= MAX_POR_FUENTE:
                continue
            por_fuente[n["fuente"]] = por_fuente.get(n["fuente"], 0) + 1
            elegidos.append(n)
            if len(elegidos) >= MAX_POR_SECCION:
                break

        elegidos_global += elegidos
        salida = []
        for n in elegidos:
            f = n["_fecha"].astimezone(BOGOTA)
            salida.append({
                "titulo": acortar(n["titulo"]),
                "fuente": n["fuente"],
                "fecha": f"{f.day:02d} {MESES[f.month - 1]}",
                "url": n["url"],
            })
            historial.append({"t": clave(n["titulo"]), "u": n["url"], "f": hoy_iso})
        resultado[seccion] = salida

    local = ahora.astimezone(BOGOTA)
    resultado["actualizado"] = (
        f"{DIAS[local.weekday()]} {local.day} de {MESES_LARGOS[local.month - 1]}, "
        f"{local:%H:%M}"
    )
    if errores:
        resultado["errores"] = errores

    SALIDA.write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    HISTORIAL.write_text(json.dumps(historial, ensure_ascii=False), encoding="utf-8")
    print(f"CO: {len(resultado['noticias_co'])} · Mundo: {len(resultado['noticias_mundo'])} · errores: {errores}")


if __name__ == "__main__":
    main()
