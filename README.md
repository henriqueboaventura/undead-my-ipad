# Painel para iPad mini (iOS 9)

Página única para um iPad mini de 1ª geração (iOS 9.3.5) usado como tela fixa:
relógio, sol e lua (nascer/pôr e fase), tempo detalhado de Osório (agora,
gráfico das próximas 14 h, avisos de chuva e rajadas), 7 dias alternando
entre Osório e Porto Alegre e a câmera Tapo de casa.

## Como funciona

```
Open-Meteo ─┐
            ├─► server.py (computador sempre ligado) ──HTTP──► Safari do iPad
Câmera Tapo ┘     porta 8090, rede local
```

O iPad não consegue buscar os dados sozinho: o Safari do iOS 9 não confia nos
certificados HTTPS atuais e não abre RTSP. O `server.py` (Python, sem
dependências) busca tudo e entrega por HTTP simples na rede de casa.

Arquivos:

| Arquivo | O que é |
|---|---|
| `index.html` | A página (ES5 + flexbox, compatível com Safari 9) |
| `server.py` | Servidor: previsão, lua, câmera e agenda |
| `config.example.json` | Modelo de configuração, sem senhas |
| `config.json` | Sua configuração com senhas — **fora do git** |
| `scripts/install-macos.sh` | Instala como serviço no macOS (roda sem terminal) |
| `scripts/uninstall-macos.sh` | Remove o serviço |
| `apps-script.gs` | Opcional: ponte para Google Agenda |

---

## Instalar em outro computador (macOS)

### 1. Pré-requisitos

Abra o **Terminal** e rode:

```sh
# Python da Apple (/usr/bin/python3) e git
xcode-select --install

# ffmpeg (para a câmera). Se não tiver Homebrew, veja https://brew.sh
brew install ffmpeg
```

Se o `xcode-select` disser que já está instalado, siga em frente.

> **Use sempre `/usr/bin/python3`** (o da Apple), não o `python3` do Homebrew.
> O macOS (Privacidade e Segurança → Rede Local) bloqueia o Python e o ffmpeg
> do Homebrew de acessar aparelhos da rede e a câmera fica preta
> ("No route to host"). O servidor faz uma ponte em `127.0.0.1` para o ffmpeg.

### 2. Baixar o projeto

O repositório é privado; entre na sua conta do GitHub antes:

```sh
brew install gh
gh auth login
gh repo clone henriqueboaventura/ipad-page ~/ipad-page
cd ~/ipad-page
```

### 3. Configurar

```sh
cp config.example.json config.json
open -e config.json
```

Preencha:

