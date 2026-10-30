#!/usr/bin/env python3
"""Servidor do painel do iPad.

Serve index.html e faz proxy dos dados externos (previsão do tempo e câmera),
porque o Safari do iOS 9 não confia nos certificados HTTPS modernos e não
consegue autenticar direto em muitas câmeras.

Uso: python3 server.py   (sem dependências; Python 3.7+)
"""
import base64
import calendar
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("PAINEL_CONFIG") or os.path.join(ROOT, "config.json")

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

LOCAL_TZ = ZoneInfo(CONFIG.get("timezone", "America/Sao_Paulo"))

WEATHER_TTL = 10 * 60  # segundos
_weather_cache = {"at": 0, "body": None}
_weather_lock = threading.Lock()


def weather_url():
    cities = CONFIG["cities"]
    params = {
        "latitude": ",".join(str(c["lat"]) for c in cities),
        "longitude": ",".join(str(c["lon"]) for c in cities),
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
                   "is_day,wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,"
                   "uv_index,cloud_cover,visibility,dew_point_2m,precipitation",
        "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,"
                  "wind_gusts_10m,wind_direction_10m,is_day",
        "forecast_hours": 15,  # hora atual + próximas 14
        # Chuva de 15 em 15 min nas próximas ~2h, para o aviso "chuva em X min".
        "minutely_15": "precipitation",
        "forecast_minutely_15": 10,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,uv_index_max,"
                 "wind_speed_10m_max,wind_direction_10m_dominant,sunrise,sunset,"
                 "daylight_duration",
        "timezone": CONFIG.get("timezone", "America/Sao_Paulo"),
        "forecast_days": 7,
    }
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)


# ---------- Lua (nascer/pôr) ----------
# Port do algoritmo de moon times do SunCalc (Vladimir Agafonkin, BSD-2).

RAD = math.pi / 180
OBLIQUITY = RAD * 23.4397


def _moon_altitude(when, lat, lon):
    d = when.timestamp() / 86400 - 0.5 + 2440588 - 2451545
    L = RAD * (218.316 + 13.176396 * d)
    M = RAD * (134.963 + 13.064993 * d)
    F = RAD * (93.272 + 13.229350 * d)
    l = L + RAD * 6.289 * math.sin(M)
    b = RAD * 5.128 * math.sin(F)
    ra = math.atan2(math.sin(l) * math.cos(OBLIQUITY) - math.tan(b) * math.sin(OBLIQUITY),
                    math.cos(l))
    dec = math.asin(math.sin(b) * math.cos(OBLIQUITY) +
                    math.cos(b) * math.sin(OBLIQUITY) * math.sin(l))
    H = RAD * (280.16 + 360.9856235 * d) + RAD * lon - ra
    phi = RAD * lat
    h = math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(H))
    hr = max(h, 0)  # refração atmosférica
    return h + 0.0002967 / math.tan(hr + 0.00312536 / (hr + 0.08901179))


def moon_times(day, lat, lon):
    """Nascer e pôr da lua no dia local `day` (datetime ou None)."""
    t = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_TZ)
    hc = 0.133 * RAD

    def alt(hours):
        return _moon_altitude(t + timedelta(hours=hours), lat, lon) - hc

    rise = set_ = None
    h0 = alt(0)
    for i in range(1, 25, 2):
        h1, h2 = alt(i), alt(i + 1)
        a = (h0 + h2) / 2 - h1
        b = (h2 - h0) / 2
        xe = -b / (2 * a)
        ye = (a * xe + b) * xe + h1
        disc = b * b - 4 * a * h1
        roots, x1, x2 = 0, 0, 0
        if disc >= 0:
            dx = math.sqrt(disc) / (abs(a) * 2)
            x1, x2 = xe - dx, xe + dx
            roots = (abs(x1) <= 1) + (abs(x2) <= 1)
            if x1 < -1:
                x1 = x2
        if roots == 1:
            if h0 < 0:
                rise = i + x1
            else:
                set_ = i + x1
        elif roots == 2:
            rise = i + (x2 if ye < 0 else x1)
            set_ = i + (x1 if ye < 0 else x2)
        if rise is not None and set_ is not None:
            break
        h0 = h2
    to_dt = lambda hrs: None if hrs is None else t + timedelta(hours=hrs)
    return to_dt(rise), to_dt(set_)


