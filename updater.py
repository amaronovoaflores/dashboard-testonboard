#!/usr/bin/env python3
"""
updater.py - Dashboard multi-cliente (dash.net.pe)
Lee config.json del repo y, para cada vehiculo, descarga datos de Hunter GPS
y mantenimientos (Drive). Genera datos_hunter_{PLACA}.json y datos_mantos_{PLACA}.json.
Corre automaticamente cada noche via GitHub Actions.

GPS: un solo usuario/clave por CLIENTE (secrets HUNTER_USER / HUNTER_PASS del repo),
aplica a todas las placas listadas en config.json -> vehiculos.
Mantenimientos: un file_id_mantos de Drive por VEHICULO (columna en config.json).
"""

import json, urllib.request, urllib.error, ssl, math, sys, os, io, shutil
from datetime import datetime, timedelta

HUNTER_LOGIN   = 'http://pxapi.24hm.net/apiGeo/login'
HUNTER_REPORTE = 'http://pxapi.24hm.net/apiGeo/reporteHistoricoPBi'
CONFIG_FILE    = 'config.json'

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# -- HELPERS --------------------------------------------------------------
def http_request(method, url, payload=None, headers={}):
    data = json.dumps(payload).encode('utf-8') if payload else None
    urls = [url.replace('http://', 'https://'), url] if url.startswith('http://') else [url]
    last_error = None
    for target_url in urls:
        req = urllib.request.Request(target_url, data=data, headers={
            'Content-Type': 'application/json', **headers
        }, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 307, 308):
                new_url = e.headers.get('Location', '')
                print(f"  Redirect {e.code} -> {new_url}")
                if new_url:
                    req2 = urllib.request.Request(new_url, data=data, headers={
                        'Content-Type': 'application/json', **headers
                    }, method=method)
                    try:
                        with urllib.request.urlopen(req2, timeout=30, context=ctx) as r2:
                            return r2.read()
                    except Exception as e2:
                        last_error = e2; continue
            last_error = e; continue
        except Exception as e:
            last_error = e; continue
    raise last_error or Exception(f"No se pudo conectar a {url}")

def post_json(url, payload, headers={}):
    return json.loads(http_request('POST', url, payload, headers).decode('utf-8'))

def get_json(url, payload, headers={}):
    return json.loads(http_request('GET', url, payload, headers).decode('utf-8'))

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dLat = math.radians(lat2 - lat1); dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# -- HUNTER GPS -------------------------------------------------------------
def login(usuario, contrasena):
    print(f"  Login Hunter usuario={usuario}...")
    resp = post_json(HUNTER_LOGIN, {"usuario": usuario, "contrasena": contrasena})
    print(f"  Login status: {resp.get('status')} auth: {resp.get('auth')}")
    token = resp.get('token', '')
    if not token:
        raise Exception(f"Login fallido: {resp}")
    return token

def descargar_dia(token, usuario, placa, fecha_str):
    try:
        resp = get_json(
            HUNTER_REPORTE,
            {"usuario": usuario, "placa": placa, "fecha": fecha_str},
            {"x-access-token": token}
        )
        regs = resp.get('registros', [])
        if regs:
            print(f"    OK {fecha_str} -> {len(regs)} registros")
            return regs
        status = resp.get('status', '')
        msg = resp.get('message', resp.get('msg', str(resp)[:80]))
        print(f"    [{fecha_str}]: status='{status}' msg='{msg}'")
    except Exception as e:
        print(f"    [{fecha_str}] ERROR: {e}")
    return []

def procesar_dia(fecha_str, registros):
    if not registros: return None
    campos = list(registros[0].keys())
    tiene_km = 'kilometraje' in campos
    km_dia = odo_ini = odo_fin = 0
    fuente_km = 'haversine'
    if tiene_km:
        odos = [float(r['kilometraje']) for r in registros if r.get('kilometraje') and float(r.get('kilometraje', 0)) > 0]
        if odos:
            odo_ini = min(odos); odo_fin = max(odos)
            km_dia = round(odo_fin - odo_ini, 1)
            fuente_km = 'hunter_kilometraje'
    else:
        lats = []; lons = []
        for reg in registros:
            try:
                lat = float(reg.get('latitud', 0) or 0); lon = float(reg.get('longitud', 0) or 0)
                if lat and lon: lats.append(lat); lons.append(lon)
            except: pass
        for i in range(1, len(lats)):
            km_dia += haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
        km_dia = round(km_dia, 1)
    return {"fecha": fecha_str, "km": km_dia, "odo_ini": odo_ini, "odo_fin": odo_fin,
            "registros": len(registros), "campos": campos, "fuente_km": fuente_km,
            "tiene_km": tiene_km, "tiene_odo": tiene_km}

