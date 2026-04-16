#!/usr/bin/env python3
"""
Bot de Horarios UPF — 4 Enginyeries
Telegram + Claude Haiku con prompt caching + onboarding con botones
"""
import requests
import anthropic
import json
import os
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

_TZ = ZoneInfo("Europe/Madrid")

def _now() -> datetime:
    return datetime.now(_TZ)

def _today() -> date:
    return _now().date()

# ── Configuración ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ADMIN_CHAT_IDS    = set(os.environ.get("ADMIN_CHAT_IDS", "").split(","))

HORARIOS_FILE   = "horarios.json"
CURRICULUM_FILE = "curriculum.json"
PROFILES_FILE   = "user_profiles.json"
LOG_FILE        = "upf-bot.log"
OFFSET_FILE     = "telegram_offset.txt"
VENTANA_DIAS    = 14   # días hacia adelante que se pasan a handlers y Claude

# Estudios de cada titulación (old plan + new plan)
# Teclado persistente mostrado al usuario
REPLY_KB = {
    "keyboard": [
        [{"text": "📅 Avui"},    {"text": "📆 Aquesta setmana"}],
        [{"text": "📖 Guia"},    {"text": "⚙️ El meu perfil"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

DEGREE_ESTUDIS = {
    "informatica":  {3377, 3701},
    "dades":        {3370, 3700},
    "audiovisuals": {3375, 3704},
    "xarxes":       {3379, 3703},
}
DEGREE_LABELS = {
    "informatica":  "💻 Enginyeria en Informàtica",
    "dades":        "📊 Enginyeria Matemàtica en Ciència de Dades",
    "audiovisuals": "🎬 Enginyeria en Sistemes Audiovisuals",
    "xarxes":       "📡 Enginyeria de Xarxes de Telecomunicació",
}

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Estado en memoria para onboarding activo {str(chat_id): {step, cursos, grups}}
onboard_estado = {}

# Asignaturas mostradas en el menú rápido {str(chat_id): [nombre, ...]}
_qk_subjects: dict = {}

# ── Rate limiting ──────────────────────────────────────────────────────────────
# Máximo RATE_MAX_CALLS llamadas a Claude por usuario en una ventana de RATE_WINDOW segundos
RATE_MAX_CALLS = 10
RATE_WINDOW    = 60   # segundos
_rate_log: dict = {}  # {chat_id: [timestamps]}

def _rate_ok(chat_id: int) -> bool:
    """Devuelve True si el usuario puede hacer otra llamada a Claude."""
    now  = time.time()
    key  = str(chat_id)
    hist = _rate_log.get(key, [])
    hist = [t for t in hist if now - t < RATE_WINDOW]
    if len(hist) >= RATE_MAX_CALLS:
        _rate_log[key] = hist
        return False
    hist.append(now)
    _rate_log[key] = hist
    return True

# ── Curriculum ─────────────────────────────────────────────────────────────────
_CURRICULUM: dict | None = None

def _curriculum() -> dict:
    global _CURRICULUM
    if _CURRICULUM is None:
        with open("curriculum.json", "r", encoding="utf-8") as f:
            _CURRICULUM = json.load(f)
    return _CURRICULUM

def _reset_curriculum_cache():
    global _CURRICULUM
    _CURRICULUM = None


# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg):
    ts    = _now().strftime("%d/%m %H:%M:%S")
    linea = f"[{ts}] {msg}"
    print(linea)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(linea + "\n")


# ── Datos: leer / guardar (cache en memoria) ───────────────────────────────────
_DATOS_CACHE: dict | None = None

def cargar_datos() -> dict:
    global _DATOS_CACHE
    if _DATOS_CACHE is None:
        with open(HORARIOS_FILE, "r", encoding="utf-8") as f:
            _DATOS_CACHE = json.load(f)
    return _DATOS_CACHE

def guardar_datos(datos: dict, fuente: str = "manual"):
    global _DATOS_CACHE
    datos["ultima_actualizacion"] = _now().strftime("%Y-%m-%dT%H:%M:%S")
    datos["fuente"] = fuente
    with open(HORARIOS_FILE, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    _DATOS_CACHE = datos
    log(f"💾 Datos guardados ({datos.get('total_eventos', '?')} eventos, fuente: {fuente})")

def contar_eventos(datos: dict) -> int:
    return len(datos.get("eventos", []))


# ── Perfiles de usuario ────────────────────────────────────────────────────────
def cargar_perfiles() -> dict:
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def guardar_perfil(chat_id: str, perfil: dict):
    perfiles = cargar_perfiles()
    perfiles[chat_id] = perfil
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(perfiles, f, ensure_ascii=False, indent=2)

def obtener_perfil(chat_id: str) -> dict | None:
    return cargar_perfiles().get(str(chat_id))

def perfil_resumen(perfil: dict) -> str:
    mode = perfil.get("mode", "carrera")
    if mode == "manual":
        asigs = perfil.get("asignaturas", [])
        if not asigs:
            return "✏️ Mode manual (sense assignatures)"
        shown = ", ".join(f"{a['nombre']} (G{a['grup_teoria']})" for a in asigs[:3])
        if len(asigs) > 3:
            shown += f" +{len(asigs) - 3} més"
        return f"✏️ Manual · {shown}"
    deg_label  = DEGREE_LABELS.get(perfil.get("titulacio", ""), "?")
    curs       = perfil.get("curs")
    grup       = perfil.get("grup_teoria", "?")
    curs_label = f"{curs}r" if curs == 1 else (f"{curs}n" if curs else "?")
    return f"{deg_label} · {curs_label} curs · Grup {grup}"


# ── Filtrado por perfil ────────────────────────────────────────────────────────
def _grup_ok_simple(ev_grup: str, grup_teoria: str | None) -> bool:
    """
    True si el evento pertenece al grupo del usuario.
    Grup teoria '1' cubre teoría '1' y labs '101', '102', etc.
    Grupos fusionados '101+102' → basta con que una parte encaje.
    """
    if not grup_teoria:
        return True
    for parte in ev_grup.split('+'):
        if parte == grup_teoria:
            return True
        if len(parte) > len(grup_teoria) and parte.startswith(grup_teoria):
            return True
    return False

def filtrar_por_perfil(datos: dict, perfil: dict) -> dict:
    mode = perfil.get("mode", "carrera")

    if mode == "manual":
        # Cada asignatura tiene su propio grup_teoria
        asig_map = {a["nombre"]: a["grup_teoria"] for a in perfil.get("asignaturas", [])}
        evs = [
            ev for ev in datos.get("eventos", [])
            if ev["nombre"] in asig_map
            and _grup_ok_simple(ev["grup"], asig_map[ev["nombre"]])
        ]
        return {**datos, "eventos": evs}

    # Modo carrera: asignaturas definidas en curriculum.json para titulació+any
    # (NO filtra por ev["curso"] — el año viene del currículum, no de la DB)
    titulacio   = perfil.get("titulacio")
    curs        = perfil.get("curs")       # int: 1 o 2, o None
    grup_teoria = perfil.get("grup_teoria")

    per_any = _curriculum()["per_any"].get(titulacio, {})

    if curs is not None:
        mi_subjects = set(per_any.get(str(curs), []))
    else:
        # Perfil sin año configurado → mostrar todas las asignaturas de la carrera
        mi_subjects = {s for lst in per_any.values() for s in lst}

    evs = [
        ev for ev in datos.get("eventos", [])
        if ev["nombre"] in mi_subjects
        and _grup_ok_simple(ev["grup"], grup_teoria)
    ]
    return {**datos, "eventos": evs}


def filtrar_ventana(datos: dict, dias: int = VENTANA_DIAS) -> dict:
    """Recorta los eventos al rango [hoy, hoy+dias]. Reduce drásticamente el volumen de datos."""
    hoy   = _today()
    hasta = hoy + timedelta(days=dias)
    evs   = [
        ev for ev in datos.get("eventos", [])
        if hoy <= datetime.strptime(ev["fecha"], "%Y-%m-%d").date() <= hasta
    ]
    return {**datos, "eventos": evs}


# ── Auto-scraper al arrancar ───────────────────────────────────────────────────
def intentar_actualizar_auto():
    try:
        from scraper import scrape_todos_los_cursos
        datos_actuales = cargar_datos()
        if datos_actuales.get("fuente", "template") == "template":
            log("🔄 Intentando auto-scrape de UPF...")
            horarios, curriculum = scrape_todos_los_cursos(log_fn=log)
            if horarios:
                guardar_datos(horarios, fuente="gestioacademica.upf.edu")
                with open(CURRICULUM_FILE, "w", encoding="utf-8") as f:
                    json.dump(curriculum, f, ensure_ascii=False, indent=2)
                _reset_curriculum_cache()
                log(f"✅ Auto-scrape OK: {contar_eventos(horarios)} eventos")
        else:
            log(f"   Datos cargados desde {datos_actuales.get('ultima_actualizacion', '?')}")
    except Exception as e:
        log(f"⚠️  Error en auto-scrape: {e}")


# ── Formateo para el prompt de Claude ─────────────────────────────────────────
def filtrar_eventos(eventos: list, desde: date, hasta: date) -> list:
    result = []
    for ev in eventos:
        try:
            fecha_ev = datetime.strptime(ev['fecha'], '%Y-%m-%d').date()
        except (ValueError, KeyError):
            continue
        if desde <= fecha_ev <= hasta:
            result.append(ev)
    return result

def _eventos_a_texto(eventos: list) -> str:
    if not eventos:
        return "(Sin clases en este período)"
    por_fecha: dict = defaultdict(list)
    for ev in eventos:
        por_fecha[ev['fecha']].append(ev)
    lines = []
    for fecha in sorted(por_fecha):
        day_evs = sorted(por_fecha[fecha], key=lambda x: (x['inicio'], x['nombre'], x['grup']))
        d   = datetime.strptime(fecha, '%Y-%m-%d')
        dia = day_evs[0]['dia']
        lines.append(f"\n{dia} {d.strftime('%d/%m')}:")
        for ev in day_evs:
            lines.append(
                f"  {ev['inicio']}–{ev['fin']} · {ev['nombre']} · {ev['tipo']} · Grup {ev['grup']} · 🏫 {ev['aula']}"
            )
    return "\n".join(lines)


# ── System prompt ──────────────────────────────────────────────────────────────
def construir_system_prompt(datos: dict) -> str:
    hoy    = _now()
    hoy_d  = hoy.date()
    dia_es = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"][hoy.weekday()]
    ultima = datos.get("ultima_actualizacion", "?")
    hasta_d = hoy_d + timedelta(days=VENTANA_DIAS)
    # datos ya llega pre-filtrado por filtrar_ventana — no hace falta re-filtrar
    eventos_ventana = datos.get("eventos", [])

    return f"""Ets UPF-Bot, assistent d'horaris de les Enginyeries de la UPF.
Respons sempre en català. No tradueixis els noms de les assignatures.

AVUI: {hoy.strftime('%A')} {hoy.strftime('%d/%m/%Y')} ({dia_es})
TRIMESTRE: T3 2026 · Dades: {ultima}

════════════════════════════════════════
CLASSES PRÒXIMS {VENTANA_DIAS} DIES · fins {hasta_d:%d/%m}
Font: gestioacademica.upf.edu — dates i aules exactes
Quan el grup és "101+102" significa que ambdós grups comparteixen la mateixa aula.
════════════════════════════════════════
{_eventos_a_texto(eventos_ventana)}
════════════════════════════════════════

REGLES:
1. Les dates són EXACTES. Usa-les directament sense recalcular.
2. Per "avui/demà/aquesta setmana": filtra per data de la llista.
3. Per "quan és X": mostra tots els seus events del període.
4. Format: "Dijous 16/04 · 12:30–14:30 · Seguretat · Teoria · Grups 1 · 🏫 52.221"
5. Si pregunten per dates fora del període → redirigeix a gestioacademica.upf.edu."""


def preguntar_a_claude(pregunta: str, datos: dict) -> str:
    system_prompt = construir_system_prompt(datos)
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": pregunta}]
        )
        return resp.content[0].text
    except Exception as e:
        log(f"⚠️ Claude error: {e}")
        return "❌ Error al consultar el asistente. Inténtalo de nuevo."


# ── Telegram helpers ───────────────────────────────────────────────────────────
API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def telegram_get(endpoint, params=None):
    try:
        r = requests.get(f"{API}/{endpoint}", params=params, timeout=35)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log(f"⚠️ Telegram GET {endpoint}: {e}")
        return None

def telegram_send(chat_id, texto):
    MAX = 4000
    chunks = [texto[i:i+MAX] for i in range(0, len(texto), MAX)]
    ok = True
    for chunk in chunks:
        try:
            r = requests.post(f"{API}/sendMessage", data={
                "chat_id": chat_id, "text": chunk, "parse_mode": "HTML"
            }, timeout=10)
            if r.status_code != 200:
                log(f"⚠️ Telegram send error {r.status_code}: {r.text[:100]}")
                ok = False
        except Exception as e:
            log(f"⚠️ Telegram send: {e}")
            ok = False
    return ok

def telegram_send_keyboard(chat_id, texto, keyboard) -> int | None:
    """Envía un mensaje con inline keyboard. Retorna el message_id."""
    try:
        r = requests.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }, timeout=10)
        if r.status_code == 200:
            return r.json()["result"]["message_id"]
        log(f"⚠️ send_keyboard error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"⚠️ send_keyboard: {e}")
    return None

def telegram_edit_message(chat_id, msg_id, texto, keyboard):
    """Edita texto + teclado de un mensaje existente."""
    try:
        requests.post(f"{API}/editMessageText", json={
            "chat_id": chat_id,
            "message_id": msg_id,
            "text": texto,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }, timeout=10)
    except Exception as e:
        log(f"⚠️ edit_message: {e}")

def telegram_answer_callback(callback_id, text=""):
    try:
        requests.post(f"{API}/answerCallbackQuery", json={
            "callback_query_id": callback_id, "text": text
        }, timeout=5)
    except Exception:
        pass

def telegram_send_menu(chat_id, texto):
    """Envía un mensaje con el teclado persistente de acceso rápido."""
    try:
        import json as _json
        r = requests.post(f"{API}/sendMessage", json={
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "reply_markup": REPLY_KB,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log(f"⚠️ send_menu: {e}")
        return False

def telegram_send_photo(chat_id, bio):
    """Envía una imagen PNG (BytesIO) como foto con el teclado persistente."""
    try:
        r = requests.post(f"{API}/sendPhoto",
                          data={"chat_id": chat_id,
                                "reply_markup": json.dumps(REPLY_KB)},
                          files={"photo": ("horario.png", bio, "image/png")},
                          timeout=20)
        if r.status_code != 200:
            log(f"⚠️ send_photo error {r.status_code}: {r.text[:100]}")
        return r.status_code == 200
    except Exception as e:
        log(f"⚠️ send_photo: {e}")
        return False

def telegram_typing(chat_id):
    try:
        requests.post(f"{API}/sendChatAction",
                      data={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass

def cargar_offset():
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE) as f:
            try: return int(f.read().strip())
            except: return 0
    return 0

def guardar_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


# ── Onboarding — teclados ──────────────────────────────────────────────────────
def _kb_degrees() -> dict:
    return {"inline_keyboard": [
        [{"text": "💻 Informàtica",                    "callback_data": "ob_d_informatica"}],
        [{"text": "📊 Ciència de Dades",               "callback_data": "ob_d_dades"}],
        [{"text": "🎬 Audiovisuals",                    "callback_data": "ob_d_audiovisuals"}],
        [{"text": "📡 Xarxes Telecom",                 "callback_data": "ob_d_xarxes"}],
        [{"text": "✏️ Afegir assignatures manualment", "callback_data": "ob_d_manual"}],
    ]}

def _kb_curs() -> dict:
    return {"inline_keyboard": [
        [{"text": "1r curs", "callback_data": "ob_yr_1"},
         {"text": "2n curs", "callback_data": "ob_yr_2"}],
        [{"text": "3r, 4t o optatives", "callback_data": "ob_yr_opt"}],
    ]}

def _kb_grup_teoria() -> dict:
    return {"inline_keyboard": [[
        {"text": "Grup 1", "callback_data": "ob_gt_1"},
        {"text": "Grup 2", "callback_data": "ob_gt_2"},
        {"text": "Grup 3", "callback_data": "ob_gt_3"},
    ]]}

def _kb_manual_grup() -> dict:
    return {"inline_keyboard": [
        [{"text": "Grup 1", "callback_data": "ob_mg_1"},
         {"text": "Grup 2", "callback_data": "ob_mg_2"},
         {"text": "Grup 3", "callback_data": "ob_mg_3"}],
        [{"text": "❌ No és aquesta assignatura", "callback_data": "ob_mg_no"}],
    ]}

def _kb_quick_menu(subjects: list) -> dict:
    """Menú inline de accions ràpides + asignaturas del usuario."""
    rows = [
        [{"text": "📅 El meu horari avui",          "callback_data": "qk_hoy"},
         {"text": "📅 Demà",                         "callback_data": "qk_manana"}],
        [{"text": "📆 El meu horari aquesta setmana","callback_data": "qk_semana"}],
    ]
    rows.append([{"text": "🔍 Buscar assignatura", "callback_data": "qk_buscar"}])
    rows.append([{"text": "⚙️ Editar perfil",      "callback_data": "qk_perfil"}])
    return {"inline_keyboard": rows}


def _carrera_to_manual(perfil: dict) -> dict:
    """Converteix un perfil carrera a manual mantenint les assignatures actuals."""
    titulacio = perfil.get("titulacio")
    curs      = perfil.get("curs")
    grup      = perfil.get("grup_teoria", "1")
    per_any   = _curriculum()["per_any"].get(titulacio, {})
    subjects  = per_any.get(str(curs), []) if curs else []
    return {"mode": "manual",
            "asignaturas": [{"nombre": s, "grup_teoria": grup} for s in subjects]}

def _perfil_asignaturas(perfil: dict) -> list:
    """Retorna la llista efectiva d'assignatures del perfil (carrera o manual)."""
    if perfil.get("mode") == "manual":
        return perfil.get("asignaturas", [])
    titulacio = perfil.get("titulacio")
    curs      = perfil.get("curs")
    grup      = perfil.get("grup_teoria", "1")
    per_any   = _curriculum()["per_any"].get(titulacio, {})
    subjects  = per_any.get(str(curs), []) if curs else []
    return [{"nombre": s, "grup_teoria": grup} for s in subjects]

def _dashboard_text_kb(perfil: dict) -> tuple:
    """Retorna (text, teclado) pel dashboard de gestió de perfil."""
    asigs = _perfil_asignaturas(perfil)
    mode  = perfil.get("mode", "carrera")
    if mode == "carrera":
        titulacio  = DEGREE_LABELS.get(perfil.get("titulacio", ""), "?")
        curs       = perfil.get("curs", "?")
        curs_label = f"{curs}r" if curs == 1 else f"{curs}n"
        mode_label = f"Mode automàtic · {titulacio} {curs_label} curs"
    else:
        mode_label = "Mode manual"

    if asigs:
        lines = "\n".join(f"  • {a['nombre']} (G{a['grup_teoria']})" for a in asigs)
    else:
        lines = "  Sense assignatures configurades."

    text = f"⚙️ <b>Les teves assignatures</b>\n<i>{mode_label}</i>\n\n{lines}"

    rows = []
    for i, a in enumerate(asigs):
        label = a['nombre'] if len(a['nombre']) <= 26 else a['nombre'][:24] + "…"
        rows.append([{"text": f"❌ {label}", "callback_data": f"gp_del_{i}"}])
    rows.append([{"text": "➕ Afegir assignatura",       "callback_data": "gp_add"}])
    rows.append([{"text": "🔄 Reiniciar perfil complet", "callback_data": "gp_reset"}])
    rows.append([{"text": "← Tornar",                   "callback_data": "gp_back"}])
    return text, {"inline_keyboard": rows}

def _kb_manual_more() -> dict:
    return {"inline_keyboard": [
        [{"text": "➕ Afegir altra assignatura", "callback_data": "ob_mc_add"}],
        [{"text": "💾 Guardar i acabar",         "callback_data": "ob_mc_save"}],
    ]}


# ── Onboarding — flujo ─────────────────────────────────────────────────────────
def iniciar_onboarding(chat_id):
    onboard_estado[str(chat_id)] = {
        "step": "degree",
        "titulacio": None,
        "curs": None,
        "asignaturas_temp": [],
        "asignatura_pendiente": None,
        "msg_id": None,
    }
    msg_id = telegram_send_keyboard(
        chat_id,
        "👋 <b>Benvingut a UPF-Bot!</b>\n\n<b>En quina enginyeria estàs?</b>",
        _kb_degrees()
    )
    if msg_id:
        onboard_estado[str(chat_id)]["msg_id"] = msg_id

def _finalizar_perfil(chat_id, msg_id, perfil, nombre, datos):
    """Guarda el perfil, edita el mensaje de onboarding y mostra el menú ràpid."""
    guardar_perfil(str(chat_id), perfil)
    onboard_estado.pop(str(chat_id), None)
    resumen = perfil_resumen(perfil)
    log(f"   Perfil guardat [{nombre}]: {resumen}")
    telegram_edit_message(chat_id, msg_id,
        f"✅ <b>Perfil guardat</b>\n{resumen}",
        {"inline_keyboard": []})
    telegram_send_menu(chat_id, "Llest! Utilitza els botons de sota o escriu-me directament 👇")
    datos_usuario = filtrar_ventana(filtrar_por_perfil(datos, perfil))
    _mostrar_menu_rapid(chat_id, datos_usuario)


def procesar_callback(cq, datos):
    chat_id = cq["from"]["id"]
    msg_id  = cq["message"]["message_id"]
    data    = cq["data"]
    cb_id   = cq["id"]
    nombre  = cq["from"].get("first_name", "Estudiant")

    telegram_answer_callback(cb_id)

    if data == "noop":
        return

    # ── Menú ràpid ────────────────────────────────────────────────────────────
    if data.startswith("qk_"):
        if data == "qk_hoy":
            procesar_mensaje(chat_id, "que tengo hoy", nombre, datos)
        elif data == "qk_manana":
            procesar_mensaje(chat_id, "que tengo manana", nombre, datos)
        elif data == "qk_semana":
            procesar_mensaje(chat_id, "que tengo esta semana", nombre, datos)
        elif data.startswith("qk_a_"):
            idx      = int(data.split("_")[-1])
            subjects = _qk_subjects.get(str(chat_id), [])
            if idx < len(subjects):
                procesar_mensaje(chat_id, subjects[idx], nombre, datos)
        elif data == "qk_buscar":
            onboard_estado[str(chat_id)] = {"step": "qk_search"}
            telegram_send(chat_id, "🔍 Escriu el nom de l'assignatura que vols buscar:")
        elif data == "qk_perfil":
            perfil = obtener_perfil(str(chat_id))
            if not perfil:
                iniciar_onboarding(chat_id)
            else:
                txt, kb = _dashboard_text_kb(perfil)
                telegram_send_keyboard(chat_id, txt, kb)
        return

    # ── Gestió de perfil (dashboard) ─────────────────────────────────────────
    if data.startswith("gp_"):
        perfil = obtener_perfil(str(chat_id))
        if not perfil:
            iniciar_onboarding(chat_id); return

        if data == "gp_reset":
            iniciar_onboarding(chat_id)
            return

        if data == "gp_back":
            datos_usuario = filtrar_ventana(filtrar_por_perfil(datos, perfil))
            _mostrar_menu_rapid(chat_id, datos_usuario)
            return

        if data == "gp_add":
            # Convertir a manual si cal (preservant assignatures actuals)
            if perfil.get("mode") != "manual":
                perfil = _carrera_to_manual(perfil)
                guardar_perfil(str(chat_id), perfil)
            onboard_estado[str(chat_id)] = {"step": "gp_add", "dashboard_msg": msg_id}
            telegram_edit_message(chat_id, msg_id,
                "➕ Escriu el nom de l'assignatura que vols afegir:",
                {"inline_keyboard": []})
            return

        if data.startswith("gp_del_"):
            idx = int(data.split("_")[-1])
            # Convertir a manual si cal
            if perfil.get("mode") != "manual":
                perfil = _carrera_to_manual(perfil)
            asigs = perfil.get("asignaturas", [])
            if idx < len(asigs):
                asigs.pop(idx)
            perfil["asignaturas"] = asigs
            guardar_perfil(str(chat_id), perfil)
            # Actualitzar el dashboard al mateix missatge
            txt, kb = _dashboard_text_kb(perfil)
            telegram_edit_message(chat_id, msg_id, txt, kb)
            return

        return

    estado = onboard_estado.get(str(chat_id))
    if not estado:
        return

    # ── Paso 1: selección de titulación ───────────────────────────────────────
    if data.startswith("ob_d_"):
        suffix = data[5:]
        if suffix == "manual":
            estado["step"] = "manual_subject"
            estado["asignaturas_temp"] = []
            telegram_edit_message(chat_id, msg_id,
                "✏️ <b>Afegir assignatures manualment</b>\n\n"
                "Escriu el nom d'una assignatura (p.ex. <i>Xarxes</i>, <i>Seguretat</i>):",
                {"inline_keyboard": []})
        else:
            estado["titulacio"] = suffix
            estado["step"] = "curs"
            deg_label = DEGREE_LABELS.get(suffix, suffix)
            telegram_edit_message(chat_id, msg_id,
                f"✅ {deg_label}\n\n<b>Quin curs fas?</b>",
                _kb_curs())

    # ── Paso 2 (modo carrera): selección de curso ─────────────────────────────
    elif data.startswith("ob_yr_"):
        suffix = data[6:]

        # Opció 3r/4t/optatives → flux manual
        if suffix == "opt":
            deg_label = DEGREE_LABELS.get(estado.get("titulacio", ""), "")
            estado["step"]              = "manual_subject"
            estado["asignaturas_temp"]  = []
            estado["asignatura_pendiente"] = None
            telegram_edit_message(chat_id, msg_id,
                f"✅ {deg_label} · 3r/4t o optatives\n\n"
                f"Afegeix les assignatures que estàs cursant aquest trimestre.\n\n"
                f"Escriu el nom de la primera assignatura:",
                {"inline_keyboard": []})
            return

        curs = int(suffix)
        estado["curs"] = curs
        estado["step"] = "grup"
        deg_label = DEGREE_LABELS.get(estado.get("titulacio", ""), "")
        curs_label = f"{curs}r" if curs == 1 else f"{curs}n"
        telegram_edit_message(chat_id, msg_id,
            f"✅ {deg_label} · {curs_label} curs\n\n<b>Quin grup de teoria tens?</b>",
            _kb_grup_teoria())

    # ── Paso 3 (modo carrera): grup de teoría ─────────────────────────────────
    elif data.startswith("ob_gt_"):
        grup = data[6:]
        titulacio = estado.get("titulacio")
        curs      = estado.get("curs")
        if not titulacio or not curs:
            return
        perfil = {"mode": "carrera", "titulacio": titulacio, "curs": curs, "grup_teoria": grup}
        _finalizar_perfil(chat_id, msg_id, perfil, nombre, datos)

    # ── Modo manual: grup para una asignatura ─────────────────────────────────
    elif data.startswith("ob_mg_"):
        suffix = data[6:]

        # Contexto: afegir assignatura des de gestió de perfil
        if estado and estado.get("step") == "gp_add_grup":
            dash_msg = estado.get("dashboard_msg")
            onboard_estado.pop(str(chat_id), None)
            if suffix == "no":
                onboard_estado[str(chat_id)] = {"step": "gp_add", "dashboard_msg": dash_msg}
                telegram_edit_message(chat_id, msg_id,
                    "➕ Escriu el nom de l'assignatura que vols afegir:", {"inline_keyboard": []})
            else:
                nom    = estado.get("nombre")
                perfil = obtener_perfil(str(chat_id)) or {"mode": "manual", "asignaturas": []}
                perfil["asignaturas"].append({"nombre": nom, "grup_teoria": suffix})
                guardar_perfil(str(chat_id), perfil)
                # Tancar el selector de grup i actualitzar el dashboard
                telegram_edit_message(chat_id, msg_id,
                    f"✅ <b>{nom}</b> afegida (Grup {suffix}).", {"inline_keyboard": []})
                if dash_msg:
                    txt, kb = _dashboard_text_kb(perfil)
                    telegram_edit_message(chat_id, dash_msg, txt, kb)
            return

        if suffix == "no":
            # No era la asignatura correcta — volver a pedir nombre
            estado["step"] = "manual_subject"
            estado["asignatura_pendiente"] = None
            n = len(estado["asignaturas_temp"])
            ctx = ""
            if n:
                names = "\n".join(f"• {a['nombre']}" for a in estado["asignaturas_temp"])
                ctx = f"Assignatures fins ara ({n}):\n{names}\n\n"
            telegram_edit_message(chat_id, msg_id,
                f"{ctx}Escriu el nom de l'assignatura:",
                {"inline_keyboard": []})
        else:
            grup = suffix
            asignatura = estado.get("asignatura_pendiente")
            if asignatura:
                estado["asignaturas_temp"].append({"nombre": asignatura, "grup_teoria": grup})
                estado["asignatura_pendiente"] = None
            estado["step"] = "manual_more"
            n = len(estado["asignaturas_temp"])
            names_str = "\n".join(
                f"• {a['nombre']} (Grup {a['grup_teoria']})"
                for a in estado["asignaturas_temp"]
            )
            telegram_edit_message(chat_id, msg_id,
                f"✅ <b>{n} assignatura{'s' if n != 1 else ''} afegida{'s' if n != 1 else ''}:</b>\n"
                f"{names_str}\n\nVols afegir-ne una altra?",
                _kb_manual_more())

    # ── Modo manual: continuar o guardar ──────────────────────────────────────
    elif data.startswith("ob_mc_"):
        suffix = data[6:]
        if suffix == "add":
            estado["step"] = "manual_subject"
            n = len(estado["asignaturas_temp"])
            names_str = "\n".join(f"• {a['nombre']}" for a in estado["asignaturas_temp"])
            telegram_edit_message(chat_id, msg_id,
                f"Assignatures fins ara ({n}):\n{names_str}\n\n"
                "Escriu el nom de la següent assignatura:",
                {"inline_keyboard": []})
        elif suffix == "save":
            asigs = estado["asignaturas_temp"]
            if not asigs:
                estado["step"] = "manual_subject"
                telegram_edit_message(chat_id, msg_id,
                    "⚠️ No has afegit cap assignatura. Escriu el nom d'una assignatura:",
                    {"inline_keyboard": []})
                return
            perfil = {"mode": "manual", "asignaturas": asigs}
            _finalizar_perfil(chat_id, msg_id, perfil, nombre, datos)


# ── Modo manual: input de nombre de asignatura ────────────────────────────────
def _handle_manual_subject_input(chat_id: int, texto: str, datos: dict):
    from query_parser import _norm, _detectar_asignatura

    estado = onboard_estado.get(str(chat_id))
    if not estado:
        return

    termino = texto.strip()
    if len(termino) < 3:
        telegram_send(chat_id, "Escriu almenys 3 caràcters per buscar l'assignatura.")
        return

    nombres_db = list({ev['nombre'] for ev in datos.get('eventos', [])})
    matches = _detectar_asignatura(_norm(termino), nombres_db)

    if not matches:
        telegram_send(chat_id,
            "❌ No he trobat cap assignatura amb aquest nom.\n"
            "Prova amb més caràcters o una altra paraula clau.")
        return

    matched = matches[0]

    # Evitar duplicados
    already = [a["nombre"] for a in estado.get("asignaturas_temp", [])]
    if matched in already:
        telegram_send(chat_id,
            f"⚠️ <b>{matched}</b> ja està a la llista.\n\nEscriu una altra assignatura:")
        return

    estado["asignatura_pendiente"] = matched
    estado["step"] = "manual_grup"

    new_msg_id = telegram_send_keyboard(
        chat_id,
        f"He trobat: <b>{matched}</b>\n\n<b>Quin grup de teoria tens per aquesta assignatura?</b>",
        _kb_manual_grup()
    )
    if new_msg_id:
        estado["msg_id"] = new_msg_id


# ── Mensajes fijos ─────────────────────────────────────────────────────────────
MSG_AYUDA = """📖 <b>UPF-Bot — Ordres</b>

/avui — Classes d'avui
/setmana — Vista de la setmana actual
/guia — Exemples de consultes
/perfil — Canviar el teu curs i grup
/estat — Info de les dades

També pots escriure directament en llenguatge natural."""

MSG_GUIA = """📖 <b>Com usar UPF-Bot</b>

<b>📅 Per data</b>
• <i>Què tinc avui</i>
• <i>Classes de demà</i>
• <i>Què hi ha aquesta setmana</i>
• <i>Horari de la setmana que ve</i>
• <i>Dijous què tinc</i>

<b>📚 Per assignatura</b>
• <i>Quan és Seguretat en Computadors</i>
• <i>Horari de Xarxes de Sensors</i>
• <i>Pròxima classe d'Aprenentatge Automàtic</i>

<b>🔀 Combinades</b>
• <i>Seguretat aquesta setmana</i>
• <i>Aprenentatge el dijous</i>
• <i>Xarxes demà</i>

<b>⚡ Ordres ràpides</b>
/avui · /setmana

Els noms d'assignatura poden tenir errors tipogràfics lleus — els entenc igualment."""

MSG_AYUDA_ADMIN = """📖 <b>UPF-Bot — Ordres Admin</b>

Normals: /start · /ajuda · /guia · /avui · /setmana · /perfil · /estat
Admin: /actualizar — Scraping UPF

🔑 Només disponibles per a tu"""


# ── Comandos admin ─────────────────────────────────────────────────────────────
def cmd_actualizar(chat_id):
    telegram_send(chat_id, "🔄 Scraping UPF gestioacademica... (~30s)")
    telegram_typing(chat_id)
    try:
        from scraper import scrape_todos_los_cursos
        msgs = []
        horarios, curriculum = scrape_todos_los_cursos(log_fn=lambda m: msgs.append(m))
        guardar_datos(horarios, fuente="gestioacademica.upf.edu")
        with open(CURRICULUM_FILE, "w", encoding="utf-8") as f:
            json.dump(curriculum, f, ensure_ascii=False, indent=2)
        _reset_curriculum_cache()
        n_ev   = horarios.get("total_eventos", 0)
        n_asig = horarios.get("total_asignaturas", 0)
        telegram_send(chat_id,
            f"✅ <b>Dades actualitzades des de la UPF</b>\n"
            f"📅 {n_ev} events · 📚 {n_asig} assignatures\n"
            f"🕐 {_now().strftime('%d/%m/%Y %H:%M')}\n\n"
            + "\n".join(msgs[-6:])
        )
    except Exception as e:
        telegram_send(chat_id, f"❌ <b>Error en el scraping</b>\n<code>{e}</code>")


def cmd_estado(chat_id):
    datos  = cargar_datos()
    ultima = datos.get("ultima_actualizacion", "nunca")
    fuente = datos.get("fuente", "desconocida")
    n_ev   = contar_eventos(datos)
    n_asig = datos.get("total_asignaturas", "?")
    t3_fin = datos.get("t3_fin", "?")
    telegram_send(chat_id,
        f"📊 <b>Estat de les dades</b>\n\n"
        f"🕐 Actualització: <code>{ultima}</code>\n"
        f"🔗 Font: <code>{fuente}</code>\n"
        f"📅 Events T3: <b>{n_ev}</b>\n"
        f"📚 Assignatures: <b>{n_asig}</b>\n"
        f"📆 T3 fins: {t3_fin}"
    )


# ── Menú ràpid ────────────────────────────────────────────────────────────────
def _mostrar_menu_rapid(chat_id, datos_usuario):
    subjects = sorted({ev['nombre'] for ev in datos_usuario.get('eventos', [])})
    _qk_subjects[str(chat_id)] = subjects
    telegram_send_keyboard(chat_id,
        "Què vols consultar?",
        _kb_quick_menu(subjects))
    log(f"   → menú ràpid ({len(subjects)} assignatures)")


def _handle_gp_add(chat_id, texto, datos, dashboard_msg=None):
    """Afegeix una assignatura al perfil manual (fuzzy match)."""
    from query_parser import _norm, _detectar_asignatura

    nombres = list({ev['nombre'] for ev in datos.get('eventos', [])})
    asigs   = _detectar_asignatura(_norm(texto), nombres)

    if not asigs:
        telegram_send(chat_id, f"No he trobat cap assignatura amb «{texto}». Torna-ho a intentar:")
        onboard_estado[str(chat_id)] = {"step": "gp_add", "dashboard_msg": dashboard_msg}
        return

    nombre = asigs[0]
    perfil = obtener_perfil(str(chat_id)) or {"mode": "manual", "asignaturas": []}

    if any(a["nombre"] == nombre for a in perfil.get("asignaturas", [])):
        telegram_send(chat_id, f"<b>{nombre}</b> ja està al teu perfil.")
        # Tornar al dashboard
        if dashboard_msg:
            txt, kb = _dashboard_text_kb(perfil)
            telegram_edit_message(chat_id, dashboard_msg, txt, kb)
        return

    onboard_estado[str(chat_id)] = {"step": "gp_add_grup", "nombre": nombre,
                                    "dashboard_msg": dashboard_msg}
    telegram_send_keyboard(chat_id,
        f"➕ <b>{nombre}</b>\nQuin és el teu grup de teoria?",
        _kb_manual_grup())


def _handle_qk_search(chat_id, texto, datos):
    """Busca una assignatura per nom (en tot el trimestre, no sols 14 dies)."""
    from query_parser import _norm, _detectar_asignatura
    from datetime import date as _date

    perfil = obtener_perfil(str(chat_id))
    datos_perfil = filtrar_por_perfil(datos, perfil) if perfil else datos
    nombres = list({ev['nombre'] for ev in datos_perfil.get('eventos', [])})
    asigs   = _detectar_asignatura(_norm(texto), nombres)

    if not asigs:
        telegram_send(chat_id, f"No he trobat cap assignatura amb «{texto}».")
        return

    nombre  = asigs[0]
    hoy_d   = _today()
    # Mostrar la setmana actual (fins al proper divendres) o com a màxim 7 dies
    dies_fins_dv = (4 - hoy_d.weekday()) % 7
    hasta_d = hoy_d + timedelta(days=dies_fins_dv if dies_fins_dv > 0 else 7)
    evs = sorted(
        [ev for ev in datos_perfil.get('eventos', [])
         if ev['nombre'] == nombre
         and hoy_d <= datetime.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta_d],
        key=lambda x: (x['fecha'], x['inicio'])
    )

    if not evs:
        telegram_send(chat_id, f"No hi ha classes de <b>{nombre}</b> aquesta setmana.")
        return

    try:
        from renderer import render_semana
        desde = datetime.strptime(evs[0]['fecha'],  '%Y-%m-%d').date()
        hasta = datetime.strptime(evs[-1]['fecha'], '%Y-%m-%d').date()
        if desde == hasta:
            from renderer import render_dia
            bio = render_dia(evs, desde)
        else:
            bio = render_semana(evs, desde, hasta)
        telegram_send_photo(chat_id, bio)
    except Exception as e:
        log(f"⚠️ qk_search render: {e}")
        telegram_send(chat_id, f"📚 <b>{nombre}</b>\n" +
            "\n".join(f"{ev['fecha']} {ev['inicio']}–{ev['fin']} · {ev['tipo']} · {ev['aula']}"
                      for ev in evs[:10]))


# ── Procesador de mensajes ─────────────────────────────────────────────────────
def procesar_mensaje(chat_id, texto, nombre, datos):
    texto    = texto.strip()
    es_admin = str(chat_id) in ADMIN_CHAT_IDS
    log(f"💬 [{nombre}] {texto[:60]}")

    # Si está en medio del onboarding o búsqueda
    _estado_ob = onboard_estado.get(str(chat_id))
    if _estado_ob:
        if _estado_ob.get("step") == "manual_subject":
            _handle_manual_subject_input(chat_id, texto, datos)
            return
        if _estado_ob.get("step") == "qk_search":
            onboard_estado.pop(str(chat_id), None)
            _handle_qk_search(chat_id, texto, datos)
            return
        if _estado_ob.get("step") == "gp_add":
            dash_msg = _estado_ob.get("dashboard_msg")
            onboard_estado.pop(str(chat_id), None)
            _handle_gp_add(chat_id, texto, datos, dashboard_msg=dash_msg)
            return
        telegram_send(chat_id, "⬆️ Completa primer la configuració de dalt.")
        return

    # Comandos admin
    if es_admin:
        if texto in ["/actualizar", "/actualizar@upfbot"]:
            cmd_actualizar(chat_id); return

    # Ordres estàndard
    if texto in ["/start", "/start@upfbot"]:
        perfil = obtener_perfil(str(chat_id))
        if perfil:
            telegram_send_menu(chat_id,
                f"👋 Hola de nou, {nombre}!\n"
                f"<i>{perfil_resumen(perfil)}</i>\n\n"
                f"Utilitza els botons o escriu-me directament."
            )
        else:
            iniciar_onboarding(chat_id)
        return

    if texto in ["/perfil", "/perfil@upfbot", "⚙️ El meu perfil"]:
        iniciar_onboarding(chat_id); return
    if texto in ["/ajuda", "/help", "/ajuda@upfbot"]:
        telegram_send(chat_id, MSG_AYUDA_ADMIN if es_admin else MSG_AYUDA); return
    if texto in ["/guia", "/guia@upfbot", "📖 Guia"]:
        telegram_send(chat_id, MSG_GUIA); return
    if texto in ["/estat", "/estat@upfbot"]:
        cmd_estado(chat_id); return

    # Si no tiene perfil configurado, iniciar onboarding
    perfil = obtener_perfil(str(chat_id))
    if not perfil:
        iniciar_onboarding(chat_id)
        return

    # Filtrar datos por perfil del usuario y recortar a la ventana temporal
    datos_usuario = filtrar_ventana(filtrar_por_perfil(datos, perfil))

    # Shortcuts — comandos y botones del teclado
    if texto in ["/avui", "📅 Avui"]:
        texto = "que tengo hoy"
    elif texto in ["/setmana", "📆 Aquesta setmana"]:
        texto = "que tengo esta semana"

    hoy_d = _today()

    # Intentar resposta com a imatge
    from query_parser import eventos_para_imagen
    img_data = eventos_para_imagen(texto, datos_usuario, hoy_d)
    if img_data:
        evs, desde, hasta = img_data
        try:
            from renderer import render_dia, render_semana
            bio = render_dia(evs, desde) if desde == hasta else render_semana(evs, desde, hasta)
            telegram_send_photo(chat_id, bio)
            log(f"   → imagen ({len(evs)} eventos)")
            return
        except Exception as e:
            log(f"⚠️ render error: {e}")

    # No s'ha entès (o render error) → menú ràpid
    _mostrar_menu_rapid(chat_id, datos_usuario)


# ── Bucle principal ────────────────────────────────────────────────────────────
def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    log("🎓 UPF-Bot arrancando...")

    intentar_actualizar_auto()

    log(f"   Eventos en memoria: {contar_eventos(cargar_datos())}")

    me = telegram_get("getMe")
    if not me or not me.get("ok"):
        log("❌ ERROR: no se puede conectar con Telegram."); return
    log(f"   Bot: @{me['result']['username']}")

    offset = cargar_offset()
    log(f"   Offset: {offset}")
    log("   Escuchando...\n")

    while True:
        updates = telegram_get("getUpdates", {
            "offset": offset, "timeout": 30,
            "allowed_updates": ["message", "callback_query"]
        })
        if not updates or not updates.get("ok"):
            time.sleep(5); continue

        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            guardar_offset(offset)

            # Callback de botón
            if "callback_query" in update:
                cq = update["callback_query"]
                nombre = cq["from"].get("first_name", "Estudiante")
                log(f"🔘 [{nombre}] callback: {cq['data']}")
                try:
                    procesar_callback(cq, cargar_datos())
                except Exception as e:
                    log(f"⚠️ Error callback: {e}")
                continue

            # Mensaje de texto
            msg   = update.get("message", {})
            texto = msg.get("text", "").strip()
            if not msg or not texto:
                continue

            chat_id = msg["chat"]["id"]
            nombre  = msg.get("from", {}).get("first_name", "Estudiante")

            try:
                procesar_mensaje(chat_id, texto, nombre, cargar_datos())
            except Exception as e:
                log(f"⚠️ Error: {e}")
                telegram_send(chat_id, "❌ Alguna cosa ha anat malament. Torna-ho a intentar.")


if __name__ == "__main__":
    main()
