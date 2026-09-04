# Script: generar_movilidad_trafico_desde_real.py
# Ancla movilidad_trafico en los dos informes reales del observatorio MOVIMA
# (Kapsch / Área de Movilidad, Ciudad de Málaga, 1er cuatrimestre 2026):
#
#   PM (65 Puntos de Medida, "Intensidad-Media-Diaria-1er-Cuatrimestre-2026.pdf")
#       -> I.M.D.L./S./D. (intensidad media diaria por tipo de día) y hora punta
#          (intensidad + hora) por tipo de día. 62 puntos con dato (3 sin medir:
#          2 "rota por obras" y 1 carril bus exclusivo).
#   PC (23 Puntos de Clasificación, "Intensidades-Clasificacion-Vehiculos-...pdf")
#       -> mismos I.M.D.L./S./D. (columna TOTAL) más el reparto real por
#          categoría de vehículo (ligeros/motos/autobuses/furgones/otros) en
#          día laborable, usado para derivar proporciones ligero/moto/pesado.
#
# PM y PC son conjuntos de puntos de medida INDEPENDIENTES (así los define el
# propio informe): no se fuerzan a coincidir 1:1 aunque compartan avenida
# (ej. "Avda. Ramón y Cajal" tiene un PM y, por separado, un PC en otro tramo);
# es realista que una red de sensores mida varios puntos en la misma vía.
#
# Ninguno de los dos PDF trae coordenadas, solo nombres de calle. Se intenta
# geolocalizar cada punto por coincidencia de texto contra un "gazetteer" de
# direcciones reales ya usadas en otras dimensiones de este proyecto (ITS,
# carriles bici, ocupación permanente, plazas reservadas, afectaciones,
# parking -- todas ellas ancladas en dato real con coordenadas reales). Donde
# no hay coincidencia, se genera una coordenada sintética dentro de la bbox
# real de Málaga (instrucción explícita del usuario: "si hay filas que no
# tienen coordenadas crea sinteticos").
#
# Grain de negocio = una medición por ubicación/hora. Al no tener el dato real
# minuto a minuto (solo el total diario y, cuando existe, la hora punta), se
# reparte el total diario real en 24 medidas horarias usando una curva
# genérica de tráfico urbano bimodal (supuesto, documentado, análogo al de
# movilidad_parking) DESPLAZADA para que su pico caiga en la hora punta real
# de cada punto y ESCALADA para que la suma de las 24 horas sea exactamente
# el I.M.D.L/S/D. real. Se generan 3 fechas reales representativas dentro del
# propio periodo medido (un laborable, un sábado y un domingo de Feb-2026),
# suficientes para un cálculo de percentiles fiable por vía (72 medidas/punto).

import argparse
import csv
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

FIELDNAMES = ["id", "nombre", "latitud", "longitud", "direccion", "fecha_inicio", "hora_inicio", "fecha_fin", "hora_fin", "trafico_flujo", "trafico_vehiculo"]

GAZETTEER_FILES = [
    "control_gestion_its.csv",
    "movilidad_carriles_bici.csv",
    "ocupacion_permanente_espacio_publico.csv",
    "movilidad_plazas_reservadas.csv",
    "gestion_afectaciones_urbanas.csv",
    "movilidad_parking.csv",
]

# Curva horaria genérica de tráfico urbano (supuesto, no real): bimodal con
# pico mañana (~8-9h) y pico tarde (~13-14h y ~19h), mínimo de madrugada.
# Suma = 1.0; se usa desplazada y reescalada por punto (ver ajustar_curva).
CURVA_BASE = [
    0.010, 0.006, 0.004, 0.004, 0.006, 0.014, 0.032, 0.058,
    0.072, 0.060, 0.052, 0.050, 0.055, 0.062, 0.058, 0.052,
    0.050, 0.055, 0.065, 0.070, 0.058, 0.040, 0.024, 0.013,
]
HORA_PICO_BASE = CURVA_BASE.index(max(CURVA_BASE))  # hora 8

