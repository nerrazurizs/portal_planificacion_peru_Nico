"""Tarifario B2B de transporte — costos desde Santiago a 25 bases Chile.

Source: TARIFARIO B2B DOREL - 09-01-2025.xlsx (proveedor logístico).
Reutilizable por: redistribucion, plan_compras, capacidad_volumetrica, etc.

Reglas de negocio
-----------------
- Costo se cobra por MAX(peso_fisico, peso_volumetrico).
  Peso volumétrico = (largo × ancho × alto) / 4000  (cm → kg).
- Origen distinto a SANTIAGO → tarifa × 1.2
- Destino radio EXTREMO → tarifa × 1.5
- Mismo origen/destino → tarifa local (sin recargo).
"""

from __future__ import annotations

# ============================================================================
# CONSTANTS
# ============================================================================

SURCHARGE_NON_SANTIAGO: float = 1.2   # origen fuera de Santiago
SURCHARGE_EXTREMA: float = 1.5       # destino radio EXTREMA
CD_BASE: str = "SANTIAGO"             # base del centro de distribución

# ============================================================================
# TARIFF RATES — Per-kg brackets (CLP/kg), origin = Santiago
# ============================================================================
# Bracket keys: "100-500", "500-2000", "2000-4000", "4000-6000", ">6000"
# These are the most useful for scoring (per-unit cost estimation).

TARIFA_POR_KG: dict[str, dict[str, int]] = {
    "ARICA":        {"100-500": 325, "500-2000": 299, "2000-4000": 270, "4000-6000": 244, ">6000": 227},
    "IQUIQUE":      {"100-500": 308, "500-2000": 292, "2000-4000": 264, "4000-6000": 238, ">6000": 219},
    "CALAMA":       {"100-500": 308, "500-2000": 292, "2000-4000": 264, "4000-6000": 238, ">6000": 219},
    "ANTOFAGASTA":  {"100-500": 284, "500-2000": 275, "2000-4000": 247, "4000-6000": 225, ">6000": 206},
    "COPIAPO":      {"100-500": 192, "500-2000": 178, "2000-4000": 167, "4000-6000": 160, ">6000": 157},
    "LA SERENA":    {"100-500": 144, "500-2000": 131, "2000-4000": 124, "4000-6000": 123, ">6000": 120},
    "OVALLE":       {"100-500": 144, "500-2000": 131, "2000-4000": 124, "4000-6000": 123, ">6000": 120},
    "LA CALERA":    {"100-500": 127, "500-2000": 124, "2000-4000": 122, "4000-6000": 119, ">6000": 116},
    "LOS ANDES":    {"100-500": 127, "500-2000": 124, "2000-4000": 122, "4000-6000": 119, ">6000": 116},
    "VINA DEL MAR": {"100-500": 117, "500-2000": 114, "2000-4000": 110, "4000-6000": 107, ">6000": 105},
    "SANTIAGO":     {"100-500": 103, "500-2000": 100, "2000-4000":  96, "4000-6000":  92, ">6000":  83},
    "MELIPILLA":    {"100-500": 117, "500-2000": 114, "2000-4000": 110, "4000-6000": 107, ">6000": 105},
    "RANCAGUA":     {"100-500": 125, "500-2000": 122, "2000-4000": 119, "4000-6000": 115, ">6000": 113},
    "CURICO":       {"100-500": 128, "500-2000": 125, "2000-4000": 122, "4000-6000": 120, ">6000": 117},
    "TALCA":        {"100-500": 128, "500-2000": 125, "2000-4000": 122, "4000-6000": 120, ">6000": 117},
    "CHILLAN":      {"100-500": 149, "500-2000": 142, "2000-4000": 135, "4000-6000": 130, ">6000": 128},
    "CONCEPCION":   {"100-500": 149, "500-2000": 142, "2000-4000": 135, "4000-6000": 130, ">6000": 128},
    "LOS ANGELES":  {"100-500": 159, "500-2000": 146, "2000-4000": 140, "4000-6000": 134, ">6000": 131},
    "TEMUCO":       {"100-500": 166, "500-2000": 158, "2000-4000": 147, "4000-6000": 137, ">6000": 134},
    "VALDIVIA":     {"100-500": 184, "500-2000": 177, "2000-4000": 167, "4000-6000": 158, ">6000": 155},
    "OSORNO":       {"100-500": 184, "500-2000": 177, "2000-4000": 167, "4000-6000": 158, ">6000": 155},
    "PUERTO MONTT": {"100-500": 182, "500-2000": 175, "2000-4000": 165, "4000-6000": 156, ">6000": 154},
    "CASTRO":       {"100-500": 268, "500-2000": 244, "2000-4000": 220, "4000-6000": 201, ">6000": 196},
    "COYHAIQUE":    {"100-500": 440, "500-2000": 413, "2000-4000": 379, "4000-6000": 380, ">6000": 360},
    "PUNTA ARENAS": {"100-500": 440, "500-2000": 413, "2000-4000": 379, "4000-6000": 380, ">6000": 360},
}

# ============================================================================
# TARIFF RATES — Fixed brackets (flat CLP per shipment), origin = Santiago
# ============================================================================

