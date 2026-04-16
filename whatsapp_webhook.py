"""
WhatsApp bot para UPF-Bot.
Puerto 8000 — Cloudflare Tunnel: https://upfbot.claritmarket.org → localhost:8000
Token permanente (no caduca): sistema upfbot-system en Meta Business Suite
"""
import os
import sys
import time
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import (
    _now, _today, log,
    cargar_datos, filtrar_por_perfil, filtrar_ventana,
    guardar_perfil, obtener_perfil, perfil_resumen,
    DEGREE_LABELS, preguntar_a_claude,
)
from query_parser import eventos_para_imagen, _norm, _detectar_asignatura
from renderer import render_dia, render_semana

# ── Credenciales ──────────────────────────────────────────────────────────────
VERIFY_TOKEN    = "upfbot2026"
WA_TOKEN        = "EAAkVVKKyKRYBRHeZBYWl8HyAbJAbRXzvQyW8qvK9KOv8ZAQpDVNhKtkKSqy5AFglwoJU6SDFhZA3RPEwSDBpNgUU0WbSIPbwPeqhoTQaRASvMZBp6iH5dAcVXaDzeCvZCiUTGp5X0jVZCEAErREksm07OowaXX66aZBuuQhIqb3CtbYv73EK4FZCYWqELJUDnYTlLgZDZD"
PHONE_NUMBER_ID = "996354416903969"
WA_API          = f"https://graph.facebook.com/v17.0/{PHONE_NUMBER_ID}"
PUBLIC_URL      = "https://upfbot.claritmarket.org"

# Directorio para servir imágenes sin subirlas a WA (mucho más rápido)
IMG_DIR = Path(__file__).parent / "wa_img"
IMG_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Estado onboarding en memoria
wa_onboard: dict = {}


# ── WA API helpers ────────────────────────────────────────────────────────────
def _headers():
    return {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}

def _send(payload: dict):
    try:
        r = requests.post(f"{WA_API}/messages", headers=_headers(), json=payload, timeout=15)
        if r.status_code not in (200, 201):
            log(f"⚠️ WA send {r.status_code}: {r.text[:200]}")
        return r.json()
    except Exception as e:
        log(f"⚠️ WA send: {e}")
        return None

def wa_mark_read(msg_id: str):
    """Marca el mensaje como leído (doble check azul) inmediatamente."""
    try:
        requests.post(f"{WA_API}/messages", headers=_headers(), json={
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": msg_id
        }, timeout=5)
    except Exception:
        pass

def wa_text(to: str, text: str):
    _send({"messaging_product": "whatsapp", "to": to,
           "type": "text", "text": {"body": text}})

def wa_buttons(to: str, body: str, buttons: list, footer: str = ""):
    """buttons: list of (id, title) — max 3"""
    interactive = {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
            for bid, title in buttons[:3]
        ]}
    }
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    _send({"messaging_product": "whatsapp", "to": to,
           "type": "interactive", "interactive": interactive})

def wa_list(to: str, body: str, button_label: str, rows: list):
    """rows: list of (id, title) — max 10"""
    _send({
        "messaging_product": "whatsapp", "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": [{"rows": [
                    {"id": rid, "title": title[:24]}
                    for rid, title in rows[:10]
                ]}]
            }
        }
    })

def wa_image_url(to: str, bio) -> bool:
    """
    Guarda la imagen localmente y envía la URL pública.
    Mucho más rápido que subir a WA Media API:
    WA descarga la imagen en paralelo sin bloquear nuestra respuesta.
    """
    try:
        bio.seek(0)
        fname = f"{to[-6:]}_{int(time.time())}.png"
        fpath = IMG_DIR / fname
        fpath.write_bytes(bio.read())
        url = f"{PUBLIC_URL}/img/{fname}"
        _send({"messaging_product": "whatsapp", "to": to,
               "type": "image", "image": {"link": url}})
        _cleanup_images()
        return True
    except Exception as e:
        log(f"⚠️ wa_image_url: {e}")
        return False

def _cleanup_images():
    """Elimina imágenes con más de 1 hora."""
    now = time.time()
    for f in IMG_DIR.glob("*.png"):
        try:
            if now - f.stat().st_mtime > 3600:
                f.unlink()
        except Exception:
            pass


# ── Menús ─────────────────────────────────────────────────────────────────────
def wa_menu_rapid(wa_id: str):
    """3 botones para las acciones más comunes — respuesta inmediata al toque."""
    wa_buttons(
        wa_id,
        "Què vols consultar?",
        [
            ("qk_hoy",    "📅 Avui"),
            ("qk_manana", "📅 Demà"),
            ("qk_semana", "📆 Aquesta setmana"),
        ],
        footer="O escriu el que necessites · «perfil» per canviar configuració"
    )

