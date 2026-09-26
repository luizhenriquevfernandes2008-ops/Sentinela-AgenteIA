@echo off
setlocal
title Sentinela
cd /d "%~dp0"

echo.
echo   ==========================================
echo      S E N T I N E L A
echo      Agente de IA com controle humano
echo   ==========================================
echo.

REM ---------------------------------------------------------------
REM 1. Encontrar um Python de verdade.
REM    "where python" nao serve: no Windows ele acha o atalho falso da
REM    Microsoft Store, que trava. Testamos com --version, que so
REM    funciona com um Python instalado de verdade.
REM ---------------------------------------------------------------
set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :temPython
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :temPython

echo   Nao encontrei o Python instalado.
echo.
echo   1. Baixe em https://www.python.org/downloads/
echo   2. Na instalacao, marque "Add python.exe to PATH"
echo   3. Rode este arquivo de novo.
echo.
pause
exit /b 1

:temPython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo   Seu Python e antigo demais: este projeto precisa do 3.10 ou mais novo.
    echo   Baixe a versao atual em https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo   Usando %%v

REM ---------------------------------------------------------------
REM 2. Ambiente virtual + dependencias (so na primeira vez).
REM    O arquivo .venv\instalado.ok so e criado quando a instalacao
REM    termina bem; se faltar, a instalacao e refeita.
REM ---------------------------------------------------------------
if exist ".venv\instalado.ok" goto :rodar

if exist ".venv\Scripts\python.exe" goto :instalar
echo.
echo   [1/2] Criando o ambiente virtual...
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv
if errorlevel 1 (
    echo.
    echo   Nao consegui criar o ambiente virtual. Veja a mensagem acima.
    pause
    exit /b 1
)

:instalar
echo.
echo   [2/2] Instalando as dependencias. Na primeira vez leva de 1 a 3 minutos...
echo.
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo   A instalacao falhou. Confira sua internet e rode este arquivo de novo.
    pause
    exit /b 1
)
echo ok> ".venv\instalado.ok"

:rodar
echo.
echo   Abrindo o painel em http://127.0.0.1:8000
echo   Deixe esta janela ABERTA enquanto usa. Para encerrar, feche-a.
echo.
set "ABRIR_NAVEGADOR=1"
".venv\Scripts\python.exe" -m app

echo.
echo   O servidor encerrou. Se foi um erro, a mensagem esta acima.
pause
