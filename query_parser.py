"""
Parser de consultas de horario — responde sin llamar a la API de Claude.

Cubre el ~85% de las consultas reales:
  - fecha relativa: hoy, mañana, esta semana, el jueves...
  - asignatura: por nombre exacto, parcial o con typos
  - combinación: "Seguretat esta semana"

Devuelve None cuando la pregunta es ambigua o conversacional → va a Claude.
"""
import re
import unicodedata
from datetime import date, timedelta
from collections import defaultdict


# ── Normalización ──────────────────────────────────────────────────────────────
def _norm(text: str) -> str:
    """Minúsculas + eliminar acentos + quitar puntuación."""
    text = text.lower()
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    text = re.sub(r'[^\w\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


# ── Detección de fechas ────────────────────────────────────────────────────────
_DIAS = {
    'lunes': 0, 'dilluns': 0, 'monday': 0,
    'martes': 1, 'dimarts': 1, 'tuesday': 1,
    'miercoles': 2, 'dimecres': 2, 'wednesday': 2,
    'jueves': 3, 'dijous': 3, 'thursday': 3,
    'viernes': 4, 'divendres': 4, 'friday': 4,
}

_DIAS_ES = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']


def _detectar_rango(tnorm: str, hoy: date) -> tuple[date, date] | None:
    """Detecta referencia temporal y retorna (desde, hasta). None si no hay."""

    if any(w in tnorm for w in ['hoy', 'avui', 'today', 'hui']):
        return (hoy, hoy)

    if any(w in tnorm for w in ['manana', 'dema', 'tomorrow']):
        m = hoy + timedelta(1)
        return (m, m)

    if any(p in tnorm for p in ['esta semana', 'aquesta setmana', 'this week',
                                  'la semana', 'la setmana']):
        # desde hoy hasta el viernes de esta semana
        viernes = hoy + timedelta(4 - hoy.weekday())
        return (hoy, viernes if viernes >= hoy else hoy)

    if any(p in tnorm for p in ['proxima semana', 'setmana que ve', 'next week',
                                  'semana que viene', 'semana siguiente']):
        prox_lun = hoy - timedelta(hoy.weekday()) + timedelta(7)
        return (prox_lun, prox_lun + timedelta(4))

    # Día de la semana específico ("el jueves", "dijous")
    for nombre, num in _DIAS.items():
        if re.search(r'\b' + nombre + r'\b', tnorm):
            diff = (num - hoy.weekday()) % 7
            target = hoy + timedelta(diff if diff > 0 else 7)
            return (target, target)

    return None


# ── Detección de asignatura (fuzzy por tokens) ─────────────────────────────────
def _token_match(query_tok: str, subject_tok: str) -> bool:
    """
    True si los tokens son suficientemente similares:
      - igualdad exacta (tras normalizar acentos)
      - prefijo: uno contiene al otro al inicio (mínimo 5 chars cada uno, ≥60% del largo)
      - typo: misma longitud ±1 con ≥80% de caracteres coincidentes en posición
        (cubre "seguratat" → "seguretat", "aprenentatge" → "aprenentatge", etc.)
    """
    if query_tok == subject_tok:
        return True

    lq, ls = len(query_tok), len(subject_tok)

    # Prefijo (ambos tokens ≥5 chars para evitar falsos positivos con palabras cortas)
    largo = max(lq, ls)
    corto = min(lq, ls)
    if corto >= 5 and corto / largo >= 0.6:
        if subject_tok.startswith(query_tok) or query_tok.startswith(subject_tok):
            return True

    # Typo de 1 carácter (misma longitud o diferencia de 1, ≥80% posiciones iguales)
    if abs(lq - ls) <= 1 and min(lq, ls) >= 6:
        coincidencias = sum(a == b for a, b in zip(query_tok, subject_tok))
        if coincidencias / max(lq, ls) >= 0.80:
            return True

    return False


def _detectar_asignatura(tnorm: str, nombres: list[str]) -> list[str]:
    """
    Busca la asignatura más probable en la consulta.
    Retorna [] (ninguna), [nombre] (encontrada) o [n1, n2] (ambigua → Claude).
    """
    q_tokens = [t for t in tnorm.split() if len(t) >= 5]  # mín 5 chars evita falsos positivos
    if not q_tokens:
        return []

    scored: list[tuple[str, int]] = []
    for nombre in nombres:
        s_tokens = _norm(nombre).split()
        hits = sum(
            1 for qt in q_tokens
            if any(_token_match(qt, st) for st in s_tokens)
        )
        if hits > 0:
            scored.append((nombre, hits))

    if not scored:
        return []

    scored.sort(key=lambda x: -x[1])
    best = scored[0][1]

    # Devolver todas las que empatan en el mejor score
    return [s[0] for s in scored if s[1] == best]


# ── Detección de saludos ───────────────────────────────────────────────────────
_SALUDOS = {'hola', 'buenas', 'buenos', 'hey', 'hi', 'hello', 'bon', 'bona',
            'hablame', 'hola!', 'buenas!', 'com vas', 'que tal', 'ola'}

def _es_saludo(tnorm: str) -> bool:
    tokens = set(tnorm.split())
    return bool(tokens & _SALUDOS) and len(tokens) <= 4


# ── Formateo de respuestas ─────────────────────────────────────────────────────
def _fmt_evento(ev: dict) -> str:
    return f"  {ev['inicio']}–{ev['fin']} · {ev['tipo']} · Grups {ev['grup']} · 🏫 {ev['aula']}"


def _fmt_periodo(eventos: list, desde: date, hasta: date) -> str:
    """Formatea eventos de un rango de fechas agrupados por día."""
    from datetime import datetime as dt
    por_fecha: dict = defaultdict(list)
    for ev in eventos:
        por_fecha[ev['fecha']].append(ev)

    if not por_fecha:
        label = "hoy" if desde == hasta else f"{desde:%d/%m}–{hasta:%d/%m}"
        return f"No hay clases registradas para {label}."

    lines = []
    for fecha in sorted(por_fecha):
        day_evs = sorted(por_fecha[fecha], key=lambda x: x['inicio'])
        d = dt.strptime(fecha, '%Y-%m-%d')
        dia = _DIAS_ES[d.weekday()]
        lines.append(f"\n<b>{dia} {d.strftime('%d/%m')}:</b>")
        prev_asig = None
        for ev in day_evs:
            if ev['nombre'] != prev_asig:
                lines.append(f"  📚 {ev['nombre']}")
                prev_asig = ev['nombre']
            lines.append(f"    {ev['inicio']}–{ev['fin']} · {ev['tipo']} · Grups {ev['grup']} · 🏫 {ev['aula']}")

    return "\n".join(lines).strip()


def _fmt_asignatura(eventos: list, nombre: str) -> str:
    """Formatea todos los eventos de una asignatura."""
    from datetime import datetime as dt
    evs = sorted(eventos, key=lambda x: (x['fecha'], x['inicio']))
    if not evs:
        return f"No hay clases de <b>{nombre}</b> en las próximas semanas."

    lines = [f"📚 <b>{nombre}</b>\n"]
    for ev in evs:
        d = dt.strptime(ev['fecha'], '%Y-%m-%d')
        dia = _DIAS_ES[d.weekday()]
        lines.append(f"{dia} {d.strftime('%d/%m')} · {ev['inicio']}–{ev['fin']} · {ev['tipo']} · Grups {ev['grup']} · 🏫 {ev['aula']}")

    return "\n".join(lines)


# ── Función principal ──────────────────────────────────────────────────────────
def eventos_para_imagen(texto: str, datos: dict, hoy: date):
    """
    Si la consulta es resoluble localmente y tiene sentido visual,
    retorna (eventos, desde, hasta) para renderizar una imagen.
    Retorna None si la consulta es ambigua o conversacional.
    """
    from datetime import datetime as dt

    tnorm = _norm(texto)
    eventos_todos = datos.get('eventos', [])
    if not eventos_todos:
        return None

    rango   = _detectar_rango(tnorm, hoy)
    nombres = list({ev['nombre'] for ev in eventos_todos})
    asigs   = _detectar_asignatura(tnorm, nombres)

    if len(asigs) > 2:
        return None

    if rango and not asigs:
        desde, hasta = rango
        evs = [ev for ev in eventos_todos
               if desde <= dt.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta]
        return (sorted(evs, key=lambda x: (x['fecha'], x['inicio'])), desde, hasta)

    if asigs and rango:
        nombre = asigs[0]
        desde, hasta = rango
        evs = [ev for ev in eventos_todos
               if ev['nombre'] == nombre
               and desde <= dt.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta]
        return (sorted(evs, key=lambda x: (x['fecha'], x['inicio'])), desde, hasta)

    return None  # subject-only queries → text is clearer


def respuesta_local(texto: str, datos: dict, hoy: date) -> str | None:
    """
    Intenta responder la consulta con reglas.
    Retorna la respuesta (str) o None si hay que llamar a Claude.
    """
    from datetime import datetime as dt

    tnorm = _norm(texto)
    eventos_todos = datos.get('eventos', [])

    # ── Saludo simple ──────────────────────────────────────────────────────────
    if _es_saludo(tnorm):
        return (
            "👋 ¡Hola! Puedo decirte con fecha y aula exactas:\n\n"
            "• <i>¿Qué clases tengo hoy?</i>\n"
            "• <i>¿Cuándo es Xarxes de Sensors?</i>\n"
            "• <i>¿Qué hay esta semana?</i>\n\n"
            "Pregúntame lo que quieras 🎓"
        )

    # ── Detectar intención ─────────────────────────────────────────────────────
    rango   = _detectar_rango(tnorm, hoy)
    nombres = list({ev['nombre'] for ev in eventos_todos})
    asigs   = _detectar_asignatura(tnorm, nombres)

    # Asignatura ambigua → Claude para desambiguar
    if len(asigs) > 2:
        return None

    # ── Caso 1: fecha sin asignatura ("qué tengo hoy") ─────────────────────────
    if rango and not asigs:
        desde, hasta = rango
        evs = [
            ev for ev in eventos_todos
            if desde <= dt.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta
        ]
        evs.sort(key=lambda x: (x['fecha'], x['inicio'], x['nombre']))
        label = (
            "hoy" if desde == hasta == hoy
            else ("mañana" if desde == hasta == hoy + timedelta(1)
                  else f"del {desde:%d/%m} al {hasta:%d/%m}")
        )
        header = f"📅 Clases {label}:\n"
        body = _fmt_periodo(evs, desde, hasta)
        return header + body

    # ── Caso 2: asignatura sin fecha ("cuándo es Xarxes") ─────────────────────
    if asigs and not rango:
        nombre = asigs[0]
        # Mostrar próximas 4 semanas para esa asignatura
        hasta  = hoy + timedelta(weeks=4)
        evs = [
            ev for ev in eventos_todos
            if ev['nombre'] == nombre
            and hoy <= dt.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta
        ]
        return _fmt_asignatura(evs, nombre)

    # ── Caso 3: asignatura + fecha ("Seguretat esta semana") ──────────────────
    if asigs and rango:
        nombre = asigs[0]
        desde, hasta = rango
        evs = [
            ev for ev in eventos_todos
            if ev['nombre'] == nombre
            and desde <= dt.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta
        ]
        return _fmt_asignatura(evs, nombre)

    # ── No detectado → Claude ──────────────────────────────────────────────────
    return None
