# hello_world/app.py

import os
import json
import logging
import boto3
import pandas as pd
import re
import spacy
import base64

from datetime import datetime
from jira import JIRA, JIRAError
from botocore.exceptions import ClientError
from xlsxwriter.utility import xl_col_to_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ===========================
# 1) INICIALIZAR CLIENTES Y MODELOS FUERA DEL HANDLER
# ===========================
# Cliente S3
s3 = boto3.client("s3")

# Intentar cargar modelo spaCy en español
try:
    nlp = spacy.load("es_core_news_sm")
except OSError:
    from spacy.cli import download as spacy_download
    spacy_download("es_core_news_sm")
    nlp = spacy.load("es_core_news_sm")

# ===========================
# 2) CONFIGURACIÓN DE PUNTAJES
# ===========================
P1 = 20   # Descripción
P2 = 20   # Criterios de Aceptación
P3 = 15   # Asignatario
P4 = 20   # Subtareas
P5 = 10   # Épica Principal
P6 = 15   # Backlog Priorizado

# ===========================
# 3) FUNCIONES AUXILIARES PARA EVALUACIÓN
# ===========================
def es_texto_valido(texto):
    if not isinstance(texto, str) or not texto.strip():
        return False
    limpia = re.sub(r'[^\wáéíóúüñÁÉÍÓÚÜÑ]+', '', texto)
    return bool(re.search(r'\w', limpia))

