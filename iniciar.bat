@echo off
rem ==========================================================================
rem  iniciar.bat - AI Dungeon Local: sobe o backend e abre o navegador.
rem
rem  Uso: duplo-clique. O backend (uvicorn) roda em janela minimizada,
rem  log em logs\backend-AAAAMMDD.log. Para encerrar: parar.bat.
rem  Defina AIDL_NO_BROWSER=1 para nao abrir o navegador (scripts/testes).
rem ==========================================================================
setlocal EnableExtensions
set "ROOT=%~dp0"
cd /d "%ROOT%"

rem ---- carrega .env (parse simples KEY=VALUE; ignora # comentarios) --------
rem (O backend tambem le o .env sozinho; aqui garantimos as vars no processo.)
if exist "%ROOT%.env" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ROOT%.env") do (
        if not "%%A"=="" set "%%A=%%B"
    )
) else (
    echo [AVISO] .env nao encontrado. Rode instalar.ps1 primeiro.
)

rem ---- pre-condicao: venv do backend ---------------------------------------
if not exist "%ROOT%backend\.venv\Scripts\python.exe" (
    echo [ERRO] backend\.venv nao encontrado.
    echo        Rode antes:  powershell -ExecutionPolicy Bypass -File instalar.ps1
    exit /b 1
)

rem ---- ja esta no ar? ------------------------------------------------------
curl.exe -s -o NUL --max-time 2 http://127.0.0.1:8000/api/health/llm 2>NUL
if %errorlevel%==0 (
    echo Backend JA esta rodando em http://127.0.0.1:8000
    goto :open_browser
)

rem ---- log diario ----------------------------------------------------------
if not exist "%ROOT%logs" mkdir "%ROOT%logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOGDATE=%%i"
set "LOGFILE=%ROOT%logs\backend-%LOGDATE%.log"

rem ---- sobe o uvicorn (janela minimizada, cwd = backend\) ------------------
echo Subindo backend... log: %LOGFILE%
cd /d "%ROOT%backend"
start "AI Dungeon Local - backend" /min cmd /c ".\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "%LOGFILE%" 2>&1"
cd /d "%ROOT%"

rem ---- espera o health: qualquer resposta HTTP = no ar ---------------------
rem (/api/health/llm exige Bearer, entao 401 ja significa "servidor OK")
set /a TRIES=0
:wait_health
curl.exe -s -o NUL --max-time 2 http://127.0.0.1:8000/api/health/llm 2>NUL
if %errorlevel%==0 goto :health_ok
set /a TRIES+=1
if %TRIES% GEQ 60 goto :health_fail
timeout /t 1 /nobreak >NUL
goto :wait_health

:health_fail
echo [ERRO] Backend nao respondeu em 60s. Veja o log:
echo        %LOGFILE%
exit /b 1

:health_ok
echo Backend no ar em http://127.0.0.1:8000
echo Login: usuario/senha definidos no .env. Para encerrar: parar.bat

:open_browser
if /i "%AIDL_NO_BROWSER%"=="1" goto :end
start "" "http://127.0.0.1:8000"

:end
endlocal
