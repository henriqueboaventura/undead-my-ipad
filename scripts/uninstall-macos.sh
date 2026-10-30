#!/bin/sh
# Para o painel e remove o início automático (LaunchAgent).
LABEL="com.painel.ipad"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Painel parado e removido do início automático."