TARIFA_FIJA: dict[str, dict[str, int]] = {
    "ARICA":        {"0-20": 16489, "20-60": 20611, "60-80": 25904, "80-100": 32500},
    "IQUIQUE":      {"0-20": 16098, "20-60": 20124, "60-80": 25291, "80-100": 30800},
    "CALAMA":       {"0-20": 16098, "20-60": 20124, "60-80": 25291, "80-100": 30800},
    "ANTOFAGASTA":  {"0-20": 15465, "20-60": 19330, "60-80": 24295, "80-100": 28400},
    "COPIAPO":      {"0-20":  9147, "20-60": 11434, "60-80": 14371, "80-100": 19200},
    "LA SERENA":    {"0-20":  7600, "20-60":  9570, "60-80": 12027, "80-100": 14400},
    "OVALLE":       {"0-20":  7600, "20-60":  9570, "60-80": 12027, "80-100": 14400},
    "LA CALERA":    {"0-20":  7051, "20-60":  8896, "60-80": 11181, "80-100": 12700},
    "LOS ANDES":    {"0-20":  7051, "20-60":  8896, "60-80": 11181, "80-100": 12700},
    "VINA DEL MAR": {"0-20":  6700, "20-60":  8323, "60-80": 10461, "80-100": 11700},
    "SANTIAGO":     {"0-20":  5392, "20-60":  6740, "60-80":  8471, "80-100": 10300},
    "MELIPILLA":    {"0-20":  6700, "20-60":  8323, "60-80": 10461, "80-100": 11700},
    "RANCAGUA":     {"0-20":  6900, "20-60":  8797, "60-80": 10847, "80-100": 12500},
    "CURICO":       {"0-20":  7372, "20-60":  9215, "60-80": 11581, "80-100": 12800},
    "TALCA":        {"0-20":  7372, "20-60":  9215, "60-80": 11581, "80-100": 12800},
    "CHILLAN":      {"0-20":  7754, "20-60":  9692, "60-80": 12180, "80-100": 14900},
    "CONCEPCION":   {"0-20":  7754, "20-60":  9692, "60-80": 12180, "80-100": 14900},
    "LOS ANGELES":  {"0-20":  7900, "20-60": 10051, "60-80": 12631, "80-100": 15900},
    "TEMUCO":       {"0-20":  8300, "20-60": 10459, "60-80": 13144, "80-100": 16600},
    "VALDIVIA":     {"0-20":  8831, "20-60": 11253, "60-80": 14157, "80-100": 18400},
    "OSORNO":       {"0-20":  8831, "20-60": 11253, "60-80": 14157, "80-100": 18400},
    "PUERTO MONTT": {"0-20":  8757, "20-60": 10947, "60-80": 13757, "80-100": 18200},
    "CASTRO":       {"0-20": 13513, "20-60": 16892, "60-80": 21230, "80-100": 26800},
    "COYHAIQUE":    {"0-20": 22000, "20-60": 28602, "60-80": 36309, "80-100": 44000},
    "PUNTA ARENAS": {"0-20": 22000, "20-60": 28602, "60-80": 36309, "80-100": 44000},
}

# Default per-kg bracket for scoring (most useful for per-unit cost estimation)
DEFAULT_BRACKET: str = "100-500"

# ============================================================================
# ZONA metadata (informational, for grouping in dashboards)
# ============================================================================

BASE_ZONA: dict[str, str] = {
    "ARICA": "EXTREMO NORTE", "IQUIQUE": "EXTREMO NORTE",
    "CALAMA": "EXTREMO NORTE", "ANTOFAGASTA": "EXTREMO NORTE",
    "COPIAPO": "NORTE", "LA SERENA": "NORTE", "OVALLE": "NORTE",
    "LA CALERA": "CENTRO", "LOS ANDES": "CENTRO",
    "VINA DEL MAR": "CENTRO", "MELIPILLA": "CENTRO",
    "RANCAGUA": "CENTRO", "CURICO": "CENTRO", "TALCA": "CENTRO",
    "SANTIAGO": "SANTIAGO",
    "CHILLAN": "SUR", "CONCEPCION": "SUR", "LOS ANGELES": "SUR",
    "TEMUCO": "SUR", "VALDIVIA": "SUR", "OSORNO": "SUR",
    "PUERTO MONTT": "SUR", "CASTRO": "SUR",
    "COYHAIQUE": "EXTREMO SUR", "PUNTA ARENAS": "EXTREMO SUR",
}

# ============================================================================
# COMUNA → BASE mapping (346 comunas from the logistics provider's Excel)
# ============================================================================

