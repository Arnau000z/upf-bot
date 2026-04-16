#!/usr/bin/env python3
"""
Scraper de horaris UPF — multi-grau, curriculum automàtic
=========================================================
Genera DOS fitxers:
  horarios.json   — tots els events del T3 amb data exacta
  curriculum.json — assignatures per titulació/any, derivat de la pròpia API

Per afegir una nova titulació: afegir una entrada a DEGREES i fer /actualizar.

Ordre de fetch CRÍTIC (any×any, pla nou primer):
  Garanteix que ev["curso"] = any mínim en el qual apareix la matèria.
  Exemple: Tècniques d'Optimització → Dades C2 (pla nou) processa ABANS de
  qualsevol C3 (pla antic), així s'etiqueta curso=2.
"""
import re
import requests
import json
from datetime import datetime
from collections import defaultdict

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False

BASE     = 'https://gestioacademica.upf.edu/pds/consultaPublica/'
AJAX_URL = BASE + '%5BAjax%5DselecionarRangoHorarios'
HEADERS  = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36'}

T3_START = datetime(2026, 3,  9)
T3_END   = datetime(2026, 6, 20)

DIAS_CAT_ES = {
    'Dilluns': 'Lunes', 'Dimarts': 'Martes', 'Dimecres': 'Miércoles',
    'Dijous': 'Jueves', 'Divendres': 'Viernes', 'Dissabte': 'Sábado',
}
TIPOS_EXCLUIR = {'Examen', 'Exàmen', 'Examen Parcial', 'Examen Final', 'Recuperació'}


# ── Configuració de titulacions ────────────────────────────────────────────────
# Per afegir una nova titulació: afegir aquí i fer /actualizar.
# pla_nou: pla vigent per als estudiants de 1r i 2n actuals
# pla_antic: pla anterior, actiu per a estudiants de 2n, 3r i 4t
DEGREES = {
    "informatica": {
        "label":    "Enginyeria en Informàtica",
        "centro":   337,
        "pla_nou":  {"estudio": 3701, "plan": 763},
        "pla_antic": {"estudio": 3377, "plan": 634},
    },
    "dades": {
        "label":    "Enginyeria Matemàtica en Ciència de Dades",
        "centro":   337,
        "pla_nou":  {"estudio": 3700, "plan": 762},
        "pla_antic": {"estudio": 3370, "plan": 654},
    },
    "audiovisuals": {
        "label":    "Enginyeria en Sistemes Audiovisuals",
        "centro":   337,
        "pla_nou":  {"estudio": 3704, "plan": 766},
        "pla_antic": {"estudio": 3375, "plan": 635},
    },
    "xarxes": {
        "label":    "Enginyeria de Xarxes de Telecomunicació",
        "centro":   337,
        "pla_nou":  {"estudio": 3703, "plan": 765},
        "pla_antic": {"estudio": 3379, "plan": 636},
    },
}

# Ordre de fetch: primer C1 i C2 (pla nou) per a TOTES les titulacions,
# després C2/C3/C4 (pla antic). Garanteix curso=2 per a matèries de 2n any.
def _build_fetch_order() -> list:
    order = []
    # Passa 1: pla nou C1 i C2 (màxima prioritat per a etiquetatge de curso)
    for curs in [1, 2]:
        for tid, cfg in DEGREES.items():
            order.append({
                "titulacio": tid, "curso": curs,
                "estudio": cfg["pla_nou"]["estudio"],
                "plan":    cfg["pla_nou"]["plan"],
                "centro":  cfg["centro"],
                "desc":    f"{cfg['label']} C{curs} (Pla nou)",
            })
    # Passa 2: pla antic C2, C3, C4 (per a estudiants que cursen el pla vell)
    for curs in [2, 3, 4]:
        for tid, cfg in DEGREES.items():
            order.append({
                "titulacio": tid, "curso": curs,
                "estudio": cfg["pla_antic"]["estudio"],
                "plan":    cfg["pla_antic"]["plan"],
                "centro":  cfg["centro"],
                "desc":    f"{cfg['label']} C{curs} (Pla antic)",
            })
    return order

FETCH_ORDER = _build_fetch_order()


