#!/usr/bin/env python3
"""Servidor do painel do iPad.

Serve index.html e faz proxy dos dados externos (previsão do tempo e câmera),
porque o Safari do iOS 9 não confia nos certificados HTTPS modernos e não
consegue autenticar direto em muitas câmeras.

Uso: python3 server.py   (sem dependências; Python 3.7+)
"""
import base64
import calendar
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
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
CLOCK_STYLES = ("classico", "flip", "digital")


def config_version():
    """Muda quando o config ou o index.html mudam: a página compara e recarrega sozinha
    (também depois de um deploy)."""
    try:
        page = str(os.path.getmtime(os.path.join(ROOT, "index.html")))
    except OSError:
        page = ""
    return hashlib.sha1((json.dumps(CONFIG, sort_keys=True) + page).encode("utf-8")).hexdigest()[:12]

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


# ---------- Sol (crepúsculo, meio-dia, hora dourada) ----------
# Port do getTimes do SunCalc (Vladimir Agafonkin, BSD-2).

def sun_times(day, lat, lon):
    """Instantes do dia local `day` em que o sol passa por cada altura (graus)."""
    J1970, J2000, J0 = 2440588, 2451545, 0.0009
    noon_local = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_TZ) + timedelta(hours=12)
    d = noon_local.timestamp() / 86400 - 0.5 + J1970 - J2000
    lw, phi = RAD * -lon, RAD * lat
    n = round(d - J0 - lw / (2 * math.pi))
    ds = J0 + lw / (2 * math.pi) + n
    M = RAD * (357.5291 + 0.98560028 * ds)
    L = M + RAD * (1.9148 * math.sin(M) + 0.02 * math.sin(2 * M) + 0.0003 * math.sin(3 * M)) \
        + RAD * 102.9372 + math.pi
    dec = math.asin(math.sin(OBLIQUITY) * math.sin(L))
    j_noon = J2000 + ds + 0.0053 * math.sin(M) - 0.0069 * math.sin(2 * L)

    def to_dt(j):
        return datetime.fromtimestamp((j + 0.5 - J1970) * 86400, LOCAL_TZ)

    def pair(angle):
        cos_w = (math.sin(angle * RAD) - math.sin(phi) * math.sin(dec)) / (math.cos(phi) * math.cos(dec))
        if not -1 <= cos_w <= 1:
            return None, None  # sol nunca chega nessa altura (latitudes altas)
        w = math.acos(cos_w)
        a = J0 + (w + lw) / (2 * math.pi) + n
        j_set = J2000 + a + 0.0053 * math.sin(M) - 0.0069 * math.sin(2 * L)
        return to_dt(j_noon - (j_set - j_noon)), to_dt(j_set)

    rise, set_ = pair(-0.833)
    dawn, dusk = pair(-6)
    golden_end, golden_start = pair(6)
    return {"noon": to_dt(j_noon), "rise": rise, "set": set_, "dawn": dawn, "dusk": dusk,
            "golden_morning_end": golden_end, "golden_evening_start": golden_start}


def sun_extra(lat, lon):
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M") if dt else None
    today = datetime.now(LOCAL_TZ).date()
    t, y = sun_times(today, lat, lon), sun_times(today - timedelta(days=1), lat, lon)
    out = {k: fmt(v) for k, v in t.items() if k not in ("rise", "set")}
    if t["rise"] and y["rise"]:
        # Quanto o dia cresceu (ou encolheu) desde ontem, em segundos.
        out["daylight_change"] = round(((t["set"] - t["rise"]) - (y["set"] - y["rise"])).total_seconds())
    return out


# ---------- Fases da lua (Meeus, Astronomical Algorithms, cap. 49) ----------
# Sem os termos planetários: erro de poucos minutos.