COMUNA_TO_BASE: dict[str, dict[str, str]] = {
    "ALGARROBO": {"base": "MELIPILLA", "radio": "BASE"},
    "ALHUE": {"base": "MELIPILLA", "radio": "BASE"},
    "ALTO BIOBIO": {"base": "LOS ANGELES", "radio": "EXTREMA"},
    "ALTO DEL CARMEN": {"base": "LA SERENA", "radio": "EXTREMA"},
    "ALTO HOSPICIO": {"base": "IQUIQUE", "radio": "BASE"},
    "ANCUD": {"base": "CASTRO", "radio": "BASE"},
    "ANDACOLLO": {"base": "LA SERENA", "radio": "BASE"},
    "ANGOL": {"base": "LOS ANGELES", "radio": "BASE"},
    "ANTARTICA": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "ANTOFAGASTA": {"base": "ANTOFAGASTA", "radio": "EXTREMA"},
    "ANTUCO": {"base": "LOS ANGELES", "radio": "EXTREMA"},
    "ARAUCO": {"base": "CONCEPCION", "radio": "BASE"},
    "ARICA": {"base": "ARICA", "radio": "EXTREMA"},
    "AYSEN": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "BUIN": {"base": "SANTIAGO", "radio": "BASE"},
    "BULNES": {"base": "CHILLAN", "radio": "BASE"},
    "CABILDO": {"base": "LA CALERA", "radio": "BASE"},
    "CABO DE HORNOS": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "CABRERO": {"base": "CHILLAN", "radio": "BASE"},
    "CALAMA": {"base": "CALAMA", "radio": "EXTREMA"},
    "CALBUCO": {"base": "PUERTO MONTT", "radio": "BASE"},
    "CALDERA": {"base": "COPIAPO", "radio": "BASE"},
    "CALERA": {"base": "LA CALERA", "radio": "BASE"},
    "CALERA DE TANGO": {"base": "SANTIAGO", "radio": "BASE"},
    "CALLE LARGA": {"base": "LOS ANDES", "radio": "BASE"},
    "CAMARONES": {"base": "ARICA", "radio": "EXTREMA"},
    "CAMINA": {"base": "IQUIQUE", "radio": "EXTREMA"},
    "CANELA": {"base": "LA CALERA", "radio": "EXTREMA"},
    "CANETE": {"base": "CONCEPCION", "radio": "EXTREMA"},
    "CARAHUE": {"base": "TEMUCO", "radio": "EXTREMA"},
    "CARTAGENA": {"base": "MELIPILLA", "radio": "BASE"},
    "CASABLANCA": {"base": "MELIPILLA", "radio": "BASE"},
    "CASTRO": {"base": "CASTRO", "radio": "EXTREMA"},
    "CATEMU": {"base": "LOS ANDES", "radio": "BASE"},
    "CAUQUENES": {"base": "TALCA", "radio": "EXTREMA"},
    "CERRILLOS": {"base": "SANTIAGO", "radio": "BASE"},
    "CERRO NAVIA": {"base": "SANTIAGO", "radio": "BASE"},
    "CHAITEN": {"base": "PUERTO MONTT", "radio": "EXTREMA"},
    "CHANARAL": {"base": "COPIAPO", "radio": "EXTREMA"},
    "CHANCO": {"base": "TALCA", "radio": "EXTREMA"},
    "CHEPICA": {"base": "CURICO", "radio": "BASE"},
    "CHIGUAYANTE": {"base": "CONCEPCION", "radio": "BASE"},
    "CHILE CHICO": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "CHILLAN": {"base": "CHILLAN", "radio": "BASE"},
    "CHILLAN VIEJO": {"base": "CHILLAN", "radio": "BASE"},
    "CHIMBARONGO": {"base": "CURICO", "radio": "BASE"},
    "CHOL CHOL": {"base": "TEMUCO", "radio": "BASE"},
    "CHONCHI": {"base": "CASTRO", "radio": "EXTREMA"},
    "CISNES": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "COBQUECURA": {"base": "CHILLAN", "radio": "EXTREMA"},
    "COCHAMO": {"base": "PUERTO MONTT", "radio": "EXTREMA"},
    "COCHRANE": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "CODEGUA": {"base": "RANCAGUA", "radio": "BASE"},
    "COELEMU": {"base": "CONCEPCION", "radio": "BASE"},
    "COIHUECO": {"base": "CHILLAN", "radio": "BASE"},
    "COINCO": {"base": "RANCAGUA", "radio": "BASE"},
    "COLBUN": {"base": "TALCA", "radio": "BASE"},
    "COLCHANE": {"base": "IQUIQUE", "radio": "EXTREMA"},
    "COLINA": {"base": "SANTIAGO", "radio": "BASE"},
    "COLLIPULLI": {"base": "LOS ANGELES", "radio": "EXTREMA"},
    "COLTAUCO": {"base": "RANCAGUA", "radio": "BASE"},
    "COMBARBALA": {"base": "OVALLE", "radio": "EXTREMA"},
    "CONCEPCION": {"base": "CONCEPCION", "radio": "BASE"},
    "CONCHALI": {"base": "SANTIAGO", "radio": "BASE"},
    "CONCON": {"base": "VINA DEL MAR", "radio": "BASE"},
    "CONSTITUCION": {"base": "TALCA", "radio": "EXTREMA"},
    "CONTULMO": {"base": "CONCEPCION", "radio": "EXTREMA"},
    "COPIAPO": {"base": "COPIAPO", "radio": "EXTREMA"},
    "COQUIMBO": {"base": "LA SERENA", "radio": "BASE"},
    "CORONEL": {"base": "CONCEPCION", "radio": "BASE"},
    "CORRAL": {"base": "VALDIVIA", "radio": "BASE"},
    "COYHAIQUE": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "CUNCO": {"base": "TEMUCO", "radio": "EXTREMA"},
    "CURACAUTIN": {"base": "TEMUCO", "radio": "EXTREMA"},
    "CURACAVI": {"base": "MELIPILLA", "radio": "BASE"},
    "CURACO DE VELEZ": {"base": "CASTRO", "radio": "EXTREMA"},
    "CURANILAHUE": {"base": "CONCEPCION", "radio": "EXTREMA"},
    "CURARREHUE": {"base": "TEMUCO", "radio": "EXTREMA"},
    "CUREPTO": {"base": "CURICO", "radio": "EXTREMA"},
    "CURICO": {"base": "CURICO", "radio": "BASE"},
    "DALCAHUE": {"base": "CASTRO", "radio": "BASE"},
    "DIEGO DE ALMAGRO": {"base": "COPIAPO", "radio": "EXTREMA"},
    "DONIHUE": {"base": "RANCAGUA", "radio": "BASE"},
    "EL BOSQUE": {"base": "SANTIAGO", "radio": "BASE"},
    "EL CARMEN": {"base": "CHILLAN", "radio": "BASE"},
    "EL MONTE": {"base": "MELIPILLA", "radio": "BASE"},
    "EL QUISCO": {"base": "MELIPILLA", "radio": "BASE"},
    "EL TABO": {"base": "MELIPILLA", "radio": "BASE"},
    "EMPEDRADO": {"base": "TALCA", "radio": "EXTREMA"},
    "ERCILLA": {"base": "TEMUCO", "radio": "BASE"},
    "ESTACION CENTRAL": {"base": "SANTIAGO", "radio": "BASE"},
    "FLORIDA": {"base": "CONCEPCION", "radio": "BASE"},
    "FREIRE": {"base": "TEMUCO", "radio": "BASE"},
    "FREIRINA": {"base": "LA SERENA", "radio": "EXTREMA"},
    "FRESIA": {"base": "PUERTO MONTT", "radio": "BASE"},
    "FRUTILLAR": {"base": "OSORNO", "radio": "BASE"},
    "FUTALEUFU": {"base": "PUERTO MONTT", "radio": "EXTREMA"},
    "FUTRONO": {"base": "VALDIVIA", "radio": "EXTREMA"},
    "GALVARINO": {"base": "TEMUCO", "radio": "BASE"},
    "GENERAL LAGOS": {"base": "ARICA", "radio": "EXTREMA"},
    "GORBEA": {"base": "TEMUCO", "radio": "BASE"},
    "GRANEROS": {"base": "RANCAGUA", "radio": "BASE"},
    "GUAITECAS": {"base": "PUERTO MONTT", "radio": "EXTREMA"},
    "HIJUELAS": {"base": "LA CALERA", "radio": "BASE"},
    "HUALAIHUE": {"base": "PUERTO MONTT", "radio": "EXTREMA"},
    "HUALANE": {"base": "CURICO", "radio": "EXTREMA"},
    "HUALPEN": {"base": "CONCEPCION", "radio": "BASE"},
    "HUALQUI": {"base": "CONCEPCION", "radio": "BASE"},
    "HUARA": {"base": "IQUIQUE", "radio": "EXTREMA"},
    "HUASCO": {"base": "LA SERENA", "radio": "BASE"},
    "HUECHURABA": {"base": "SANTIAGO", "radio": "BASE"},
    "ILLAPEL": {"base": "LA CALERA", "radio": "EXTREMA"},
    "INDEPENDENCIA": {"base": "SANTIAGO", "radio": "BASE"},
    "IQUIQUE": {"base": "IQUIQUE", "radio": "EXTREMA"},
    "ISLA DE MAIPO": {"base": "SANTIAGO", "radio": "BASE"},
    "LA CISTERNA": {"base": "SANTIAGO", "radio": "BASE"},
    "LA CRUZ": {"base": "LA CALERA", "radio": "BASE"},
    "LA ESTRELLA": {"base": "MELIPILLA", "radio": "EXTREMA"},
    "LA FLORIDA": {"base": "SANTIAGO", "radio": "BASE"},
    "LA GRANJA": {"base": "SANTIAGO", "radio": "BASE"},
    "LA HIGUERA": {"base": "LA SERENA", "radio": "BASE"},
    "LA LIGUA": {"base": "LA CALERA", "radio": "BASE"},
    "LA PINTANA": {"base": "SANTIAGO", "radio": "BASE"},
    "LA REINA": {"base": "SANTIAGO", "radio": "BASE"},
    "LA SERENA": {"base": "LA SERENA", "radio": "BASE"},
    "LA UNION": {"base": "OSORNO", "radio": "BASE"},
    "LAGO RANCO": {"base": "OSORNO", "radio": "EXTREMA"},
    "LAGO VERDE": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "LAGUNA BLANCA": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "LAJA": {"base": "LOS ANGELES", "radio": "BASE"},
    "LAMPA": {"base": "SANTIAGO", "radio": "BASE"},
    "LANCO": {"base": "TEMUCO", "radio": "EXTREMA"},
    "LAS CABRAS": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "LAS CONDES": {"base": "SANTIAGO", "radio": "BASE"},
    "LAUTARO": {"base": "TEMUCO", "radio": "BASE"},
    "LEBU": {"base": "CONCEPCION", "radio": "EXTREMA"},
    "LICANTEN": {"base": "CURICO", "radio": "EXTREMA"},
    "LIMACHE": {"base": "LA CALERA", "radio": "BASE"},
    "LINARES": {"base": "TALCA", "radio": "EXTREMA"},
    "LITUECHE": {"base": "MELIPILLA", "radio": "EXTREMA"},
    "LLAILLAY": {"base": "LOS ANDES", "radio": "BASE"},
    "LLANQUIHUE": {"base": "PUERTO MONTT", "radio": "BASE"},
    "LO BARNECHEA": {"base": "SANTIAGO", "radio": "BASE"},
    "LO ESPEJO": {"base": "SANTIAGO", "radio": "BASE"},
    "LO PRADO": {"base": "SANTIAGO", "radio": "BASE"},
    "LOLOL": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "LONCOCHE": {"base": "TEMUCO", "radio": "BASE"},
    "LONGAVI": {"base": "TALCA", "radio": "EXTREMA"},
    "LONQUIMAY": {"base": "TEMUCO", "radio": "EXTREMA"},
    "LOS ALAMOS": {"base": "CONCEPCION", "radio": "EXTREMA"},
    "LOS ANDES": {"base": "LOS ANDES", "radio": "EXTREMA"},
    "LOS ANGELES": {"base": "LOS ANGELES", "radio": "BASE"},
    "LOS LAGOS": {"base": "VALDIVIA", "radio": "EXTREMA"},
    "LOS MUERMOS": {"base": "PUERTO MONTT", "radio": "BASE"},
    "LOS SAUCES": {"base": "TEMUCO", "radio": "EXTREMA"},
    "LOS VILOS": {"base": "LA CALERA", "radio": "BASE"},
    "LOTA": {"base": "CONCEPCION", "radio": "BASE"},
    "LUMACO": {"base": "TEMUCO", "radio": "EXTREMA"},
    "MACHALI": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "MACUL": {"base": "SANTIAGO", "radio": "BASE"},
    "MAFIL": {"base": "VALDIVIA", "radio": "BASE"},
    "MAIPU": {"base": "SANTIAGO", "radio": "BASE"},
    "MALLOA": {"base": "RANCAGUA", "radio": "BASE"},
    "MARCHIHUE": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "MARIA ELENA": {"base": "ANTOFAGASTA", "radio": "EXTREMA"},
    "MARIA PINTO": {"base": "MELIPILLA", "radio": "BASE"},
    "MARIQUINA": {"base": "VALDIVIA", "radio": "EXTREMA"},
    "MAULE": {"base": "TALCA", "radio": "BASE"},
    "MAULLIN": {"base": "PUERTO MONTT", "radio": "BASE"},
    "MEJILLONES": {"base": "ANTOFAGASTA", "radio": "EXTREMA"},
    "MELIPEUCO": {"base": "TEMUCO", "radio": "EXTREMA"},
    "MELIPILLA": {"base": "MELIPILLA", "radio": "BASE"},
    "MOLINA": {"base": "CURICO", "radio": "EXTREMA"},
    "MONTE PATRIA": {"base": "OVALLE", "radio": "EXTREMA"},
    "MOSTAZAL": {"base": "RANCAGUA", "radio": "BASE"},
    "MULCHEN": {"base": "LOS ANGELES", "radio": "BASE"},
    "NACIMIENTO": {"base": "LOS ANGELES", "radio": "BASE"},
    "NANCAGUA": {"base": "RANCAGUA", "radio": "BASE"},
    "NATALES": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "NAVIDAD": {"base": "MELIPILLA", "radio": "EXTREMA"},
    "NEGRETE": {"base": "LOS ANGELES", "radio": "BASE"},
    "NINHUE": {"base": "CHILLAN", "radio": "BASE"},
    "NIQUEN": {"base": "CHILLAN", "radio": "BASE"},
    "NOGALES": {"base": "LA CALERA", "radio": "BASE"},
    "NUEVA IMPERIAL": {"base": "TEMUCO", "radio": "BASE"},
    "NUNOA": {"base": "SANTIAGO", "radio": "BASE"},
    "OHIGGINS": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "OLIVAR": {"base": "RANCAGUA", "radio": "BASE"},
    "OLLAGUE": {"base": "CALAMA", "radio": "EXTREMA"},
    "OLMUE": {"base": "LA CALERA", "radio": "BASE"},
    "OSORNO": {"base": "OSORNO", "radio": "BASE"},
    "OVALLE": {"base": "OVALLE", "radio": "BASE"},
    "PADRE HURTADO": {"base": "SANTIAGO", "radio": "BASE"},
    "PADRE LAS CASAS": {"base": "TEMUCO", "radio": "BASE"},
    "PAIGUANO": {"base": "LA SERENA", "radio": "EXTREMA"},
    "PAILLACO": {"base": "VALDIVIA", "radio": "BASE"},
    "PAINE": {"base": "SANTIAGO", "radio": "BASE"},
    "PALENA": {"base": "PUERTO MONTT", "radio": "EXTREMA"},
    "PALMILLA": {"base": "RANCAGUA", "radio": "BASE"},
    "PANGUIPULLI": {"base": "TEMUCO", "radio": "EXTREMA"},
    "PANQUEHUE": {"base": "LOS ANDES", "radio": "BASE"},
    "PAPUDO": {"base": "LA CALERA", "radio": "BASE"},
    "PAREDONES": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "PARRAL": {"base": "TALCA", "radio": "EXTREMA"},
    "PEDRO AGUIRRE CERDA": {"base": "SANTIAGO", "radio": "BASE"},
    "PELARCO": {"base": "TALCA", "radio": "BASE"},
    "PELLUHUE": {"base": "TALCA", "radio": "EXTREMA"},
    "PEMUCO": {"base": "CHILLAN", "radio": "BASE"},
    "PENAFLOR": {"base": "MELIPILLA", "radio": "BASE"},
    "PENALOLEN": {"base": "SANTIAGO", "radio": "BASE"},
    "PENCAHUE": {"base": "TALCA", "radio": "BASE"},
    "PENCO": {"base": "CONCEPCION", "radio": "BASE"},
    "PERALILLO": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "PERQUENCO": {"base": "TEMUCO", "radio": "BASE"},
    "PETORCA": {"base": "LA CALERA", "radio": "EXTREMA"},
    "PEUMO": {"base": "RANCAGUA", "radio": "BASE"},
    "PICA": {"base": "IQUIQUE", "radio": "EXTREMA"},
    "PICHIDEGUA": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "PICHILEMU": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "PINTO": {"base": "CHILLAN", "radio": "BASE"},
    "PIRQUE": {"base": "SANTIAGO", "radio": "BASE"},
    "PITRUFQUEN": {"base": "TEMUCO", "radio": "BASE"},
    "PLACILLA": {"base": "RANCAGUA", "radio": "BASE"},
    "PORTEZUELO": {"base": "CHILLAN", "radio": "EXTREMA"},
    "PORVENIR": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "POZO ALMONTE": {"base": "IQUIQUE", "radio": "EXTREMA"},
    "PRIMAVERA": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "PROVIDENCIA": {"base": "SANTIAGO", "radio": "BASE"},
    "PUCHUNCAVI": {"base": "LA CALERA", "radio": "BASE"},
    "PUCON": {"base": "TEMUCO", "radio": "EXTREMA"},
    "PUDAHUEL": {"base": "SANTIAGO", "radio": "BASE"},
    "PUENTE ALTO": {"base": "SANTIAGO", "radio": "BASE"},
    "PUERTO MONTT": {"base": "PUERTO MONTT", "radio": "BASE"},
    "PUERTO OCTAY": {"base": "OSORNO", "radio": "EXTREMA"},
    "PUERTO VARAS": {"base": "PUERTO MONTT", "radio": "BASE"},
    "PUMANQUE": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "PUNITAQUI": {"base": "OVALLE", "radio": "BASE"},
    "PUNTA ARENAS": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "PUQUELDON": {"base": "CASTRO", "radio": "EXTREMA"},
    "PUREN": {"base": "TEMUCO", "radio": "EXTREMA"},
    "PURRANQUE": {"base": "OSORNO", "radio": "BASE"},
    "PUTAENDO": {"base": "LOS ANDES", "radio": "BASE"},
    "PUTRE": {"base": "ARICA", "radio": "EXTREMA"},
    "PUYEHUE": {"base": "OSORNO", "radio": "EXTREMA"},
    "QUEILEN": {"base": "CASTRO", "radio": "EXTREMA"},
    "QUELLON": {"base": "CASTRO", "radio": "EXTREMA"},
    "QUEMCHI": {"base": "CASTRO", "radio": "BASE"},
    "QUILACO": {"base": "LOS ANGELES", "radio": "BASE"},
    "QUILICURA": {"base": "SANTIAGO", "radio": "BASE"},
    "QUILLECO": {"base": "LOS ANGELES", "radio": "BASE"},
    "QUILLON": {"base": "CHILLAN", "radio": "BASE"},
    "QUILLOTA": {"base": "LA CALERA", "radio": "BASE"},
    "QUILPUE": {"base": "VINA DEL MAR", "radio": "BASE"},
    "QUINCHAO": {"base": "CASTRO", "radio": "EXTREMA"},
    "QUINTA DE TILCOCO": {"base": "RANCAGUA", "radio": "BASE"},
    "QUINTA NORMAL": {"base": "SANTIAGO", "radio": "BASE"},
    "QUINTERO": {"base": "VINA DEL MAR", "radio": "BASE"},
    "QUIRIHUE": {"base": "CHILLAN", "radio": "BASE"},
    "RANCAGUA": {"base": "RANCAGUA", "radio": "BASE"},
    "RANQUIL": {"base": "CHILLAN", "radio": "EXTREMA"},
    "RAUCO": {"base": "CURICO", "radio": "BASE"},
    "RECOLETA": {"base": "SANTIAGO", "radio": "BASE"},
    "RENAICO": {"base": "LOS ANGELES", "radio": "BASE"},
    "RENCA": {"base": "SANTIAGO", "radio": "BASE"},
    "RENGO": {"base": "RANCAGUA", "radio": "BASE"},
    "REQUINOA": {"base": "RANCAGUA", "radio": "BASE"},
    "RETIRO": {"base": "TALCA", "radio": "BASE"},
    "RINCONADA": {"base": "LOS ANDES", "radio": "BASE"},
    "RIO BUENO": {"base": "OSORNO", "radio": "BASE"},
    "RIO CLARO": {"base": "TALCA", "radio": "BASE"},
    "RIO HURTADO": {"base": "OVALLE", "radio": "EXTREMA"},
    "RIO IBANEZ": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "RIO NEGRO": {"base": "OSORNO", "radio": "BASE"},
    "RIO VERDE": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "ROMERAL": {"base": "CURICO", "radio": "BASE"},
    "SAAVEDRA": {"base": "TEMUCO", "radio": "BASE"},
    "SAGRADA FAMILIA": {"base": "CURICO", "radio": "BASE"},
    "SALAMANCA": {"base": "LA CALERA", "radio": "EXTREMA"},
    "SAN ANTONIO": {"base": "MELIPILLA", "radio": "BASE"},
    "SAN BERNARDO": {"base": "SANTIAGO", "radio": "BASE"},
    "SAN CARLOS": {"base": "CHILLAN", "radio": "BASE"},
    "SAN CLEMENTE": {"base": "TALCA", "radio": "EXTREMA"},
    "SAN ESTEBAN": {"base": "LOS ANDES", "radio": "BASE"},
    "SAN FABIAN": {"base": "CHILLAN", "radio": "EXTREMA"},
    "SAN FELIPE": {"base": "LOS ANDES", "radio": "BASE"},
    "SAN FERNANDO": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "SAN GREGORIO": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "SAN IGNACIO": {"base": "CHILLAN", "radio": "BASE"},
    "SAN JAVIER": {"base": "TALCA", "radio": "BASE"},
    "SAN JOAQUIN": {"base": "SANTIAGO", "radio": "BASE"},
    "SAN JOSE DE MAIPO": {"base": "SANTIAGO", "radio": "EXTREMA"},
    "SAN JUAN DE LA COSTA": {"base": "OSORNO", "radio": "BASE"},
    "SAN MIGUEL": {"base": "SANTIAGO", "radio": "BASE"},
    "SAN NICOLAS": {"base": "CHILLAN", "radio": "BASE"},
    "SAN PABLO": {"base": "OSORNO", "radio": "BASE"},
    "SAN PEDRO": {"base": "MELIPILLA", "radio": "BASE"},
    "SAN PEDRO DE ATACAMA": {"base": "CALAMA", "radio": "EXTREMA"},
    "SAN PEDRO DE LA PAZ": {"base": "CONCEPCION", "radio": "BASE"},
    "SAN RAFAEL": {"base": "TALCA", "radio": "BASE"},
    "SAN RAMON": {"base": "SANTIAGO", "radio": "BASE"},
    "SAN ROSENDO": {"base": "LOS ANGELES", "radio": "BASE"},
    "SAN VICENTE": {"base": "RANCAGUA", "radio": "BASE"},
    "SANTA BARBARA": {"base": "LOS ANGELES", "radio": "BASE"},
    "SANTA CRUZ": {"base": "RANCAGUA", "radio": "EXTREMA"},
    "SANTA JUANA": {"base": "CONCEPCION", "radio": "BASE"},
    "SANTA MARIA": {"base": "LOS ANDES", "radio": "BASE"},
    "SANTIAGO": {"base": "SANTIAGO", "radio": "BASE"},
    "SANTO DOMINGO": {"base": "MELIPILLA", "radio": "BASE"},
    "SIERRA GORDA": {"base": "CALAMA", "radio": "EXTREMA"},
    "TALAGANTE": {"base": "MELIPILLA", "radio": "BASE"},
    "TALCA": {"base": "TALCA", "radio": "BASE"},
    "TALCAHUANO": {"base": "CONCEPCION", "radio": "BASE"},
    "TALTAL": {"base": "COPIAPO", "radio": "EXTREMA"},
    "TEMUCO": {"base": "TEMUCO", "radio": "BASE"},
    "TENO": {"base": "CURICO", "radio": "EXTREMA"},
    "TEODORO SCHMIDT": {"base": "TEMUCO", "radio": "EXTREMA"},
    "TIERRA AMARILLA": {"base": "COPIAPO", "radio": "EXTREMA"},
    "TILTIL": {"base": "SANTIAGO", "radio": "BASE"},
    "TIMAUKEL": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "TIRUA": {"base": "TEMUCO", "radio": "EXTREMA"},
    "TOCOPILLA": {"base": "ANTOFAGASTA", "radio": "EXTREMA"},
    "TOLTEN": {"base": "TEMUCO", "radio": "EXTREMA"},
    "TOME": {"base": "CONCEPCION", "radio": "BASE"},
    "TORRES DEL PAINE": {"base": "PUNTA ARENAS", "radio": "EXTREMA"},
    "TORTEL": {"base": "COYHAIQUE", "radio": "EXTREMA"},
    "TRAIGUEN": {"base": "TEMUCO", "radio": "BASE"},
    "TREHUACO": {"base": "CHILLAN", "radio": "EXTREMA"},
    "TUCAPEL": {"base": "LOS ANGELES", "radio": "BASE"},
    "VALDIVIA": {"base": "VALDIVIA", "radio": "BASE"},
    "VALLENAR": {"base": "LA SERENA", "radio": "EXTREMA"},
    "VALPARAISO": {"base": "VINA DEL MAR", "radio": "BASE"},
    "VICHUQUEN": {"base": "CURICO", "radio": "EXTREMA"},
    "VICTORIA": {"base": "TEMUCO", "radio": "BASE"},
    "VICUNA": {"base": "LA SERENA", "radio": "EXTREMA"},
    "VILCUN": {"base": "TEMUCO", "radio": "BASE"},
    "VILLA ALEGRE": {"base": "TALCA", "radio": "EXTREMA"},
    "VILLA ALEMANA": {"base": "VINA DEL MAR", "radio": "BASE"},
    "VILLARRICA": {"base": "TEMUCO", "radio": "BASE"},
    "VINA DEL MAR": {"base": "VINA DEL MAR", "radio": "BASE"},
    "VITACURA": {"base": "SANTIAGO", "radio": "BASE"},
    "YERBAS BUENAS": {"base": "TALCA", "radio": "BASE"},
    "YUMBEL": {"base": "LOS ANGELES", "radio": "BASE"},
    "YUNGAY": {"base": "CHILLAN", "radio": "EXTREMA"},
    "ZAPALLAR": {"base": "LA CALERA", "radio": "BASE"},
}