# ── Helpers ────────────────────────────────────────────────────────────────────
def decode_html(s: str) -> str:
    return (str(s)
        .replace('&Agrave;', 'À').replace('&agrave;', 'à')
        .replace('&Aacute;', 'Á').replace('&aacute;', 'á')
        .replace('&Eacute;', 'É').replace('&eacute;', 'é')
        .replace('&Egrave;', 'È').replace('&egrave;', 'è')
        .replace('&Iacute;', 'Í').replace('&iacute;', 'í')
        .replace('&Oacute;', 'Ó').replace('&oacute;', 'ó')
        .replace('&Ograve;', 'Ò').replace('&ograve;', 'ò')
        .replace('&Uacute;', 'Ú').replace('&uacute;', 'ú')
        .replace('&ntilde;', 'ñ').replace('&Ntilde;', 'Ñ')
        .replace('&ccedil;', 'ç').replace('&Ccedil;', 'Ç')
        .replace('&amp;', '&').replace('&uuml;', 'ü')
        .replace('&iuml;', 'ï').replace('&middot;', '·')
        .replace('&#209;', 'Ñ').replace('&#243;', 'ó').replace('&#233;', 'é')
        .strip())

def _parse_asig_name(raw: str) -> str:
    """Extreu el nom net de l'assignatura. Format UPF: "26734 - Tècniques d'Optimització"."""
    text = decode_html(raw).strip()
    # Treure prefix numèric "12345 - " si existeix
    text = re.sub(r'^\d+\s*[-–]\s*', '', text)
    return text.strip()


# ── Fetch principal ────────────────────────────────────────────────────────────
def _fetch_estudi_curs(estudio: int, plan: int, centro: int, curs: int,
                       trimestre: str = 'T/3') -> tuple[list, list]:
    """
    Retorna (asig_names, raw_events) per a un estudi+curs.

    asig_names: llista de noms d'assignatures disponibles (font: UPF, camp select)
    raw_events: llista d'events JSON del calendari T3
    """
    s = requests.Session()
    s.headers.update(HEADERS)

    init_url = (f'{BASE}look%5Bconpub%5DInicioPubHora'
                f'?entradaPublica=true&idiomaPais=ca.ES&planDocente=2025'
                f'&centro={centro}&estudio={estudio}')
    r1 = s.get(init_url, timeout=20)
    if r1.status_code != 200:
        raise ConnectionError(f"HTTP {r1.status_code} de gestioacademica.upf.edu")

    pd_update = [
        ('planEstudio', str(plan)), ('idPestana', '1'), ('ultimoPlanDocente', '2025'),
        ('accesoSecretaria', 'null'), ('jsonBusquedaAsignaturas', '{}'),
        ('limpiarParametrosBusqueda', 'N'), ('planDocente', '2025'),
        ('centro', str(centro)), ('estudio', str(estudio)),
        ('curso', str(curs)), ('trimestre', trimestre),
    ]
    r_combos = s.post(BASE + 'look%5Bconpub%5DActualizarCombosPubHora?rnd=1.0',
                      data=pd_update, headers={'Referer': init_url}, timeout=20)
    soup = BeautifulSoup(r_combos.content, 'html.parser', from_encoding='iso-8859-15')

    sel_g = soup.find('select', {'name': 'grupos'})
    sel_a = soup.find('select', {'name': 'asignaturas'})
    if not sel_g or not sel_a:
        return [], []

    # ── Noms d'assignatures (font oficial UPF per a curriculum.json) ──────────
    asig_names = [
        _parse_asig_name(o.get_text())
        for o in sel_a.find_all('option')
        if o.get_text().strip()
    ]

    grupos = [o['value'] for o in sel_g.find_all('option')]
    asigs  = [o['value'] for o in sel_a.find_all('option')]
    if not grupos or not asigs:
        return asig_names, []

    # ── Events del calendari ──────────────────────────────────────────────────
    pd = [
        ('planEstudio', str(plan)), ('idPestana', '1'), ('ultimoPlanDocente', '2025'),
        ('accesoSecretaria', 'null'), ('jsonBusquedaAsignaturas', '{}'),
        ('limpiarParametrosBusqueda', 'N'), ('planDocente', '2025'),
        ('centro', str(centro)), ('estudio', str(estudio)),
        ('curso', str(curs)), ('trimestre', trimestre),
        ('grupos', ','.join(grupos)), ('asignaturas', ','.join(asigs)),
    ]
    for g in grupos: pd.append((f'grupo{g}', g))
    for a in asigs:  pd.append((f'asignatura{a}', a))

    r3 = s.post(BASE + 'look%5Bconpub%5DMostrarPubHora?rnd=1.0', data=pd,
                headers={'Referer': init_url}, timeout=30)
    r  = s.get(AJAX_URL,
               params={'start': int(T3_START.timestamp()),
                       'end':   int(T3_END.timestamp()), 'rnd': '1.0'},
               headers={'Referer': r3.url,
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'application/json'},
               timeout=30)

    raw_events = []
    if 'json' in r.headers.get('Content-Type', '').lower():
        data = json.loads(r.text)
        if isinstance(data, list) and len(data) > 1:
            raw_events = data[:-1]

    return asig_names, raw_events