def wa_iniciar_onboarding(wa_id: str):
    wa_onboard[wa_id] = {"step": "degree", "asignaturas_temp": []}
    wa_list(wa_id,
        "⚙️ Configura el teu perfil\n\nEn quina enginyeria estàs?",
        "Selecciona",
        [
            ("ob_d_informatica",  "💻 Informàtica"),
            ("ob_d_dades",        "📊 Ciència de Dades"),
            ("ob_d_audiovisuals", "🎬 Audiovisuals"),
            ("ob_d_xarxes",       "📡 Xarxes Telecom"),
            ("ob_d_manual",       "✏️ Mode manual"),
        ]
    )

def wa_finalizar_perfil(wa_id: str, perfil: dict):
    guardar_perfil(wa_id, perfil)
    wa_onboard.pop(wa_id, None)
    wa_text(wa_id, f"✅ Perfil guardat!\n{perfil_resumen(perfil)}")
    wa_menu_rapid(wa_id)

def wa_dashboard(wa_id: str):
    """Muestra el perfil actual con opciones de edición — sin borrar nada."""
    from bot import _perfil_asignaturas
    perfil = obtener_perfil(wa_id)
    if not perfil:
        wa_iniciar_onboarding(wa_id); return

    asigs = _perfil_asignaturas(perfil)
    mode  = perfil.get("mode", "carrera")

    if mode == "carrera":
        deg   = DEGREE_LABELS.get(perfil.get("titulacio", ""), "?")
        curs  = perfil.get("curs", "?")
        grup  = perfil.get("grup_teoria", "?")
        header = f"⚙️ *El teu perfil*\n{deg}\n{curs}r curs · Grup {grup}"
    else:
        header = "⚙️ *El teu perfil* (mode manual)"

    if asigs:
        lines = "\n".join(f"  • {a['nombre']} (G{a['grup_teoria']})" for a in asigs)
        body  = f"{header}\n\n{lines}"
    else:
        body = f"{header}\n\nSense assignatures configurades."

    wa_buttons(wa_id, body,
        [("gp_add", "➕ Afegir assignatura"),
         ("gp_del", "❌ Eliminar assignatura"),
         ("gp_reset", "🔄 Reiniciar perfil")],
        footer="Tornar: escriu qualsevol cosa"
    )


# ── Procesador de texto ───────────────────────────────────────────────────────
def process_text(wa_id: str, text: str, datos: dict):
    text = text.strip()
    log(f"📱 WA [{wa_id[-4:]}] {text[:60]}")

    # Estado de onboarding activo
    estado = wa_onboard.get(wa_id)
    if estado:
        step = estado.get("step")
        if step == "manual_subject":
            handle_manual_subject(wa_id, text, datos); return
        if step == "gp_add":
            wa_onboard.pop(wa_id, None)
            handle_gp_add(wa_id, text, datos); return
        if step == "qk_search":
            wa_onboard.pop(wa_id, None)
            handle_search(wa_id, text, datos); return
        wa_text(wa_id, "⬆️ Completa primer la configuració anterior.")
        return

    # Sin perfil → bienvenida + onboarding
    perfil = obtener_perfil(wa_id)
    if not perfil:
        wa_text(wa_id,
            "👋 Hola! Soc *UPF-Bot*, l'assistent d'horaris de les Enginyeries de la UPF.\n\n"
            "Puc dir-te:\n"
            "• 📅 Quines classes tens avui o demà\n"
            "• 📆 El teu horari de la setmana\n"
            "• 🔍 Quan és una assignatura concreta\n\n"
            "Escriu en llenguatge natural, com ara:\n"
            "_«Què tinc demà?»_\n"
            "_«Quan és Seguretat?»_\n"
            "_«Horari d'aquesta setmana»_\n\n"
            "Primer configura el teu perfil 👇"
        )
        wa_iniciar_onboarding(wa_id); return

    # Comandos de texto especiales
    if text.lower() in ["perfil", "configuració", "canviar", "/perfil"]:
        wa_dashboard(wa_id); return
    if text.lower() in ["avui", "hoy", "/avui"]:
        text = "que tengo hoy"
    elif text.lower() in ["demà", "manana", "mañana"]:
        text = "que tengo manana"
    elif text.lower() in ["setmana", "semana", "/setmana"]:
        text = "que tengo esta semana"
    elif text.lower() in ["hola", "hi", "bon dia", "buenas", "/start"]:
        wa_text(wa_id,
            f"👋 Hola! Soc UPF-Bot.\n\n"
            f"Pregunta'm el que necessites:\n"
            f"• _«Què tinc avui?»_\n"
            f"• _«Quan és Xarxes?»_\n"
            f"• _«Horari de la setmana»_\n\n"
            f"O usa els botons de sota 👇"
        )
        wa_menu_rapid(wa_id); return

    datos_usuario = filtrar_ventana(filtrar_por_perfil(datos, perfil))
    hoy_d = _today()

    # Intentar respuesta como imagen
    img_data = eventos_para_imagen(text, datos_usuario, hoy_d)
    if img_data:
        evs, desde, hasta = img_data
        try:
            bio = render_dia(evs, desde) if desde == hasta else render_semana(evs, desde, hasta)
            if wa_image_url(wa_id, bio):
                log(f"   → imagen WA URL ({len(evs)} eventos)")
                return
        except Exception as e:
            log(f"⚠️ WA render: {e}")

    # Fallback: pregunta a Claude
    try:
        resposta = preguntar_a_claude(text, datos_usuario)
        wa_text(wa_id, resposta)
        log(f"   → Claude WA")
    except Exception as e:
        log(f"⚠️ WA Claude: {e}")
        wa_menu_rapid(wa_id)