# ============================================================================
# Fallback CIUDAD → BASE (when COMUNA is not available)
# ============================================================================

CIUDAD_TO_BASE: dict[str, str] = {
    "SANTIAGO": "SANTIAGO",
    "CONCEPCION": "CONCEPCION",
    "TEMUCO": "TEMUCO",
    "VALDIVIA": "VALDIVIA",
    "ANTOFAGASTA": "ANTOFAGASTA",
    "CALAMA": "CALAMA",
    "ARICA": "ARICA",
    "IQUIQUE": "IQUIQUE",
    "LA SERENA": "LA SERENA",
    "COQUIMBO": "LA SERENA",
    "OVALLE": "OVALLE",
    "COPIAPO": "COPIAPO",
    "VINA DEL MAR": "VINA DEL MAR",
    "VALPARAISO": "VINA DEL MAR",
    "QUILPUE": "VINA DEL MAR",
    "VILLA ALEMANA": "VINA DEL MAR",
    "RANCAGUA": "RANCAGUA",
    "CURICO": "CURICO",
    "TALCA": "TALCA",
    "CHILLAN": "CHILLAN",
    "LOS ANGELES": "LOS ANGELES",
    "OSORNO": "OSORNO",
    "PUERTO MONTT": "PUERTO MONTT",
    "CASTRO": "CASTRO",
    "COYHAIQUE": "COYHAIQUE",
    "PUNTA ARENAS": "PUNTA ARENAS",
    "LA CALERA": "LA CALERA",
    "LOS ANDES": "LOS ANDES",
    "MELIPILLA": "MELIPILLA",
    "SAN ANTONIO": "MELIPILLA",
    "LINARES": "TALCA",
    "CORONEL": "CONCEPCION",
    "TALCAHUANO": "CONCEPCION",
    "PUENTE ALTO": "SANTIAGO",
    "MAIPU": "SANTIAGO",
    "LAS CONDES": "SANTIAGO",
    "LA FLORIDA": "SANTIAGO",
    "NUNOA": "SANTIAGO",
    "PROVIDENCIA": "SANTIAGO",
    "VITACURA": "SANTIAGO",
    "SAN BERNARDO": "SANTIAGO",
}


