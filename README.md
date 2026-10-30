# Painel para iPad mini (iOS 9)

Página única com relógio, fase da lua, tempo atual + previsão de 7 dias
(Osório e Porto Alegre), nascer/pôr do sol e, opcionalmente, a câmera de casa.

## Por que tem um servidor?

O iPad mini 1 para no iOS 9.3.5. O Safari dele:
- não confia nos certificados Let's Encrypt atuais (a API Open-Meteo usa um) → HTTPS falha;
- não tem `fetch`, CSS grid, ES6 etc. (a página é toda em ES5 + flexbox);
- não consegue abrir câmeras RTSP nem fazer login digest em muitas câmeras.

Então `server.py` (Python 3.7+, sem dependências) roda num computador de casa
sempre ligado (Mac, Raspberry Pi, NAS...) e o iPad acessa por HTTP na rede local.

## Rodar

Na primeira vez, copie a configuração de exemplo e preencha (cidades, câmera, agenda).
`config.json` fica fora do git porque guarda senhas:

```sh
cp config.example.json config.json
```

```sh
/usr/bin/python3 server.py
```

Use o Python da Apple (`/usr/bin/python3`), não o do Homebrew: no macOS,
Privacidade > Rede Local bloqueia o Python e o ffmpeg do Homebrew de acessar
aparelhos da rede ("No route to host"). O servidor faz uma ponte em
`127.0.0.1` para o ffmpeg chegar na câmera.

No iPad, abra `http://IP-DO-COMPUTADOR:8090` no Safari.

Dicas no iPad:
- Compartilhar → **Adicionar à Tela de Início** → abre em tela cheia, sem barra do Safari.
- Ajustes → Geral → **Bloqueio Automático: Nunca**.
- Deixe na tomada.

## Câmera

Preencha `camera` no `config.json` com a URL de *snapshot* JPEG da câmera
(quase toda câmera IP tem uma). Exemplos comuns:

| Marca | URL |
|---|---|
| Intelbras / Dahua | `http://IP/cgi-bin/snapshot.cgi` |
| Hikvision | `http://IP/ISAPI/Streaming/channels/101/picture` |
| Reolink | `http://IP/cgi-bin/api.cgi?cmd=Snap&channel=0&user=U&password=S` |
| Tapo (C500 etc.) / outras só RTSP | use `rtsp_url` (abaixo) |

### Tapo / RTSP

Tapo não tem snapshot HTTP, só RTSP. Preencha `rtsp_url` no `config.json`:

```
rtsp://USUARIO:SENHA@192.168.1.210:554/stream2
```

- Usuário/senha: **não** é a conta TP-Link. No app Tapo: câmera → Configurações
  → Configurações avançadas → **Conta da câmera** → criar.
- `stream2` = baixa resolução (leve); `stream1` = resolução cheia.
- Requer `ffmpeg` instalado no computador do servidor (`brew install ffmpeg`).
  Só roda enquanto a página estiver aberta.

Usuário/senha ficam no servidor (suporta Basic e Digest), nunca vão para o iPad.
A imagem atualiza a cada `refresh_seconds`; toque nela para tela cheia.

Câmeras de nuvem (Ring, Arlo, Wyze sem RTSP...) não expõem snapshot local —
nesse caso, troque o painel por um calendário.

## Rodar sempre (macOS)

Exemplo com `launchd`: crie `~/Library/LaunchAgents/painel.ipad.plist` apontando
para `python3 /caminho/server.py` com `KeepAlive` = true. Num Raspberry Pi, use um
serviço `systemd`.

## Agenda (Google Agenda)

No computador, em calendar.google.com: Configurações → clique na agenda à
esquerda → "Integrar agenda" → copie o **Endereço secreto no formato iCal**.
Cole em `config.json`:

```json
"calendar": {
  "ics_urls": ["https://calendar.google.com/calendar/ical/.../basic.ics"],
  "days": 14,
  "max_events": 12
}
```

Pode colocar várias agendas na lista. O link é secreto (dá leitura da agenda
inteira) — fica só no servidor. O Google atualiza o .ics com alguns minutos (às
vezes horas) de atraso; o painel busca a cada 10 min.

### Sem "Secret address in iCal format"? Use o Apps Script

1. script.google.com (na conta certa) → **New project** → cole `apps-script.gs`.
2. Troque `TOKEN` por uma senha longa. Salve.
3. **Deploy → New deployment** → tipo **Web app** → *Execute as*: **Me**,
   *Who has access*: **Anyone** → **Deploy** → autorize.
4. Copie a **Web app URL** e coloque em `config.json`:
   `"apps_script_urls": ["https://script.google.com/macros/s/.../exec?token=SUA_SENHA"]`

Ao mudar o código depois: **Deploy → Manage deployments → editar → Version: New version**.