def _phase_jde(k, phase):
    T = k / 1236.85
    jde = (2451550.09766 + 29.530588861 * k + 0.00015437 * T ** 2
           - 0.000000150 * T ** 3 + 0.00000000073 * T ** 4)
    E = 1 - 0.002516 * T - 0.0000074 * T ** 2
    M = RAD * (2.5534 + 29.10535670 * k - 0.0000014 * T ** 2 - 0.00000011 * T ** 3)
    Mp = RAD * (201.5643 + 385.81693528 * k + 0.0107582 * T ** 2 + 0.00001238 * T ** 3)
    F = RAD * (160.7108 + 390.67050284 * k - 0.0016118 * T ** 2 - 0.00000227 * T ** 3)
    Om = RAD * (124.7746 - 1.56375588 * k + 0.0020672 * T ** 2 + 0.00000215 * T ** 3)
    sin = math.sin
    if phase in (0, 0.5):
        c = (-0.40720, 0.17241, 0.01608, 0.01039, 0.00739, -0.00514, 0.00208) if phase == 0 else \
            (-0.40614, 0.17302, 0.01614, 0.01043, 0.00734, -0.00515, 0.00209)
        jde += (c[0] * sin(Mp) + c[1] * E * sin(M) + c[2] * sin(2 * Mp) + c[3] * sin(2 * F)
                + c[4] * E * sin(Mp - M) + c[5] * E * sin(Mp + M) + c[6] * E * E * sin(2 * M)
                - 0.00111 * sin(Mp - 2 * F) - 0.00057 * sin(Mp + 2 * F) + 0.00056 * E * sin(2 * Mp + M)
                - 0.00042 * sin(3 * Mp) + 0.00042 * E * sin(M + 2 * F) + 0.00038 * E * sin(M - 2 * F)
                - 0.00024 * E * sin(2 * Mp - M) - 0.00017 * sin(Om))
    else:
        jde += (-0.62801 * sin(Mp) + 0.17172 * E * sin(M) - 0.01183 * E * sin(Mp + M)
                + 0.00862 * sin(2 * Mp) + 0.00804 * sin(2 * F) + 0.00454 * E * sin(Mp - M)
                + 0.00204 * E * E * sin(2 * M) - 0.00180 * sin(Mp - 2 * F) - 0.00070 * sin(Mp + 2 * F)
                - 0.00040 * sin(3 * Mp) - 0.00034 * E * sin(2 * Mp - M) + 0.00032 * E * sin(M + 2 * F)
                + 0.00032 * E * sin(M - 2 * F) - 0.00028 * E * E * sin(Mp + 2 * M)
                + 0.00027 * E * sin(2 * Mp + M) - 0.00017 * sin(Om))
        W = (0.00306 - 0.00038 * E * math.cos(M) + 0.00026 * math.cos(Mp) - 0.00002 * math.cos(Mp - M)
             + 0.00002 * math.cos(Mp + M) + 0.00002 * math.cos(2 * F))
        jde += W if phase == 0.25 else -W
    return jde


def next_moon_phases(count=4):
    """Próximas fases principais: [{"phase": "new|first|full|last", "at": "AAAA-MM-DDTHH:MM"}]."""
    now = datetime.now(LOCAL_TZ)
    names = {0: "new", 0.25: "first", 0.5: "full", 0.75: "last"}
    k0 = math.floor((now.year + (now.timetuple().tm_yday - 1) / 365.25 - 2000) * 12.3685) - 1
    out = []
    for k in range(k0, k0 + 8):
        for ph in (0, 0.25, 0.5, 0.75):
            at = datetime.fromtimestamp((_phase_jde(k + ph, ph) - 2440587.5) * 86400, LOCAL_TZ)
            if at > now and len(out) < count:
                out.append({"phase": names[ph], "at": at.strftime("%Y-%m-%dT%H:%M")})
    return out


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
            # Todas as cidades levam o tempo atual e as próximas horas (card de cima desliza
            # entre elas); "detailed" marca a cidade principal (avisos, fundo, card padrão).
            c = {"name": city["name"], "detailed": bool(city.get("detailed")), "daily": d["daily"],
                 "current": d["current"],
                 "hourly": {k: v[1:] for k, v in d["hourly"].items()}}  # pula a hora atual
            if c["detailed"]:
                c["minutely_15"] = d.get("minutely_15")
            cities.append(c)
        first = CONFIG["cities"][0]
        body = json.dumps({
            "cities": cities,
            "sun": dict({k: data[0]["daily"][k][0] for k in ("sunrise", "sunset", "daylight_duration")},
                        **sun_extra(first["lat"], first["lon"])),
            "moon": dict(next_moon_events(first["lat"], first["lon"]), phases=next_moon_phases()),
        }).encode("utf-8")
        _weather_cache.update(at=time.time(), body=body)
        return body


