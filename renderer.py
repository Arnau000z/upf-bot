"""
Renderiza horarios UPF — Pillow, fuentes Ubuntu, diseño dark.
"""
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from io import BytesIO
import math

_TZ = ZoneInfo("Europe/Madrid")

def _now() -> datetime:
    return datetime.now(_TZ)

def _today() -> date:
    return _now().date()

# ── Fuentes ────────────────────────────────────────────────────────────────────
_FB = "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"
_FR = "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf"
_FL = "/usr/share/fonts/truetype/ubuntu/Ubuntu-L.ttf"

def _font(bold=False, size=14):
    try:
        return ImageFont.truetype(_FB if bold else _FR, size)
    except Exception:
        return ImageFont.load_default()

# ── Paleta ─────────────────────────────────────────────────────────────────────
BG       = (13,  17,  23)
SURFACE  = (22,  27,  34)
SURFACE2 = (30,  38,  50)
SURFACE3 = (38,  48,  62)
BORDER   = (48,  54,  61)
TEXT     = (230, 237, 243)
MUTED    = (110, 118, 130)
DIM      = (60,  68,  80)
ACCENT   = (88, 166, 255)   # azul UPF para badges y "avui"
NOW_LINE = (255,  85,  85)  # rojo para línea de hora actual

PALETTE = [
    (0,   210, 255),   # cyan
    (255,  77, 109),   # rosa
    (162,  89, 255),   # violeta
    (0,   245, 160),   # menta
    (255, 159,  28),   # naranja
    (76,  201, 240),   # azul cielo
    (247,  37, 133),   # magenta
    (77,  222, 123),   # verde
    (255, 209, 102),   # amarillo
    (239,  71, 111),   # rojo
    (6,   214, 160),   # esmeralda
    (58,  134, 255),   # azul
]

TIPO_LABEL = {
    'Teoria': 'T', 'Pràctiques': 'P', 'Seminari': 'S',
    'Laboratori': 'L', 'Tutoria': 'Tu',
}
DIAS_ES  = ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo']
DIAS_ABR = ['LUN','MAR','MIÉ','JUE','VIE']

_CMAP: dict = {}

def _color(nombre: str) -> tuple:
    if nombre not in _CMAP:
        _CMAP[nombre] = PALETTE[len(_CMAP) % len(PALETTE)]
    return _CMAP[nombre]

def _mix(color: tuple, bg: tuple, alpha: float) -> tuple:
    return tuple(int(c * alpha + b * (1 - alpha)) for c, b in zip(color, bg))

def _t2px(t: str, t0: float, px_h: int) -> int:
    h, m = map(int, t.split(':'))
    return int((h + m / 60 - t0) * px_h)

def _t2f(t: str) -> float:
    h, m = map(int, t.split(':'))
    return h + m / 60

def _fit(text: str, font, max_w: int) -> str:
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text + '…') > max_w:
        text = text[:-1]
    return text + '…'

def _rounded_rect(draw, xy, radius, fill, outline=None, width=1):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                           fill=fill, outline=outline, width=width)

def _bio(img: Image.Image) -> BytesIO:
    bio = BytesIO()
    img.save(bio, format='PNG', optimize=True)
    bio.seek(0)
    return bio


# ── Detecció de carrils per a events solapats ──────────────────────────────────
def _assign_lanes(events: list) -> list:
    """
    Detecta events solapats i assigna carrils (columnes) a cadascun.
    Retorna [(ev, col_idx, total_cols), ...]
    """
    if not events:
        return []

    evs = sorted(events, key=lambda e: (_t2f(e['inicio']), _t2f(e['fin'])))
    n   = len(evs)

    # Union-Find per agrupar events que se solapen
    parent = list(range(n))
    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if (_t2f(evs[i]['inicio']) < _t2f(evs[j]['fin']) and
                    _t2f(evs[j]['inicio']) < _t2f(evs[i]['fin'])):
                parent[root(i)] = root(j)

    grups: dict = defaultdict(list)
    for i in range(n):
        grups[root(i)].append(i)

    result = [None] * n
    for indices in grups.values():
        indices_ord = sorted(indices, key=lambda i: _t2f(evs[i]['inicio']))
        total = len(indices_ord)
        for col, idx in enumerate(indices_ord):
            result[idx] = (evs[idx], col, total)

    return result


