@echo off
REM Instala as dependencias (na primeira vez) e abre o painel.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python nao encontrado. Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist .venv (
    echo Criando ambiente virtual...
    python -m venv .venv
    .venv\Scripts\python -m pip install -q -r requirements.txt
)

start "" http://127.0.0.1:8000
.venv\Scripts\python -m app
pause