def _raw_to_eventos(raw: list, curso: int, estudio: int) -> list:
    """Converteix events raw de la API a llista neta amb data exacta."""
    eventos = []
    seen = set()

    for ev in raw:
        if ev.get('festivoNoLectivo'):
            continue
        tipo = decode_html(ev.get('tipologia', ''))
        if not tipo or tipo in TIPOS_EXCLUIR or 'exam' in tipo.lower():
            continue

        cod   = ev.get('codAsignatura')
        title = decode_html(ev.get('title', ''))
        start = ev.get('start', '')
        end   = ev.get('end', '')
        aula  = decode_html(ev.get('aula', '')) or 'Per confirmar'
        grup  = str(ev.get('grup', ''))
        dia   = DIAS_CAT_ES.get(ev.get('diaSemana', ''), '')

        if not start or cod is None or not dia:
            continue

        fecha = start[:10]
        h_ini = start[11:16] if len(start) >= 16 else ''
        h_fin = end[11:16]   if len(end)   >= 16 else ''
        if not h_ini:
            continue

        key = (cod, tipo, grup, fecha, h_ini)
        if key in seen:
            continue
        seen.add(key)

        eventos.append({
            'fecha':   fecha,
            'dia':     dia,
            'curso':   curso,
            'estudio': estudio,
            'cod':     cod,
            'nombre':  title,
            'tipo':    tipo,
            'grup':    grup,
            'inicio':  h_ini,
            'fin':     h_fin,
            'aula':    aula,
        })

    return sorted(eventos, key=lambda x: (x['fecha'], x['inicio'], x['nombre'], x['grup']))


def _resolver_aulas_y_mergear(eventos: list) -> list:
    """
    1. Resolució d'aules desconegudes: copia l'aula d'un altre event del mateix slot.
    2. Merge de grups: events iguals però amb grup diferent es fusionen (grup="101+102").
    """
    aula_per_slot: dict = {}
    for ev in eventos:
        if ev['aula'] != 'Per confirmar':
            key = (ev['nombre'], ev['tipo'], ev['fecha'], ev['inicio'])
            aula_per_slot.setdefault(key, ev['aula'])
    for ev in eventos:
        if ev['aula'] == 'Per confirmar':
            key = (ev['nombre'], ev['tipo'], ev['fecha'], ev['inicio'])
            if key in aula_per_slot:
                ev['aula'] = aula_per_slot[key]

    merged: dict = {}
    for ev in eventos:
        key = (ev['nombre'], ev['tipo'], ev['fecha'], ev['inicio'], ev['fin'], ev['aula'])
        if key not in merged:
            merged[key] = {**ev, '_grups': [ev['grup']], '_estudis': set(ev.get('estudis', [ev['estudio']]))}
        else:
            if ev['grup'] not in merged[key]['_grups']:
                merged[key]['_grups'].append(ev['grup'])
            merged[key]['_estudis'].update(ev.get('estudis', [ev['estudio']]))

    result = []
    for ev in merged.values():
        grups = sorted(ev['_grups'], key=lambda g: (len(g), g))
        ev_clean = {k: v for k, v in ev.items() if k not in ('_grups', '_estudis')}
        ev_clean['grup']   = '+'.join(grups)
        ev_clean['estudis'] = sorted(ev['_estudis'])
        result.append(ev_clean)

    return result


# ── Construcció automàtica de curriculum ───────────────────────────────────────
def _build_curriculum(raw_curriculum: dict) -> dict:
    """
    raw_curriculum: {titulacio: {curs: [noms]}}

    Assigna cada assignatura al MÍNIM any en el qual apareix per a cada titulació.
    Només inclou els anys 1 i 2 (3+ són optatives → mode manual).
    """
    per_any = {}
    for tid in DEGREES:
        years_data = raw_curriculum.get(tid, {})
        assigned   = set()
        per_any[tid] = {}
        for curs in sorted(years_data.keys()):
            if curs > 2:
                continue
            new_subjects = sorted(set(years_data[curs]) - assigned)
            if new_subjects:
                per_any[tid][str(curs)] = new_subjects
                assigned.update(new_subjects)

    return {
        "_nota": (
            "Generat automàticament per scraper.py — NO editar manualment.\n"
            "Conté les assignatures obligatòries per titulació i any curricular,\n"
            "extretes directament de la API de gestioacademica.upf.edu."
        ),
        "per_any": per_any,
    }