# ── Procesador de botones / lista ─────────────────────────────────────────────
def process_interactive(wa_id: str, reply_id: str, datos: dict):
    log(f"📱 WA [{wa_id[-4:]}] btn: {reply_id}")

    # Menú ràpid
    if reply_id in ("qk_hoy", "qk_manana", "qk_semana"):
        text_map = {"qk_hoy": "que tengo hoy", "qk_manana": "que tengo manana",
                    "qk_semana": "que tengo esta semana"}
        process_text(wa_id, text_map[reply_id], datos); return
    if reply_id == "qk_buscar":
        wa_onboard[wa_id] = {"step": "qk_search"}
        wa_text(wa_id, "🔍 Escriu el nom de l'assignatura:"); return
    if reply_id == "qk_perfil":
        wa_dashboard(wa_id); return

    # Dashboard de perfil
    if reply_id == "gp_add":
        from bot import _carrera_to_manual
        perfil = obtener_perfil(wa_id)
        if perfil and perfil.get("mode") != "manual":
            perfil = _carrera_to_manual(perfil)
            guardar_perfil(wa_id, perfil)
        wa_onboard[wa_id] = {"step": "gp_add"}
        wa_text(wa_id, "➕ Escriu el nom de l'assignatura que vols afegir:"); return

    if reply_id == "gp_del":
        from bot import _perfil_asignaturas, _carrera_to_manual
        perfil = obtener_perfil(wa_id)
        if not perfil:
            wa_dashboard(wa_id); return
        if perfil.get("mode") != "manual":
            perfil = _carrera_to_manual(perfil)
            guardar_perfil(wa_id, perfil)
        asigs = _perfil_asignaturas(perfil)
        if not asigs:
            wa_text(wa_id, "No tens assignatures per eliminar."); return
        wa_list(wa_id, "Selecciona l'assignatura que vols eliminar:", "Selecciona",
            [(f"gp_del_{i}", a['nombre'][:24]) for i, a in enumerate(asigs)])
        return

    if reply_id.startswith("gp_del_"):
        from bot import _perfil_asignaturas, _carrera_to_manual
        idx    = int(reply_id.split("_")[-1])
        perfil = obtener_perfil(wa_id)
        if not perfil: return
        if perfil.get("mode") != "manual":
            perfil = _carrera_to_manual(perfil)
        asigs = perfil.get("asignaturas", [])
        if idx < len(asigs):
            nom = asigs[idx]["nombre"]
            asigs.pop(idx)
            perfil["asignaturas"] = asigs
            guardar_perfil(wa_id, perfil)
            wa_text(wa_id, f"✅ {nom} eliminada.")
        wa_dashboard(wa_id); return

    if reply_id == "gp_reset":
        wa_iniciar_onboarding(wa_id); return

    # Onboarding: titulació
    if reply_id.startswith("ob_d_"):
        suffix = reply_id[5:]
        if suffix == "manual":
            wa_onboard[wa_id] = {"step": "manual_subject", "asignaturas_temp": []}
            wa_text(wa_id, "✏️ Escriu el nom d'una assignatura (p.ex. Xarxes, Seguretat):")
        else:
            estado = wa_onboard.setdefault(wa_id, {"asignaturas_temp": []})
            estado.update({"titulacio": suffix, "step": "curs"})
            wa_buttons(wa_id,
                f"✅ {DEGREE_LABELS.get(suffix, suffix)}\n\nQuin curs fas?",
                [("ob_yr_1", "1r curs"), ("ob_yr_2", "2n curs"), ("ob_yr_opt", "3r/4t/opt")])
        return

    # Onboarding: curs
    if reply_id.startswith("ob_yr_"):
        suffix = reply_id[6:]
        estado = wa_onboard.get(wa_id, {})
        if suffix == "opt":
            estado.update({"step": "manual_subject", "asignaturas_temp": []})
            wa_onboard[wa_id] = estado
            wa_text(wa_id, "Escriu el nom de la primera assignatura d'aquest trimestre:")
        else:
            estado["curs"] = int(suffix)
            estado["step"] = "grup"
            wa_onboard[wa_id] = estado
            curs = estado["curs"]
            wa_buttons(wa_id,
                f"✅ {DEGREE_LABELS.get(estado.get('titulacio',''), '')} · "
                f"{'1r' if curs == 1 else '2n'} curs\n\nQuin grup de teoria tens?",
                [("ob_gt_1", "Grup 1"), ("ob_gt_2", "Grup 2"), ("ob_gt_3", "Grup 3")])
        return

    # Onboarding: grup carrera
    if reply_id.startswith("ob_gt_"):
        grup  = reply_id[6:]
        estado = wa_onboard.get(wa_id, {})
        if estado.get("titulacio") and estado.get("curs"):
            wa_finalizar_perfil(wa_id, {"mode": "carrera",
                                        "titulacio": estado["titulacio"],
                                        "curs": estado["curs"],
                                        "grup_teoria": grup})
        return

    # Onboarding manual: grup per assignatura
    if reply_id.startswith("ob_mg_"):
        grup  = reply_id[6:]
        estado = wa_onboard.get(wa_id, {})

        # Viene del dashboard gp_add
        if estado.get("step") == "gp_add_grup":
            nom = estado.get("nombre")
            wa_onboard.pop(wa_id, None)
            if grup != "no" and nom:
                perfil = obtener_perfil(wa_id) or {"mode": "manual", "asignaturas": []}
                perfil["asignaturas"].append({"nombre": nom, "grup_teoria": grup})
                guardar_perfil(wa_id, perfil)
                wa_text(wa_id, f"✅ {nom} (Grup {grup}) afegida!")
            wa_dashboard(wa_id); return

        nom   = estado.get("asignatura_pendiente")
        if grup == "no":
            estado.update({"step": "manual_subject", "asignatura_pendiente": None})
            wa_onboard[wa_id] = estado
            wa_text(wa_id, "D'acord. Escriu el nom d'una altra assignatura:")
        elif nom:
            estado["asignaturas_temp"].append({"nombre": nom, "grup_teoria": grup})
            estado["asignatura_pendiente"] = None
            wa_onboard[wa_id] = estado
            n = len(estado["asignaturas_temp"])
            wa_buttons(wa_id,
                f"✅ {n} assignatura{'s' if n > 1 else ''} afegida{'s' if n > 1 else ''}.\n"
                "Vols afegir-ne una altra?",
                [("ob_mc_add", "➕ Afegir més"), ("ob_mc_save", "💾 Guardar")])
        return

    if reply_id == "ob_mc_add":
        estado = wa_onboard.get(wa_id, {})
        estado["step"] = "manual_subject"
        wa_onboard[wa_id] = estado
        wa_text(wa_id, "Escriu el nom de la següent assignatura:"); return

    if reply_id == "ob_mc_save":
        estado = wa_onboard.get(wa_id, {})
        asigs  = estado.get("asignaturas_temp", [])
        if not asigs:
            wa_text(wa_id, "⚠️ No has afegit cap assignatura. Escriu-ne una:"); return
        wa_finalizar_perfil(wa_id, {"mode": "manual", "asignaturas": asigs}); return


