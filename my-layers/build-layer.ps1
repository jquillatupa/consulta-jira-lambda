# build-layer.ps1
Param(
  [string]$RequirementsFile = "requirements-layer.txt"
)

Write-Host "=== 1) Eliminar carpeta previa si existe ==="
if (Test-Path .\python) { Remove-Item .\python -Recurse -Force }

Write-Host "=== 2) Crear carpeta python/ e instalar dependencias mínimas ==="
New-Item -ItemType Directory -Path .\python | Out-Null

# Instala jira, openpyxl y spaCy en la carpeta python/
python -m pip install --upgrade pip
python -m pip install -r $RequirementsFile -t .\python --no-cache-dir

# ========================================
# 3) Limpieza de spaCy para reducir tamaño
# ========================================

# 3.1 Quitar todos los data models de spaCy: 
# Eliminamos directorios que empiecen con "es_core_news", "en_core_web", etc., 
# y también la carpeta "spacy/data" (vectores, datos).
$spacyDataPaths = Get-ChildItem .\python\spacy\data -Recurse -Force -ErrorAction SilentlyContinue
foreach ($item in $spacyDataPaths) {
    # Remueve todos los archivos de datos de "spacy/data"
    Remove-Item $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# 3.2 Quitar tests y scripts de spaCy (no se necesitan en runtime)
$patternsToDelete = @(
  ".*\btests?\b.*",     # carpetas o archivos que contengan "test" o "tests"
  ".*\b__pycache__\b.*", # cachés de Python
  ".*\.pyc$",           # archivos compilados
  ".*\.dist-info.*",    # metadatos de instalación
  ".*\.egg-info.*"      # metadatos de egg
)

foreach ($pattern in $patternsToDelete) {
    $matches = Get-ChildItem .\python -Recurse -Force | 
               Where-Object { $_.FullName -match $pattern } |
               Sort-Object FullName -Descending

    foreach ($m in $matches) {
        # Elimina carpeta o archivo
        if ($m.PSIsContainer) {
            Remove-Item $m.FullName -Recurse -Force -ErrorAction SilentlyContinue
        } else {
            Remove-Item $m.FullName -Force -ErrorAction SilentlyContinue
        }
    }
}

# 3.3 Para spaCy, podemos conservar únicamente el paquete base 'spacy' sin los datos:
# Elimina todo lo que no sea el paquete spacy/ minimal: 
#    python\spacy\   -> pero conserva sólo el __init__.py y carpetas necesarias de tokenización.
#
# Básicamente, deja solo:
#    python\spacy\__init__.py
#    python\spacy\lang\   -> solo la subcarpeta 'es' si quieres POS sin datos de modelo completos.

$spacyRoot = Join-Path .\python spacy

if (Test-Path $spacyRoot) {
    # 3.3.1 Primero, eliminamos toda la subcarpeta data/ (ya la borramos antes).
    # 3.3.2 Ahora, de la carpeta lang/, borramos todos los assets excepto el código de símbolo:
    $langFolder = Join-Path $spacyRoot lang
    if (Test-Path $langFolder) {
        # Conserva únicamente la carpeta básica de tokenizador 'spacy/lang/es' con archivos *.py
        $esFolder = Join-Path $langFolder es
        # Borra todo lo que no sea python (*.py) dentro de spacy/lang/es (el modelo 'es' completo NO es necesario).
        Get-ChildItem $esFolder -Recurse | Where-Object {
            $_.Extension -ne ".py"
        } | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue

        # Ahora, borra todas las otras carpetas de spacy/lang/ que no sean 'es':
        Get-ChildItem $langFolder -Directory | Where-Object { $_.Name -ne "es" } |
            ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }

    # 3.3.3 Borra también los directorios de tokenizadores basados en datos:
    # Por ejemplo, la carpeta tokenizer_data suele pesar muchísimo:
    $tokenizerData = Join-Path $spacyRoot "tokenizer_data"
    if (Test-Path $tokenizerData) {
        Remove-Item $tokenizerData -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 3.3.4 Borra todos los demás subpaquetes de spacy que no sean estrictamente necesarios (. models, .glossary, etc.)
    # Conservaremos solo spacy/__init__.py, spacy/lang/es/__init__.py y spacy/lang/es/*.py
    # Borra todo lo demás dentro de spacy/ (por ejemplo, spacy/model, spacy/cli, spacy/test, etc.)
    Get-ChildItem $spacyRoot -Directory | Where-Object {
        ($_.Name -ne "__pycache__") -and
        ($_.Name -ne "lang")
    } | ForEach-Object {
        Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ========================================
# 4) Informe del tamaño final
# ========================================
$sizeMB = (Get-ChildItem .\python -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("Tamaño de la carpeta python/ tras limpieza: {0:N2} MB" -f $sizeMB) -ForegroundColor Green

Write-Host "=== Carpeta my-layers/python/ lista para usar en template.yaml ==="