# ── Bloc d'event ───────────────────────────────────────────────────────────────
def _draw_event(draw, x0, y0, x1, y1, color, nombre, tipo, inicio, fin, aula, grup,
                name_size=17, detail_size=12):
    h      = y1 - y0
    bar_w  = 5
    r      = 6

    bg_color = _mix(color, SURFACE2, 0.18)
    _rounded_rect(draw, (x0, y0, x1, y1), r, fill=bg_color,
                  outline=_mix(color, SURFACE2, 0.4), width=1)

    _rounded_rect(draw, (x0, y0, x0 + bar_w, y1), min(r, bar_w // 2), fill=color)
    if bar_w > 2:
        draw.rectangle([x0 + bar_w // 2, y0, x0 + bar_w, y1], fill=color)

    pad_l  = bar_w + 10
    text_w = x1 - x0 - pad_l - 8
    mid_y  = (y0 + y1) // 2

    if h >= 55:
        name_label   = _fit(nombre, _font(True, name_size), text_w)
        tipo_s       = TIPO_LABEL.get(tipo, tipo[:1])
        detail_label = _fit(f"{inicio}–{fin}  {tipo_s}  {aula}",
                            _font(False, detail_size), text_w)

        name_h   = _font(True,  name_size).getbbox(name_label)[3]
        detail_h = _font(False, detail_size).getbbox(detail_label)[3]

        # Badge de grup
        grup_label = f"G{grup}" if grup else ""
        badge_h = 0
        if grup_label and h >= 75:
            badge_h = detail_size + 4

        gap      = 3
        total_h  = name_h + gap + detail_h + (gap + badge_h if badge_h else 0)
        y_name   = mid_y - total_h // 2

        draw.text((x0 + pad_l, y_name), name_label,
                  font=_font(True, name_size), fill=TEXT)
        draw.text((x0 + pad_l, y_name + name_h + gap), detail_label,
                  font=_font(False, detail_size), fill=MUTED)

        if badge_h:
            y_badge = y_name + name_h + gap + detail_h + gap
            badge_txt = grup_label
            bw = int(_font(False, detail_size - 1).getlength(badge_txt)) + 10
            bx0 = x0 + pad_l
            bx1 = bx0 + bw
            by0 = y_badge
            by1 = by0 + badge_h - 2
            badge_bg = _mix(color, SURFACE2, 0.30)
            _rounded_rect(draw, (bx0, by0, bx1, by1), 3, fill=badge_bg)
            draw.text((bx0 + 5, by0 + 1), badge_txt,
                      font=_font(False, detail_size - 1), fill=color)

    elif h >= 30:
        name_label = _fit(nombre, _font(True, name_size - 1), text_w)
        bh = _font(True, name_size - 1).getbbox(name_label)[3]
        draw.text((x0 + pad_l, mid_y - bh // 2), name_label,
                  font=_font(True, name_size - 1), fill=TEXT)


# ── Vista diaria ───────────────────────────────────────────────────────────────
def render_dia(eventos: list, fecha: date) -> BytesIO:
    W      = 940
    ML     = 72    # margen izquierdo (etiquetas de hora)
    MR     = 20
    HEADER = 80
    FOOTER = 36
    PX_H   = 100

    es_avui = (fecha == _today())

    if not eventos:
        img = Image.new('RGB', (W, 260), BG)
        d   = ImageDraw.Draw(img)
        dia = DIAS_ES[fecha.weekday()].upper()
        # Header simple
        _draw_header_dia(d, W, HEADER, fecha, dia, 0, es_avui)
        d.text((W // 2, 160), "Cap classe aquest dia  🎉",
               font=_font(False, 16), fill=MUTED, anchor='mm')
        _draw_footer(d, W, ML, MR, 260, FOOTER, 0)
        return _bio(img)

    evs  = sorted(eventos, key=lambda e: e['inicio'])
    t0   = max(7.5, math.floor(_t2f(evs[0]['inicio'])) - 0.0)
    t1   = min(22.5, math.ceil(_t2f(evs[-1]['fin'])) + 0.5)
    h_px = int((t1 - t0) * PX_H)
    H    = HEADER + h_px + FOOTER

    img = Image.new('RGB', (W, H), BG)
    d   = ImageDraw.Draw(img)

    dia = DIAS_ES[fecha.weekday()].upper()
    _draw_header_dia(d, W, HEADER, fecha, dia, len(evs), es_avui)

    # ── Cuadrícula ─────────────────────────────────────────────────────────────
    for h in range(int(t0), int(t1) + 1):
        y = HEADER + _t2px(f"{h:02d}:00", t0, PX_H)
        d.line([(ML, y), (W - MR, y)], fill=SURFACE3, width=1)
        d.text((ML - 8, y), f"{h:02d}:00",
               font=_font(False, 11), fill=DIM, anchor='rm')
        # Mitja hora (línia tènue)
        yh = HEADER + _t2px(f"{h:02d}:30", t0, PX_H)
        if yh < HEADER + h_px:
            d.line([(ML, yh), (W - MR, yh)], fill=SURFACE2, width=1)

    # ── Línia de l'hora actual ─────────────────────────────────────────────────
    if es_avui:
        now   = _now()
        t_now = now.hour + now.minute / 60
        if t0 <= t_now <= t1:
            y_now = HEADER + int((t_now - t0) * PX_H)
            d.line([(ML, y_now), (W - MR, y_now)], fill=NOW_LINE, width=2)
            draw_circle(d, ML - 5, y_now, 4, NOW_LINE)

    # ── Blocs (amb carrils per solapaments) ───────────────────────────────────
    col_area = W - ML - MR - 8
    lanes    = _assign_lanes(evs)
    for ev, col_idx, n_cols in lanes:
        y0_ev  = HEADER + _t2px(ev['inicio'], t0, PX_H) + 3
        y1_ev  = HEADER + _t2px(ev['fin'],    t0, PX_H) - 3
        col_w  = (col_area // n_cols)
        gap    = 3 if n_cols > 1 else 0
        x0_ev  = ML + 4 + col_idx * col_w + gap
        x1_ev  = ML + 4 + (col_idx + 1) * col_w - gap - 2
        ns     = max(15, 17 - (n_cols - 1) * 2)   # font més petita si hi ha solapament
        ds     = max(10, 12 - (n_cols - 1))
        color  = _color(ev['nombre'])
        _draw_event(d, x0_ev, y0_ev, x1_ev, y1_ev, color,
                    ev['nombre'], ev['tipo'], ev['inicio'], ev['fin'],
                    ev['aula'], ev.get('grup', ''),
                    name_size=ns, detail_size=ds)

    # ── Footer ─────────────────────────────────────────────────────────────────
    _draw_footer(d, W, ML, MR, H, FOOTER, len(evs))

    return _bio(img)


# ── Vista semanal ──────────────────────────────────────────────────────────────
def render_semana(eventos: list, desde: date, hasta: date) -> BytesIO:
    days = [desde + timedelta(i)
            for i in range((hasta - desde).days + 1)
            if (desde + timedelta(i)).weekday() < 5]
    if not days:
        days = [desde]

    por_dia = defaultdict(list)
    for ev in eventos:
        d_ev = datetime.strptime(ev['fecha'], '%Y-%m-%d').date()
        if d_ev in days:
            por_dia[d_ev].append(ev)

    all_evs = [ev for evs_l in por_dia.values() for ev in evs_l]
    if all_evs:
        t0 = max(7.5, math.floor(min(_t2f(e['inicio']) for e in all_evs)))
        t1 = min(22.5, math.ceil( max(_t2f(e['fin'])   for e in all_evs)) + 0.5)
    else:
        t0, t1 = 8.0, 20.0

    n      = len(days)
    ML     = 62
    MR     = 14
    HEADER = 76
    FOOTER = 36
    PX_H   = 88
    COL_W  = max(140, (960 - ML - MR) // n)
    W      = ML + n * COL_W + MR
    H      = HEADER + int((t1 - t0) * PX_H) + FOOTER
    hoy    = _today()

    img = Image.new('RGB', (W, H), BG)
    d   = ImageDraw.Draw(img)

    # Header
    titulo = f"Setmana  {desde.strftime('%-d/%m')} – {hasta.strftime('%-d/%m/%Y')}"
    d.text((W // 2, HEADER // 2 - 8), titulo,
           font=_font(True, 18), fill=TEXT, anchor='mm')
    n_cls = len(all_evs)
    d.text((W // 2, HEADER // 2 + 12),
           f"{n_cls} classe{'s' if n_cls != 1 else ''}  ·  UPF  ·  T3 2026",
           font=_font(False, 11), fill=MUTED, anchor='mm')
    d.line([(ML, HEADER - 1), (W - MR, HEADER - 1)], fill=BORDER, width=1)

    # Cuadrícula horas
    for h in range(int(t0), int(t1) + 1):
        y = HEADER + int((h - t0) * PX_H)
        d.line([(ML, y), (W - MR, y)], fill=SURFACE3, width=1)
        d.text((ML - 6, y), f"{h:02d}:00",
               font=_font(False, 10), fill=DIM, anchor='rm')
        yh = HEADER + int((h + 0.5 - t0) * PX_H)
        if yh < HEADER + int((t1 - t0) * PX_H):
            d.line([(ML, yh), (W - MR, yh)], fill=SURFACE2, width=1)

    # Columnes
    for i, day in enumerate(days):
        x0_col = ML + i * COL_W
        is_today = (day == hoy)

        if i > 0:
            d.line([(x0_col, HEADER), (x0_col, H - FOOTER)], fill=BORDER, width=1)

        # Fons suau per al dia d'avui
        if is_today:
            d.rectangle([x0_col, HEADER, x0_col + COL_W - 1, H - FOOTER],
                        fill=_mix(ACCENT, BG, 0.04))

        cx       = x0_col + COL_W // 2
        dia_s    = DIAS_ABR[day.weekday()]
        fecha_s  = day.strftime('%-d/%m')
        dia_color = ACCENT if is_today else TEXT
        d.text((cx, HEADER - 32), dia_s,
               font=_font(True, 12), fill=dia_color, anchor='mm')
        d.text((cx, HEADER - 16), fecha_s,
               font=_font(False, 10), fill=MUTED if not is_today else ACCENT, anchor='mm')

        # Línia "ara" (vista setmanal)
        if is_today:
            now   = datetime.now()
            t_now = now.hour + now.minute / 60
            if t0 <= t_now <= t1:
                y_now = HEADER + int((t_now - t0) * PX_H)
                d.line([(x0_col, y_now), (x0_col + COL_W, y_now)], fill=NOW_LINE, width=2)

        # Events (amb carrils)
        evs_dia = sorted(por_dia.get(day, []), key=lambda e: e['inicio'])
        col_pad  = 4
        col_area = COL_W - col_pad * 2
        lanes    = _assign_lanes(evs_dia)
        for ev, col_idx, n_cols in lanes:
            y0_ev = HEADER + int((_t2f(ev['inicio']) - t0) * PX_H) + 2
            y1_ev = HEADER + int((_t2f(ev['fin'])    - t0) * PX_H) - 2
            lw    = col_area // n_cols
            gap   = 2 if n_cols > 1 else 0
            ex0   = x0_col + col_pad + col_idx * lw + gap
            ex1   = x0_col + col_pad + (col_idx + 1) * lw - gap - 1
            ns    = max(10, 13 - (n_cols - 1) * 2)
            ds    = max(8,  10 - (n_cols - 1))
            color = _color(ev['nombre'])
            _draw_event(d, ex0, y0_ev, ex1, y1_ev, color,
                        ev['nombre'], ev['tipo'], ev['inicio'], ev['fin'],
                        ev['aula'], ev.get('grup', ''),
                        name_size=ns, detail_size=ds)

    # Footer
    _draw_footer(d, W, ML, MR, H, FOOTER, n_cls)

    return _bio(img)


# ── Helpers de dibuix ──────────────────────────────────────────────────────────
def draw_circle(draw, cx, cy, r, fill):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def _draw_header_dia(draw, W, HEADER, fecha: date, dia: str, n_cls: int, es_avui: bool):
    ML, MR = 72, 20

    # Fons de capçalera lleugerament diferent
    draw.rectangle([0, 0, W, HEADER - 1], fill=SURFACE)

    # Dia de la setmana — gran, a l'esquerra
    x_left = ML + 4
    draw.text((x_left, HEADER // 2 - 2), dia,
              font=_font(True, 26), fill=TEXT, anchor='lm')

    # Data — dreta
    fecha_str = fecha.strftime('%-d de %B de %Y')
    draw.text((W - MR - 4, HEADER // 2 - 8), fecha_str,
              font=_font(False, 13), fill=MUTED, anchor='rm')

    # Nombre de classes — dreta, sota la data
    if n_cls > 0:
        draw.text((W - MR - 4, HEADER // 2 + 10),
                  f"{n_cls} classe{'s' if n_cls != 1 else ''}",
                  font=_font(False, 11), fill=DIM, anchor='rm')

    # Badge "AVUI" si és avui
    if es_avui:
        badge_txt = "AVUI"
        bf        = _font(True, 11)
        bw        = int(bf.getlength(badge_txt)) + 14
        bh        = 20
        bx0       = x_left + int(_font(True, 26).getlength(dia)) + 14
        bx1       = bx0 + bw
        by0       = HEADER // 2 - bh // 2 - 2
        by1       = by0 + bh
        _rounded_rect(draw, (bx0, by0, bx1, by1), 4, fill=ACCENT)
        draw.text(((bx0 + bx1) // 2, (by0 + by1) // 2), badge_txt,
                  font=bf, fill=BG, anchor='mm')

    # Línia separadora amb gradient (simulat)
    draw.line([(ML, HEADER - 1), (W - MR, HEADER - 1)], fill=BORDER, width=1)


def _draw_footer(draw, W, ML, MR, H, FOOTER, n_cls):
    draw.line([(ML, H - FOOTER), (W - MR, H - FOOTER)], fill=BORDER, width=1)
    txt = f"gestioacademica.upf.edu  ·  T3 2026"
    draw.text((W // 2, H - FOOTER // 2), txt,
              font=_font(False, 11), fill=MUTED, anchor='mm')
