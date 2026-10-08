<#
.SYNOPSIS
  instalar.ps1 — setup local 1-clique do AI Dungeon Local (idempotente).

.DESCRIPTION
  1. Verifica Python 3.12+ e Node.js 20+ (erro claro se faltar);
  2. Cria backend\.venv (se não existir) e instala backend\requirements.txt;
  3. npm ci + npm run build em frontend\ (gera frontend\dist servido pelo FastAPI);
  4. Cria .env a partir de .env.example (se não existir — NUNCA sobrescreve);
  5. Diagnóstico do LLM da VM via tools\check_llm.py (aviso, não bloqueia);
  6. Imprime os próximos passos.

  Não interativo: nunca pede input. Rodar quantas vezes quiser.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File instalar.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Ok($msg)   { Write-Host "[ OK ] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[AVISO] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "[ERRO ] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "== AI Dungeon Local - instalacao ==" -ForegroundColor Cyan
Write-Host ""

# --------------------------------------------------------------------------
# 1. Python 3.12+
# --------------------------------------------------------------------------
function Get-PythonCandidate {
    # Ordem: py launcher (3.13, 3.12) -> python -> python3
    $candidates = @(
        @{ Cmd = 'py'; Args = @('-3.13') },
        @{ Cmd = 'py'; Args = @('-3.12') },
        @{ Cmd = 'python'; Args = @() },
        @{ Cmd = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        try {
            $out = & $c.Cmd @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match '(\d+\.\d+)') {
                $v = [version]$Matches[1]
                if ($v -ge [version]'3.12') {
                    return @{ Cmd = $c.Cmd; Args = $c.Args; Version = $v }
                }
            }
        }
        catch { <# comando não existe — tenta o próximo #> }
    }
    return $null
}

$py = Get-PythonCandidate
if (-not $py) {
    Write-Fail "Python 3.12+ nao encontrado no PATH."
    Write-Host "       Instale de https://www.python.org/downloads/ marcando 'Add python.exe to PATH'"
    Write-Host "       e rode este script novamente."
    exit 1
}
Write-Ok "Python $($py.Version) (via '$($py.Cmd)')"

# --------------------------------------------------------------------------
# 2. Node.js 20+ e npm
# --------------------------------------------------------------------------
$nodeMajor = $null
try {
    $raw = (& node --version) 2>$null
    if ($raw -match '^v(\d+)') { $nodeMajor = [int]$Matches[1] }
}
catch { }
if ($null -eq $nodeMajor -or $nodeMajor -lt 20) {
    Write-Fail "Node.js 20+ nao encontrado (detectado: $($raw ?? 'nenhum'))."
    Write-Host "       Instale o LTS de https://nodejs.org/ e rode este script novamente."
    exit 1
}
Write-Ok "Node.js $(& node --version) / npm $(& npm --version)"

# --------------------------------------------------------------------------
# 3. backend\.venv + dependências Python
# --------------------------------------------------------------------------
$venvDir = Join-Path $root 'backend\.venv'
$venvPy  = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host "Criando backend\.venv ..."
    & $py.Cmd @($py.Args) -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Write-Fail "falha ao criar backend\.venv"
        exit 1
    }
    Write-Ok "backend\.venv criado"
}
else {
    Write-Ok "backend\.venv ja existe (mantido)"
}

Write-Host "Instalando dependencias Python (backend\requirements.txt) ..."
& $venvPy -m pip install --disable-pip-version-check --quiet --upgrade pip
& $venvPy -m pip install --disable-pip-version-check --quiet -r (Join-Path $root 'backend\requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install falhou (veja a saida acima)"
    exit 1
}
Write-Ok "dependencias Python instaladas"

# --------------------------------------------------------------------------
# 4. Frontend: npm ci + build (frontend\dist é o que o FastAPI serve)
# --------------------------------------------------------------------------
Write-Host "Instalando dependencias do frontend (npm ci) ..."
Push-Location (Join-Path $root 'frontend')
try {
    & npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Write-Fail "npm ci falhou"; exit 1 }
    Write-Host "Gerando build de producao (npm run build) ..."
    & npm run build
    if ($LASTEXITCODE -ne 0) { Write-Fail "npm run build falhou"; exit 1 }
}
finally {
    Pop-Location
}
Write-Ok "frontend buildado em frontend\dist"

# --------------------------------------------------------------------------
# 5. .env a partir de .env.example (nunca sobrescreve um .env existente)
# --------------------------------------------------------------------------
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    Write-Ok ".env ja existe (mantido — nao sobrescrevo segredos)"
}
else {
    Copy-Item (Join-Path $root '.env.example') $envFile
    Write-Warn ".env criado a partir de .env.example"
    Write-Warn "PREENCHA os 4 valores antes de jogar — os comentarios no arquivo"
    Write-Warn "ensinam a gerar AUTH_PASSWORD_HASH e AUTH_SECRET."
}

# --------------------------------------------------------------------------
# 6. Diagnóstico do LLM da VM (aviso apenas — a VM pode estar fora do ar)
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "Testando o servidor LLM (tools\check_llm.py) ..."
& $venvPy (Join-Path $root 'tools\check_llm.py') --timeout 8
$llmCode = $LASTEXITCODE
switch ($llmCode) {
    0 { Write-Ok "LLM da VM acessivel e com API key" }
    2 { Write-Warn "VM acessivel, mas falta LLM_API_KEY no .env" }
    3 { Write-Warn "VM do LLM inalcancavel agora — o jogo so funciona com ela no ar" }
    default { Write-Warn "check_llm retornou codigo $llmCode (veja a saida acima)" }
}

# --------------------------------------------------------------------------
# Próximos passos
# --------------------------------------------------------------------------
Write-Host ""
Write-Host "== Instalacao concluida ==" -ForegroundColor Cyan
Write-Host ""
Write-Host "Proximos passos:"
Write-Host "  1. Se o .env foi criado agora: edite-o e preencha LLM_API_KEY,"
Write-Host "     AUTH_PASSWORD_HASH e AUTH_SECRET (instrucoes dentro do arquivo)."
Write-Host "  2. Duplo-clique em iniciar.bat — sobe o backend e abre o navegador."
Write-Host "  3. Login com o usuario/senha do .env. Para encerrar: parar.bat."
Write-Host ""
exit 0