# ============================================================================
# LOOKUP FUNCTIONS
# ============================================================================

def get_rate_per_kg(dest_base: str, bracket: str = DEFAULT_BRACKET) -> int:
    """Return CLP/kg rate from Santiago to *dest_base*.

    Parameters
    ----------
    dest_base : str
        Destination base (e.g. "CONCEPCION", "CASTRO").
    bracket : str
        Weight bracket key (default "100-500").

    Returns 0 if base or bracket not found.
    """
    rates = TARIFA_POR_KG.get(dest_base.upper().strip(), {})
    return rates.get(bracket, 0)


def get_fixed_rate(dest_base: str, weight_kg: float) -> int:
    """Return flat-rate CLP for shipments ≤ 100 kg.

    Selects the bracket by total weight.  Returns 0 if base not found
    or weight exceeds 100 kg (use per-kg rate instead).
    """
    base = dest_base.upper().strip()
    rates = TARIFA_FIJA.get(base, {})
    if not rates or weight_kg > 100:
        return 0
    if weight_kg <= 20:
        return rates.get("0-20", 0)
    if weight_kg <= 60:
        return rates.get("20-60", 0)
    if weight_kg <= 80:
        return rates.get("60-80", 0)
    return rates.get("80-100", 0)


def get_store_base(comuna: str, ciudad: str = "") -> dict[str, str]:
    """Resolve a store's tariff base from COMUNA (primary) or CIUDAD (fallback).

    Returns ``{"base": str, "radio": "BASE"|"EXTREMA"|""}``.
    """
    key = comuna.upper().strip() if comuna else ""
    if key and key in COMUNA_TO_BASE:
        return COMUNA_TO_BASE[key]

    # Fallback via CIUDAD
    city_key = ciudad.upper().strip() if ciudad else ""
    if city_key and city_key in CIUDAD_TO_BASE:
        return {"base": CIUDAD_TO_BASE[city_key], "radio": "BASE"}

    return {"base": "", "radio": ""}


