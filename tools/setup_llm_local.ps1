#Requires -Version 5.1
<#
.SYNOPSIS
    Fallback local do LLM: baixa KoboldCpp (Windows x64) + GGUF e gera start_llm_local.bat.

.DESCRIPTION
    SOMENTE para o cenario em que a VM (http://vm.groupabz.com:1234/v1) esta fora do ar.

    Passos executados:
      [1/3] Baixa o release ESTAVEL mais recente do KoboldCpp (asset koboldcpp.exe,
            Windows x64, build CPU) do GitHub -> data\koboldcpp\koboldcpp.exe
      [2/3] Baixa o GGUF definido em config\models.json -> local_fallbacks
            (padrao: Qwen2.5-7B-Instruct Q4_K_M, bartowski/Qwen2.5-7B-Instruct-GGUF)
            -> data\models\
      [3/3] Gera start_llm_local.bat na raiz do repo (porta 5001, ctx 8192, threads 8)

    O servidor NAO e iniciado por este script; isso so acontece ao rodar start_llm_local.bat.

.PARAMETER Model
    Chave de config\models.json -> local_fallbacks.
    Padrao: qwen2.5-7b-instruct-q4_k_m (alternativa: qwen2.5-3b-instruct-q5_k_m).

.PARAMETER Force
    Refaz os downloads mesmo que os arquivos ja existam.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\setup_llm_local.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\setup_llm_local.ps1 -Model qwen2.5-3b-instruct-q5_k_m -Force
#>
[CmdletBinding()]
param(
    [string]$Model = "qwen2.5-7b-instruct-q4_k_m",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # Invoke-WebRequest muito mais rapido
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root     = Split-Path -Parent $PSScriptRoot
$DataDir  = Join-Path $Root "data"
$ModelDir = Join-Path $DataDir "models"
$KobDir   = Join-Path $DataDir "koboldcpp"
$CfgPath  = Join-Path $Root "config\models.json"
New-Item -ItemType Directory -Force $ModelDir, $KobDir | Out-Null

Write-Host "============================================================"
Write-Host " AI Dungeon Local - setup do fallback local (KoboldCpp)"
Write-Host "============================================================"

# --- le config\models.json -------------------------------------------------
if (-not (Test-Path $CfgPath)) { throw "config nao encontrado: $CfgPath" }
$cfg = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$fb  = $cfg.local_fallbacks.PSObject.Properties[$Model].Value
if (-not $fb) {
    $keys = ($cfg.local_fallbacks.PSObject.Properties.Name) -join ", "
    throw "Modelo '$Model' nao existe em local_fallbacks. Opcoes: $keys"
}

$GgufPath = Join-Path $ModelDir $fb.file
$GgufUrl  = "https://huggingface.co/$($fb.hf_repo)/resolve/main/$($fb.file)"

Write-Host " modelo  : $($fb.name) $($fb.quant)  (~$($fb.ram_estimate_gb) GB RAM, $($fb.role))"
Write-Host " destino : $GgufPath"

# --- [1/3] KoboldCpp ---------------------------------------------------------
$KobExe = Join-Path $KobDir "koboldcpp.exe"
if ((Test-Path $KobExe) -and -not $Force) {
    Write-Host "`n[1/3] KoboldCpp ja existe - pulando (use -Force p/ atualizar)"
    Write-Host "      $KobExe"
} else {
    Write-Host "`n[1/3] Consultando release estavel do KoboldCpp no GitHub..."
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/LostRuins/koboldcpp/releases/latest" `
                             -Headers @{ "User-Agent" = "aidungeon-local" }
    $asset = $rel.assets | Where-Object { $_.name -ieq "koboldcpp.exe" } | Select-Object -First 1
    if (-not $asset) { throw "Asset 'koboldcpp.exe' nao encontrado no release $($rel.tag_name)." }
    $mb = [math]::Round($asset.size / 1MB, 1)
    Write-Host "      release $($rel.tag_name) - baixando $($asset.name) ($mb MB)..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $KobExe
    Write-Host "      OK -> $KobExe"
}

# --- [2/3] GGUF ---------------------------------------------------------------
if ((Test-Path $GgufPath) -and -not $Force) {
    Write-Host "`n[2/3] GGUF ja existe - pulando (use -Force p/ refazer)"
    Write-Host "      $GgufPath"
} else {
    Write-Host "`n[2/3] Baixando GGUF (arquivo grande, pode demorar bastante)..."
    Write-Host "      $GgufUrl"
    Invoke-WebRequest -Uri $GgufUrl -OutFile $GgufPath
    $gb = [math]::Round((Get-Item $GgufPath).Length / 1GB, 2)
    Write-Host "      OK -> $GgufPath ($gb GB)"
}

# --- [3/3] start_llm_local.bat -------------------------------------------------
$BatPath = Join-Path $Root "start_llm_local.bat"
$Bat = @"
@echo off
REM start_llm_local.bat - gerado por tools\setup_llm_local.ps1
REM Fallback local: KoboldCpp com API OpenAI-compativel em http://127.0.0.1:5001/v1
setlocal
set "ROOT=%~dp0"
"%ROOT%data\koboldcpp\koboldcpp.exe" --model "%ROOT%data\models\$($fb.file)" --port 5001 --contextsize $($fb.context_size) --threads $($fb.threads)
"@
Set-Content -Path $BatPath -Value $Bat -Encoding ASCII
Write-Host "`n[3/3] Gerado: $BatPath"

Write-Host "`n============================================================"
Write-Host " Concluido. Proximos passos:"
Write-Host "============================================================"
Write-Host "  1. Suba o servidor local:   .\start_llm_local.bat"
Write-Host "  2. Aponte o backend p/ ele (uma das opcoes):"
Write-Host "       - UI aba Config: base_url = http://127.0.0.1:5001/v1"
Write-Host "       - PUT /api/settings/llm com {""base_url"":""http://127.0.0.1:5001/v1""}"
Write-Host "  3. Valide:  python tools\check_llm.py --base-url http://127.0.0.1:5001/v1"
Write-Host "  4. Bench :  python tools\bench_llm.py --base-url http://127.0.0.1:5001/v1"
Write-Host "     (KoboldCpp nao exige API key por padrao)"