def limpiar_para_bloques(texto):
    t = texto.lower()
    t = re.sub(r'[*_~:`]', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def _bloque_cqp(texto):
    t = limpiar_para_bloques(texto)
    lines = t.splitlines()
    c = q = p = False
    variantes = [
        'quiero','queremos','quisiera','quisiéramos',
        'necesito','necesitamos','requiero','deseo',
        'busco','me gustaría'
    ]
    for l in lines:
        w = l.split()
        if not w:
            continue
        if w[0] == 'como' and len(w) > 1:
            c = True
        if w[0] in variantes and len(w) > 1:
            q = True
        if w[0] == 'para' and len(w) > 1:
            p = True
    return c and q and p

def _contiene_verbo_directo(txt, lista_verbos):
    palabras = re.findall(r'\b\w+\b', txt.lower())
    return any(v in palabras for v in lista_verbos)

# ===========================
# 4) EVALUACIÓN “DESCRIPCIÓN DETALLADA” (C1)
# ===========================
VERBOS_DESC      = ['crear','desarrollar','implementar','validar','mostrar','generar','obtener','marcar']
SUST_DESC        = ['validación','consistencia','ejecución','documentación']
VARIANTES_QUIERO = ['quiero','necesito','busco','me gustaría']

def evaluar_descripcion_detallada(texto):
    if not es_texto_valido(texto):
        return 0
    txt = re.sub(r'[\*\-•]', '', texto).replace('\n', ' ')
    doc = nlp(txt)
    expr = r'\bcomo\b.*\b(' + '|'.join(VARIANTES_QUIERO) + r')\b.*\bpara\b'
    estructura = bool(re.search(expr, txt, flags=re.IGNORECASE)) or _bloque_cqp(texto)
    verbo_nlp = any(tok.pos_ == "VERB" for tok in doc)
    sust_nlp  = any(tok.pos_ == "NOUN" for tok in doc)
    palabras = len(txt.split())
    longitud = palabras >= 15 or (verbo_nlp and palabras >= 8)
    return P1 if (estructura or verbo_nlp or sust_nlp) and longitud else 0

# ===========================
# 5) EVALUACIÓN “CRITERIOS DE ACEPTACIÓN” (C2)
# ===========================
VERBOS_CRIT = ['validar','entregar','realizar','listar','actualizar','eliminar']
SUST_CRIT   = ['validación','exactitud','cumplimiento','consistencia']

def evaluar_criterios_detallado(texto):
    if not es_texto_valido(texto):
        return 0
    txt   = re.sub(r'[\*\-•]', '', texto)
    lines = texto.splitlines()
    items = [l for l in lines if re.match(r'^\s*([-*•]|\d+\.)\s+.+', l)]
    paras = [b for b in texto.split('\n\n') if len(b.split()) >= 4]
    impl  = [l for l in lines if len(l.strip().split()) >= 3]
    lista = len(items) >= 2 or len(paras) >= 2 or len(impl) >= 2
    doc   = nlp(txt)
    verbo = any(t.pos_ == "VERB" for t in doc)
    verbo_bl = _contiene_verbo_directo(txt, VERBOS_CRIT)
    sust     = any(s in txt.lower() for s in SUST_CRIT)
    return P2 if lista and (verbo or verbo_bl or sust) else 0

# ===========================
# 6) EVALUACIÓN OTROS CRITERIOS (C3, C4, C5)
# ===========================
def evaluar_criterio_asignatario(a):
    return P3 if isinstance(a, str) and a.strip() else 0

def evaluar_criterio_subtareas(n):
    try:
        return P4 if int(n) > 1 else 0
    except:
        return 0

def evaluar_criterio_principal(p):
    return P5 if isinstance(p, str) and p.strip() else 0

# ===========================
# 7) OBSERVACIONES DE FALLOS
# ===========================
def observar_falla_descripcion(texto):
    if not es_texto_valido(texto):
        return "Texto vacío o inválido"
    txt = texto.lower()
    fallas = []
    if len(txt.split()) < 15:
        fallas.append("Menos de 15 palabras")
    expr = re.search(
        r'\bcomo\b.*\b(' + '|'.join(VARIANTES_QUIERO) + r')\b.*\bpara\b',
        txt, flags=re.IGNORECASE
    )
    if not (expr or _bloque_cqp(texto)):
        fallas.append("Sin estructura Como-Quiero-Para")
    doc = nlp(txt)
    if not any(t.pos_ == "VERB" for t in doc) and not _contiene_verbo_directo(txt, VERBOS_DESC):
        fallas.append("Sin verbo válido")
    return "; ".join(fallas)

def observar_falla_criterios(texto):
    if not es_texto_valido(texto):
        return "Texto vacío o inválido"
    lines = texto.splitlines()
    items = [l for l in lines if re.match(r'^\s*([-*•]|\d+\.)\s+.+', l)]
    impl  = [l for l in lines if len(l.split()) >= 3]
    if len(items) < 2 and len(impl) < 2:
        return "Sin lista válida"
    txt = texto.lower()
    doc = nlp(txt)
    if not any(t.pos_ == "VERB" for t in doc) and not _contiene_verbo_directo(txt, VERBOS_CRIT):
        return "Sin verbo/acción clara"
    return ""

def obs_desc_row(r):
    desc = r.get('Description', "")
    punt = r.get('Puntaje Descripción', 0)
    if punt > 0:
        razones = []
        if (re.search(r'\bcomo\b', desc, flags=re.IGNORECASE) or _bloque_cqp(desc)):
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
    crit = r.get('Criterios de aceptación', "")
    punt = r.get('Puntaje Criterios', 0)
    if punt > 0:
        razones = []
        lines = crit.splitlines()
        if len([l for l in lines if re.match(r'^\s*([-*•]|\d+\.)\s+.+', l)]) >= 2 or \
           len([b for b in crit.split('\n\n') if len(b.split()) >= 4]) >= 2:
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
# 8) FUNCIÓN PRINCIPAL LAMBDA
# ===========================
def lambda_handler(event, context):
    """
    1) Lee variables de entorno (JIRA_DOMAIN, JIRA_USER, JIRA_API_TOKEN, S3_BUCKET, CRUDO_KEY)
    2) Conecta a JIRA usando UTF-8 Base64 en lugar de basic_auth directo
    3) Extrae las User Stories de TBVNS en un rango fijo de fechas
    4) Genera el Excel “crudo” y lo sube a S3 → reports/jira_data_<fecha>.xlsx
    5) Procesa df_issues con spaCy + pandas para generar el Excel “analizado”
    6) Sube el Excel analizado a S3 → reports/Analizado/jira_data_analizado_<fecha>.xlsx
    7) Devuelve URL pre-firmada para el Excel analizado
    """

    # ===========================
    # 8.1) LEER VARIABLES DE ENTORNO
    # ===========================
    jira_domain    = os.getenv("JIRA_DOMAIN")
    jira_user      = os.getenv("JIRA_USER")
    raw_token      = os.getenv("JIRA_API_TOKEN")
    S3_BUCKET      = os.getenv("S3_BUCKET") or os.getenv("OUTPUT_S3_BUCKET")
    CRUDO_KEY      = os.getenv("CRUDO_KEY")

    missing = []
    if not jira_domain:     missing.append("JIRA_DOMAIN")
    if not jira_user:       missing.append("JIRA_USER")
    if not raw_token:       missing.append("JIRA_API_TOKEN")
    if not S3_BUCKET:       missing.append("S3_BUCKET or OUTPUT_S3_BUCKET")
    if not CRUDO_KEY:       missing.append("CRUDO_KEY")

    if missing:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "error": f"Faltan variables de entorno: {', '.join(missing)}"
            })
        }

    # ===========================
    # 8.2) CONSTRUIR HEADER BASIC AUTH UTF-8 → Base64
    # ===========================
    cred_utf8 = f"{jira_user}:{raw_token}".encode("utf-8")
    b64_cred  = base64.b64encode(cred_utf8).decode("ascii")
    custom_headers = {
        "Authorization": f"Basic {b64_cred}"
    }

    # ===========================
    # 8.3) CONECTAR A JIRA usando headers personalizados
    # ===========================
    try:
        options = {
            "server": jira_domain,
            "headers": custom_headers
        }
        jira_client = JIRA(options=options)
    except Exception as e:
        logger.exception("No se pudo autenticar en Jira usando UTF-8 Basic Auth")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Fallo al autenticar en Jira con UTF-8 Basic Auth",
                "details": str(e)
            })
        }

    # ===========================
    # 8.4) RANGO DE FECHAS Y PROYECTOS
    # ===========================
    start_date      = datetime(2025, 1, 1)
    end_date        = datetime(2025, 5, 31, 23, 59, 59)
    CUSTOM_FIELD_ID = "customfield_10031"
    PROJECT_KEYS = ["TEODV", "CDV", "TBVNS", "TBRS", "DIGB2B08", "AFG001", "FRL001", "FSP001", "FVS001", "TDSGCP", "TDSID", "SMTOND", "ESB003SQ9", "ESB001SQ3", "ESB003SQ8", "TCED", "TTRDP", "TTD", "CAP001SQ4", "PCGDA", "DMD", "OPVID", "CMD", "WXDIG", "WXSC", "DIGB2B02", "TBADES", "DIGB2B07", "RNORM", "BDBEN", "TCDRS", "PDAD", "ROY008", "T0105", "ROY011", "ROY002", "ROV004", "PAYX", "PAYXDESA", "GRO006", "GRO009", "EBDAT", "GRO010", "IEBMP", "IEEBEO", "IPS", "IPSC", "CNTRL", "TCTM", "TPMGDG", "MEJORAVIDA", "PRODVIDA", "VIVENTA360", "TVPCD", "TCCDE"]
    #PROJECT_KEYS    = ["TBVNS"]

    # ===========================
    # 8.5) AUXILIAR: ÚLTIMO ESTADO EN PERIODO
    # ===========================
    def obtener_ultimo_estado_en_periodo(issue, start_d, end_d):
        ultimo_estado = None
        ultimo_cambio = None
        for history in issue.changelog.histories:
            history_date = datetime.strptime(history.created[:19], "%Y-%m-%dT%H:%M:%S")
            if start_d <= history_date <= end_d:
                for item in history.items:
                    if item.field == "status":
                        if (ultimo_cambio is None) or (history_date > ultimo_cambio):
                            ultimo_estado = item.toString
                            ultimo_cambio = history_date
        return ultimo_estado

    # ===========================
    # 8.6) EXTRAER DATA DE JIRA
    # ===========================
    all_data   = []
    sin_issues = []

    for project_key in PROJECT_KEYS:
        issues_encontrados = False
        try:
            detalle       = jira_client.project(project_key)
            nombre_proy   = detalle.name
            categoria     = detalle.projectCategory.name if (hasattr(detalle, "projectCategory") and detalle.projectCategory) else "Sin categoría"
            responsable   = detalle.lead.displayName if hasattr(detalle.lead, "displayName") else "Sin responsable"
        except JIRAError as e:
            logger.error(f"Proyecto '{project_key}' inválido o sin acceso: {e}")
            sin_issues.append({
                "Proyecto": project_key,
                "Nombre del Proyecto": "",
                "Categoría Proyecto": "",
                "Responsable Proyecto": "",
                "Mensaje": f"Proyecto '{project_key}' inválido o sin acceso."
            })
            continue  # pasar al siguiente proyecto

        jql_query = f'project = {project_key} AND issuetype = Story'
        start_at   = 0
        batch_size = 100

        while True:
            try:
                issues_batch = jira_client.search_issues(
                    jql_query,
                    startAt   = start_at,
                    maxResults= batch_size,
                    fields    = [
                        "key","summary","status","description",
                        CUSTOM_FIELD_ID, "assignee","subtasks","parent"
                    ],
                    expand    = "changelog"
                )
            except JIRAError as e:
                logger.error(f"Error en search_issues para {project_key}: {e}")
                break

            if not issues_batch:
                break

            for issue in issues_batch:
                ultimo_estado = obtener_ultimo_estado_en_periodo(issue, start_date, end_date)
                incluir_issue = False
                estado_actual = issue.fields.status.name if issue.fields.status else ""
                if estado_actual.lower() == "backlog priorizado":
                    incluir_issue = True
                    estado_final = "backlog priorizado"
                elif ultimo_estado:
                    incluir_issue = True
                    estado_final = ultimo_estado
                else:
                    estado_final = None

                if incluir_issue:
                    issues_encontrados = True
                    parent_key     = issue.fields.parent.key if hasattr(issue.fields, "parent") and issue.fields.parent else None
                    parent_summary = issue.fields.parent.fields.summary if hasattr(issue.fields, "parent") and issue.fields.parent else None

                    all_data.append({
                        "Proyecto": project_key,
                        "Nombre del Proyecto": nombre_proy,
                        "Categoría Proyecto": categoria,
                        "Responsable Proyecto": responsable,
                        "Key": issue.key,
                        "Summary": issue.fields.summary,
                        "Status": estado_final,
                        "Description": issue.fields.description or "",
                        "Criterios de aceptación": getattr(issue.fields, CUSTOM_FIELD_ID, None) or "",
                        "Épica Principal": parent_summary or "",
                        "Assignee": issue.fields.assignee.displayName if issue.fields.assignee else "",
                        "Número de Sub-tareas": len(issue.fields.subtasks) if hasattr(issue.fields, "subtasks") else 0
                    })

            start_at += batch_size

        if not issues_encontrados:
            sin_issues.append({
                "Proyecto": project_key,
                "Nombre del Proyecto": nombre_proy,
                "Categoría Proyecto": categoria,
                "Responsable Proyecto": responsable,
                "Mensaje": "Proyecto no tiene historias actualizadas en este periodo."
            })

    # ===========================
    # 8.7) CREAR DATAFRAMES INICIALES
    # ===========================
    df_issues     = pd.DataFrame(all_data, columns=[
        "Proyecto","Nombre del Proyecto","Categoría Proyecto","Responsable Proyecto",
        "Key","Summary","Status","Description","Criterios de aceptación",
        "Épica Principal","Assignee","Número de Sub-tareas"
    ])
    df_sin_issues = pd.DataFrame(sin_issues, columns=[
        "Proyecto","Nombre del Proyecto","Categoría Proyecto","Responsable Proyecto","Mensaje"
    ])

    # ===========================
    # 8.8) GENERAR EL EXCEL “CRUDO” EN /tmp
    # ===========================
    fecha_str    = datetime.utcnow().strftime("%Y-%m-%d")
    nombre_crudo = f"jira_data_{fecha_str}.xlsx"
    ruta_crudo   = f"/tmp/{nombre_crudo}"

    try:
        with pd.ExcelWriter(ruta_crudo, engine="xlsxwriter") as writer:
            if not df_issues.empty:
                df_issues.to_excel(writer, sheet_name="User Stories", index=False)
            else:
                # Crear hoja vacía con encabezados si no hay datos
                pd.DataFrame(columns=[
                    "Proyecto","Nombre del Proyecto","Categoría Proyecto","Responsable Proyecto",
                    "Key","Summary","Status","Description","Criterios de aceptación",
                    "Épica Principal","Assignee","Número de Sub-tareas"
                ]).to_excel(writer, sheet_name="User Stories", index=False)

            if not df_sin_issues.empty:
                df_sin_issues.to_excel(writer, sheet_name="Proyectos sin Issues", index=False)
            else:
                pd.DataFrame(columns=[
                    "Proyecto","Nombre del Proyecto","Categoría Proyecto","Responsable Proyecto","Mensaje"
                ]).to_excel(writer, sheet_name="Proyectos sin Issues", index=False)

            # Ajustes de ancho/formatos opcionales:
            workbook  = writer.book
            worksheet1 = writer.sheets["User Stories"]
            worksheet1.set_column("A:A", 15)
            worksheet2 = writer.sheets["Proyectos sin Issues"]
            worksheet2.set_column("A:A", 15)
            worksheet2.set_column("B:B", 30)
            worksheet2.set_column("C:C", 20)
            worksheet2.set_column("D:D", 25)
            worksheet2.set_column("E:E", 50)
    except Exception as e:
        logger.exception("Error al escribir el Excel crudo en /tmp/")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Error al generar Excel crudo",
                "details": str(e)
            })
        }

    # ===========================
    # 8.9) SUBIR EL EXCEL “CRUDO” A S3 EN reports/
    # ===========================
    s3_key_crudo = f"reports/{nombre_crudo}"
    try:
        s3.upload_file(ruta_crudo, S3_BUCKET, s3_key_crudo)
    except Exception as e:
        logger.exception("Error al subir el Excel crudo a S3")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Fallo al subir Excel crudo a S3",
                "details": str(e)
            })
        }

    # ===========================
    # 8.10) PROCESAR “ANALIZADO” SIN VOLVER A DESCARGAR (reutilizando df_issues)
    # ===========================
    if df_issues.empty:
        # Si no hay historias, creamos un DataFrame pivot vacío con las columnas esperadas.
        pivot = pd.DataFrame(
            columns=[
                "Proyecto","Nombre del Proyecto","Responsable del Proyecto",
                "Total Historias Analizadas",
                "C1 Descripción","C2 Criterios","C3 Asignatario",
                "C4 Subtareas","C5 Épica","C6 Backlog",
                "Puntaje Total","Clasificación General"
            ]
        )
    else:
        # 8.10.1) Agregar columnas de puntaje (C1…C5)
        df_issues['Puntaje Descripción'] = df_issues['Description'].apply(evaluar_descripcion_detallada)
        df_issues['Puntaje Criterios']   = df_issues['Criterios de aceptación'].apply(evaluar_criterios_detallado)
        df_issues['Puntaje Asignatario'] = df_issues['Assignee'].apply(evaluar_criterio_asignatario)
        df_issues['Puntaje Subtareas']   = df_issues['Número de Sub-tareas'].apply(evaluar_criterio_subtareas)
        df_issues['Puntaje Épica']       = df_issues['Épica Principal'].apply(evaluar_criterio_principal)

        # 8.10.2) Agregar columnas de observación
        df_issues['Observación Descripción']             = df_issues.apply(obs_desc_row, axis=1)
        df_issues['Observación Criterios de Aceptación'] = df_issues.apply(obs_crit_row, axis=1)

        # 8.10.3) Construir DataFrame “Resumen por Proyecto”
        res = []
        for _, r in df_issues.iterrows():
            res.append({
                'Proyecto': r['Proyecto'],
                'Nombre del Proyecto': r['Nombre del Proyecto'],
                'Responsable del Proyecto': r['Responsable Proyecto'],
                'C1 Descripción': r['Puntaje Descripción'],
                'C2 Criterios':   r['Puntaje Criterios'],
                'C3 Asignatario': r['Puntaje Asignatario'],
                'C4 Subtareas':   r['Puntaje Subtareas'],
                'C5 Épica':       r['Puntaje Épica']
            })
        res_df = pd.DataFrame(res)

        pivot = (
            res_df
            .groupby(['Proyecto','Nombre del Proyecto','Responsable del Proyecto'])
            .mean()
            .reset_index()
        )

        counts = df_issues['Proyecto'].value_counts()
        pivot['Total Historias Analizadas'] = pivot['Proyecto'].map(counts).fillna(0).astype(int)

        base  = ['Proyecto','Nombre del Proyecto','Responsable del Proyecto','Total Historias Analizadas']
        resto = [c for c in pivot.columns if c not in base]
        pivot = pivot[ base + resto ]

        backlog_flag = (
            df_issues
            .groupby('Proyecto')['Status']
            .apply(lambda series: P6 if series.str.lower().eq('backlog priorizado').any() else 0)
        )
        pivot['C6 Backlog'] = pivot['Proyecto'].map(backlog_flag).fillna(0)
        pivot['Puntaje Total'] = pivot[
            ['C1 Descripción','C2 Criterios','C3 Asignatario','C4 Subtareas','C5 Épica','C6 Backlog']
        ].sum(axis=1)
        pivot['Clasificación General'] = pivot['Puntaje Total'].apply(
            lambda v: 'Excelente'    if v >= 90 else
                      'Adecuado'     if v >= 80 else
                      'Por mejorar' if v >= 65 else
                      'Incompleto'   if v >= 50 else 'Desastre'
        )

    # ===========================
    # 8.11) DataFrame “Clasificación Calidad” estático
    # ===========================
    clasif_df = pd.DataFrame([
        {'Rango':'90-100','Clasificación':'Excelente','Descripción':'Totalmente clara y estructurada'},
        {'Rango':'80-89','Clasificación':'Adecuado','Descripción':'Aceptable pero con oportunidad de mejora'},
        {'Rango':'65-79','Clasificación':'Por mejorar','Descripción':'Le falta información importante'},
        {'Rango':'50-64','Clasificación':'Incompleto','Descripción':'Le falta información importante'},
        {'Rango':'0-49','Clasificación':'Desastre','Descripción':'Necesita ser reformulada completamente'}
    ])

    # ===========================
    # 8.12) DataFrame “Criterios Evaluados” estático
    # ===========================
    crit_doc = pd.DataFrame([
        {'Criterio':'C1','Descripción':'Estructura+Verbo+Longitud','Puntaje':P1},
        {'Criterio':'C2','Descripción':'Lista+Acción','Puntaje':P2},
        {'Criterio':'C3','Descripción':'Asignatario','Puntaje':P3},
        {'Criterio':'C4','Descripción':'Subtareas','Puntaje':P4},
        {'Criterio':'C5','Descripción':'Épica','Puntaje':P5},
        {'Criterio':'C6','Descripción':'Backlog Priorizado','Puntaje':P6},
    ])

    # ===========================
    # 8.13) Generar el Excel “analizado” en /tmp
    # ===========================
    nombre_analizado = f"jira_data_analizado_{fecha_str}.xlsx"
    ruta_analizado   = f"/tmp/{nombre_analizado}"
    try:
        with pd.ExcelWriter(ruta_analizado, engine='xlsxwriter') as writer:
            if not df_issues.empty:
                df_issues.to_excel(writer, sheet_name='User Stories', index=False)
            else:
                pd.DataFrame(columns=[
                    "Proyecto","Nombre del Proyecto","Categoría Proyecto","Responsable Proyecto",
                    "Key","Summary","Status","Description","Criterios de aceptación",
                    "Épica Principal","Assignee","Número de Sub-tareas",
                    "Puntaje Descripción","Puntaje Criterios","Puntaje Asignatario",
                    "Puntaje Subtareas","Puntaje Épica","Observación Descripción",
                    "Observación Criterios de Aceptación"
                ]).to_excel(writer, sheet_name='User Stories', index=False)

            pivot.to_excel(writer, sheet_name='Resumen por Proyecto', index=False)
            clasif_df.to_excel(writer, sheet_name='Clasificación Calidad', index=False)
            crit_doc.to_excel(writer, sheet_name='Criterios Evaluados', index=False)

            workbook  = writer.book
            worksheet = writer.sheets['Resumen por Proyecto']
            num_fmt   = workbook.add_format({'num_format': '0.00'})
            cols_a_formatear = [
                'C1 Descripción','C2 Criterios','C3 Asignatario',
                'C4 Subtareas','C5 Épica','Puntaje Total'
            ]
            for idx, col in enumerate(pivot.columns):
                if col in cols_a_formatear:
                    max_val   = pivot[col].astype(str).map(len).max()
                    max_head  = len(col)
                    ancho_col = max(max_val, max_head) + 2
                    letra = xl_col_to_name(idx)
                    worksheet.set_column(f'{letra}:{letra}', ancho_col, num_fmt)
    except Exception as e:
        logger.exception("Error al generar el Excel analizado en /tmp")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Error al generar Excel analizado",
                "details": str(e)
            })
        }

    # ===========================
    # 8.14) Subir el Excel “analizado” a S3 en reports/Analizado/
    # ===========================
    analizado_key = f"reports/Analizado/{nombre_analizado}"
    try:
        s3.upload_file(
            ruta_analizado,
            S3_BUCKET,
            analizado_key,
            ExtraArgs={"ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        )
    except ClientError as e:
        logger.exception("Error al subir el Excel analizado a S3")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Fallo al subir Excel analizado a S3",
                "details": str(e)
            })
        }

    # ===========================
    # 8.15) Generar URL pre-firmada del Excel “analizado”
    # ===========================
    try:
        url_presigned = s3.generate_presigned_url(
            ClientMethod='get_object',
            Params={'Bucket': S3_BUCKET, 'Key': analizado_key},
            ExpiresIn=3600
        )
    except ClientError as e:
        logger.exception("Error al generar URL pre-firmada")
        url_presigned = f"https://{S3_BUCKET}.s3.amazonaws.com/{analizado_key}"

    # ===========================
    # 8.16) Respuesta final
    # ===========================
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Proceso completo: crudo y analizado generados correctamente",
            "raw_report_url":       f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_key_crudo}",
            "analizado_report_url": url_presigned
        })
    }