def next_moon_events(lat, lon):
    """Próximo nascer e próximo pôr da lua a partir de agora."""
    now = datetime.now(LOCAL_TZ)
    rise = set_ = None
    for offset in range(3):
        r, s = moon_times(now.date() + timedelta(days=offset), lat, lon)
        if rise is None and r and r > now:
            rise = r
        if set_ is None and s and s > now:
            set_ = s
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M") if dt else None
    return {"rise": fmt(rise), "set": fmt(set_)}


def get_weather():
    with _weather_lock:
        if _weather_cache["body"] and time.time() - _weather_cache["at"] < WEATHER_TTL:
            return _weather_cache["body"]
        with urllib.request.urlopen(weather_url(), timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, dict):
            data = [data]
        cities = []
        for city, d in zip(CONFIG["cities"], data):
            c = {"name": city["name"], "detailed": bool(city.get("detailed")), "daily": d["daily"]}
            if c["detailed"]:
                c["current"] = d["current"]
                c["hourly"] = {k: v[1:] for k, v in d["hourly"].items()}  # pula a hora atual
                c["minutely_15"] = d.get("minutely_15")
            cities.append(c)
        first = CONFIG["cities"][0]
        body = json.dumps({
            "cities": cities,
            "sun": {k: data[0]["daily"][k][0] for k in ("sunrise", "sunset", "daylight_duration")},
            "moon": next_moon_events(first["lat"], first["lon"]),
        }).encode("utf-8")
        _weather_cache.update(at=time.time(), body=body)
        return body


# ---------- Calendário (iCal / .ics) ----------
# Parser mínimo de iCalendar: eventos simples, dia inteiro e recorrências
# comuns (DAILY/WEEKLY/MONTHLY/YEARLY com INTERVAL, COUNT, UNTIL, BYDAY,
# BYMONTHDAY, BYMONTH), EXDATE e ocorrências editadas (RECURRENCE-ID).

CAL_TTL = 10 * 60
_cal_cache = {"at": 0, "body": None}
_cal_lock = threading.Lock()
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def ics_events(text):
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    events, ev, nested = [], None, 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT":
            events.append(ev)
            ev = None
        elif ev is not None and line.startswith("BEGIN:"):
            nested += 1  # VALARM etc.
        elif ev is not None and line.startswith("END:"):
            nested -= 1
        elif ev is not None and not nested and ":" in line:
            head, value = line.split(":", 1)
            parts = head.split(";")
            params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            ev.setdefault(parts[0].upper(), []).append((params, value))
    return events


def ics_time(params, value):
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return dt.replace(tzinfo=timezone.utc)
    try:
        return dt.replace(tzinfo=ZoneInfo(params["TZID"].strip('"')))
    except Exception:
        return dt.replace(tzinfo=LOCAL_TZ)


