# hello_world/app.py

import os
import re
import json
import logging
import tempfile
import boto3
import pandas as pd
import requests
import spacy

from datetime import datetime
from jira import JIRA
from xlsxwriter.utility import xl_col_to_name
from spacy.cli import download as spacy_download

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------
# 1) Configuración inicial
# ---------------------------------------------------
# Rango de fechas (ajusta según necesites)
START_DATE = datetime(2025, 1, 1)
END_DATE   = datetime(2025, 3, 31, 23, 59, 59)

# IDs de categoría en Jira que nos interesan
IDS_CATEGORIAS = ["10008", "10009"]

# Campo de “Criterios de aceptación”
CUSTOM_FIELD_ID = "customfield_10031"

# Puntuaciones
P1 = 20   # Descripción
P2 = 20   # Criterios de Aceptación
P3 = 15   # Asignatario
P4 = 20   # Subtareas
P5 = 10   # Épica Principal
P6 = 15   # Backlog Priorizado

# Aseguramos modelo spaCy (español)
try:
    nlp = spacy.load("es_core_news_sm")
except OSError:
    spacy_download("es_core_news_sm")
    nlp = spacy.load("es_core_news_sm")

# Cliente S3
s3 = boto3.client("s3")


# ---------------------------------------------------
# 2) Funciones auxiliares para scoring
# ---------------------------------------------------
def es_texto_valido(texto):
    return isinstance(texto, str) and bool(texto.strip())

