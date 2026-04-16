# upf-bot

Bot de Telegram para consultar horarios de las 4 Enginyeries de la UPF (T3 2025-26).
Los estudiantes configuran su perfil (titulación + curso + grupo) y consultan en lenguaje natural.

## Arranque

```bash
bash start.sh   # arranca bot.py en background
bash stop.sh    # detiene el bot
tail -f upf-bot.log
```

Tras editar código: `bash stop.sh && bash start.sh`

## Archivos clave

| Archivo | Rol |
|---|---|
| `bot.py` | Bot Telegram: onboarding, filtrado, renderer, Claude |
| `scraper.py` | Scraping gestioacademica.upf.edu → horarios.json + curriculum.json |
| `query_parser.py` | Parser local de consultas (~85% sin llamar a Claude) |
| `renderer.py` | Genera imágenes PNG del horario con Pillow |
| `horarios.json` | ~1.862 eventos T3 con fecha exacta (generado por scraper) |
| `curriculum.json` | Asignaturas por titulación/año — auto-generado por scraper |
| `user_profiles.json` | Perfiles de usuario `{chat_id: {mode, titulacio, curs, grup_teoria}}` |

## Titulaciones soportadas

| Key | Nombre | Pla nou | Pla antic |
|---|---|---|---|
| `informatica` | Enginyeria en Informàtica | estudio=3701 plan=763 | estudio=3377 plan=634 |
| `dades` | Enginyeria Matemàtica en Ciència de Dades | estudio=3700 plan=762 | estudio=3370 plan=654 |
| `audiovisuals` | Enginyeria en Sistemes Audiovisuals | estudio=3704 plan=766 | estudio=3375 plan=635 |
| `xarxes` | Enginyeria de Xarxes de Telecomunicació | estudio=3703 plan=765 | estudio=3379 plan=636 |

Para añadir una nueva titulación: añadir entrada en `DEGREES` en `scraper.py` y hacer `/actualizar`.

## Perfiles de usuario

Dos modos:
- **carrera**: titulació + curs (1 o 2) + grup_teoria → filtra por curriculum.json
- **manual**: lista de asignaturas con grup_teoria individual (para 3r/4t o asignaturas mixtas)

```json
// Carrera
{"mode": "carrera", "titulacio": "dades", "curs": 2, "grup_teoria": "2"}
// Manual
{"mode": "manual", "asignaturas": [{"nombre": "Seguretat en Computadors", "grup_teoria": "1"}]}
```

El onboarding es un flujo de 3 pasos con botones inline: titulació → curs → grup.

## Filtrado de eventos (pipeline por mensaje)

```
horarios.json (RAM, cache) — 1862 eventos
    ↓ filtrar_por_perfil()     → asignaturas + grupo del usuario
    ↓ filtrar_ventana(14 días) → solo los próximos 14 días
    ↓ query_parser / renderer / Claude  ← trabaja con ~10-15 eventos
```

**IMPORTANTE**: El filtrado usa `curriculum.json` (`per_any`), NO el campo `ev["curso"]` del JSON,
porque `ev["curso"]` es poco fiable (mismo evento aparece en múltiples fetches).

## Scraper — arquitectura

`scrape_todos_los_cursos()` retorna `(horarios_dict, curriculum_dict)`.

Orden de fetch crítico (garantiza etiquetado correcto de `curso`):
1. Pla nou C1 → todas las titulaciones
2. Pla nou C2 → todas las titulaciones
3. Pla antic C2, C3, C4 → todas las titulaciones

`curriculum.json` se auto-genera extrayendo nombres del `<select name="asignaturas">` de la API UPF.
Solo incluye años 1 y 2 (3+ = optativas → modo manual).

Para re-scrapear: `python3 scraper.py horarios.json curriculum.json`
Desde Telegram (solo admin): `/actualizar` — regenera y guarda ambos ficheros.

## Renderer

`renderer.py` genera imágenes PNG con Pillow (dark theme, fuentes Ubuntu).

- `render_dia(eventos, fecha)` → vista diaria
- `render_semana(eventos, desde, hasta)` → vista semanal

Soporte de eventos solapados: `_assign_lanes()` detecta conflictos de horario y reparte el ancho
en columnas side-by-side. Cada evento muestra badge de grupo (G201, G202...).
Incluye línea roja de hora actual cuando es hoy + badge "AVUI" en el header.

## Rendimiento y seguridad

- **Cache en RAM**: `horarios.json` se lee una vez al arrancar (`_DATOS_CACHE`). `guardar_datos()` actualiza la cache. Igual para `curriculum.json` (`_CURRICULUM`).
- **Ventana temporal**: `VENTANA_DIAS = 14` — solo se pasan los próximos 14 días a handlers y Claude.
- **Rate limiting**: máx. `RATE_MAX_CALLS = 10` llamadas a Claude por usuario en `RATE_WINDOW = 60` segundos.
- **Admin**: solo `ADMIN_CHAT_IDS` puede usar `/actualizar`. Resto de comandos admin son ignorados silenciosamente.

## T3 2025-26

- Inicio: 9 marzo 2026
- Fin: 20 junio 2026
- centro=337 para todas las titulaciones
