# Script: generar_datos_sinteticos_multidimension.py
# Genera CSVs sintéticos coordinados espacial y temporalmente para las 6 dimensiones de EDINT.

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

# Ubicaciones clave coordinadas (Málaga centro)
LOCATIONS = [
    {"direccion": "Calle Marqués de Larios", "latitud": 36.7196, "longitud": -4.4214},
    {"direccion": "Avenida de Andalucía", "latitud": 36.7180, "longitud": -4.4280},
    {"direccion": "Alameda Principal", "latitud": 36.7185, "longitud": -4.4230},
    {"direccion": "Calle Mayor", "latitud": 36.7205, "longitud": -4.4200},
    {"direccion": "Paseo del Parque", "latitud": 36.7190, "longitud": -4.4160},
    {"direccion": "Calle Granada", "latitud": 36.7215, "longitud": -4.4190},
    {"direccion": "Plaza de la Marina", "latitud": 36.7175, "longitud": -4.4205},
    {"direccion": "Calle Alameda Colón", "latitud": 36.7160, "longitud": -4.4250},
]


def generate_all(seed=42, output_dir=Path("data/sinteticos")):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    base_date = datetime(2026, 8, 1, 8, 0)

    # 1. Afectaciones Urbanas
    afectaciones = []
    tipos_int = ["obra", "evento", "actuacion_municipal", "mantenimiento"]
    tipos_afec = ["corte_trafico", "ocupacion_calzada", "restriccion_paso", "ocupacion_acera"]
    for i in range(1, 101):
        loc = rng.choice(LOCATIONS)
        begin = base_date + timedelta(days=rng.randint(0, 25), hours=rng.randint(0, 10))
        end = begin + timedelta(hours=rng.randint(2, 48))
        afectaciones.append({
            "id": f"AFEC-{i:04d}",
            "nombre": f"Intervención {loc['direccion']}",
            "descripcion": f"Trabajos de adecuación en {loc['direccion']}",
            "direccion": loc["direccion"],
            "latitud": f"{loc['latitud'] + rng.uniform(-0.0005, 0.0008):.6f}",
            "longitud": f"{loc['longitud'] + rng.uniform(-0.0005, 0.0008):.6f}",
            "fecha_inicio": begin.strftime("%Y-%m-%d"),
            "hora_inicio": begin.strftime("%H:%M"),
            "fecha_fin": end.strftime("%Y-%m-%d"),
            "hora_fin": end.strftime("%H:%M"),
            "tipo_intervencion": rng.choice(tipos_int),
            "tipo_afectacion": rng.choice(tipos_afec),
            "titularidad": rng.choice(["municipal", "autonomica", "estatal"]),
            "impacto_pmr": rng.choice(["si", "no"]),
            "notas": "Generado sintéticamente",
        })
    _write_csv(output_dir / "gestion_afectaciones_urbanas.csv", afectaciones[0].keys(), afectaciones)

    # 2. Control y Gestión ITS
    its = []
    cats = ["panel_mensaje_variable", "semaforo", "camara_trafico", "radar_trafico"]
    for i in range(1, 81):
        loc = rng.choice(LOCATIONS)
        cat = rng.choice(cats)
        its.append({
            "id": f"ITS-{i:04d}",
            "categoria": cat,
            "nombre": f"{cat.upper()} {loc['direccion']}",
            "descripcion": f"Dispositivo ITS operativo en {loc['direccion']}",
            "direccion": loc["direccion"],
            "latitud": f"{loc['latitud'] + rng.uniform(-0.0004, 0.0004):.6f}",
            "longitud": f"{loc['longitud'] + rng.uniform(-0.0004, 0.0004):.6f}",
            "titularidad": rng.choice(["municipal", "concesionada"]),
        })
    _write_csv(output_dir / "control_gestion_its.csv", its[0].keys(), its)

    # 3. Ocupación Permanente
    ocupacion = []
    tipos_oc = ["terraza", "quiosco", "puesto", "concesion"]
    for i in range(1, 90):
        loc = rng.choice(LOCATIONS)
        ocupacion.append({
            "id": f"OCUP-{i:04d}",
            "nombre_establecimiento": f"Establecimiento {loc['direccion']} {i}",
            "tipo_ocupacion": rng.choice(tipos_oc),
            "direccion": loc["direccion"],
            "latitud": f"{loc['latitud'] + rng.uniform(-0.0003, 0.0003):.6f}",
            "longitud": f"{loc['longitud'] + rng.uniform(-0.0003, 0.0003):.6f}",
            "ocupacion_longitud": str(rng.randint(5, 25)),
            "ocupacion_superficie": str(rng.randint(12, 60)),
            "titularidad": "privada",
            "estado_autorizacion": rng.choice(["activa", "activa", "activa", "en_revision"]),
            "fecha_inicio_autorizacion": "2026-01-01",
            "fecha_fin_autorizacion": "2026-12-31",
            "notas": "Autorización municipal vigente",
        })
    _write_csv(output_dir / "ocupacion_permanente.csv", ocupacion[0].keys(), ocupacion)

    # 4. Movilidad Tráfico
    trafico = []
    for i in range(1, 150):
        loc = rng.choice(LOCATIONS)
        begin = base_date + timedelta(days=rng.randint(0, 10), hours=rng.randint(0, 12))
        end = begin + timedelta(hours=2)
        flujo = rng.randint(120, 1800)
        trafico.append({
            "id": f"TRAF-{i:04d}",
            "nombre": f"Medición Tráfico {loc['direccion']}",
            "latitud": f"{loc['latitud']:.6f}",
            "longitud": f"{loc['longitud']:.6f}",
            "direccion": loc["direccion"],
            "fecha_inicio": begin.strftime("%Y-%m-%d"),
            "hora_inicio": begin.strftime("%H:%M"),
            "fecha_fin": end.strftime("%Y-%m-%d"),
            "hora_fin": end.strftime("%H:%M"),
            "trafico_flujo": str(flujo),
            "trafico_vehiculo": rng.choice(["general", "ligero", "pesado", "moto"]),
        })
    _write_csv(output_dir / "movilidad_trafico.csv", trafico[0].keys(), trafico)

    # 5. Movilidad Parking
    parking = []
    for i in range(1, 120):
        loc = rng.choice(LOCATIONS)
        dt = base_date + timedelta(days=rng.randint(0, 10), hours=rng.randint(0, 14))
        total = rng.choice([250, 400, 600, 800])
        libres = rng.randint(5, int(total * 0.35))
        parking.append({
            "id": f"PARK-{i:04d}",
            "nombre": f"Parking Publico {loc['direccion']}",
            "latitud": f"{loc['latitud']:.6f}",
            "longitud": f"{loc['longitud']:.6f}",
            "direccion": loc["direccion"],
            "fecha": dt.strftime("%Y-%m-%d"),
            "hora": dt.strftime("%H:%M"),
            "ocupacion_total": str(total),
            "ocupacion_libres": str(libres),
        })
    _write_csv(output_dir / "movilidad_parking.csv", parking[0].keys(), parking)

    # 6. Movilidad Plazas Reservadas
    plazas = []
    tipos_pl = ["electrico_recarga", "carga_descarga", "carga_descarga", "moto", "bicicleta", "taxi"]
    for i in range(1, 100):
        loc = rng.choice(LOCATIONS)
        plazas.append({
            "id": f"PLAZ-{i:04d}",
            "nombre": f"Reserva {loc['direccion']}",
            "latitud": f"{loc['latitud'] + rng.uniform(-0.0003, 0.0003):.6f}",
            "longitud": f"{loc['longitud'] + rng.uniform(-0.0003, 0.0003):.6f}",
            "direccion": loc["direccion"],
            "tipo_plaza": rng.choice(tipos_pl),
            "plaza_numero": str(rng.randint(1, 8)),
            "plaza_longitud": str(rng.randint(6, 18)),
        })
    _write_csv(output_dir / "movilidad_plazas_reservadas.csv", plazas[0].keys(), plazas)

    print(f"CSVs sintéticos generados en {output_dir}")


def _write_csv(filepath, fieldnames, data):
    with Path(filepath).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)


if __name__ == "__main__":
    generate_all()