- `camera.rtsp_url`: troque `USUARIO:SENHA` pela **Conta da câmera** do app Tapo
  (ver [Câmera](#câmera-tapo-c500)). Se a senha tiver `@ : / # ?`, troque
  esses caracteres por `%40 %3A %2F %23 %3F`.
- `camera.active_hours`: horário em que a câmera atualiza (padrão
  `07:30-18:00`). Fora dele a página mostra "Câmera desligada" e o ffmpeg
  não roda. Deixe `""` para a câmera ficar sempre ligada.
- `port`: porta do painel (padrão `8090`). Mude se já estiver em uso.
- `reminders`: lembretes fixos que aparecem no card de Avisos nos dias
  escolhidos (`seg ter qua qui sex sab dom`). Ex.:
  `{"days": ["seg", "qua", "sex"], "text": "Dia de lixo", "detail": "Colocar o lixo para fora"}`.
  Com `"urgent": true` o lembrete aparece em vermelho e negrito.
- `night_hours`: modo noturno (padrão `23:00-06:00`). Nesse horário a tela
  fica preta, só com o relógio apagado; um toque mostra o painel por 1 min.
  Deixe `""` para desligar.
- `cities`: cidades e coordenadas. A cidade com `"detailed": true` ganha a
  previsão completa.

### 4. Testar no terminal

```sh
/usr/bin/python3 server.py
```

No iPad, abra `http://IP-DO-COMPUTADOR:8090`. Para descobrir o IP do Mac:
`ipconfig getifaddr en0` (Wi-Fi/cabo) ou Ajustes do Sistema → Rede.

Se aparecer tudo, pare com **Ctrl+C** e siga para o passo 5.

### 5. Rodar sem terminal aberto (início automático)

```sh
./scripts/install-macos.sh
```

Isso cria um serviço do macOS (LaunchAgent) que:

- inicia o painel sozinho quando você entra na sua conta do Mac;
- roda em segundo plano, sem nenhuma janela de terminal;
- reinicia o servidor se ele cair.

Se o macOS perguntar se o **python3** pode acessar a rede local, clique em
**Permitir**.

Comandos úteis:

| Para... | Rode |
|---|---|
| Ver se está rodando | `launchctl print gui/$(id -u)/com.painel.ipad \| grep state` |
| Ver o log | `tail -f ~/ipad-page/server.log` |
| Reiniciar (após mudar `config.json`) | `launchctl kickstart -k gui/$(id -u)/com.painel.ipad` |
| Parar e remover o início automático | `./scripts/uninstall-macos.sh` |

> O serviço roda enquanto sua conta estiver logada (pode estar com a tela
> bloqueada). Para o painel voltar sozinho após falta de luz, ative
> Ajustes do Sistema → Usuários e Grupos → **Login automático**, e em
> Bateria/Economia de Energia → **Iniciar automaticamente após falta de energia**.
> O serviço roda dentro do `caffeinate -i`, então o Mac não entra em repouso
> por inatividade enquanto o painel estiver rodando (a tela pode apagar
> normalmente). Num notebook, fechar a tampa ainda põe o Mac em repouso.
>
> O Mac precisa ficar **na mesma rede Wi-Fi do iPad**. Se ele tiver outras
> redes salvas, desligue a **Conexão automática** delas (Ajustes do Sistema →
> Wi-Fi → Detalhes da rede), senão ele pode trocar de rede ao acordar.

### 6. Atualizar para uma versão nova

```sh
cd ~/ipad-page
git pull
launchctl kickstart -k gui/$(id -u)/com.painel.ipad
```

O iPad pega a página nova sozinho em até 6 h, ou na hora se recarregar.

---

## Instalar em Linux / Raspberry Pi (alternativa)

Funciona igual e gasta menos energia que um Mac ligado. Não tem o problema de
Rede Local do macOS.

```sh
sudo apt install -y git python3 ffmpeg tzdata gh
gh auth login
gh repo clone henriqueboaventura/ipad-page ~/ipad-page
cd ~/ipad-page && cp config.example.json config.json && nano config.json
python3 server.py   # teste; Ctrl+C para parar
```

Python 3.9 ou mais novo (`python3 --version`). Para rodar sem terminal, crie
`/etc/systemd/system/painel.service` (troque `pi` pelo seu usuário):

```ini
[Unit]
Description=Painel iPad
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/ipad-page
ExecStart=/usr/bin/python3 -u server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now painel     # inicia agora e em todo boot
journalctl -u painel -f                # log
sudo systemctl restart painel          # reiniciar
```

---

## Configurar o iPad

1. Safari → `http://IP-DO-COMPUTADOR:8090`.
2. Compartilhar (□↑) → **Adicionar à Tela de Início**. Abra sempre pelo ícone:
   assim fica em tela cheia, sem a barra do Safari (o iOS 9 não permite um
   botão de tela cheia).
3. Ajustes → Geral → **Bloqueio Automático: Nunca**.
4. Opcional: Ajustes → Geral → Acessibilidade → **Acesso Guiado** (prende o
   iPad no painel; ative com 3 cliques no botão Início).
5. Deixe na tomada, brilho em 30–40%. Bateria antiga sempre carregando pode
   estufar: se a tela levantar, desligue.

O computador e o iPad precisam estar na **mesma rede**. Se o IP do computador
mudar, o ícone para de funcionar: reserve o IP dele no roteador (DHCP →
Reserva de endereço) ou crie o ícone de novo.

Atualizações automáticas: relógio a cada 1 s, câmera a cada 5 s
(`refresh_seconds`, só dentro de `active_hours`), tempo/sol/lua/avisos a cada
10 min, página inteira a cada 6 h.

Avisos (card abaixo do sol e lua): chuva começando ou parando (previsão de
15 em 15 min), chuva provável nas próximas 14 h (≥ 50%) e rajadas a partir de
50 km/h (vermelho a partir de 70 km/h). Os limites ficam no início da seção
Avisos do `index.html`.

---

## Câmera (Tapo C500)

A Tapo só oferece RTSP; o servidor usa o ffmpeg para tirar uma imagem por
segundo (só enquanto o painel estiver aberto) e o iPad busca a mais recente.

1. App Tapo → câmera → ⚙️ → Configurações avançadas → **Conta da câmera** →
   crie usuário e senha (não é a conta TP-Link).
2. Se existir: aba Eu → Tapo Lab → **Compatibilidade com terceiros** → ligado.
3. `config.json`:
   ```json
   "rtsp_url": "rtsp://USUARIO:SENHA@192.168.1.210:554/stream2"
   ```
   `stream2` = leve (recomendado); `stream1` = resolução máxima.

### Rede desta casa

```
Internet ─ Claro HG8245W5 192.168.0.1  (modem; Wi-Fi só reserva, oculto)
             └─ TP-Link Archer AX23  (roteador + EasyMesh)
                  lado Claro: 192.168.0.69 (DMZ)
                  lado casa:  192.168.1.1
                    ├─ RE315   (satélite EasyMesh, dentro de casa)
                    ├─ RE605X  (satélite EasyMesh, área da rua)
                    ├─ computador do painel 192.168.1.114
                    ├─ Tapo C500            192.168.1.210
                    └─ iPad, demais aparelhos
```

Tudo fica na rede do AX23 (`192.168.1.x`): o computador alcança a câmera
direto, sem port forwarding.

- **AX23** (`http://192.168.1.1`): Advanced → Network → DHCP Server →
  **Address Reservation** para câmera (`192.168.1.210`), computador do painel
  (`192.168.1.114`), iPad e extensores. Em Macs com reserva, use Ajustes →
  Wi-Fi → Detalhes → **Endereço Wi-Fi privado: Fixo** (ou Desativado), senão o
  MAC muda e a reserva deixa de valer.
- **Claro** (`http://192.168.0.1`): reserva DHCP `192.168.0.69` para o MAC
  da WAN do AX23 e **DMZ** apontando para `192.168.0.69`.
  O Wi-Fi da Claro fica desligado, exceto uma rede reserva de 2,4 GHz oculta
  para emergências (ou use cabo numa porta LAN dela).
- **5 GHz do AX23**: no canal 149 o RE605X só conecta ao AX23 pelo 2,4 GHz;
  nos canais 36–48 ele usa o 5 GHz, mas o Wi-Fi da casa fica mais lento
  (bloco dividido com vizinhos). Fixe o canal (não use automático).
- **iPad mini 1**: use WPA2 (AES) ou WPA2/WPA3 misto; WPA3 puro não conecta.

Em outra rede, basta o computador alcançar a câmera; use o IP dela direto
na `rtsp_url`.

---

## Agenda (opcional)

Com agendas configuradas, o card de baixo ganha uma terceira página (depois dos
7 dias de cada cidade) com os próximos eventos em duas colunas, uma cor por
agenda. A configuração fica em `calendar` no `config.json`:

```json
"calendar": {
  "ics_urls": [
    {"name": "Pessoal", "url": "https://calendar.google.com/calendar/ical/.../private-.../basic.ics"}
  ],
  "apps_script_urls": [],
  "days": 14,
  "max_events": 12
}
```

- `ics_urls`: links iCal **privados** (Google: Configurações → a agenda →
  Integrar agenda → *Endereço secreto no formato iCal*). O link *público* só
  funciona se a agenda for pública; senão dá 404. Também aceita só a URL, sem
  nome.
- `apps_script_urls`: quando o endereço secreto não existe (comum em contas de
  empresa), publique `apps-script.gs` em script.google.com com essa conta como
  **Web app** (*Execute as: Me*, *Who has access: Anyone*) e use
  `{"name": "Trabalho", "url": ".../exec?token=SUA_SENHA"}`.
- `max_events`: 12 cabem no card (6 por coluna).

O servidor baixa as agendas a cada 30 min. Se uma falhar, usa a última versão
boa dela e as outras continuam aparecendo. Eventos iguais em duas agendas
aparecem uma vez.

---

## Problemas comuns

| Sintoma | Causa / solução |
|---|---|
| Página abre mas nada aparece | Servidor antigo rodando com código novo: reinicie. Veja a mensagem abaixo do relógio. |
| Câmera preta | `server.log` com "No route to host": servidor rodando com o Python do Homebrew. Use `/usr/bin/python3` (o script de instalação já usa). |
| Câmera preta, log "sem imagem da câmera" | Teste a porta: `nc -z 192.168.1.210 554`. Fechada → confira se a câmera está online no app Tapo e se a reserva de IP dela no AX23 está ativa. |
| `Address already in use` | Outro programa na porta. Mude `port` no `config.json` ou pare o servidor antigo: `lsof -ti tcp:8090 -sTCP:LISTEN \| xargs kill`. |
| iPad não abre a página | Confira o IP do computador e se os dois estão na mesma rede (`192.168.1.x`). |
