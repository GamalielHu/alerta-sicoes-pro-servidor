"""Deja listo el proyecto de Firebase con la cuenta de servicio (secreto FIREBASE_CUENTA).

Cada paso se intenta y su resultado queda en el informe; si alguno no tiene permiso, se hace
a mano en la consola de Firebase. Escribe panel/config.js (datos públicos de la app web).
No imprime ninguna clave.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

PAQUETE = "bo.alertasicoes.pro"
CARPETA = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(CARPETA)


class Google:
    def __init__(self, cuenta_json):
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        info = json.loads(cuenta_json)
        self.proyecto = info["project_id"]
        self.cred = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform", "https://www.googleapis.com/auth/firebase"])
        self.cred.refresh(Request())

    def pedir(self, metodo, url, datos=None):
        req = urllib.request.Request(url, method=metodo, data=json.dumps(datos).encode() if datos is not None else None,
                                     headers={"Authorization": "Bearer " + self.cred.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "replace")
            try:
                cuerpo = json.loads(cuerpo)["error"]["message"]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {str(cuerpo)[:300]}") from None

    def esperar(self, op):
        """Operaciones largas de la API de Firebase."""
        for _ in range(30):
            if op.get("done"):
                if "error" in op:
                    raise RuntimeError(op["error"].get("message", "error"))
                return op.get("response", {})
            time.sleep(3)
            op = self.pedir("GET", "https://firebase.googleapis.com/v1beta1/" + op["name"])
        raise RuntimeError("la operación no terminó a tiempo")


def paso(informe, nombre, fn):
    try:
        r = fn()
        informe.append(f"OK    {nombre}" + (f": {r}" if r else ""))
        return True
    except Exception as e:
        informe.append(f"ERROR {nombre}: {e}")
        return False


def main():
    informe = []
    if not os.environ.get("FIREBASE_CUENTA", "").strip():
        print("ERROR falta el secreto FIREBASE_CUENTA")
        return 0
    g = Google(os.environ["FIREBASE_CUENTA"])
    p = g.proyecto
    fb = f"https://firebase.googleapis.com/v1beta1/projects/{p}"
    informe.append(f"proyecto: {p}")

    def firestore():
        url = f"https://firestore.googleapis.com/v1/projects/{p}/databases/(default)"
        try:
            g.pedir("GET", url)
            return "ya existía"
        except RuntimeError as e:
            if "404" not in str(e):
                raise
        g.pedir("POST", f"https://firestore.googleapis.com/v1/projects/{p}/databases?databaseId=(default)",
                {"type": "FIRESTORE_NATIVE", "locationId": "southamerica-east1"})
        return "creada (São Paulo)"

    def correo():
        g.pedir("PATCH", f"https://identitytoolkit.googleapis.com/admin/v2/projects/{p}/config"
                         "?updateMask=signIn.email.enabled,signIn.email.passwordRequired",
                {"signIn": {"email": {"enabled": True, "passwordRequired": True}}})
        return "correo y contraseña habilitados"

    def reglas():
        with open(os.path.join(RAIZ, "firestore.rules"), encoding="utf-8") as f:
            texto = f.read()
        rs = g.pedir("POST", f"https://firebaserules.googleapis.com/v1/projects/{p}/rulesets",
                     {"source": {"files": [{"name": "firestore.rules", "content": texto}]}})
        nombre = f"projects/{p}/releases/cloud.firestore"
        try:
            g.pedir("PATCH", f"https://firebaserules.googleapis.com/v1/{nombre}",
                    {"release": {"name": nombre, "rulesetName": rs["name"]}})
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            g.pedir("POST", f"https://firebaserules.googleapis.com/v1/projects/{p}/releases",
                    {"name": nombre, "rulesetName": rs["name"]})
        return "publicadas"

    def android():
        apps = g.pedir("GET", fb + "/androidApps").get("apps", [])
        if any(a.get("packageName") == PAQUETE for a in apps):
            return "ya existía"
        g.esperar(g.pedir("POST", fb + "/androidApps", {"packageName": PAQUETE, "displayName": "Alerta SICOES Pro"}))
        return "registrada"

    def web():
        apps = g.pedir("GET", fb + "/webApps").get("apps", [])
        app = apps[0] if apps else g.esperar(g.pedir("POST", fb + "/webApps", {"displayName": "Panel"}))
        cfg = g.pedir("GET", f"https://firebase.googleapis.com/v1beta1/{app['name']}/config")
        conf = {k: cfg[k] for k in ("apiKey", "authDomain", "projectId", "appId", "messagingSenderId") if k in cfg}
        conf.setdefault("authDomain", f"{p}.firebaseapp.com")
        with open(os.path.join(RAIZ, "panel", "config.js"), "w", encoding="utf-8") as f:
            f.write("// Datos públicos de la app web de Firebase (los genera servidor/configurar.py)\n")
            f.write("export const configuracion = " + json.dumps(conf, indent=2) + ";\n")
        return "config.js listo"

    def dominio():
        # El panel se publica en GitHub Pages: ese dominio tiene que estar autorizado para iniciar sesión
        dueno = os.environ.get("GITHUB_REPOSITORY_OWNER", "").lower()
        if not dueno:
            return "sin dueño de repositorio"
        dom = dueno + ".github.io"
        url = f"https://identitytoolkit.googleapis.com/admin/v2/projects/{p}/config"
        actuales = g.pedir("GET", url).get("authorizedDomains", [])
        if dom in actuales:
            return dom + " ya estaba"
        g.pedir("PATCH", url + "?updateMask=authorizedDomains", {"authorizedDomains": actuales + [dom]})
        return dom + " autorizado"

    paso(informe, "Base de datos Firestore", firestore)
    paso(informe, "Ingreso con correo", correo)
    paso(informe, "Dominio del panel", dominio)
    paso(informe, "Reglas de seguridad", reglas)
    paso(informe, "App Android " + PAQUETE, android)
    paso(informe, "App web del panel", web)
    texto = "\n".join(informe)
    print(texto)
    with open(os.path.join(RAIZ, "informe.txt"), "w", encoding="utf-8") as f:
        f.write(texto + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