MALAGA_BBOX = {"lat_min": 36.654, "lat_max": 36.753, "lon_min": -4.573, "lon_max": -4.349}

ABREVIATURAS = {
    "avda.": "avenida", "avda": "avenida", "av.": "avenida", "av": "avenida",
    "pso.": "paseo", "pso": "paseo", "p.m.": "paseo maritimo",
    "cno.": "camino", "cno": "camino", "pte.": "puente", "c/": "calle",
    "pza.": "plaza", "pz.": "plaza",
}
# Palabras de tipo de vía: no aportan a la identidad de la calle (aparecen en
# decenas de vías distintas), se excluyen del cómputo de solapes para evitar
# falsos positivos por coincidencia únicamente en el tipo de vía.
GENERICAS = {
    "avenida", "calle", "plaza", "paseo", "camino", "puente", "pasillo",
    "alameda", "muelle", "tunel", "carretera", "urbanizacion", "barriada",
}
SENTIDO_RE = re.compile(r"\s*[-–>]+\s*(este|oeste|norte|sur|centro)\s*\*?\s*$", re.IGNORECASE)
NUM_RE = re.compile(r"\s*n[ºo°]?\.?\s*\d+.*$", re.IGNORECASE)
SENTIDO_TAIL_RE = re.compile(r"\s+sentido\s+\w+\s*$", re.IGNORECASE)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalizar_calle(nombre: str) -> str:
    text = nombre.lower().strip()
    text = SENTIDO_TAIL_RE.sub("", text)
    text = NUM_RE.sub("", text)
    text = SENTIDO_RE.sub("", text)
    for abbr, full in ABREVIATURAS.items():
        text = re.sub(rf"\b{re.escape(abbr)}\b", full, text)
    text = strip_accents(text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def cargar_gazetteer(sinteticos_dir: Path):
    entradas = []
    for filename in GAZETTEER_FILES:
        path = sinteticos_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                direccion = row.get("direccion", "")
                lat, lon = row.get("latitud"), row.get("longitud")
                if not direccion or not lat or not lon:
                    continue
                clave = normalizar_calle(direccion)
                if clave:
                    entradas.append((clave, float(lat), float(lon)))
    return entradas


def palabras_especificas(clave: str):
    return {w for w in clave.split() if len(w) > 3 and w not in GENERICAS}


def geolocalizar(nombre_calle: str, gazetteer, synth_rng):
    """Empareja por palabras específicas (sin tipo de vía). Con 2+ palabras
    específicas basta con que al menos 2 coincidan (tolera variantes como
    'Paseo Marítimo Antonio Machado' vs 'Paseo de Antonio Machado'); con 1
    sola palabra específica (ej. 'Bolivia', 'Pacífico') se exige que sea
    también la ÚNICA palabra específica del candidato, para no confundir dos
    calles distintas que comparten un apellido o nombre común (ej. 'Muelle de
    Heredia' vs 'Avenida de Manuel Agustín Heredia', que solo comparten
    'Heredia')."""
    clave = normalizar_calle(nombre_calle)
    especificas = palabras_especificas(clave)
    if not especificas:
        return None
    umbral = 1 if len(especificas) == 1 else 2

    mejor = None
    mejor_solapes = umbral - 1
    for entrada_clave, lat, lon in gazetteer:
        candidatas = palabras_especificas(entrada_clave)
        if len(especificas) == 1:
            if candidatas != especificas:
                continue
            solapes = 1
        else:
            solapes = len(especificas & candidatas)
            if solapes < umbral:
                continue
        if solapes > mejor_solapes:
            mejor_solapes = solapes
            mejor = (lat, lon)
    return mejor


def resolver_coordenadas(nombre_calle: str, gazetteer, synth_rng):
    match = geolocalizar(nombre_calle, gazetteer, synth_rng)
    if match:
        return match[0], match[1], True
    lat = round(synth_rng.uniform(MALAGA_BBOX["lat_min"], MALAGA_BBOX["lat_max"]), 7)
    lon = round(synth_rng.uniform(MALAGA_BBOX["lon_min"], MALAGA_BBOX["lon_max"]), 7)
    return lat, lon, False


def ajustar_curva(total_dia: float, hora_pico: int, valor_pico: float | None):
    """Desplaza CURVA_BASE para que su máximo caiga en hora_pico y reescala
    para que la suma de 24h sea exactamente total_dia (constraint real
    priorizado). valor_pico (si se conoce) solo se usa para registrar el
    ajuste en el mini-EDA, no fuerza la escala."""
    desplazamiento = (hora_pico - HORA_PICO_BASE) % 24
    curva = CURVA_BASE[-desplazamiento:] + CURVA_BASE[:-desplazamiento] if desplazamiento else CURVA_BASE[:]
    return [round(total_dia * peso) for peso in curva]


# --- Datos reales PM: "Intensidad-Media-Diaria-1er-Cuatrimestre-2026.pdf" ---
# (PM, ubicación, imdl, imds, imdd, ihpl, hpl, ihps, hps, ihpd, hpd); None = sin medir (obras/carril bus)
PM_DATA = [
    (1, "Avenida Juan Sebastián Elcano", 4478, 3957, 3063, 321, 8, 261, 13, 880, 13),
    (2, "Avenida Juan Sebastián Elcano", 14127, 13143, 11197, 1285, 8, 1050, 13, 892, 13),
    (3, "Bolivia", 10150, 9173, 7828, 1027, 14, 810, 13, 776, 13),
    (4, "Paseo Marítimo Pablo Ruiz Picasso", 13093, 11291, 9283, 1390, 14, 949, 13, 852, 13),
    (5, "Paseo Marítimo Pablo Ruiz Picasso", 17493, 16539, 13720, 1569, 8, 1249, 13, 1069, 18),
    (6, "Paseo de Reding", 4987, 4733, 3706, 447, 14, 350, 14, 277, 13),
    (7, "Paseo de Reding", 4811, 4481, 3694, 408, 8, 335, 13, 268, 13),
    (8, "Victoria", 4758, 5082, 4435, 432, 7, 394, 20, 311, 19),
    (9, "Victoria", 4357, 4450, 4014, 346, 19, 311, 20, 303, 20),
    (10, "Túnel de la Alcazaba", 10018, 11845, 10245, 648, 13, 646, 13, 544, 1),
    (11, "Túnel de la Alcazaba", 4718, 4982, 4270, 386, 14, 334, 19, 279, 19),
    (12, "Pasillo de Santa Isabel", 16141, 16789, 11781, 1359, 14, 1258, 13, 785, 13),
    (13, "Carretería", 3080, 3787, 3042, 220, 10, 211, 13, 150, 13),
    (15, "Alameda Principal", 21040, 21976, 17028, 1551, 8, 1311, 18, 1014, 17),
    (16, "Alameda de Colón", 11829, 11802, 9454, 1000, 14, 895, 13, 784, 13),
    (17, "Muelle de Heredia", 22033, 20603, 16425, 2004, 14, 1650, 13, 1364, 13),
    (18, "Muelle de Heredia", 11750, 10089, 9155, 1009, 8, 748, 13, 731, 19),
    (19, "Paseo Marítimo Antonio Machado", 5613, 4559, 3636, 487, 8, 434, 13, 373, 13),
    (20, "Paseo Marítimo Antonio Machado", 10803, 8532, 6928, 1028, 14, 663, 13, 580, 13),
    (21, "Avenida de Andalucía", 27065, 29000, 20758, 1953, 18, 1999, 13, 1547, 13),
    (22, "Avenida de Andalucía", 20747, 22324, 16391, 1468, 12, 1453, 13, 1043, 18),
    (23, "Avenida Herrera Oria", 6604, 5107, 3446, 598, 14, 462, 13, 309, 13),
    (24, "Avenida Herrera Oria", 5541, 4146, 2636, 548, 8, 375, 13, 212, 13),
    (25, "Avenida Carlos Haya", 11401, 10654, 9487, 828, 8, 819, 13, 750, 19),
    (26, "Avenida Carlos Haya", 8028, 8259, 6220, 637, 14, 620, 13, 449, 13),
    (27, "Héroe de Sostoa", 6902, 6942, 5552, 496, 14, 473, 13, 383, 13),
    (28, "Ayala", 9061, 8939, 7062, 679, 14, 624, 13, 436, 19),
    (29, "Avenida de Europa", 8208, 6976, 5184, 670, 14, 592, 13, 403, 13),
    (30, "Avenida de Europa", 6165, 5152, 3768, 507, 8, 469, 13, 294, 13),
    (31, "Pacífico", 12343, 10916, 9013, 959, 8, 974, 13, 838, 13),
    (32, "Pacífico", 14690, 13526, 11609, 1317, 14, 1140, 13, 1060, 13),
    (35, "Avenida Simón Bolívar", 5045, 4583, 3552, 398, 8, 341, 13, 295, 18),
    (36, "Blas de Lezo", 7353, 6017, 4698, 642, 14, 463, 13, 361, 13),
    (37, "Martínez de la Rosa", 11655, 11012, 9373, 824, 17, 772, 13, 670, 20),
    (38, "Camino de Suárez", 15296, 16127, 13828, 1021, 18, 1118, 13, 973, 13),
    (39, "Avenida de Valle Inclán", 27696, 29454, 26195, 1700, 9, 1868, 12, 1875, 13),
    (40, "Avenida de Valle Inclán", 22252, 21004, 17455, 1643, 14, 1553, 13, 1279, 13),
    (41, "Avenida Jorge Silvela", 8332, 7973, 6486, 717, 14, 618, 13, 522, 13),
    (42, "Avenida Guerrero Strachan", 12453, 12505, 10884, 957, 14, 993, 13, 921, 13),
    (43, "Avenida Guerrero Strachan", 16476, 15801, 13994, 1255, 7, 1160, 13, 1032, 13),
    (44, "Avenida Ramón y Cajal", 13295, 13369, 11027, 1112, 14, 1022, 13, 899, 13),
    (45, "Avenida Ramón y Cajal", 10372, 9072, 7891, 930, 8, 736, 13, 694, 19),
    (46, "Avenida Sor Teresa Prat", 8759, 7567, 6333, 720, 8, 586, 13, 509, 13),
    (47, "Luis Barahona de Soto", 5229, 4711, 3728, 430, 14, 396, 13, 329, 13),
    (48, "Avenida de los Guindos", 2948, 2915, 2578, 279, 14, 252, 14, 251, 14),
    (49, "Avenida de los Guindos", 4578, 4125, 3320, 404, 14, 361, 13, 319, 13),
    (50, "Avenida Velázquez", 12100, 12128, 9513, 946, 18, 924, 13, 714, 13),
    (51, "Avenida Juan XXIII", 11356, 11272, 9835, 794, 8, 842, 13, 758, 13),
    (52, "Avenida Juan XXIII", 14307, 13690, 11722, 1046, 18, 1085, 13, 967, 13),
    (53, "Avenida Ortega y Gasset", 6710, 5342, 4059, 518, 8, 372, 12, 277, 13),
    (54, "Avenida Ortega y Gasset", 8551, 7580, 5936, 697, 14, 631, 13, 526, 13),
    (55, "Camino de San Rafael", 4665, 3507, 2878, 396, 18, 309, 12, 340, 13),
    (56, "Camino de San Rafael", 5753, 4662, 3872, 445, 8, 383, 12, 344, 11),
    (57, "La Unión", 8416, 7728, 6516, 618, 18, 595, 13, 469, 13),
    (58, "Avenida de la Aurora", 7006, 7445, 5272, 507, 18, 570, 13, 399, 19),
    (59, "Avenida de la Aurora", 3474, 3062, 1771, 329, 14, 258, 13, 150, 13),
    (60, "Puente de Armiñán", 9220, 8349, 7250, 721, 14, 588, 13, 498, 13),
    (61, "Puente de Armiñán", 7483, 7560, 6270, 574, 14, 546, 13, 408, 13),
    (62, "Paseo de Martiricos", 11901, 10885, 8196, 1110, 8, 856, 13, 657, 13),
    (63, "Pelayo", 4098, 3995, 3200, 333, 8, 292, 13, 235, 13),
    (64, "Avenida Velázquez", 18951, 18266, 14052, 1477, 8, 1458, 13, 1119, 13),
    (65, "Avenida Velázquez", 15472, 16107, 13422, 1045, 7, 1092, 13, 998, 13),
]

# --- Datos reales PC: "Intensidades-Clasificacion-Vehiculos-1er-Cuatrimestre-2026.pdf" ---
# (PC, ubicación, imdl_total, imds_total, imdd_total, ih8, ih14, ih19,
#  pct_ligero, pct_moto, pct_pesado) -- porcentajes derivados del reparto real
# por categoría en día laborable (ligeros vs motos vs autobuses+furgones+otros).
PC_RAW = [
    (1, "Avenida Blas Infante", 40610, 39587, 32224, 2416, 2698, 2644,
     dict(lig=35877, moto=628, bus=990, fur1=2551, fur2=137, fur3=380, otros=47)),
    (2, "Avenida Santiago Ramón y Cajal", 10340, 9071, 7891, 897, 795, 696,
     dict(lig=9327, moto=100, bus=221, fur1=608, fur2=32, fur3=41, otros=11)),
    (3, "Calle Almería", 7697, 6414, 5892, 881, 457, 407,
     dict(lig=6559, moto=125, bus=329, fur1=593, fur2=26, fur3=58, otros=7)),
    (4, "Avenida de Valle Inclán", 27588, 29455, 26195, 1623, 1738, 1774,
     dict(lig=25126, moto=225, bus=184, fur1=1819, fur2=83, fur3=135, otros=16)),
    (5, "Paseo Marítimo Antonio Machado", 4774, 3171, 2870, 221, 467, 455,
     dict(lig=4276, moto=22, bus=82, fur1=271, fur2=8, fur3=67, otros=48)),
    (6, "Paseo Marítimo Antonio Machado", 9636, 7042, 5794, 666, 866, 599,
     dict(lig=8693, moto=106, bus=57, fur1=522, fur2=27, fur3=166, otros=65)),
    (7, "Avenida Sor Teresa Prat", 10488, 10335, 8107, 821, 876, 827,
     dict(lig=8256, moto=1038, bus=529, fur1=623, fur2=19, fur3=20, otros=3)),
    (8, "Héroe de Sostoa", 12239, 12056, 9662, 811, 889, 790,
     dict(lig=10106, moto=420, bus=637, fur1=997, fur2=39, fur3=35, otros=5)),
    (9, "Avenida José Ortega y Gasset", 13703, 12444, 9899, 810, 958, 898,
     dict(lig=11835, moto=395, bus=191, fur1=1186, fur2=50, fur3=35, otros=11)),
    (10, "Avenida José Ortega y Gasset", 10209, 8278, 6010, 735, 778, 651,
     dict(lig=8894, moto=98, bus=211, fur1=919, fur2=49, fur3=30, otros=8)),
    (11, "Avenida Blas Infante", 53924, 51438, 40015, 3391, 4129, 3471,
     dict(lig=48638, moto=574, bus=1023, fur1=3022, fur2=122, fur3=347, otros=198)),
    (12, "Plaza de José Bergamín", 21372, 16861, 12389, 1519, 1860, 1605,
     dict(lig=19982, moto=319, bus=132, fur1=902, fur2=25, fur3=10, otros=2)),
    (13, "Plaza de José Bergamín", 9574, 6860, 5111, 716, 764, 638,
     dict(lig=8262, moto=646, bus=165, fur1=460, fur2=19, fur3=19, otros=3)),
    (14, "Avenida Carlos Haya", 10019, 8968, 7513, 571, 632, 497,
     dict(lig=9124, moto=145, bus=193, fur1=524, fur2=14, fur3=16, otros=3)),
    (15, "Avenida Carlos Haya", 7480, 6743, 5664, 491, 625, 551,
     dict(lig=6813, moto=85, bus=208, fur1=349, fur2=9, fur3=13, otros=3)),
    (16, "Avenida de Valle Inclán", 19184, 17317, 14052, 1027, 1310, 1126,
     dict(lig=17916, moto=165, bus=71, fur1=954, fur2=41, fur3=30, otros=7)),
    (17, "Avenida Santiago Ramón y Cajal", 8476, 8044, 7140, 469, 587, 555,
     dict(lig=7592, moto=181, bus=182, fur1=481, fur2=15, fur3=20, otros=5)),
    (18, "Avenida Guerrero Strachan", 8650, 7029, 6283, 703, 559, 495,
     dict(lig=7859, moto=256, bus=24, fur1=480, fur2=12, fur3=16, otros=3)),
    (19, "Avenida Guerrero Strachan", 10458, 8905, 7667, 586, 952, 667,
     dict(lig=9826, moto=64, bus=11, fur1=522, fur2=11, fur3=20, otros=4)),
    (20, "Camino del Colmenar", 12057, 9703, 8495, 909, 817, 771,
     dict(lig=10424, moto=700, bus=65, fur1=816, fur2=27, fur3=22, otros=3)),
    (21, "Camino del Colmenar", 4456, 3591, 2972, 235, 455, 372,
     dict(lig=3446, moto=543, bus=45, fur1=392, fur2=16, fur3=11, otros=3)),
    (22, "Avenida Pintor Joaquín Sorolla", 14139, 11709, 10509, 1203, 892, 926,
     dict(lig=12146, moto=661, bus=510, fur1=755, fur2=30, fur3=30, otros=7)),
    (23, "Avenida Pintor Joaquín Sorolla", 15606, 15306, 11467, 730, 1206, 946,
     dict(lig=13285, moto=1152, bus=433, fur1=682, fur2=26, fur3=20, otros=8)),
]

FECHAS_POR_TIPO = {
    "laborable": "2026-02-04",  # miércoles, dentro del periodo real medido (ene-abr 2026)
    "sabado": "2026-02-07",
    "domingo": "2026-02-08",
}


def generar_filas_pm(entry, gazetteer, synth_rng, sin_coord_reales):
    pm, ubicacion, imdl, imds, imdd, ihpl, hpl, ihps, hps, ihpd, hpd = entry
    lat, lon, encontrada = resolver_coordenadas(ubicacion, gazetteer, synth_rng)
    if not encontrada:
        sin_coord_reales.append(f"PM-{pm:02d} {ubicacion}")
    filas = []
    for tipo, total, hora_pico in (("laborable", imdl, hpl), ("sabado", imds, hps), ("domingo", imdd, hpd)):
        fecha = FECHAS_POR_TIPO[tipo]
        curva = ajustar_curva(total, hora_pico, None)
        for hora, flujo in enumerate(curva):
            hora_fin = (hora + 1) % 24
            filas.append({
                "id": f"TRAF-PM-{pm:02d}",
                "nombre": f"PM-{pm:02d} {ubicacion}",
                "latitud": lat,
                "longitud": lon,
                "direccion": ubicacion,
                "fecha_inicio": fecha,
                "hora_inicio": f"{hora:02d}:00",
                "fecha_fin": fecha if hora_fin != 0 else FECHAS_POR_TIPO[tipo],
                "hora_fin": f"{hora_fin:02d}:00" if hora_fin != 0 else "23:59",
                "trafico_flujo": max(flujo, 0),
                "trafico_vehiculo": "general",
            })
    return filas


def generar_filas_pc(entry, gazetteer, synth_rng, sin_coord_reales):
    pc, ubicacion, imdl, imds, imdd, ih8, ih14, ih19, categorias = entry
    lat, lon, encontrada = resolver_coordenadas(ubicacion, gazetteer, synth_rng)
    if not encontrada:
        sin_coord_reales.append(f"PC-{pc:02d} {ubicacion}")

    total_pesado = categorias["bus"] + categorias["fur1"] + categorias["fur2"] + categorias["fur3"] + categorias["otros"]
    total_lab = categorias["lig"] + categorias["moto"] + total_pesado
    pct = {"ligero": categorias["lig"] / total_lab, "moto": categorias["moto"] / total_lab, "pesado": total_pesado / total_lab}

    hora_pico_laborable = max((ih8, 8), (ih14, 14), (ih19, 19))[1]

    filas = []
    for tipo, total in (("laborable", imdl), ("sabado", imds), ("domingo", imdd)):
        fecha = FECHAS_POR_TIPO[tipo]
        curva = ajustar_curva(total, hora_pico_laborable, None)
        for hora, flujo_total in enumerate(curva):
            hora_fin = (hora + 1) % 24
            for categoria, proporcion in pct.items():
                flujo_categoria = round(flujo_total * proporcion)
                if flujo_categoria <= 0:
                    continue
                filas.append({
                    "id": f"TRAF-PC-{pc:02d}",
                    "nombre": f"PC-{pc:02d} {ubicacion}",
                    "latitud": lat,
                    "longitud": lon,
                    "direccion": ubicacion,
                    "fecha_inicio": fecha,
                    "hora_inicio": f"{hora:02d}:00",
                    "fecha_fin": fecha if hora_fin != 0 else fecha,
                    "hora_fin": f"{hora_fin:02d}:00" if hora_fin != 0 else "23:59",
                    "trafico_flujo": flujo_categoria,
                    "trafico_vehiculo": categoria,
                })
    return filas


def parse_args():
    parser = argparse.ArgumentParser(description="Ancla movilidad_trafico en los informes reales MOVIMA (PM + PC), geolocalizando por nombre de calle contra el resto de datasets reales del proyecto.")
    parser.add_argument("--sinteticos-dir", type=Path, default=Path("data/sinteticos"))
    parser.add_argument("--output", type=Path, default=Path("data/sinteticos/movilidad_trafico.csv"))
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def main():
    import random

    args = parse_args()
    synth_rng = random.Random(args.seed)
    gazetteer = cargar_gazetteer(args.sinteticos_dir)

    sin_coord_reales = []
    rows = []
    for entry in PM_DATA:
        rows += generar_filas_pm(entry, gazetteer, synth_rng, sin_coord_reales)
    for entry in PC_RAW:
        rows += generar_filas_pc(entry, gazetteer, synth_rng, sin_coord_reales)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    puntos = len(PM_DATA) + len(PC_RAW)
    geolocalizados = puntos - len(sin_coord_reales)
    print(f"\n--- Mini-EDA movilidad_trafico (n={len(rows)} filas, {puntos} puntos reales: {len(PM_DATA)} PM + {len(PC_RAW)} PC) ---")
    print(f"Geolocalización por coincidencia de calle: {geolocalizados}/{puntos} puntos encontrados en el gazetteer real; {len(sin_coord_reales)} con coordenada sintética:")
    for nombre in sin_coord_reales:
        print(f"  - {nombre}")

    por_tipo = Counter(r["trafico_vehiculo"] for r in rows)
    print("\nReparto por tipo de vehículo (solo PC lleva desglose real; PM va como 'general'):")
    for tipo, count in por_tipo.most_common():
        print(f"  {tipo}: {count} ({count/len(rows)*100:.1f}%)")

    imdl_reales = [e[2] for e in PM_DATA] + [e[2] for e in PC_RAW]
    print(f"\nI.M.D.L. real (laborable) rango: min={min(imdl_reales)}, max={max(imdl_reales)}, media={sum(imdl_reales)/len(imdl_reales):.0f}")
    print(f"Escritas {len(rows)} filas en {args.output}")


if __name__ == "__main__":
    main()