def limpiar_para_bloques(texto):
    t = texto.lower()
    t = re.sub(r'[*_~:`]', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def _bloque_cqp(texto):
    t = limpiar_para_bloques(texto)
    variantes = ['quiero','queremos','quisiera','necesito','deseo','busco','me gustaría']
    lines = t.splitlines()
    c = q = p = False
    for l in lines:
        w = l.split()
        if not w: continue
        if w[0]=='como'         and len(w)>1: c = True
        if w[0] in variantes    and len(w)>1: q = True
        if w[0]=='para'         and len(w)>1: p = True
    return c and q and p

def evaluar_descripcion(texto):
    if not es_texto_valido(texto): return 0
    txt = re.sub(r'[\*\-•]', '', texto).replace('\n',' ')
    doc = nlp(txt)
    expr = r'\bcomo\b.*\b(quiero|necesito|busco)\b.*\bpara\b'
    estructura = bool(re.search(expr, txt, flags=re.IGNORECASE)) or _bloque_cqp(texto)
    verbo_nlp = any(tok.pos_=="VERB" for tok in doc)
    palabras  = len(txt.split())
    longitud  = palabras>=15 or (verbo_nlp and palabras>=8)
    return P1 if (estructura or verbo_nlp) and longitud else 0

def evaluar_criterios(texto):
    if not es_texto_valido(texto): return 0
    lines = texto.splitlines()
    items = [l for l in lines if re.match(r'^\s*([-*•]|\d+\.)\s+.+', l)]
    paras = [b for b in texto.split('\n\n') if len(b.split())>=4]
    doc   = nlp(texto)
    verbo = any(tok.pos_=="VERB" for tok in doc)
    return P2 if (len(items)>=2 or len(paras)>=2) and verbo else 0

def evaluar_asignatario(a):
    return P3 if isinstance(a, str) and a.strip() else 0

def evaluar_subtareas(n):
    try: return P4 if int(n)>1 else 0
    except: return 0

def evaluar_epica(p):
    return P5 if isinstance(p, str) and p.strip() else 0

# ---------------------------------------------------
# 3) Función para extraer proyectos por categoría
# ---------------------------------------------------
HEADERS = {"Accept":"application/json"}

def fetch_projects_by_category(category_id, jira_domain, jira_user, jira_api_token):
    all_values = []
    start_at   = 0
    max_results= 100
    while True:
        url = f"{jira_domain}/rest/api/3/project/search"
        params = {
            "categoryId": category_id,
            "expand":     "lead",
            "maxResults": max_results,
            "startAt":    start_at
        }
        resp = requests.get(
            url,
            auth=(jira_user, jira_api_token),
            headers=HEADERS,
            params=params
        )
        resp.raise_for_status()
        data = resp.json()
        all_values.extend(data.get("values", []))
        if data.get("isLast", True): break
        start_at += data.get("maxResults", max_results)
    return all_values


# ---------------------------------------------------
# 4) Lambda handler
# ---------------------------------------------------
def lambda_handler(event, context):
    # leer env vars
    jira_domain    = os.getenv("JIRA_DOMAIN")
    jira_user      = os.getenv("JIRA_USER")
    jira_api_token = os.getenv("JIRA_API_TOKEN")
    bucket         = os.getenv("OUTPUT_S3_BUCKET")

    # validar
    faltan = [v for v in ["JIRA_DOMAIN","JIRA_USER","JIRA_API_TOKEN","OUTPUT_S3_BUCKET"] if not os.getenv(v)]
    if faltan:
        return {"statusCode":400, "body": json.dumps({"error":f"Faltan vars: {faltan}"})}

    # autenticar JIRA
    try:
        jira = JIRA({"server":jira_domain}, basic_auth=(jira_user, jira_api_token))
    except Exception as e:
        logger.exception("No se pudo autenticar en Jira")
        return {"statusCode":500, "body":json.dumps({"error":"Autenticación Jira fallida","details":str(e)})}

    # obtener proyectos filtrados
    proyectos = []
    vistos     = set()
    for cat in IDS_CATEGORIAS:
        for p in fetch_projects_by_category(cat, jira_domain, jira_user, jira_api_token):
            if p["key"] not in vistos:
                vistos.add(p["key"])
                proyectos.append({
                    "key":          p["key"],
                    "name":         p["name"],
                    "category":     p.get("projectCategory",{}).get("name"),
                    "lead":         p.get("lead",{}).get("displayName")
                })

    PROJECT_KEYS = [p["key"] for p in proyectos]

    # recorrer cada proyecto y sus historias
    data  = []
    no_issues = []
    for pk in PROJECT_KEYS:
        issues = jira.search_issues(
            f'project="{pk}" AND issuetype=Story',
            maxResults=1000, expand="changelog"
        )
        if not issues:
            no_issues.append(pk)
        for issue in issues:
            # extraer último estado en periodo
            ultimo = None
            for h in issue.changelog.histories:
                dt = datetime.strptime(h.created[:19], "%Y-%m-%dT%H:%M:%S")
                if START_DATE<=dt<=END_DATE:
                    for it in h.items:
                        if it.field=="status":
                            ultimo = it.toString
            estado = ultimo or issue.fields.status.name
            parent = getattr(issue.fields, "parent", None)
            data.append({
                "Proyecto":           pk,
                "Nombre del Proyecto": next((x["name"] for x in proyectos if x["key"]==pk), ""),
                "Categoría Proyecto":  next((x["category"] for x in proyectos if x["key"]==pk), ""),
                "Responsable Proyecto":next((x["lead"] for x in proyectos if x["key"]==pk), ""),
                "Key":                issue.key,
                "Summary":            issue.fields.summary,
                "Status":             estado,
                "Description":        issue.fields.description or "",
                "Criterios de aceptación": getattr(issue.fields, CUSTOM_FIELD_ID, ""),
                "Epica Principal":    getattr(parent, "fields", {}).__dict__.get("summary","") if parent else "",
                "Assignee":           issue.fields.assignee.displayName if issue.fields.assignee else "",
                "Número de Sub-tareas": len(issue.fields.subtasks)
            })

    # 5) Exportar primer excel (raw) a /tmp y subir a DataJira
    df = pd.DataFrame(data)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    tmp1 = f"/tmp/jira_data_{ts}.xlsx"
    df.to_excel(tmp1, sheet_name="Stories", index=False)

    key1 = f"reports/DataJira/jira_data_{ts}.xlsx"
    s3.upload_file(tmp1, bucket, key1)

    # ---------------------------------------------------
    # 6) Scoring y generación de segundo excel
    # ---------------------------------------------------
    df["Puntaje Descripción"] = df["Description"].apply(evaluar_descripcion)
    df["Puntaje Criterios"]   = df["Criterios de aceptación"].apply(evaluar_criterios)
    df["Puntaje Asignatario"] = df["Assignee"].apply(evaluar_asignatario)
    df["Puntaje Subtareas"]   = df["Número de Sub-tareas"].apply(evaluar_subtareas)
    df["Puntaje Épica"]       = df["Epica Principal"].apply(evaluar_epica)

    # flag backlog
    backlog_flag = df.groupby("Proyecto")["Status"]\
                     .apply(lambda s: P6 if s.str.lower().eq("backlog priorizado").any() else 0)
    # resumen por proyecto
    resumen = df.groupby(
        ["Proyecto","Nombre del Proyecto","Responsable Proyecto"]
    )["Puntaje Descripción","Puntaje Criterios","Puntaje Asignatario","Puntaje Subtareas","Puntaje Épica"]\
     .mean().reset_index()

    resumen["C6 Backlog"]       = resumen["Proyecto"].map(backlog_flag)
    resumen["Puntaje Total"]    = resumen[[
        "Puntaje Descripción","Puntaje Criterios","Puntaje Asignatario",
        "Puntaje Subtareas","Puntaje Épica","C6 Backlog"
    ]].sum(axis=1)
    # clasificación
    def cls(v):
        return ("Excelente" if v>=90 else
                "Adecuado" if v>=80 else
                "Por mejorar" if v>=65 else
                "Incompleto" if v>=50 else
                "Desastre")
    resumen["Clasificación"] = resumen["Puntaje Total"].apply(cls)

    # 7) Exportar segundo excel (analizado)
    tmp2 = f"/tmp/jira_data_anali_{ts}.xlsx"
    with pd.ExcelWriter(tmp2, engine="xlsxwriter") as w:
        df.to_excel(w, sheet_name="User Stories", index=False)
        resumen.to_excel(w, sheet_name="Resumen por Proyecto", index=False)

        # formato numérico (2 decimales) en Resumen
        book  = w.book
        ws    = w.sheets["Resumen por Proyecto"]
        fmt   = book.add_format({"num_format":"0.00"})
        for i,col in enumerate(resumen.columns):
            if col in ["Puntaje Descripción","Puntaje Criterios","Puntaje Asignatario",
                       "Puntaje Subtareas","Puntaje Épica","Puntaje Total"]:
                letter = xl_col_to_name(i)
                ws.set_column(f"{letter}:{letter}", None, fmt)

    key2 = f"reports/Analizado/jira_data_anali_{ts}.xlsx"
    s3.upload_file(tmp2, bucket, key2)

    # 8) Generar URL pre-firmada del analizado
    url2 = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket":bucket, "Key":key2},
        ExpiresIn=3600
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Reportes generados correctamente",
            "raw_report":     f"https://{bucket}.s3.amazonaws.com/{key1}",
            "analizado_report": url2
        })
    }
