"""Alerta SICOES Pro: revisa el SICOES desde GitHub Actions y avisa a cada cliente por Firebase.

Lee de Firestore las cuentas (colección usuarios): solo las activas y dentro de sus fechas
reciben avisos, y cada una solo los de sus propias entidades, en el celular ligado a la cuenta.
Cada entidad se consulta una sola vez aunque la vigilen varios clientes.

El estado (qué ya se avisó) se guarda en estado.json, en la rama avisos-estado. Como este repo
es público, ahí solo van huellas (hash) y conteos: ningún nombre de cliente ni entidad.
La primera vez que un cliente vigila una entidad no se le avisa nada: solo se registra.
"""
import datetime
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

BASE = "https://www.sicoes.gob.bo"
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"
CARPETA = os.path.dirname(os.path.abspath(__file__))
TIPOS = {"O": "obras", "B": "bienes", "S": "servicios", "C": "consultoria"}


def normalizar(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


def limpiar(s):
    s = re.sub(r"<[^>]*>", " ", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


class Sicoes:
    def __init__(self):
        self.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = None
        self.columnas = []
        self.url_conv = ""

    def _pedir(self, url, datos=None, extra=None):
        cab = {"User-Agent": UA, "Accept-Language": "es-BO,es;q=0.9"}
        cab.update(extra or {})
        cuerpo = urllib.parse.urlencode(datos).encode() if datos is not None else None
        with self.web.open(urllib.request.Request(url, data=cuerpo, headers=cab), timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    @staticmethod
    def _valor(pagina, patron):
        etiqueta = re.search(patron, pagina)
        if not etiqueta:
            return ""
        # El valor puede ir entre comillas dobles o simples (las columnas son JSON: van con simples)
        v = re.search(r"""value\s*=\s*(?:"([^"]*)"|'([^']*)')""", etiqueta.group(0))
        return html.unescape(v.group(1) if v.group(1) is not None else v.group(2)) if v else ""

    def abrir(self):
        inicio = self._pedir(BASE + "/portal/index.php")
        t1 = self._valor(inicio, r"""<input[^>]*id=["']token["'][^>]*>""")
        if not t1:
            raise RuntimeError("sin token en index.php")
        self.url_conv = BASE + "/portal/contrataciones/busqueda/convocatorias.php?tipo=convNacional&token=" + t1
        pag = self._pedir(self.url_conv)
        form = re.search(r"""<form[^>]*id=["']formSimple["'].*?</form>""", pag, re.S)
        self.token = self._valor(form.group(0) if form else pag, r"""<input[^>]*name=["']token["'][^>]*>""")
        if not self.token:
            self.token = self._valor(pag, r"""<input[^>]*name=["']token["'][^>]*>""")
        cols = self._valor(pag, r"""<input[^>]*id=["']colsConvocatoriasSimple["'][^>]*>""")
        if not self.token or not cols:
            i = pag.find("colsConvocatoriasSimple")
            raise RuntimeError("la página de convocatorias cambió (sin token o columnas): "
                               + repr(pag[max(0, i - 80):i + 120] if i >= 0 else pag[:200]))
        self.columnas = json.loads(cols)

    def buscar(self, entidad, tipos, max_paginas=30):
        if not self.token:
            self.abrir()
        filas, pagina = {}, 1
        while pagina <= max_paginas:
            datos = {
                "token": self.token, "entidad": entidad, "objetoContrato": "",
                "publicacionDesde": "", "publicacionHasta": "",
                "presentacionPropuestasDesde": "", "presentacionPropuestasHasta": "",
                "cuce1": "", "cuce2": "", "cuce3": "", "cuce4": "", "cuce5": "", "cuce6": "",
                "r1": "11", "subasta": "", "tipo": "Simple", "operacion": "convNacional",
                "nroRegistros": "10", "draw": str(pagina), "start": "0", "length": "10", "captcha": "",
            }
            for t in tipos:
                if t in TIPOS:
                    datos[TIPOS[t]] = t
            texto = self._pedir(BASE + "/portal/contrataciones/operacion.php", datos, {
                "X-Requested-With": "XMLHttpRequest", "Origin": BASE, "Referer": self.url_conv,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }).strip()
            if not texto.startswith("{"):
                raise RuntimeError("el SICOES rechazó la consulta (sesión vencida)")
            js = json.loads(texto)
            nuevas = 0
            for o in js.get("data") or []:
                col = lambda n: limpiar(o.get(self.columnas[n])) if n < len(self.columnas) else ""
                cuce = col(0)
                if cuce and cuce not in filas:
                    nuevas += 1
                    filas[cuce] = {"cuce": cuce, "entidad": col(1), "tipo": col(2), "modalidad": col(3),
                                   "objeto": col(4), "publicacion": col(6), "presentacion": col(7)}
            # Se corta cuando ya están todas o una página no trae nada nuevo
            if nuevas == 0 or len(filas) >= int(js.get("recordsTotal") or 0):
                break
            pagina += 1
            time.sleep(0.5)
        return list(filas.values())


class Firebase:
    """Firestore (cuentas de los clientes) y Cloud Messaging (avisos), con la cuenta de servicio."""

    def __init__(self, cuenta_json):
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        info = json.loads(cuenta_json)
        self.proyecto = info["project_id"]
        self.cred = service_account.Credentials.from_service_account_info(info, scopes=[
            "https://www.googleapis.com/auth/firebase.messaging",
            "https://www.googleapis.com/auth/datastore",
        ])
        self.cred.refresh(Request())

    def _pedir(self, url, datos=None):
        req = urllib.request.Request(url, data=json.dumps(datos).encode("utf-8") if datos is not None else None, headers={
            "Authorization": "Bearer " + self.cred.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def usuarios(self):
        base = f"https://firestore.googleapis.com/v1/projects/{self.proyecto}/databases/(default)/documents/usuarios?pageSize=300"
        docs, pagina = [], ""
        while True:
            js = self._pedir(base + ("&pageToken=" + urllib.parse.quote(pagina) if pagina else ""))
            docs += js.get("documents", [])
            pagina = js.get("nextPageToken", "")
            if not pagina:
                return [(d["name"].rsplit("/", 1)[-1], valor({"mapValue": {"fields": d.get("fields", {})}})) for d in docs]

    def enviar(self, token, titulo, cuerpo):
        self._pedir(f"https://fcm.googleapis.com/v1/projects/{self.proyecto}/messages:send", {"message": {
            "token": token,
            "notification": {"title": titulo, "body": cuerpo},
            "android": {"priority": "high", "notification": {
                "channel_id": "nuevas_convocatorias_v2", "sound": "default", "default_vibrate_timings": True}},
        }})


def valor(v):
    """Valor de Firestore (REST) → Python."""
    if "mapValue" in v:
        return {k: valor(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v:
        return [valor(x) for x in v["arrayValue"].get("values", [])]
    if "timestampValue" in v:
        t = v["timestampValue"].replace("Z", "+00:00")
        t = re.sub(r"\.(\d{6})\d*", r".\1", t)
        return datetime.datetime.fromisoformat(t).timestamp()
    for k in ("stringValue", "booleanValue", "doubleValue"):
        if k in v:
            return v[k]
    if "integerValue" in v:
        return int(v["integerValue"])
    return None


def huella(*partes):
    return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()[:20]


def activo(u, ahora):
    return u.get("activo") is True and (u.get("inicio") or 0) <= ahora < (u.get("vence") or 0) and u.get("fcmToken")


def texto_aviso(c):
    return (f"Nueva convocatoria - {c['tipo']}",
            f"{c['objeto']}\n{c['entidad']}\nModalidad: {c['modalidad']}\nPresentación: {c['presentacion']}\nCUCE: {c['cuce']}")


def main():
    ruta_estado = os.path.join(CARPETA, "estado.json")
    try:
        with open(ruta_estado, encoding="utf-8") as f:
            estado = json.load(f)
    except (OSError, ValueError):
        estado = {}
    avisados = estado.setdefault("avisados", {})       # huella(entidad, CUCE) -> fecha en que se vio
    iniciadas = set(estado.setdefault("iniciadas", []))  # huella(cliente, entidad): ya tuvo su primera carga
    ahora = time.time()
    hora_bo = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ahora - 4 * 3600))
    registro = []

    if not os.environ.get("FIREBASE_CUENTA", "").strip():
        registro.append("ERROR: falta el secreto FIREBASE_CUENTA")
        guardar(estado, ruta_estado, iniciadas, hora_bo, registro)
        return 0
    try:
        fb = Firebase(os.environ["FIREBASE_CUENTA"])
        usuarios = [(uid, u) for uid, u in fb.usuarios() if activo(u, ahora)]
    except Exception as err:
        registro.append(f"ERROR Firebase: {err}")
        guardar(estado, ruta_estado, iniciadas, hora_bo, registro)
        return 0

    # Entidad → clientes que la vigilan (cada entidad se consulta una sola vez)
    por_entidad = {}
    for uid, u in usuarios:
        for e in u.get("entidades") or []:
            if not isinstance(e, dict) or not e.get("entidad"):
                continue
            tipos = "".join(sorted(e.get("tipos") or "O"))
            clave = normalizar(e["entidad"]) + "|" + tipos + ("|exacta" if e.get("exacta") else "")
            por_entidad.setdefault(clave, {"entidad": e["entidad"], "tipos": tipos, "exacta": bool(e.get("exacta")), "clientes": []})
            por_entidad[clave]["clientes"].append(uid)

    tokens = {uid: u["fcmToken"] for uid, u in usuarios}
    envios = {}  # cliente -> convocatorias nuevas para él
    sicoes, errores = Sicoes(), 0
    for clave, e in por_entidad.items():
        try:
            filas = sicoes.buscar(e["entidad"], e["tipos"])
        except Exception:  # una sesión vencida se reabre una vez
            try:
                sicoes.abrir()
                filas = sicoes.buscar(e["entidad"], e["tipos"])
            except Exception:
                errores += 1
                continue
        if e["exacta"]:
            filas = [f for f in filas if normalizar(f["entidad"]) == normalizar(e["entidad"])]
        nuevas = []
        for f in filas:
            k = huella(clave, f["cuce"])
            if k not in avisados:
                avisados[k] = hora_bo
                nuevas.append(f)
        for uid in e["clientes"]:
            h = huella(uid, clave)
            if h not in iniciadas:
                iniciadas.add(h)  # primera carga de este cliente para esta entidad: sin avisar
                continue
            envios.setdefault(uid, []).extend(nuevas)

    enviados = fallidos = 0
    for uid, lista in envios.items():
        for c in lista[:15]:
            try:
                fb.enviar(tokens[uid], *texto_aviso(c))
                enviados += 1
            except Exception:
                fallidos += 1
    if os.environ.get("AVISO_PRUEBA") == "1":
        for uid, token in tokens.items():
            try:
                fb.enviar(token, "Alerta SICOES Pro", "Los avisos de convocatorias nuevas están activos en este celular.")
                enviados += 1
            except Exception:
                fallidos += 1
        registro.append("aviso de prueba enviado a las cuentas activas")

    registro.append(f"cuentas activas: {len(usuarios)}, entidades: {len(por_entidad)}, errores SICOES: {errores}")
    registro.append(f"avisos enviados: {enviados}, fallidos: {fallidos}")
    guardar(estado, ruta_estado, iniciadas, hora_bo, registro)
    return 0  # los errores quedan en estado.json; fallar cada 15 min solo llenaría el correo de avisos


def guardar(estado, ruta, iniciadas, hora_bo, registro):
    estado["iniciadas"] = sorted(iniciadas)
    estado["ultima_revision"] = hora_bo
    estado["registro"] = registro
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)
    print("\n".join(registro))


if __name__ == "__main__":
    sys.exit(main())