# ── Funció principal ───────────────────────────────────────────────────────────
def scrape_todos_los_cursos(log_fn=print, trimestre='T/3') -> tuple[dict, dict]:
    """
    Retorna (horarios_dict, curriculum_dict).
    Ambdós es poden desar directament com a JSON.
    """
    if not BS4_OK:
        raise ImportError("beautifulsoup4 no instal·lat. Executa: pip3 install beautifulsoup4")

    todos_eventos   = []
    raw_curriculum  = {tid: defaultdict(list) for tid in DEGREES}

    for cfg in FETCH_ORDER:
        tid   = cfg["titulacio"]
        curs  = cfg["curso"]
        log_fn(f"   {cfg['desc']}...")
        try:
            asig_names, raw = _fetch_estudi_curs(
                cfg['estudio'], cfg['plan'], cfg['centro'], curs, trimestre
            )
            evs = _raw_to_eventos(raw, curs, cfg['estudio'])
            todos_eventos.extend(evs)

            # Acumular noms per al curriculum (anys 1 i 2 només)
            if curs <= 2 and asig_names:
                raw_curriculum[tid][curs].extend(asig_names)
                log_fn(f"     → {len(asig_names)} assignatures, {len(evs)} events")
            else:
                log_fn(f"     → {len(evs)} events")

        except Exception as e:
            log_fn(f"   ⚠️  Error: {e}")

    # ── Deduplicació global d'events ──────────────────────────────────────────
    # La clau inclou grup — events amb grups diferents es mantenen separats.
    # El primer event vist guanya (FETCH_ORDER garanteix curs baix → curs alt).
    estudis_map: dict = defaultdict(set)
    for ev in todos_eventos:
        key = (ev['nombre'], ev['tipo'], ev['grup'], ev['fecha'], ev['inicio'])
        estudis_map[key].add(ev['estudio'])

    seen_global: set = set()
    uniq = []
    for ev in todos_eventos:
        key = (ev['nombre'], ev['tipo'], ev['grup'], ev['fecha'], ev['inicio'])
        if key not in seen_global:
            seen_global.add(key)
            ev['estudis'] = sorted(estudis_map[key])
            uniq.append(ev)
    todos_eventos = uniq

    # ── Post-procés: aules i merge de grups ───────────────────────────────────
    todos_eventos = _resolver_aulas_y_mergear(todos_eventos)
    todos_eventos.sort(key=lambda x: (x['fecha'], x['inicio'], x['nombre']))

    n_ev   = len(todos_eventos)
    n_asig = len({e['cod'] for e in todos_eventos})
    log_fn(f"   Total: {n_ev} events, {n_asig} assignatures úniques")

    horarios = {
        'universidad':          'UPF',
        'curso_academico':      '2025-26',
        'trimestre':            trimestre.replace('T/', 'T'),
        't3_inicio':            T3_START.strftime('%Y-%m-%d'),
        't3_fin':               T3_END.strftime('%Y-%m-%d'),
        'eventos':              todos_eventos,
        'total_eventos':        n_ev,
        'total_asignaturas':    n_asig,
    }

    curriculum = _build_curriculum(raw_curriculum)

    return horarios, curriculum


# ── Execució directa ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    horarios_file   = sys.argv[1] if len(sys.argv) > 1 else 'horarios.json'
    curriculum_file = sys.argv[2] if len(sys.argv) > 2 else 'curriculum.json'

    print('🔄 Scraping UPF gestioacademica...')
    horarios, curriculum = scrape_todos_los_cursos(log_fn=print)

    ts = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    horarios['ultima_actualizacion'] = ts
    horarios['fuente'] = 'gestioacademica.upf.edu'

    with open(horarios_file, 'w', encoding='utf-8') as f:
        json.dump(horarios, f, ensure_ascii=False, indent=2)
    print(f'✅ {horarios_file} — {horarios["total_eventos"]} events, {horarios["total_asignaturas"]} assignatures')

    with open(curriculum_file, 'w', encoding='utf-8') as f:
        json.dump(curriculum, f, ensure_ascii=False, indent=2)
    print(f'✅ {curriculum_file} — auto-generat de la API UPF')
