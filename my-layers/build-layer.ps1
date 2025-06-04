# build-layer.ps1
param(
    [string]$RequirementsFile = "requirements-layer.txt"
)

Write-Host "=== 1) Eliminar carpeta previa si existe ==="
if (Test-Path .\python) {
    Remove-Item .\python -Recurse -Force
}

Write-Host "=== 2) Crear carpeta python/ e instalar dependencias mínimas ==="
python -m pip install --upgrade pip
python -m pip install -r $RequirementsFile -t .\python

# === 3) Eliminar archivos y carpetas innecesarios de spaCy para achicar tamaño ===
# Una vez que spaCy ya está instalado en python\, hay que borrar subcarpetas pesadas:
$spacyRoot = Join-Path .\python "spacy"
if (Test-Path $spacyRoot) {
    # 3.1) Borrar modelos y datos precompilados (vectores, idiomas extras, llaves de red):
    $toDelete = @(
        "thinc",               # Núcleo de spaCy que trae BLIS, preshed, etc.
        "blis",
        "preshed",
        "en_core_web_sm",      # si se hubiese instalado algún modelo por defecto
        "es_core_news_sm",     # modelo pequeño de español (si se descarga dinámicamente, no hace falta aquí)
        "spacy/lang/*",        # elimina todos los idiomas. Solo conservaremos "es" si quieres.
        "spacy/vectors",       # elimina vectores precompilados
        "__pycache__"
    )
    foreach ($pattern in $toDelete) {
        Get-ChildItem -Path $spacyRoot -Recurse -Force -Include $pattern `
            | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
    # 3.2) Si por algún motivo quedó carpeta “es_core_news_sm” (modelo español), la puedes borrar:  
    $esModel = Join-Path .\python "es_core_news_sm"
    if (Test-Path $esModel) {
        Remove-Item $esModel -Recurse -Force
    }
}

# === 4) (Opcional) Eliminar numpy si no lo vas a usar directamente ===
# Nota: spaCy internamente funciona sobre numpy, pero si spaCy ya está en "mode blank",
#       no necesitarás foo numpy explícito en tu código. Para ahorrar espacio, puedes borrar:
$numpypath = Join-Path .\python "numpy"
$numpylibspath = Join-Path .\python "numpy.libs"
if (Test-Path $numpypath) {
    Remove-Item $numpypath -Recurse -Force -ErrorAction SilentlyContinue
}
if (Test-Path $numpylibspath) {
    Remove-Item $numpylibspath -Recurse -Force -ErrorAction SilentlyContinue
}

# === 5) (Opcional) Limpiar “bin” u otros scripts innecesarios ===
$binFolder = Join-Path .\python "bin"
if (Test-Path $binFolder) {
    Remove-Item $binFolder -Recurse -Force
}

# === 6) Mostrar tamaño final de python/ (descomprimido) ===
Write-Host "Tamaño de la carpeta python/ tras limpieza: " -NoNewline
$sizeBytes = (Get-ChildItem .\python -Recurse -File | Measure-Object -Property Length -Sum).Sum
$sizeMB = [math]::Round($sizeBytes / 1MB, 2)
Write-Host ("Tamaño de la carpeta python/ tras limpieza: {0:N2} MB" -f $sizeMB) -ForegroundColor Green
Write-Host "=== Carpeta my-layers/python/ lista para usar en template.yaml ==="