# ---------- Calendário (iCal / .ics) ----------
# Parser mínimo de iCalendar: eventos simples, dia inteiro e recorrências
# comuns (DAILY/WEEKLY/MONTHLY/YEARLY com INTERVAL, COUNT, UNTIL, BYDAY,
# BYMONTHDAY, BYMONTH), EXDATE e ocorrências editadas (RECURRENCE-ID).

CAL_TTL = 30 * 60  # agendas grandes (o .ics do Google pode ter MBs)
_cal_cache = {"at": 0, "body": None}
_cal_last_good = {}  # url -> último conteúdo baixado com sucesso
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
        title = ics_text(ev.get("SUMMARY", [({}, "(sem título)")])[0][1]).strip()
        for occ, dur in ics_occurrences(ev, today, win_end):
            if occ in skip:
                continue
            if isinstance(occ, datetime):
                s, e = occ.astimezone(LOCAL_TZ), (occ + dur).astimezone(LOCAL_TZ)
                if e <= now or s.date() > win_end:
                    continue
                day = max(s.date(), today)
                items.append((day, s.strftime("%H:%M") if s.date() == day else "", title,
                              e.strftime("%H:%M") if e.date() == day else ""))
            else:
                if occ + dur <= today or occ > win_end:
                    continue
                items.append((max(occ, today), "", title, ""))
    return [{"date": d.isoformat(), "time": t or None, "end": end or None, "title": title}
            for d, t, title, end in items]


def calendar_sources(cal, key):
    """Entradas de ics_urls/apps_script_urls: "url" ou {"name": ..., "url": ...}."""
    for item in cal.get(key, []):
        if isinstance(item, str):
            item = {"url": item}
        yield item.get("name") or "", item["url"]