def calc_transport_cost(
    origin_base: str,
    dest_base: str,
    peso_kg: float,
    qty: int = 1,
    radio_dest: str = "BASE",
) -> float:
    """Estimate transport cost (CLP) for a shipment.

    Parameters
    ----------
    origin_base : str
        Tariff base of origin (e.g. "CASTRO").
    dest_base : str
        Tariff base of destination (e.g. "SANTIAGO").
    peso_kg : float
        Weight per unit in kg (should be MAX of physical and volumetric).
    qty : int
        Number of units.
    radio_dest : str
        "BASE" or "EXTREMA" for the destination localidad.

    Returns
    -------
    float
        Estimated cost in CLP.  Returns 0 if tariff data is unavailable.
    """
    total_weight = peso_kg * qty
    if total_weight <= 0:
        return 0.0

    # Choose bracket by total weight
    if total_weight <= 100:
        cost = float(get_fixed_rate(dest_base, total_weight))
    else:
        rate = get_rate_per_kg(dest_base)
        if rate <= 0:
            return 0.0
        # Per-kg brackets: flat up to 100kg + per-kg for the remainder
        base_cost = float(get_fixed_rate(dest_base, 100))  # first 100 kg
        extra_kg = total_weight - 100
        cost = base_cost + extra_kg * rate

    if cost <= 0:
        return 0.0

    # Surcharge: non-Santiago origin
    if origin_base.upper().strip() not in ("SANTIAGO", ""):
        cost *= SURCHARGE_NON_SANTIAGO

    # Surcharge: EXTREMA radio destination
    if radio_dest.upper().strip() == "EXTREMA":
        cost *= SURCHARGE_EXTREMA

    return round(cost, 0)


def route_cost_per_kg(
    origin_base: str,
    dest_base: str,
    radio_dest: str = "BASE",
) -> float:
    """Return the effective CLP/kg rate for a route (including surcharges).

    Useful for scoring: cheaper route → higher efficiency score.
    """
    rate = float(get_rate_per_kg(dest_base))
    if rate <= 0:
        return 0.0
    if origin_base.upper().strip() not in ("SANTIAGO", ""):
        rate *= SURCHARGE_NON_SANTIAGO
    if radio_dest.upper().strip() == "EXTREMA":
        rate *= SURCHARGE_EXTREMA
    return rate