def actualizar_hunter(usuario, contrasena, token, placa, output_file):
    print(f"\n=== HUNTER GPS: {placa} -> {output_file} ===")
    try:
        with open(output_file, 'r', encoding='utf-8') as f: datos = json.load(f)
    except:
        datos = {"placa": placa, "ultima_actualizacion": "", "dias": {}, "campos_disponibles": [], "fuente_km": ""}

    hoy = datetime.now()
    fechas = [(hoy - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(35, -1, -1)]
    print(f"  Descargando {len(fechas)} dias ({fechas[0]} -> {fechas[-1]})...")

    campos_detectados = []; fuente_detectada = ''; nuevos = 0
    for fecha_str in fechas:
        try:
            regs = descargar_dia(token, usuario, placa, fecha_str)
            if regs:
                resumen = procesar_dia(fecha_str, regs)
                if resumen:
                    datos['dias'][fecha_str] = resumen
                    nuevos += 1
                    if not campos_detectados:
                        campos_detectados = resumen['campos']
                        fuente_detectada = resumen['fuente_km']
                    print(f"  {fecha_str}: {resumen['km']} kms ({resumen['registros']} regs)")
        except Exception as e:
            print(f"  {fecha_str}: ERROR {e}")

    datos.update({'ultima_actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'placa': placa, 'campos_disponibles': campos_detectados,
                   'fuente_km': fuente_detectada, 'total_dias': len(datos['dias'])})
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    print(f"  OK {output_file}: {nuevos} dias nuevos - total {len(datos['dias'])} dias")

# -- MANTENIMIENTOS DESDE DRIVE ---------------------------------------------
def actualizar_mantos(gkey, file_id_mantos, output_file):
    print(f"\n=== MANTENIMIENTOS: {file_id_mantos} -> {output_file} ===")
    url = f'https://www.googleapis.com/drive/v3/files/{file_id_mantos}?alt=media&key={gkey}'
    try:
        req = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            data = r.read()
        print(f"  Descargado: {len(data)} bytes")

        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))

        mantos = []; llantas = []; tope_manto = 50000; tope_llanta = 60000; seccion = ''

        for row in rows:
            if not row or not any(c is not None for c in row): continue
            col0 = str(row[0]).lower().strip() if row[0] else ''
            col1 = str(row[1]).lower().strip() if row[1] else ''

            if 'mantenimiento' in col0 or 'mantenimiento' in col1: seccion = 'manto'; continue
            if 'llanta' in col0 or 'llanta' in col1: seccion = 'llanta'; continue
            if 'fecha' in col0: continue

            fecha = str(row[0]).strip() if row[0] else ''

            def to_float(v):
                if v is None: return 0.0
                try: return float(str(v).replace(',', '').strip())
                except: return 0.0

            col1v = to_float(row[1]); col2v = to_float(row[2])
            col3v = to_float(row[3]); col4v = to_float(row[4])
            detalle = str(row[5]).strip() if row[5] else ''

            if seccion == 'manto':
                if not fecha and col1v > 0: tope_manto = int(col1v); continue
                if not fecha: continue
                if col2v > 0 or col3v > 0 or col4v > 0:
                    mantos.append({'f': fecha, 'n': str(row[1]).strip() if row[1] else '', 'o': col2v, 'c': col3v, 'a': col4v})
            elif seccion == 'llanta':
                if not fecha and col1v > 0: tope_llanta = int(col1v); continue
                if not fecha: continue
                if col2v > 0 or col3v > 0:
                    llantas.append({'f': fecha, 'm': detalle or 'Llantas', 'o': col2v, 'c': col3v})

        resultado = {'mantos': mantos, 'llantas': llantas,
                     'tope_manto': tope_manto, 'tope_llanta': tope_llanta,
                     'ultima_actualizacion': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(resultado, f, ensure_ascii=False, indent=2)

        print(f"  OK {output_file}: {len(mantos)} mantos, {len(llantas)} llantas")
        print(f"  Tope manto: {tope_manto} | Tope llanta: {tope_llanta}")

    except Exception as e:
        print(f"  Error: {e}")
        import traceback; traceback.print_exc()

# -- MAIN ---------------------------------------------------------------------
if __name__ == '__main__':
    usuario    = os.environ.get('HUNTER_USER', '')
    contrasena = os.environ.get('HUNTER_PASS', '')
    gkey       = os.environ.get('GAPI_KEY', '')

    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: no se encontro {CONFIG_FILE} en el repo"); sys.exit(1)
    with open(CONFIG_FILE, encoding='utf-8') as f:
        cfg = json.load(f)

    vehiculos = [v for v in cfg.get('vehiculos', []) if v.get('placa', '').strip()]
    if not vehiculos:
        print("ERROR: config.json no tiene vehiculos con placa"); sys.exit(1)

    print(f"Cliente: {cfg.get('empresa', {}).get('nombre', '?')} - {len(vehiculos)} vehiculo(s)")

    token = None
    if usuario and contrasena:
        try:
            token = login(usuario, contrasena)
        except Exception as e:
            print(f"ERROR login Hunter GPS: {e}")
    else:
        print("Faltan HUNTER_USER / HUNTER_PASS (secrets del repo) - saltando GPS")

    for v in vehiculos:
        placa = v['placa'].strip()
        slug = placa.replace('-', '')
        print(f"\n{'=' * 60}\nVEHICULO: {placa}\n{'=' * 60}")

        if token:
            actualizar_hunter(usuario, contrasena, token, placa, f'datos_hunter_{slug}.json')

        file_id_mantos = (v.get('file_id_mantos') or '').strip()
        if file_id_mantos and gkey:
            actualizar_mantos(gkey, file_id_mantos, f'datos_mantos_{slug}.json')
        elif file_id_mantos and not gkey:
            print("  Sin GAPI_KEY (secret del repo) - saltando mantenimientos")

    # Compatibilidad: si el cliente tiene un solo vehiculo, generar tambien
    # los nombres genericos datos_hunter.json / datos_mantos.json
    if len(vehiculos) == 1:
        slug = vehiculos[0]['placa'].strip().replace('-', '')
        for prefix in ('datos_hunter', 'datos_mantos'):
            src = f'{prefix}_{slug}.json'
            if os.path.exists(src):
                shutil.copyfile(src, f'{prefix}.json')
                print(f"  Copia generica: {prefix}.json")

    print("\nListo.")