def fetch_cached(url, timeout):
    """Baixa a URL; se falhar, usa a última versão boa (uma agenda fora do ar não derruba as outras)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            _cal_last_good[url] = r.read().decode("utf-8", "replace")
    except Exception as e:
        print("agenda indisponível (%s): %s" % (url.split("/")[2], e), flush=True)
    return _cal_last_good.get(url)


def get_calendar():
    cal = CONFIG.get("calendar") or {}
    with _cal_lock:
        if _cal_cache["body"] and time.time() - _cal_cache["at"] < CAL_TTL:
            return _cal_cache["body"]
        agenda = []
        days = cal.get("days", 5)
        last_day = (datetime.now(LOCAL_TZ).date() + timedelta(days=days)).isoformat()
        sources = list(calendar_sources(cal, "ics_urls")) + list(calendar_sources(cal, "apps_script_urls"))
        for name, url in sources:
            if url.startswith("webcal://"):
                url = "https://" + url[len("webcal://"):]
            text = fetch_cached(url, 30)
            if not text:
                continue
            # Apps Script (apps-script.gs) devolve JSON pronto; .ics é iCalendar. Decide pelo
            # conteúdo, então tanto faz em qual lista a URL foi colocada.
            if text.lstrip().startswith("["):
                try:
                    events = [ev for ev in json.loads(text) if ev.get("date", "") <= last_day]
                except ValueError as e:
                    print("agenda com JSON inválido (%s): %s" % (name or url.split("/")[2], e), flush=True)
                    continue
            else:
                events = build_agenda([text], days)
            for ev in events:
                ev["cal"] = name
                agenda.append(ev)
        # Mesmo evento em duas agendas (ou duplicado na mesma) aparece uma vez.
        seen, unique = set(), []
        for ev in sorted(agenda, key=lambda i: (i["date"], i["time"] or "", i["title"])):
            key = (ev["date"], ev["time"], ev["title"])
            if key not in seen:
                seen.add(key)
                unique.append(ev)
        body = json.dumps(unique[:cal.get("max_events", 50)]).encode("utf-8")
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
        self.closed = False  # config mudou: este grabber não religa mais o ffmpeg

    def close(self):
        self.closed = True
        if self.proc:
            self.proc.kill()

    def get(self):
        self.last_request = time.time()
        for _ in range(400):  # espera até 40s por um quadro recente (~15s no Raspberry Pi 1)
            if self.closed:
                raise RuntimeError("câmera reconfigurada")
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
            while time.time() - self.last_request < self.IDLE_STOP and not self.closed:
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
                url = cam["rtsp_url"]
                if sys.platform == "darwin":  # só o macOS precisa da ponte (ver start_tcp_relay)
                    parts = urllib.parse.urlsplit(url)
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


# ---------- Admin (editar o config.json pelo navegador) ----------

HOURS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def validate_config(cfg):
    """Devolve uma mensagem de erro, ou None se o config pode ser salvo."""
    if not isinstance(cfg, dict):
        return "o config precisa ser um objeto JSON"
    try:
        ZoneInfo(cfg.get("timezone", "America/Sao_Paulo"))
    except Exception:
        return "fuso horário inválido: %s" % cfg.get("timezone")
    cities = cfg.get("cities")
    if not isinstance(cities, list) or not cities:
        return "precisa de pelo menos uma cidade"
    for c in cities:
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            return "toda cidade precisa de nome"
        lat, lon = c.get("lat"), c.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) \
                or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            return "latitude/longitude inválida em %s" % c["name"]
    for label, value in (("modo noturno", cfg.get("night_hours", "")),
                         ("horário da câmera", (cfg.get("camera") or {}).get("active_hours", ""))):
        if value and not HOURS_RE.match(value):
            return "%s: use HH:MM-HH:MM (ex.: 23:00-06:00)" % label
    if cfg.get("clock_style", "classico") not in CLOCK_STYLES:
        return "estilo de relógio inválido"
    port = cfg.get("port", 8080)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return "porta inválida"
    cal = cfg.get("calendar") or {}
    for key in ("ics_urls", "apps_script_urls"):
        for item in cal.get(key, []):
            url = item if isinstance(item, str) else (item or {}).get("url", "")
            if not re.match(r"^(https?|webcal)://", url or ""):
                return "URL de agenda inválida: %s" % (url or "(vazia)")
            color = item.get("color") if isinstance(item, dict) else None
            if color and not re.match(r"^#[0-9a-fA-F]{6}$", color):
                return "cor de agenda inválida: %s" % color
    if cal.get("color_style", "border") not in ("border", "fill"):
        return "estilo de cor da agenda inválido"
    for r in cfg.get("reminders") or []:
        if not isinstance(r, dict) or not str(r.get("text", "")).strip():
            return "todo lembrete precisa de texto"
    return None


def save_config(new):
    """Grava o config.json (com cópia .bak do anterior) e aplica sem reiniciar."""
    global LOCAL_TZ, _rtsp
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)  # tem senhas
    if os.path.exists(CONFIG_PATH):
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
    os.replace(tmp, CONFIG_PATH)

    camera_changed = new.get("camera") != CONFIG.get("camera")
    CONFIG.update(new)  # sem clear(): requisições em andamento nunca veem o config vazio
    for k in [k for k in CONFIG if k not in new]:
        del CONFIG[k]
    LOCAL_TZ = ZoneInfo(CONFIG.get("timezone", "America/Sao_Paulo"))
    with _weather_lock:
        _weather_cache["at"] = 0
    with _cal_lock:
        _cal_cache["at"] = 0
    if camera_changed:
        with _rtsp_lock:
            if _rtsp is not None:
                _rtsp.close()
                _rtsp = None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        # A página: o iPad sempre confere se mudou (responde 304 se não), senão o
        # Safari fica dias com a versão antiga em cache.
        if self.path.split("?")[0] in ("/", "/index.html", "/admin.html"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_bytes(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def admin_ok(self):
        """Admin aberto na rede local; com admin_password no config pede senha (qualquer usuário)."""
        pwd = CONFIG.get("admin_password")
        if not pwd:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                given = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)[1]
                if hmac.compare_digest(given.encode("utf-8"), str(pwd).encode("utf-8")):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Painel admin"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/admin/config":
            return self.send_error(404)
        if not self.admin_ok():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            new = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            return self.send_bytes(("JSON inválido: %s" % e).encode("utf-8"), "text/plain; charset=utf-8", 400)
        err = validate_config(new)
        if err:
            return self.send_bytes(err.encode("utf-8"), "text/plain; charset=utf-8", 400)
        restart = new.get("port", 8080) != CONFIG.get("port", 8080)
        save_config(new)
        self.log_message("config salvo pelo admin (versão %s)", config_version())
        body = json.dumps({"ok": True, "version": config_version(), "restart_needed": restart})
        return self.send_bytes(body.encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/admin", "/admin.html", "/api/admin/config") and not self.admin_ok():
            return
        try:
            if path == "/api/admin/config":
                body = json.dumps(CONFIG, ensure_ascii=False).encode("utf-8")
                return self.send_bytes(body, "application/json; charset=utf-8")
            if path == "/api/weather":
                return self.send_bytes(get_weather(), "application/json; charset=utf-8")
            if path == "/api/config":
                cam = CONFIG.get("camera") or {}
                body = json.dumps({
                    "version": config_version(),
                    "clock_style": CONFIG.get("clock_style", "classico"),
                    "camera": bool(cam.get("snapshot_url") or cam.get("rtsp_url")),
                    "camera_refresh_seconds": cam.get("refresh_seconds", 5),
                    "camera_hours": cam.get("active_hours") or None,
                    "reminders": CONFIG.get("reminders") or [],
                    # Modo noturno (só o relógio, apagado); "" desliga.
                    "night_hours": CONFIG.get("night_hours", "23:00-06:00") or None,
                    "calendar": bool((CONFIG.get("calendar") or {}).get("ics_urls")
                                     or (CONFIG.get("calendar") or {}).get("apps_script_urls")),
                    # Nomes das agendas, na ordem do config, e a cor de cada uma (None = paleta padrão).
                    "calendars": [n for k in ("ics_urls", "apps_script_urls")
                                  for n, _ in calendar_sources(CONFIG.get("calendar") or {}, k)],
                    "calendar_colors": [item.get("color") if isinstance(item, dict) else None
                                        for k in ("ics_urls", "apps_script_urls")
                                        for item in (CONFIG.get("calendar") or {}).get(k, [])],
                    # "border": só a borda esquerda colorida; "fill": fundo do evento na cor da agenda.
                    "calendar_color_style": (CONFIG.get("calendar") or {}).get("color_style", "border"),
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
        if path == "/admin":
            self.path = "/admin.html"
            path = "/admin.html"
        if path in ("/", "/index.html", "/admin.html", "/apple-touch-icon.png"):
            return super().do_GET()
        self.send_error(404)


if __name__ == "__main__":
    port = int(CONFIG.get("port", 8080))
    print("Painel em http://0.0.0.0:%d  (abra no iPad com o IP deste computador)" % port)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
