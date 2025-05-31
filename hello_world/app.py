# hello_world/app.py
import sys
import os

# Añade el subdirectorio python/ al path de módulos
current_dir = os.path.dirname(os.path.realpath(__file__))
vendor_dir = os.path.join(current_dir, "python")
sys.path.insert(0, vendor_dir)

import pandas as pd
import openpyxl
# (resto de imports)

#import os
import json
import logging
import tempfile
import boto3
import pandas as pd
from jira import JIRA
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cliente de S3 (para subir el Excel resultante)
s3 = boto3.client("s3")

def lambda_handler(event, context):
    """
    Lambda para:
      1) Conectarse a Jira usando credenciales de entorno
      2) Obtener issues según un JQL configurado
      3) Procesar esos issues con pandas (y opcionalmente spaCy)
      4) Generar un archivo Excel en /tmp/, subirlo a S3
      5) Devolver la URL (pre-signed URL) en el response JSON
    """

    # 1) Leer variables de entorno para Jira
    jira_domain = os.getenv("JIRA_DOMAIN")        # ej: "midominio.atlassian.net"
    jira_user = os.getenv("JIRA_USER")            # tu correo de Jira
    jira_api_token = os.getenv("JIRA_API_TOKEN")  # un API token de Atlassian
    s3_bucket = os.getenv("OUTPUT_S3_BUCKET")     # bucket donde guardaremos el Excel

    # Validar que existan las variables
    missing = []
    if not jira_domain:
        missing.append("JIRA_DOMAIN")
    if not jira_user:
        missing.append("JIRA_USER")
    if not jira_api_token:
        missing.append("JIRA_API_TOKEN")
    if not s3_bucket:
        missing.append("OUTPUT_S3_BUCKET")

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

    # 3) Definir la consulta JQL que quieres ejecutar
    #    (esto suele venir de tu notebook; aquí solo un ejemplo simple)
    jql = "assignee=currentUser() AND resolution = Unresolved ORDER BY priority DESC"
    max_results = 50
    fields = ["summary", "status", "assignee", "created", "customfield_10031"]  # personaliza tu lista

    # 4) Ejecutar la búsqueda en Jira
    try:
        issues_jira = jira_client.search_issues(
            jql,
            maxResults=max_results,
            fields=fields
        )
    except Exception as e:
        logger.exception("Error al ejecutar search_issues en Jira")
        return {
            "statusCode": 502,
            "body": json.dumps({"error": "Fallo al consultar Jira", "details": str(e)})
        }

    # 5) Convertir el resultado de Jira a lista de diccionarios
    registros = []
    for issue in issues_jira:
        # Extraer los campos que necesites; adapta según tu customfield
        key = issue.key
        campos = issue.fields
        summary = campos.summary
        status = campos.status.name
        assignee = campos.assignee.displayName if campos.assignee else None
        created = campos.created  # cadena tipo "2025-05-15T12:34:56.000+0000"
        criteria = getattr(campos, "customfield_10031", None)  # tu campo de "Criterios de aceptación"

        # Agrega aquí otros campos que en tu notebook estabas pivotando o analizando
        registros.append({
            "key": key,
            "summary": summary,
            "status": status,
            "assignee": assignee,
            "created": created,
            "criteria": criteria
        })

    if not registros:
        # Si no hay issues, devolvemos respuesta vacía o un Excel con solo encabezados
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No se encontraron issues"})
        }

    # 6) Crear un DataFrame de pandas con los registros
    df = pd.DataFrame(registros)

    # (Opcional)  Si en tu notebook usabas spaCy para análisis de texto,
    # puedes cargar el modelo aquí y procesar los textos. Por ejemplo:
    #
    # import spacy
    # nlp = spacy.load("es_core_news_sm")   # si en Lambda tuviste que empaquetar este modelo
    # df["summary_processed"] = df["summary"].apply(lambda txt: nlp(txt).vector.tolist())
    #
    # En este ejemplo simple omitiremos el NLP pesado para no recargar la función.

    # 7) Generar una tabla pivot o resumen, según lo que hicieras en Notebook.
    #    Supongamos que en el Notebook pivotabas por 'status' y 'assignee':
    pivot = pd.pivot_table(
        df,
        index=["assignee"],
        columns=["status"],
        values=["key"],
        aggfunc="count",
        fill_value=0
    )
    # Renombremos columnas (opcional):
    pivot.columns = pivot.columns.get_level_values(1)

    # 8) Crear un archivo Excel en /tmp/ con pandas
    #    Use openpyxl como motor; si prefieres xlsxwriter, cambia engine="xlsxwriter"
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    nombre_archivo = f"reporte_jira_{timestamp}.xlsx"
    ruta_local = f"/tmp/{nombre_archivo}"

    try:
        with pd.ExcelWriter(ruta_local, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Raw_Issues", index=False)
            pivot.to_excel(writer, sheet_name="Resumen_Pivot")
            # Si tenías otros sheets en tu Notebook, repítelos aquí
            writer.save()
    except Exception as e:
        logger.exception("No se pudo escribir el archivo Excel en /tmp/")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Error al generar Excel", "details": str(e)})
        }

    # 9) Subir el archivo Excel a S3
    s3_key = f"reports/{nombre_archivo}"
    try:
        s3.upload_file(ruta_local, s3_bucket, s3_key)
    except Exception as e:
        logger.exception("Error al subir el Excel a S3")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Fallo al subir a S3", "details": str(e)})
        }

    # 10) Generar una URL pre-firmada para quien reciba la respuesta
    try:
        url_presignada = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3_bucket, "Key": s3_key},
            ExpiresIn=3600  # válida por 1 hora
        )
    except Exception as e:
        logger.exception("Error al generar URL pre-firmada")
        url_presignada = f"https://{s3_bucket}.s3.amazonaws.com/{s3_key}"

    # 11) Devolver la URL al Excel en S3
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Reporte generado correctamente",
            "report_url": url_presignada
        })
    }
