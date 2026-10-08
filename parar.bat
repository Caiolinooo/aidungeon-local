@echo off
rem ==========================================================================
rem  parar.bat - encerra o backend (uvicorn) que escuta a porta 8000.
rem ==========================================================================
setlocal
set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /C:"LISTENING" ^| findstr /C:":8000"') do (
    echo Encerrando backend ^(PID %%P^)...
    taskkill /F /PID %%P >NUL 2>&1
    set "FOUND=1"
)
if defined FOUND (
    echo Backend encerrado.
) else (
    echo Nenhum backend escutando a porta 8000.
)
endlocal