def ics_text(value):
    return (value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def match_byday(byday, d):
    """BYDAY no escopo do mês: 'TU', '2TU', '-1FR'."""
    for token in byday.split(","):
        m = re.match(r"([+-]?\d*)([A-Z]{2})$", token)
        if not m or WEEKDAYS.get(m.group(2)) != d.weekday():
            continue
        if not m.group(1):
            return True
        n = int(m.group(1))
        dim = calendar.monthrange(d.year, d.month)[1]
        if (n > 0 and (d.day - 1) // 7 + 1 == n) or (n < 0 and (dim - d.day) // 7 + 1 == -n):
            return True
    return False


def match_monthday(bymonthday, d):
    dim = calendar.monthrange(d.year, d.month)[1]
    for x in bymonthday.split(","):
        x = int(x)
        if d.day == (x if x > 0 else dim + 1 + x):
            return True
    return False


def rrule_matches(rule, start, d):
    freq, interval = rule.get("FREQ"), int(rule.get("INTERVAL", 1))
    byday = rule.get("BYDAY")
    if "BYMONTH" in rule and d.month not in [int(x) for x in rule["BYMONTH"].split(",")]:
        return False
    if freq == "DAILY":
        return (d - start).days % interval == 0 and (
            not byday or d.weekday() in [WEEKDAYS[x[-2:]] for x in byday.split(",")])
    if freq == "WEEKLY":
        weeks = ((d - timedelta(d.weekday())) - (start - timedelta(start.weekday()))).days // 7
        days = [WEEKDAYS[x[-2:]] for x in byday.split(",")] if byday else [start.weekday()]
        return weeks % interval == 0 and d.weekday() in days
    if freq == "MONTHLY":
        months = (d.year - start.year) * 12 + d.month - start.month
        if months % interval:
            return False
    elif freq == "YEARLY":
        if (d.year - start.year) % interval:
            return False
        if "BYMONTH" not in rule and d.month != start.month:
            return False
    else:
        return False
    if byday:
        return match_byday(byday, d)
    if "BYMONTHDAY" in rule:
        return match_monthday(rule["BYMONTHDAY"], d)
    return d.day == start.day


def ics_occurrences(ev, win_start, win_end):
    """Gera (início, duração) das ocorrências que podem cair na janela de datas."""
    start = ics_time(*ev["DTSTART"][0])
    all_day = not isinstance(start, datetime)
    end = ics_time(*ev["DTEND"][0]) if "DTEND" in ev else None
    if end is None or isinstance(end, datetime) == all_day:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    dur = end - start
    if "RRULE" not in ev:
        yield start, dur
        return
    rule = dict(p.split("=", 1) for p in ev["RRULE"][0][1].split(";") if "=" in p)
    start_date = start if all_day else start.date()
    until = None
    if "UNTIL" in rule:
        u = rule["UNTIL"]
        until = date(int(u[:4]), int(u[4:6]), int(u[6:8]))
    count = int(rule.get("COUNT", 0))
    exdates = set()
    for params, value in ev.get("EXDATE", []):
        for v in value.split(","):
            t = ics_time(params, v)
            exdates.add(t if not isinstance(t, datetime) else t.astimezone(start.tzinfo).date())
    # Com COUNT é preciso contar desde o início; sem COUNT dá para pular direto à janela.
    d = start_date if count else max(start_date, win_start - timedelta(days=dur.days + 1))
    n = 0
    while d <= win_end:
        if until and d > until:
            break
        if rrule_matches(rule, start_date, d):
            n += 1
            if count and n > count:
                break
            if d not in exdates:
                yield (d if all_day else datetime.combine(d, start.time(), tzinfo=start.tzinfo)), dur
        d += timedelta(days=1)


def build_agenda(texts, days):
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    win_end = today + timedelta(days=days)
    events = [ev for text in texts for ev in ics_events(text) if "DTSTART" in ev]

    overridden = {}  # uid -> conjunto de ocorrências substituídas por edições
    for ev in events:
        if "RECURRENCE-ID" in ev:
            uid = ev.get("UID", [({}, "")])[0][1]
            overridden.setdefault(uid, set()).add(ics_time(*ev["RECURRENCE-ID"][0]))

    items = []
    for ev in events:
        if ev.get("STATUS", [({}, "")])[0][1].upper() == "CANCELLED":
            continue
        uid = ev.get("UID", [({}, "")])[0][1]
        skip = overridden.get(uid, set()) if "RECURRENCE-ID" not in ev else set()
        title = ics_text(ev.get("SUMMARY", [({}, "(sem título)")])[0][1])
        for occ, dur in ics_occurrences(ev, today, win_end):
            if occ in skip:
                continue
            if isinstance(occ, datetime):
                s, e = occ.astimezone(LOCAL_TZ), (occ + dur).astimezone(LOCAL_TZ)
                if e <= now or s.date() > win_end:
                    continue
                day = max(s.date(), today)
                items.append((day, s.strftime("%H:%M") if s.date() == day else "", title))
            else:
                if occ + dur <= today or occ > win_end:
                    continue
                items.append((max(occ, today), "", title))
    return [{"date": d.isoformat(), "time": t or None, "title": title}
            for d, t, title in items]


def get_calendar():
    cal = CONFIG.get("calendar") or {}
    with _cal_lock:
        if _cal_cache["body"] and time.time() - _cal_cache["at"] < CAL_TTL:
            return _cal_cache["body"]
        texts = []
        for url in cal.get("ics_urls", []):
            if url.startswith("webcal://"):
                url = "https://" + url[len("webcal://"):]
            with urllib.request.urlopen(url, timeout=20) as r:
                texts.append(r.read().decode("utf-8", "replace"))
        agenda = build_agenda(texts, cal.get("days", 14))
        # Apps Script (apps-script.gs) já devolve os eventos prontos em JSON.
        for url in cal.get("apps_script_urls", []):
            with urllib.request.urlopen(url, timeout=30) as r:
                agenda += json.loads(r.read().decode("utf-8"))
        agenda.sort(key=lambda i: (i["date"], i["time"] or "", i["title"]))
        body = json.dumps(agenda[:cal.get("max_events", 12)]).encode("utf-8")
        _cal_cache.update(at=time.time(), body=body)
        return body


# ---------- Câmera ----------

class RtspGrabber:
    """Mantém um ffmpeg lendo o RTSP e guarda o último quadro JPEG.

    Câmeras como a Tapo C500 só oferecem RTSP. O ffmpeg só roda enquanto a
    página estiver pedindo imagens; para sozinho após IDLE_STOP segundos.
    """
    IDLE_STOP = 60
    STALE = 15  # quadro mais velho que isso: conexão travada

    def __init__(self, url):
        self.url = url
        self.frame = None
        self.frame_at = 0
        self.proc = None
        self.last_request = 0
        self.lock = threading.Lock()
        self.thread = None

    def get(self):
        self.last_request = time.time()
        for _ in range(200):  # espera até 20s por um quadro recente
            with self.lock:
                if self.frame and time.time() - self.frame_at > self.STALE and self.proc:
                    self.proc.kill()  # travou (ex.: troca de rede): o _run termina e recomeça
                if not self.thread or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._run, daemon=True)
                    self.thread.start()
            if self.frame and time.time() - self.frame_at <= self.STALE:
                return self.frame
            time.sleep(0.1)
        raise RuntimeError("sem imagem da câmera (RTSP)")

    def _run(self):
        # -threads 1: um quadro a cada 2s não precisa de decodificação paralela (~90 MB em vez de ~200 MB).
        # -skip_frame nokey: decodifica só os quadros-chave (~1 a cada 2s na Tapo), ~57% menos CPU.
        # -timeout: sem dados da câmera por 10s, o ffmpeg desiste em vez de esperar para sempre.
        cmd = [FFMPEG, "-loglevel", "error", "-threads", "1", "-skip_frame", "nokey",
               "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", self.url,
               "-an", "-fps_mode", "passthrough", "-q:v", "6", "-f", "mjpeg", "pipe:1"]
        proc = self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        buf = b""
        try:
            while time.time() - self.last_request < self.IDLE_STOP:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                # Separa os JPEGs pelos marcadores de início (FFD8) e fim (FFD9).
                while True:
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2)
                    if start < 0 or end < 0:
                        break
                    self.frame = buf[start:end + 2]
                    self.frame_at = time.time()
                    buf = buf[end + 2:]
        finally:
            proc.kill()
            proc.wait()
            self.frame = None


