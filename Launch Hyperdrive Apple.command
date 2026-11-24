#!/bin/bash
# Launch Hyperdrive Apple — lanceur macOS (double-clic dans le Finder).
# Crée un environnement Python local au 1er lancement, installe les dépendances,
# arrête une ancienne instance, démarre le serveur et ouvre le navigateur.

cd "$(dirname "$0")" || exit 1
PORT=5050
URL="http://127.0.0.1:${PORT}/"

echo "=== Hyperdrive Remote (macOS) ==="

# 1) Python 3
PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 introuvable."
  echo "Installe-le depuis https://www.python.org/downloads/macos/ (ou : brew install python), puis relance."
  read -r -p "Appuie sur Entrée pour fermer..." _
  exit 1
fi
echo "Python : $PY ($("$PY" --version 2>&1))"

# 2) Environnement virtuel local (.venv) + dépendances
if [ ! -x ".venv/bin/python" ]; then
  echo "Création de l'environnement .venv (1er lancement)..."
  "$PY" -m venv .venv || { echo "Échec de la création du venv."; read -r -p "Entrée pour fermer..." _; exit 1; }
fi
VPY=".venv/bin/python"
if [ ! -f ".venv/.deps_ok" ] || [ requirements.txt -nt ".venv/.deps_ok" ]; then
  echo "Installation des dépendances..."
  "$VPY" -m pip install --quiet --upgrade pip
  if "$VPY" -m pip install --quiet -r requirements.txt; then
    touch ".venv/.deps_ok"
  else
    echo "Échec de l'installation des dépendances (connexion internet nécessaire la 1re fois)."
    read -r -p "Entrée pour fermer..." _
    exit 1
  fi
fi

# 3) Arrêter une ancienne instance sur le port
OLD_PIDS="$(lsof -ti tcp:${PORT} -sTCP:LISTEN 2>/dev/null)"
if [ -n "$OLD_PIDS" ]; then
  echo "Arrêt de l'ancienne instance ($OLD_PIDS)..."
  # shellcheck disable=SC2086
  kill $OLD_PIDS 2>/dev/null
  sleep 1
fi

# 4) Démarrer le serveur
"$VPY" app.py &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null' EXIT INT TERM

# 5) Attendre qu'il réponde puis ouvrir le navigateur
ok=0
for _ in $(seq 1 40); do
  if curl -s -o /dev/null "$URL"; then ok=1; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  sleep 0.5
done
if [ "$ok" -ne 1 ]; then
  echo "Le serveur n'a pas démarré sur $URL"
  read -r -p "Entrée pour fermer..." _
  exit 2
fi

echo "Serveur prêt : $URL"
echo "(Garde cette fenêtre ouverte. Ferme-la ou Ctrl+C pour arrêter Hyperdrive.)"
open "$URL"
wait "$SERVER_PID"
