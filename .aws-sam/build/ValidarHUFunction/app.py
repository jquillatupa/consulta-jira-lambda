# hello_world/app.py

import os
import re
import json
import logging
import tempfile
import boto3
import spacy
from datetime import datetime
from jira import JIRA
from collections import defaultdict
from openpyxl import Workbook

logger = logging.getLogger()
logger.setLevel(logging.INFO)
s3 = boto3.client("s3")

# ===========================
# Intentar cargar modelo spaCy en español (“en blanco”)
# ===========================
try:
    nlp = spacy.blank("es")
except Exception:
    from spacy.cli import download as spacy_download
    spacy_download("es_core_news_sm")
    nlp = spacy.load("es_core_news_sm")

# ===========================
# CONFIGURACIÓN DE PUNTAJES
# ===========================
P1 = 20   # Descripción
P2 = 20   # Criterios de Aceptación
P3 = 15   # Asignatario
P4 = 20   # Subtareas
P5 = 10   # Épica Principal
P6 = 15   # Backlog Priorizado

# ===========================
# FUNCIONES AUXILIARES
# ===========================

def es_texto_valido(texto):
    if not isinstance(texto, str) or not texto.strip():
        return False
    limpia = re.sub(r"[^\wáéíóúüñÁÉÍÓÚÜÑ]+", "", texto)
    return bool(re.search(r"\w", limpia))

