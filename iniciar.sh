#!/usr/bin/env bash
# Instala as dependências (na primeira vez) e abre o painel em http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
if [ ! -f .venv/instalado.ok ]; then
  echo "Preparando o ambiente (só na primeira vez)..."
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
  touch .venv/instalado.ok
fi
ABRIR_NAVEGADOR=1 exec .venv/bin/python -m app
