#!/bin/sh
# Instala o painel como serviço systemd no Raspberry Pi (Raspberry Pi OS):
# inicia sozinho no boot, roda sem terminal aberto e reinicia se cair.
# Rodar de novo atualiza a instalação. Log: journalctl -u painel -f
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=painel
UNIT="/etc/systemd/system/$SERVICE.service"
RUN_USER="$(id -un)"
PYTHON=/usr/bin/python3

if [ ! -f "$DIR/config.json" ]; then
  echo "Falta o config.json. Rode: cp config.example.json config.json (e preencha)"
  exit 1
fi
if ! "$PYTHON" -c 'import zoneinfo' 2>/dev/null; then
  echo "Python 3.9+ não encontrado. Rode: sudo apt install python3"
  exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  echo "Aviso: ffmpeg não encontrado (necessário para câmera RTSP). Rode: sudo apt install ffmpeg"
fi

PORT=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('port', 8090))" "$DIR/config.json")

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Painel do iPad
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$PYTHON -u $DIR/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Menos gravações no cartão SD: logs do sistema só na RAM (somem ao reiniciar).
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=20M\n' |
  sudo tee /etc/systemd/journald.conf.d/painel.conf >/dev/null
sudo systemctl restart systemd-journald

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Painel instalado e rodando."
echo "No iPad, abra: http://${IP:-IP-DO-PI}:$PORT"
echo "Log: journalctl -u $SERVICE -f"