def limpiar_para_bloques(texto):
    t = texto.lower()
    t = re.sub(r"[*_~:`]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def _bloque_cqp(texto):
    t = limpiar_para_bloques(texto)
    lines = t.splitlines()
    c = q = p = False
    variantes = ["quiero", "queremos", "quisiera", "quisiéramos", "necesito", "necesitamos", "requiero", "deseo", "busco", "me gustaría"]
    for l in lines:
        w = l.split()
        if not w:
            continue
        if w[0] == "como" and len(w) > 1:
            c = True
        if w[0] in variantes and len(w) > 1:
            q = True
        if w[0] == "para" and len(w) > 1:
            p = True
    return c and q and p

def _contiene_verbo_directo(txt, lista):
    palabras = re.findall(r"\b\w+\b", txt.lower())
    return any(v in palabras for v in lista)

# ===========================
# EVALUACIÓN DESCRIPCIÓN (C1)
# ===========================
VERBOS_DESC      = ["crear", "desarrollar", "implementar", "validar", "mostrar", "generar", "obtener", "marcar"]
SUST_DESC        = ["validación", "consistencia", "ejecución", "documentación"]
VARIANTES_QUIERO = ["quiero", "necesito", "busco", "me gustaría"]

def evaluar_descripcion_detallada(texto):
    if not es_texto_valido(texto):
        return 0

    # 1) Limpiar asteriscos y unir líneas
    txt = re.sub(r"[\*\-•]", "", texto).replace("\n", " ")
    doc = nlp(txt)

    # 2) Estructura “Como... Quiero... Para...”
    expr = r"\bcomo\b.*\b(" + "|".join(VARIANTES_QUIERO) + r")\b.*\bpara\b"
    estructura = bool(re.search(expr, txt, flags=re.IGNORECASE)) or _bloque_cqp(texto)

    # 3) Detectar verbo y sustantivo vía spaCy
    verbo_nlp = any(tok.pos_ == "VERB" for tok in doc)
    sust_nlp  = any(tok.pos_ == "NOUN" for tok in doc)

    # 4) Longitud mínima: ≥15 palabras, o ≥8 si tiene algún verbo
    palabras = len(txt.split())
    longitud = palabras >= 15 or (verbo_nlp and palabras >= 8)

    # 5) Si estructura o acción (verbo o sustantivo) + longitud → puntaje
    if (estructura or verbo_nlp or sust_nlp) and longitud:
        return P1
    return 0

def observar_falla_descripcion(texto):
    if not es_texto_valido(texto):
        return "Texto vacío o inválido"
    txt, fallas = texto.lower(), []
    if len(txt.split()) < 15:
        fallas.append("Menos de 15 palabras")
    expr = re.search(
        r"\bcomo\b.*\b(" + "|".join(VARIANTES_QUIERO) + r")\b.*\bpara\b", 
        txt, flags=re.IGNORECASE
    )
    if not (expr or _bloque_cqp(texto)):
        fallas.append("Sin estructura Como-Quiero-Para")
    doc = nlp(txt)
    if not any(t.pos_ == "VERB" for t in doc) and not _contiene_verbo_directo(txt, VERBOS_DESC):
        fallas.append("Sin verbo válido")
    return "; ".join(fallas)

# ===========================
# EVALUACIÓN CRITERIOS (C2)
# ===========================
VERBOS_CRIT = ["validar", "entregar", "realizar", "listar", "actualizar", "eliminar"]
SUST_CRIT   = ["validación", "exactitud", "cumplimiento", "consistencia"]

def evaluar_criterios_detallado(texto):
    if not es_texto_valido(texto):
        return 0
    txt   = re.sub(r"[\*\-•]", "", texto)
    lines = texto.splitlines()
    items = [l for l in lines if re.match(r"^\s*([-*•]|\d+\.)\s+.+", l)]
    paras = [b for b in texto.split("\n\n") if len(b.split()) >= 4]
    impl  = [l for l in lines if len(l.strip().split()) >= 3]
    lista = len(items) >= 2 or len(paras) >= 2 or len(impl) >= 2
    doc   = nlp(txt)
    verbo     = any(t.pos_ == "VERB" for t in doc)
    verbo_bl  = _contiene_verbo_directo(txt, VERBOS_CRIT)
    sust      = any(s in txt.lower() for s in SUST_CRIT)
    return P2 if lista and (verbo or verbo_bl or sust) else 0

def observar_falla_criterios(texto):
    if not es_texto_valido(texto):
        return "Texto vacío o inválido"
    lines = texto.splitlines()
    items = [l for l in lines if re.match(r"^\s*([-*•]|\d+\.)\s+.+", l)]
    impl  = [l for l in lines if len(l.split()) >= 3]
    if len(items) < 2 and len(impl) < 2:
        return "Sin lista válida"
    txt = texto.lower()
    doc = nlp(txt)
    if not any(t.pos_ == "VERB" for t in doc) and not _contiene_verbo_directo(txt, VERBOS_CRIT):
        return "Sin verbo/acción clara"
    return ""

# ===========================
# EVALUACIÓN ASIGNATARIO (C3)
# ===========================
def evaluar_criterio_asignatario(a):
    return P3 if isinstance(a, str) and a.strip() else 0

# ===========================
# EVALUACIÓN SUBTAREAS (C4)
# ===========================
def evaluar_criterio_subtareas(n):
    try:
        return P4 if int(n) > 1 else 0
    except:
        return 0

# ===========================
# EVALUACIÓN ÉPICA PRINCIPAL (C5)
# ===========================
def evaluar_criterio_epica(p):
    return P5 if isinstance(p, str) and p.strip() else 0

# ===========================
# EVALUACIÓN BACKLOG PRIORIZADO (C6)
# (Implementamos este flag en el “pivot”, no en el detalle de cada historia)
# ===========================

# ===========================
# OBSERVACIONES POR FILA (Opcional)
# ===========================
def obs_desc_row(r):
    desc = r["Description"] if isinstance(r["Description"], str) else ""
    punt = r["Puntaje Descripción"]
    if punt > 0:
        razones = []
        if (re.search(r"\bcomo\b", desc, flags=re.IGNORECASE) or _bloque_cqp(desc)):
            razones.append("Estructura")
        if any(t.pos_ == "VERB" for t in nlp(desc)):
            razones.append("Verbo NLP")
        if _contiene_verbo_directo(desc, VERBOS_DESC):
            razones.append("Verbo blanco")
        if any(s in desc.lower() for s in SUST_DESC):
            razones.append("Sustantivo")
        if len(desc.split()) >= 15 or (_contiene_verbo_directo(desc, VERBOS_DESC) and len(desc.split()) >= 8):
            razones.append("Longitud OK")
        return "; ".join(dict.fromkeys(razones))
    else:
        return observar_falla_descripcion(desc)

def obs_crit_row(r):
    crit = r["Criterios de aceptación"] if isinstance(r["Criterios de aceptación"], str) else ""
    punt = r["Puntaje Criterios"]
    if punt > 0:
        razones = []
        lines = crit.splitlines()
        if len([l for l in lines if re.match(r"^\s*([-*•]|\d+\.)\s+.+", l)]) >= 2 or len([b for b in crit.split("\n\n") if len(b.split()) >= 4]) >= 2:
            razones.append("Lista OK")
        if any(t.pos_ == "VERB" for t in nlp(crit)):
            razones.append("Verbo NLP")
        if _contiene_verbo_directo(crit, VERBOS_CRIT):
            razones.append("Verbo blanco")
        if any(s in crit.lower() for s in SUST_CRIT):
            razones.append("Sustantivo")
        return "; ".join(dict.fromkeys(razones))
    else:
        return observar_falla_criterios(crit)

# ===========================
# HANDLER PRINCIPAL
# ===========================
def lambda_handler(event, context):
    # 1) Leer variables de entorno para Jira y S3
    jira_domain    = os.getenv("JIRA_DOMAIN")
    jira_user      = os.getenv("JIRA_USER")
    jira_api_token = os.getenv("JIRA_API_TOKEN")
    s3_bucket      = os.getenv("OUTPUT_S3_BUCKET")

    missing = [v for v in ["JIRA_DOMAIN", "JIRA_USER", "JIRA_API_TOKEN", "OUTPUT_S3_BUCKET"] if not os.getenv(v)]
    if missing:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": f"Faltan variables de entorno: {', '.join(missing)}"
            })
        }

    # 2) Conectarse a Jira
    try:
        options = {"server": f"https://{jira_domain}"}
        jira_client = JIRA(options, basic_auth=(jira_user, jira_api_token))
    except Exception as e:
        logger.exception("No se pudo autenticar en Jira")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Fallo al autenticar en Jira", "details": str(e)})
        }

    # 3) Definir y ejecutar la consulta JQL en Jira
    jql = "assignee=currentUser() AND resolution = Unresolved ORDER BY priority DESC"
    try:
        issues_jira = jira_client.search_issues(
            jql,
            maxResults=50,
            fields=[
                "key",
                "summary",
                "status",
                "description",
                "customfield_10031",
                "assignee",
                "subtasks",
                "parent",
            ],
            expand="changelog"
        )
    except Exception as e:
        logger.exception("Error al ejecutar search_issues en Jira")
        return {
            "statusCode": 502,
            "body": json.dumps({"error": "Fallo al consultar Jira", "details": str(e)})
        }

    # 4) Armar lista de diccionarios con todos los campos y puntajes
    registros = []
    for issue in issues_jira:
        campos      = issue.fields
        key         = issue.key
        summary     = campos.summary
        status      = campos.status.name
        description = campos.description or ""
        assignee    = campos.assignee.displayName if campos.assignee else None
        criteria    = getattr(campos, "customfield_10031", None) or ""
        epic_sum    = campos.parent.fields.summary if hasattr(campos, "parent") and campos.parent else ""
        num_subs    = len(campos.subtasks) if hasattr(campos, "subtasks") else 0

        punt_desc  = evaluar_descripcion_detallada(description)
        punt_crit  = evaluar_criterios_detallado(criteria or "")
        punt_asig  = evaluar_criterio_asignatario(assignee or "")
        punt_subtk = evaluar_criterio_subtareas(num_subs)
        punt_epica = evaluar_criterio_epica(epic_sum or "")

        registros.append({
            "Key": key,
            "Summary": summary,
            "Status": status,
            "Description": description,
            "Assignee": assignee or "Sin Asignar",
            "Criterios de aceptación": criteria,
            "Épica Principal": epic_sum,
            "Número de Sub-tareas": num_subs,
            "Puntaje Descripción": punt_desc,
            "Puntaje Criterios": punt_crit,
            "Puntaje Asignatario": punt_asig,
            "Puntaje Subtareas": punt_subtk,
            "Puntaje Épica": punt_epica,
            # Si deseas agregar observaciones por fila, puedes descomentar:
            # "Observación Descripción": obs_desc_row(r),  
            # "Observación Criterios": obs_crit_row(r)
        })

    if not registros:
        return {"statusCode": 200, "body": json.dumps({"message": "No se encontraron issues"})}

    # 5) Construir “pivot” en memoria para conteo por assignee y status
    pivot = defaultdict(lambda: defaultdict(int))
    for r in registros:
        a = r["Assignee"]
        s = r["Status"]
        pivot[a][s] += 1

    todos_estados = sorted({s for sub in pivot.values() for s in sub.keys()})

    # 6) Crear un archivo Excel en /tmp/ con openpyxl
    timestamp   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    nombre_arch = f"reporte_jira_{timestamp}.xlsx"
    ruta_local  = f"/tmp/{nombre_arch}"

    try:
        wb  = Workbook()
        ws1 = wb.active
        ws1.title = "Raw_Issues"
        encabezados = [
            "Key",
            "Summary",
            "Status",
            "Description",
            "Assignee",
            "Criterios de aceptación",
            "Épica Principal",
            "Número de Sub-tareas",
            "Puntaje Descripción",
            "Puntaje Criterios",
            "Puntaje Asignatario",
            "Puntaje Subtareas",
            "Puntaje Épica"
        ]
        ws1.append(encabezados)
        for r in registros:
            fila = [
                r["Key"],
                r["Summary"],
                r["Status"],
                r["Description"],
                r["Assignee"],
                r["Criterios de aceptación"],
                r["Épica Principal"],
                r["Número de Sub-tareas"],
                r["Puntaje Descripción"],
                r["Puntaje Criterios"],
                r["Puntaje Asignatario"],
                r["Puntaje Subtareas"],
                r["Puntaje Épica"]
            ]
            ws1.append(fila)

        ws2 = wb.create_sheet("Resumen_Pivot")
        ws2.append(["Assignee"] + todos_estados)
        for a, subdict in pivot.items():
            fila = [a] + [subdict.get(s, 0) for s in todos_estados]
            ws2.append(fila)

        wb.save(ruta_local)
    except Exception as e:
        logger.exception("No se pudo escribir el archivo Excel en /tmp/")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Error al generar Excel", "details": str(e)})
        }

    # 7) Subir el archivo Excel a S3
    s3_key = f"reports/{nombre_arch}"
    try:
        s3.upload_file(ruta_local, s3_bucket, s3_key)
    except Exception as e:
        logger.exception("Error al subir el Excel a S3")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Fallo al subir a S3", "details": str(e)})
        }

    # 8) Generar una URL pre-firmada para quien reciba la respuesta
    try:
        url_presignada = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3_bucket, "Key": s3_key},
            ExpiresIn=3600  # válida por 1 hora
        )
    except Exception:
        url_presignada = f"https://{s3_bucket}.s3.amazonaws.com/{s3_key}"

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Reporte generado correctamente",
            "report_url": url_presignada
        })
    }
