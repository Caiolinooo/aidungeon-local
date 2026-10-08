<#
.SYNOPSIS
  package_backend.ps1 — empacota o backend p/ deploy na VM (aidungeon-backend.zip).

.DESCRIPTION
  Gera deploy\vm\aidungeon-backend.zip com:
    backend\    código + requirements.txt + schema.sql (SEM .venv, __pycache__, *.pyc)
    config\     settings.json + models.json
    logs\       vazio
    data\       vazio (o SQLite nasce aqui no 1º boot da VM)
    .env.example / aidungeon-backend.service / Caddyfile / README-DEPLOY.md

  Idempotente: sobrescreve o zip. NUNCA inclui .env, data\*.db ou segredos.
  Ao final, verifica o zip e falha se encontrar .env dentro dele.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\vm\package_backend.ps1
#>

[CmdletBinding()]
param(
    [string]$OutZip = (Join-Path $PSScriptRoot 'aidungeon-backend.zip')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$staging = Join-Path ([IO.Path]::GetTempPath()) ("aidl-pkg-" + [guid]::NewGuid().ToString('N'))

function Write-Ok($msg) { Write-Host "[ OK ] $msg" -ForegroundColor Green }

try {
    New-Item -ItemType Directory -Path $staging | Out-Null

    # backend\ (sem .venv / caches) e config\ via robocopy (exit 0-7 = sucesso)
    foreach ($dir in @('backend', 'config')) {
        & robocopy (Join-Path $root $dir) (Join-Path $staging $dir) /E /NFL /NDL /NJH /NJS /NP `
            /XD .venv __pycache__ .pytest_cache /XF *.pyc | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy falhou em $dir (exit $LASTEXITCODE)" }
    }

    # logs\ e data\ vazios (o app cria o que faltar; o zip já nasce com a estrutura)
    New-Item -ItemType Directory -Path (Join-Path $staging 'logs') | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $staging 'data') | Out-Null
    New-Item -ItemType File -Path (Join-Path $staging 'logs\.gitkeep') | Out-Null
    New-Item -ItemType File -Path (Join-Path $staging 'data\.gitkeep') | Out-Null

    # arquivos de apoio do deploy
    Copy-Item (Join-Path $root '.env.example') $staging
    Copy-Item (Join-Path $PSScriptRoot 'aidungeon-backend.service') $staging
    Copy-Item (Join-Path $PSScriptRoot 'Caddyfile') $staging
    Copy-Item (Join-Path $PSScriptRoot 'README.md') (Join-Path $staging 'README-DEPLOY.md')

    # cinto e suspensório: nenhum segredo vai junto
    if (Test-Path (Join-Path $staging '.env')) { throw ".env vazou para o staging — abortando" }

    if (Test-Path $OutZip) { Remove-Item $OutZip -Force }
    Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $OutZip -Force

    # verificação pós-zip
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($OutZip)
    try {
        $names = $zip.Entries.FullName
        if ($names -match '(^|/)\.env$' -or $names -match '\.db$') {
            throw "zip contém arquivo proibido (.env ou *.db) — abortando"
        }
        $count = @($names).Count
    }
    finally { $zip.Dispose() }

    $sizeKb = [math]::Round((Get-Item $OutZip).Length / 1KB, 1)
    Write-Ok "pacote gerado: $OutZip ($count arquivos, $sizeKb KB)"
    Write-Host ""
    Write-Host "Proximos passos (detalhes em deploy\vm\README.md):"
    Write-Host "  1. scp $OutZip usuario@vm.groupabz.com:~/"
    Write-Host "  2. Na VM: unzip em /opt/aidungeon, python3.12 -m venv .venv,"
    Write-Host "     pip install -r backend/requirements.txt"
    Write-Host "  3. Preencher /opt/aidungeon/.env (modelo .env.example no zip)"
    Write-Host "  4. systemctl enable --now aidungeon-backend (unit incluso no zip)"
}
finally {
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
}
