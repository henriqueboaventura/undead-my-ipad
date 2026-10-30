#!/bin/sh
# Instala o painel como serviço do usuário no macOS (LaunchAgent):
# inicia sozinho no login, roda sem terminal aberto e reinicia se cair.
# Rodar de novo atualiza a instalação.
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.painel.ipad"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# Python da Apple: o do Homebrew é bloqueado por Privacidade > Rede Local.
PYTHON=/usr/bin/python3

if [ ! -f "$DIR/config.json" ]; then
  echo "Falta o config.json. Rode: cp config.example.json config.json (e preencha)"
  exit 1
fi
if ! "$PYTHON" -c 'import zoneinfo' 2>/dev/null; then
  echo "Python da Apple não encontrado. Rode: xcode-select --install"
  exit 1
fi
if ! command -v ffmpeg >/dev/null && [ ! -x /opt/homebrew/bin/ffmpeg ] && [ ! -x /usr/local/bin/ffmpeg ]; then
  echo "Aviso: ffmpeg não encontrado (necessário para câmera RTSP). Rode: brew install ffmpeg"
fi

PORT=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1])).get('port', 8090))" "$DIR/config.json")
if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "A porta $PORT já está em uso (servidor iniciado pelo terminal?). Pare-o antes:"
  echo "  lsof -ti tcp:$PORT -sTCP:LISTEN | xargs kill"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <!-- caffeinate -i: o Mac não dorme por inatividade enquanto o painel roda (a tela pode apagar). -->
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$PYTHON</string>
    <string>-u</string>
    <string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/server.log</string>
  <key>StandardErrorPath</key><string>$DIR/server.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "IP-DESTE-MAC")
echo "Painel instalado e rodando."
echo "No iPad, abra: http://$IP:$PORT"
echo "Log: $DIR/server.log"