# Fora do PATH quando o servidor é iniciado pelo launchd.
FFMPEG = (shutil.which("ffmpeg") or
          next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                if os.path.exists(p)), "ffmpeg"))


def start_tcp_relay(host, port):
    """Encaminha 127.0.0.1:<porta livre> para host:port e devolve a porta local.

    No macOS, Privacidade > Rede Local bloqueia o ffmpeg do Homebrew de acessar
    a rede local ("No route to host"), mas não bloqueia localhost. O Python faz
    a ponte até a câmera.
    """
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            src.close()

    def accept_loop():
        while True:
            client, _ = srv.accept()
            try:
                upstream = socket.create_connection((host, port), timeout=10)
            except OSError:
                client.close()
                continue
            upstream.settimeout(None)
            threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pump, args=(upstream, client), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return srv.getsockname()[1]


_rtsp = None
_rtsp_lock = threading.Lock()


def camera_active(cam):
    """Se está dentro de camera.active_hours ("07:30-18:00"); sem horário, sempre."""
    spec = cam.get("active_hours")
    if not spec:
        return True
    start, end = [int(h) * 60 + int(m) for h, m in (p.strip().split(":") for p in spec.split("-"))]
    now = datetime.now(LOCAL_TZ)
    minute = now.hour * 60 + now.minute
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def get_camera_snapshot():
    global _rtsp
    cam = CONFIG.get("camera") or {}
    if cam.get("rtsp_url"):
        with _rtsp_lock:
            if _rtsp is None:
                parts = urllib.parse.urlsplit(cam["rtsp_url"])
                local_port = start_tcp_relay(parts.hostname, parts.port or 554)
                auth = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
                url = parts._replace(netloc="%s127.0.0.1:%d" % (auth, local_port)).geturl()
                _rtsp = RtspGrabber(url)
        return _rtsp.get()
    url = cam.get("snapshot_url")
    if not url:
        return None
    user, pwd = cam.get("username"), cam.get("password")
    handlers = []
    if user:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, user, pwd or "")
        handlers += [urllib.request.HTTPDigestAuthHandler(mgr),
                     urllib.request.HTTPBasicAuthHandler(mgr)]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url)
    if user and cam.get("force_basic_auth"):
        token = base64.b64encode(("%s:%s" % (user, pwd or "")).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    with opener.open(req, timeout=10) as r:
        return r.read()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def send_bytes(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/weather":
                return self.send_bytes(get_weather(), "application/json; charset=utf-8")
            if path == "/api/config":
                cam = CONFIG.get("camera") or {}
                body = json.dumps({
                    "camera": bool(cam.get("snapshot_url") or cam.get("rtsp_url")),
                    "camera_refresh_seconds": cam.get("refresh_seconds", 5),
                    "camera_hours": cam.get("active_hours") or None,
                    "calendar": bool((CONFIG.get("calendar") or {}).get("ics_urls")
                                     or (CONFIG.get("calendar") or {}).get("apps_script_urls")),
                }).encode("utf-8")
                return self.send_bytes(body, "application/json; charset=utf-8")
            if path == "/api/time":
                # A página usa esta hora: o fuso do iOS 9 ainda tem horário de verão no Brasil.
                now = datetime.now(LOCAL_TZ)
                body = json.dumps({"now": int(now.timestamp() * 1000),
                                   "offset": int(now.utcoffset().total_seconds() // 60)}).encode("utf-8")
                return self.send_bytes(body, "application/json; charset=utf-8")
            if path == "/api/calendar":
                return self.send_bytes(get_calendar(), "application/json; charset=utf-8")
            if path == "/api/camera.jpg":
                # Fora do horário a página nem pede; isto só evita ligar o ffmpeg à toa.
                if not camera_active(CONFIG.get("camera") or {}):
                    return self.send_bytes(b"camera fora do horario", "text/plain", 503)
                img = get_camera_snapshot()
                if img is None:
                    return self.send_bytes(b"camera not configured", "text/plain", 404)
                return self.send_bytes(img, "image/jpeg")
        except Exception as e:  # erro externo: devolve 502 e a página tenta de novo depois
            self.log_error("%s: %s", path, e)
            return self.send_bytes(str(e).encode("utf-8"), "text/plain", 502)
        if path in ("/", "/index.html", "/apple-touch-icon.png"):
            return super().do_GET()
        self.send_error(404)


if __name__ == "__main__":
    port = int(CONFIG.get("port", 8080))
    print("Painel em http://0.0.0.0:%d  (abra no iPad com o IP deste computador)" % port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