def handle_manual_subject(wa_id: str, text: str, datos: dict):
    estado  = wa_onboard.get(wa_id, {})
    termino = text.strip()
    if len(termino) < 3:
        wa_text(wa_id, "Escriu almenys 3 caràcters."); return
    nombres = list({ev['nombre'] for ev in datos.get('eventos', [])})
    matches = _detectar_asignatura(_norm(termino), nombres)
    if not matches:
        wa_text(wa_id, f"❌ No he trobat «{termino}».\nProva amb una altra paraula:"); return
    matched = matches[0]
    if matched in [a["nombre"] for a in estado.get("asignaturas_temp", [])]:
        wa_text(wa_id, f"⚠️ {matched} ja està a la llista. Escriu una altra:"); return
    estado.update({"asignatura_pendiente": matched, "step": "manual_grup"})
    wa_onboard[wa_id] = estado
    wa_buttons(wa_id,
        f"He trobat: {matched}\n\nQuin grup de teoria tens?",
        [("ob_mg_1", "Grup 1"), ("ob_mg_2", "Grup 2"), ("ob_mg_3", "Grup 3")])


def handle_gp_add(wa_id: str, text: str, datos: dict):
    """Añade una asignatura al perfil manual (desde el dashboard)."""
    nombres = list({ev['nombre'] for ev in datos.get('eventos', [])})
    matches = _detectar_asignatura(_norm(text.strip()), nombres)
    if not matches:
        wa_text(wa_id, f"❌ No he trobat «{text}». Torna-ho a intentar:"); return
    nom    = matches[0]
    perfil = obtener_perfil(wa_id) or {"mode": "manual", "asignaturas": []}
    if any(a["nombre"] == nom for a in perfil.get("asignaturas", [])):
        wa_text(wa_id, f"⚠️ {nom} ja és al teu perfil.")
        wa_dashboard(wa_id); return
    wa_onboard[wa_id] = {"step": "gp_add_grup", "nombre": nom}
    wa_buttons(wa_id, f"He trobat: {nom}\n\nQuin grup de teoria tens?",
        [("ob_mg_1", "Grup 1"), ("ob_mg_2", "Grup 2"), ("ob_mg_3", "Grup 3")])


def handle_search(wa_id: str, text: str, datos: dict):
    perfil = obtener_perfil(wa_id)
    datos_perfil = filtrar_por_perfil(datos, perfil) if perfil else datos
    nombres = list({ev['nombre'] for ev in datos_perfil.get('eventos', [])})
    matches = _detectar_asignatura(_norm(text), nombres)
    if not matches:
        wa_text(wa_id, f"No he trobat cap assignatura amb «{text}»."); return
    nombre  = matches[0]
    hoy_d   = _today()
    dies_fins_dv = (4 - hoy_d.weekday()) % 7
    hasta_d = hoy_d + timedelta(days=dies_fins_dv if dies_fins_dv > 0 else 7)
    evs = sorted(
        [ev for ev in datos_perfil.get('eventos', [])
         if ev['nombre'] == nombre
         and hoy_d <= datetime.strptime(ev['fecha'], '%Y-%m-%d').date() <= hasta_d],
        key=lambda x: (x['fecha'], x['inicio'])
    )
    if not evs:
        wa_text(wa_id, f"No hi ha classes de {nombre} aquesta setmana."); return
    try:
        desde = datetime.strptime(evs[0]['fecha'],  '%Y-%m-%d').date()
        hasta = datetime.strptime(evs[-1]['fecha'], '%Y-%m-%d').date()
        bio = render_dia(evs, desde) if desde == hasta else render_semana(evs, desde, hasta)
        wa_image_url(wa_id, bio)
    except Exception as e:
        log(f"⚠️ WA search render: {e}")
        wa_text(wa_id, f"📚 {nombre}\n" +
            "\n".join(f"{ev['fecha']} {ev['inicio']}–{ev['fin']} · {ev['aula']}"
                      for ev in evs[:8]))


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/img/<filename>")
def serve_image(filename):
    """Sirve imágenes generadas — WA las descarga directamente desde aquí."""
    return send_from_directory(IMG_DIR, filename)


@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive():
    data = request.get_json(silent=True)
    try:
        changes = data["entry"][0]["changes"][0]["value"]
        msgs    = changes.get("messages", [])
        if not msgs:
            return jsonify({"status": "ok"}), 200

        msg   = msgs[0]
        wa_id = msg["from"]
        datos = cargar_datos()

        # Marcar como leído inmediatamente → doble check azul
        wa_mark_read(msg["id"])

        mtype = msg.get("type")
        if mtype == "text":
            process_text(wa_id, msg["text"]["body"], datos)
        elif mtype == "interactive":
            itype = msg["interactive"]["type"]
            if itype == "button_reply":
                rid = msg["interactive"]["button_reply"]["id"]
            elif itype == "list_reply":
                rid = msg["interactive"]["list_reply"]["id"]
            else:
                rid = None
            if rid:
                process_interactive(wa_id, rid, datos)
    except Exception as e:
        app.logger.error(f"Error: {e}")
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
