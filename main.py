from fastapi import FastAPI, Request, Form, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import os
import google.generativeai as genai
from twilio.rest import Client
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, text, or_
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.sql import func
from fastapi.responses import HTMLResponse
import requests
import json
import base64
import re
import unicodedata
import threading
import uuid
import time

from urllib.parse import quote_plus
from sqlalchemy.dialects.postgresql import ENUM
from prompt_manager import PromptManager


LOCAL_TZ = ZoneInfo("America/Mexico_City")
prompt_manager = PromptManager()


# ============================================================
# FUENTE AUTORIZADA DE PRECIOS
# ============================================================

PRECIOS_CONFIG_PATH = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "config",
    "precios.json",
)


def cargar_configuracion_precios() -> Dict[str, Any]:
    """
    Carga la fuente externa autorizada de precios.

    Principios:
    - Los importes nunca se inventan.
    - Si el archivo no existe, está dañado o tiene una
      estructura inválida, devuelve un diccionario vacío.
    - No utiliza Gemini.
    - No modifica ningún dato.
    """

    try:
        with open(
            PRECIOS_CONFIG_PATH,
            "r",
            encoding="utf-8",
        ) as archivo:
            datos = json.load(archivo)

    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ) as e:
        print(
            "❌ No fue posible cargar la configuración "
            f"autorizada de precios: {e}"
        )
        return {}

    if not isinstance(datos, dict):
        print(
            "❌ precios.json no contiene "
            "un objeto JSON válido."
        )
        return {}

    colegiaturas = datos.get(
        "colegiaturas_mensuales",
        {},
    )

    if not isinstance(
        colegiaturas,
        dict,
    ):
        print(
            "❌ precios.json no contiene "
            "'colegiaturas_mensuales' válido."
        )
        return {}

    precios_validos = {}

    for nivel in [
        "Kínder",
        "Primaria",
        "Secundaria",
    ]:
        valor = colegiaturas.get(
            nivel
        )

        if (
            isinstance(valor, (int, float))
            and not isinstance(valor, bool)
            and valor > 0
        ):
            precios_validos[nivel] = valor

    if not precios_validos:
        print(
            "❌ precios.json no contiene "
            "colegiaturas autorizadas válidas."
        )
        return {}

    datos[
        "colegiaturas_mensuales"
    ] = precios_validos

    return datos


def obtener_colegiatura_autorizada(
    nivel: str,
) -> Optional[Dict[str, Any]]:
    """
    Obtiene la colegiatura autorizada para un nivel.

    Devuelve None si:
    - el archivo de precios no carga;
    - el nivel no está autorizado;
    - el precio es inválido.
    """

    nivel_texto = str(
        nivel or ""
    ).strip()

    equivalencias = {
        "kinder": "Kínder",
        "kínder": "Kínder",
        "preescolar": "Kínder",
        "primaria": "Primaria",
        "secundaria": "Secundaria",
    }

    nivel_normalizado = equivalencias.get(
        nivel_texto.lower(),
        nivel_texto,
    )

    if nivel_normalizado not in {
        "Kínder",
        "Primaria",
        "Secundaria",
    }:
        return None

    configuracion = (
        cargar_configuracion_precios()
    )

    if not configuracion:
        return None

    colegiaturas = configuracion.get(
        "colegiaturas_mensuales",
        {},
    )

    importe = colegiaturas.get(
        nivel_normalizado
    )

    if (
        not isinstance(
            importe,
            (int, float),
        )
        or isinstance(importe, bool)
        or importe <= 0
    ):
        return None

    return {
        "nivel": nivel_normalizado,
        "importe": importe,
        "moneda": str(
            configuracion.get(
                "moneda",
                "MXN",
            )
            or "MXN"
        ).strip(),
        "version": str(
            configuracion.get(
                "version",
                "",
            )
            or ""
        ).strip(),
        "numero_mensualidades": (
            configuracion.get(
                "numero_mensualidades"
            )
        ),
        "opciones_comerciales": (
            configuracion.get(
                "opciones_comerciales",
                {},
            )
            if isinstance(
                configuracion.get(
                    "opciones_comerciales",
                    {},
                ),
                dict,
            )
            else {}
        ),
    }


FLOW_STATE_PREFIX = "FLOW_STATE:"
ADMIN_SELECTED_TASKS = {}

# ============================================================
# BUFFER DE MENSAJES CONSECUTIVOS DE WHATSAPP
# ============================================================

MESSAGE_BUFFER_SECONDS = int(
    os.getenv(
        "MESSAGE_BUFFER_SECONDS",
        "20",
    )
)

MESSAGE_BUFFERS: Dict[
    str,
    Dict[str, Any],
] = {}

MESSAGE_BUFFER_LOCK = threading.Lock()

# ============================================================
# BLOQUEO DE PROCESAMIENTO POR CONTACTO
# ============================================================

STRUCTURED_PROCESS_LOCKS: Dict[
    str,
    threading.Lock,
] = {}

STRUCTURED_PROCESS_LOCKS_GUARD = threading.Lock()

# ============================================================
# FUENTE AUTORIZADA DE HORARIOS
# ============================================================

HORARIOS_CONFIG_PATH = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "config",
    "horarios.json",
)


def cargar_configuracion_horarios() -> Dict[str, Any]:
    """
    Carga la fuente externa autorizada de horarios escolares.

    No utiliza Gemini y falla de forma segura si el archivo
    no existe o tiene una estructura inválida.
    """

    try:
        with open(
            HORARIOS_CONFIG_PATH,
            "r",
            encoding="utf-8",
        ) as archivo:
            datos = json.load(archivo)

    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
    ) as e:
        print(
            "❌ No fue posible cargar la configuración "
            f"autorizada de horarios: {e}"
        )
        return {}

    if not isinstance(datos, dict):
        return {}

    horarios = datos.get(
        "horarios_regulares",
        {},
    )

    if not isinstance(
        horarios,
        dict,
    ):
        return {}

    return datos


def detectar_solicitud_horarios(
    mensaje_usuario: str,
) -> bool:
    """
    Detecta una solicitud explícita sobre horarios escolares.
    """

    texto = normalizar_texto_para_deteccion(
        mensaje_usuario
    )

    expresiones = [
        "horario",
        "horarios",
        "hora de entrada",
        "hora de salida",
        "a que hora entran",
        "a que hora salen",
        "a qué hora entran",
        "a qué hora salen",
        "horario escolar",
        "horario de clases",
    ]

    return any(
        expresion in texto
        for expresion in expresiones
    )


def construir_respuesta_horarios(
    niveles: Optional[List[str]] = None,
) -> str:
    """
    Construye una respuesta exclusivamente con horarios
    autorizados desde config/horarios.json.
    """

    configuracion = (
        cargar_configuracion_horarios()
    )

    horarios = configuracion.get(
        "horarios_regulares",
        {},
    )

    if not horarios:
        return ""

    niveles_solicitados = (
        niveles
        if isinstance(niveles, list)
        else []
    )

    niveles_validos = []

    for nivel in niveles_solicitados:
        nivel_texto = str(
            nivel or ""
        ).strip()

        equivalencias = {
            "kinder": "Kínder",
            "kínder": "Kínder",
            "preescolar": "Kínder",
            "primaria": "Primaria",
            "secundaria": "Secundaria",
        }

        nivel_normalizado = (
            equivalencias.get(
                nivel_texto.lower(),
                nivel_texto,
            )
        )

        if (
            nivel_normalizado in horarios
            and nivel_normalizado
            not in niveles_validos
        ):
            niveles_validos.append(
                nivel_normalizado
            )

    if not niveles_validos:
        niveles_validos = [
            "Kínder",
            "Primaria",
            "Secundaria",
        ]

    lineas = [
        "Con gusto. Nuestros horarios regulares son:",
        "",
    ]

    for nivel in niveles_validos:

        datos_nivel = horarios.get(
            nivel,
            {},
        )

        ingreso = str(
            datos_nivel.get(
                "ingreso",
                "",
            )
            or ""
        ).strip()

        salida = str(
            datos_nivel.get(
                "salida",
                "",
            )
            or ""
        ).strip()

        if ingreso and salida:
            lineas.append(
                f"{nivel}: ingreso {ingreso}, "
                f"salida {salida}"
            )

    horario_extendido = configuracion.get(
        "horario_extendido",
        {},
    )

    if (
        isinstance(
            horario_extendido,
            dict,
        )
        and horario_extendido.get(
            "disponible"
        ) is True
    ):
        minutos = horario_extendido.get(
            "duracion_adicional_minutos",
            60,
        )

        if minutos == 60:
            lineas.extend([
                "",
                (
                    "Adicionalmente contamos con horario "
                    "extendido de una hora después del horario "
                    "regular de cada nivel."
                ),
            ])

    return "\n".join(
        lineas
    ).strip()
    

def obtener_lock_procesamiento_estructurado(
    clave_contacto: str,
) -> threading.Lock:
    """
    Garantiza que un mismo contacto no tenga dos ejecuciones
    del flujo estructurado procesándose al mismo tiempo.
    """

    clave = str(
        clave_contacto or ""
    ).strip()

    with STRUCTURED_PROCESS_LOCKS_GUARD:

        lock_contacto = (
            STRUCTURED_PROCESS_LOCKS.get(
                clave
            )
        )

        if lock_contacto is None:
            lock_contacto = (
                threading.Lock()
            )

            STRUCTURED_PROCESS_LOCKS[
                clave
            ] = lock_contacto

        return lock_contacto


USE_STRUCTURED_AI_FLOW = (
    os.getenv("USE_STRUCTURED_AI_FLOW", "false")
    .strip()
    .lower()
    in ["true", "1", "yes", "si", "sí"]
)

# ============================================================
# NÚMEROS AUTORIZADOS PARA PROBAR EL FLUJO ESTRUCTURADO
# ============================================================

STRUCTURED_FLOW_TEST_NUMBERS = {
    numero.strip()
    for numero in os.getenv(
        "STRUCTURED_FLOW_TEST_NUMBERS",
        "",
    ).split(",")
    if numero.strip()
}


def normalizar_numero_whatsapp(
    numero: str,
) -> str:
    """
    Normaliza un número para poder comparar variantes como:
    +5215548123885
    whatsapp:+5215548123885
    5215548123885
    """

    numero_limpio = str(
        numero or ""
    ).strip()

    numero_limpio = numero_limpio.replace(
        "whatsapp:",
        "",
    )

    digitos = re.sub(
        r"\D",
        "",
        numero_limpio,
    )

    return digitos


def es_numero_prueba_flujo_estructurado(
    numero: str,
) -> bool:
    """
    Indica si un número está autorizado para utilizar
    el nuevo flujo estructurado aunque el feature flag
    general siga apagado.
    """

    numero_normalizado = (
        normalizar_numero_whatsapp(
            numero
        )
    )

    if not numero_normalizado:
        return False

    numeros_autorizados = {
        normalizar_numero_whatsapp(
            numero_configurado
        )
        for numero_configurado
        in STRUCTURED_FLOW_TEST_NUMBERS
        if normalizar_numero_whatsapp(
            numero_configurado
        )
    }

    return (
        numero_normalizado
        in numeros_autorizados
    )

def obtener_ubicacion_institucional_campus() -> Dict[str, str]:
    """
    Devuelve exclusivamente la ubicación institucional autorizada.

    Principios:
    - Gemini nunca genera ni reconstruye una URL de Google Maps.
    - La fuente principal es nombre + dirección institucional.
    - Si existe Place ID autorizado, se incorpora a la URL universal.
    - Puede existir una URL autorizada de respaldo configurable
      desde Railway.
    - Ninguna URL de ubicación se guarda de forma rígida en el código.
    """

    nombre = str(
        os.getenv(
            "CAMPUS_MAPS_NAME",
            "",
        )
        or ""
    ).strip()

    direccion = str(
        os.getenv(
            "CAMPUS_MAPS_ADDRESS",
            "",
        )
        or ""
    ).strip()

    place_id = str(
        os.getenv(
            "CAMPUS_MAPS_PLACE_ID",
            "",
        )
        or ""
    ).strip()

    url_autorizada_respaldo = str(
        os.getenv(
            "CAMPUS_MAPS_URL",
            "",
        )
        or ""
    ).strip()

    resultado = {
        "nombre": nombre,
        "direccion": direccion,
        "place_id": place_id,
        "url_autorizada_respaldo": (
            url_autorizada_respaldo
        ),
        "url": "",
        "fuente": "",
        "configurada": False,
    }

    # ========================================================
    # FUENTE PRINCIPAL:
    # URL UNIVERSAL CON DATOS INSTITUCIONALES
    # ========================================================

    if nombre and direccion:

        consulta = quote_plus(
            f"{nombre}, {direccion}"
        )

        url = (
            "https://www.google.com/maps/search/"
            f"?api=1&query={consulta}"
        )

        if place_id:
            url += (
                "&query_place_id="
                + quote_plus(place_id)
            )

        resultado["url"] = url
        resultado["fuente"] = (
            "GOOGLE_MAPS_UNIVERSAL"
        )
        resultado["configurada"] = True

        return resultado

    # ========================================================
    # RESPALDO OPERATIVO:
    # URL AUTORIZADA CONFIGURABLE
    # ========================================================
    #
    # Permite continuar operando aun cuando todavía no tengamos
    # nombre/dirección/Place ID completamente parametrizados.
    #
    # La URL vive en Railway, no en main.py, por lo que puede
    # reemplazarse sin modificar lógica de negocio.
    # ========================================================

    if url_autorizada_respaldo:
        resultado["url"] = (
            url_autorizada_respaldo
        )
        resultado["fuente"] = (
            "URL_AUTORIZADA_RESPALDO"
        )
        resultado["configurada"] = True

        return resultado

    return resultado

   

# ============================================================
# CONTRATO DE ANÁLISIS ESTRUCTURADO DEL MENSAJE DEL PROSPECTO
# ============================================================

INTENCIONES_PRINCIPALES_VALIDAS = {
    "SALUDO",
    "PEDIR_INFORMES",
    "PEDIR_COSTOS",
    "PEDIR_UBICACION",
    "PEDIR_CITA",
    "PROPONER_FECHA_CITA",
    "PROPONER_HORA_CITA",
    "DAR_DATOS_CITA",
    "PREGUNTAR_TEMA_EDUCATIVO",
    "RESPONDER_ZONA",
    "PAUSAR_CONVERSACION",
    "CAMPUS_EXTERNO",
    "OTRO",
}

CLASIFICACIONES_ZONA_VALIDAS = {
    "VALIDA",
    "FUERA_DE_ZONA",
    "CAMPUS_EXTERNO",
    "DUDOSA",
    "NO_MENCIONADA",
}

NIVELES_OFICIALES_VALIDOS = {
    "",
    "Kínder",
    "Primaria",
    "Secundaria",
}

TEMAS_INTERES_VALIDOS = {
    "",
    "metodo_filadelfia",
    "matematico",
    "lectura",
    "artistico_musical",
    "motriz",
    "inteligencia_emocional",
    "pantallas_ipad",
    "idiomas",
    "costos",
    "cita",
    "otro",
}

ACCIONES_RECOMENDADAS_VALIDAS = {
    "RESPONDER_SALUDO",
    "PEDIR_REFERENCIA",
    "PRESENTAR_PROPUESTA_VALOR",
    "EXPLICAR_METODO_FILADELFIA",
    "PREGUNTAR_AREA_INTERES",
    "PROFUNDIZAR_AREA_INTERES",
    "PEDIR_ZONA",
    "CONTINUAR_INFORMES",
    "RESPONDER_TEMA",
    "RESPONDER_HORARIOS",
    "RESPONDER_COSTOS",
    "PEDIR_NIVEL_COSTOS",
    "RESPONDER_UBICACION",
    "INVITAR_CITA",
    "PEDIR_FECHA_CITA",
    "PEDIR_HORA_CITA",
    "CONFIRMAR_FECHA_CITA",
    "CONSULTAR_ADMIN",
    "RECHAZAR_CAMPUS",
    "ORIENTAR_PRE_KINDER",
    "PEDIR_FECHA_NACIMIENTO",
    "PEDIR_DATOS_CITA",
    "REGISTRAR_DATOS_CITA",
    "CITA_DIA_NO_LABORAL",
    "CITA_FUERA_HORARIO",
    "SEGUIMIENTO",
    "FALLBACK_CONVERSACIONAL",
    "CONTINUAR_CONVERSACION",
}

# ============================================================
# CONTRATO DE CLASIFICACIÓN DEL ALCANCE DE LA CONVERSACIÓN
# ============================================================

ALCANCES_CONVERSACION_VALIDOS = {
    "AMBIGUO",
    "ADMISIONES",
    "EMPLEO",
    "ALUMNOS_ACTUALES",
    "TRAMITES_ADMINISTRATIVOS",
    "PROVEEDORES",
    "OTRO_CONFIGURADO",
    "SIN_RUTA_CONFIGURADA",
}


class AlcanceConversacion(BaseModel):
    """
    Representa el motivo general por el que una persona
    se comunica con el colegio.

    Este contrato se evalúa antes del análisis comercial
    de admisiones.

    No genera respuestas.
    No modifica el CRM.
    No cambia estados.
    No crea tareas administrativas.
    """

    version: str = "1.0"

    alcance_conversacion: str = "AMBIGUO"

    motivo_principal: str = ""

    resumen_solicitud: str = ""

    ruta_configurada: bool = False

    requiere_aclaracion: bool = True

    requiere_admin: bool = False

    motivo_escalacion: str = ""

    confianza: float = 0.0


def crear_alcance_conversacion_vacio() -> Dict[str, Any]:
    """
    Devuelve una clasificación de alcance segura
    con valores iniciales.

    Se utiliza cuando todavía no existe suficiente
    información o cuando falla la clasificación.
    """

    return AlcanceConversacion().model_dump()


def normalizar_alcance_conversacion(
    datos_crudos: Any,
) -> Dict[str, Any]:
    """
    Limpia y valida el resultado de la clasificación
    general de alcance.

    No ejecuta rutas.
    No modifica contactos.
    No inicia el embudo de admisiones.
    """

    base = crear_alcance_conversacion_vacio()

    if not isinstance(datos_crudos, dict):
        return base

    alcance = str(
        datos_crudos.get(
            "alcance_conversacion",
            "AMBIGUO",
        )
        or "AMBIGUO"
    ).strip().upper()

    if alcance not in ALCANCES_CONVERSACION_VALIDOS:
        alcance = "AMBIGUO"

    ruta_configurada = normalizar_booleano(
        datos_crudos.get(
            "ruta_configurada"
        )
    )

    requiere_aclaracion = normalizar_booleano(
        datos_crudos.get(
            "requiere_aclaracion"
        ),
        predeterminado=(
            alcance == "AMBIGUO"
        ),
    )

    requiere_admin = normalizar_booleano(
        datos_crudos.get(
            "requiere_admin"
        ),
        predeterminado=(
            alcance == "SIN_RUTA_CONFIGURADA"
        ),
    )

    # Reglas internas de consistencia.
    if alcance == "AMBIGUO":
        ruta_configurada = False
        requiere_aclaracion = normalizar_booleano(
            datos_crudos.get(
                "requiere_aclaracion"
            ),
            predeterminado=True,
        )
        requiere_admin = False

    elif alcance == "SIN_RUTA_CONFIGURADA":
        ruta_configurada = False
        requiere_aclaracion = False
        requiere_admin = True

    elif alcance in {
        "ADMISIONES",
        "EMPLEO",
        "ALUMNOS_ACTUALES",
        "TRAMITES_ADMINISTRATIVOS",
        "PROVEEDORES",
        "OTRO_CONFIGURADO",
    }:
        ruta_configurada = True
        requiere_aclaracion = False

    resultado_normalizado = {
        "version": "1.0",

        "alcance_conversacion": alcance,

        "motivo_principal": str(
            datos_crudos.get(
                "motivo_principal",
                "",
            )
            or ""
        ).strip(),

        "resumen_solicitud": str(
            datos_crudos.get(
                "resumen_solicitud",
                "",
            )
            or ""
        ).strip(),

        "ruta_configurada": ruta_configurada,

        "requiere_aclaracion": (
            requiere_aclaracion
        ),

        "requiere_admin": requiere_admin,

        "motivo_escalacion": str(
            datos_crudos.get(
                "motivo_escalacion",
                "",
            )
            or ""
        ).strip(),

        "confianza": normalizar_confianza(
            datos_crudos.get(
                "confianza"
            )
        ),
    }

    try:
        alcance_validado = (
            AlcanceConversacion.model_validate(
                resultado_normalizado
            )
        )

        return alcance_validado.model_dump()

    except Exception as e:
        print(
            "⚠️ Error validando contrato de alcance: "
            f"{e}"
        )

        return base
        

# ============================================================
# CONTRATO COMERCIAL Y CONVERSACIONAL DEL NUEVO FLUJO
# ============================================================

ETAPAS_CONVERSACIONALES_VALIDAS = {
    "CONTACTO_INICIAL",
    "REFERENCIA_COLEGIO",
    "VALIDACION_ZONA",
    "PRESENTACION_VALOR",
    "EXPLICACION_METODO",
    "IDENTIFICACION_INTERES",
    "PROFUNDIZACION_INTERES",
    "INVITACION_VISITA",
    "NEGOCIACION_CITA",
    "ESPERANDO_CONFIRMACION_ADMIN",
    "ESPERANDO_DATOS_CITA",
    "VISITA_CONFIRMADA",
    "SEGUIMIENTO_VISITA",
    "POST_VISITA_COSTOS",
    "SEGUIMIENTO_INSCRIPCION",
    "CIERRE_INSCRIPCION",
    "SEGUIMIENTO",
}


ESTADOS_COMERCIALES_VALIDOS = {
    "PROSPECTO_NUEVO",
    "EN_CALIFICACION",
    "PROSPECTO_INFORMADO",
    "PENDIENTE_DE_AGENDAR",
    "CITA_PENDIENTE_CONFIRMACION",
    "VISITA_CONFIRMADA",
    "VISITA_REALIZADA",
    "VISITA_NO_ASISTIO",
    "COSTOS_PRESENTADOS",
    "INSCRIPCION_PENDIENTE",
    "INSCRITO",
    "NO_INSCRITO",
    "DESCARTADO",
    "COMPETENCIA",
    "ALUMNO_ACTIVO",
    "ALUMNO_INACTIVO",
    "EX_ALUMNO",
}


HITOS_COMERCIALES_VALIDOS = {
    "PIDIO_INFORMES",
    "RESPONDIO_REFERENCIA",
    "ZONA_VALIDADA",
    "RECIBIO_PRESENTACION_VALOR",
    "RECIBIO_EXPLICACION_METODO",
    "EXPRESO_AREA_INTERES",
    "RECIBIO_RESPUESTA_PERSONALIZADA",
    "SOLICITO_COSTOS_INICIAL",
    "INSISTIO_COSTOS",
    "ACEPTO_VISITA",
    "PROPUSO_FECHA_CITA",
    "PROPUSO_HORA_CITA",
    "CITA_SOLICITADA",
    "CITA_CONFIRMADA",
    "VISITA_REALIZADA",
    "VISITA_NO_ASISTIO",
    "RECIBIO_COSTOS",
    "RECIBIO_OPCIONES_PAGO",
    "INICIO_INSCRIPCION",
    "SE_INSCRIBIO",
    "SEGUIMIENTO_PROGRAMADO",
    "SEGUIMIENTO_SIN_RESPUESTA",
}

# ============================================================
# OBJETIVOS PENDIENTES DEL FLUJO CONVERSACIONAL
# ============================================================

OBJETIVOS_PENDIENTES_VALIDOS = {
    "",
    "OBTENER_ZONA",
    "OBTENER_ZONA_PARA_COSTOS",
    "OBTENER_ZONA_PARA_CITA",
    "OBTENER_NIVEL_PARA_COSTOS",
    "OBTENER_REFERENCIA_COLEGIO",

    # Después de la presentación general, el bot termina
    # preguntando si la familia conoce el Método Filadelfia.
    "OBTENER_RESPUESTA_METODO",

    "OBTENER_AREA_INTERES",

    # Después de profundizar en el área de interés, el bot
    # normalmente pregunta si ese enfoque coincide con lo
    # que la familia busca.
    "OBTENER_CONFIRMACION_INTERES",

    "OBTENER_DECISION_VISITA",
    "OBTENER_FECHA_CITA",
    "OBTENER_HORA_CITA",
    "CONFIRMAR_FECHA_CITA_CALENDARIO",
    "ESPERAR_CONFIRMACION_ADMIN",
    "OBTENER_DATOS_CITA",
    "ESPERAR_REACTIVACION_PROSPECTO",
}

# ============================================================
# RELACIÓN SEMÁNTICA DEL MENSAJE CON EL OBJETIVO PENDIENTE
# ============================================================

RELACIONES_OBJETIVO_VALIDAS = {
    "SIN_OBJETIVO",
    "RESPONDE_OBJETIVO",
    "NO_AFECTA_OBJETIVO",
    "MODIFICA_OBJETIVO",
    "CANCELA_OBJETIVO",
}


class ContextoComercialConversacion(BaseModel):
    """
    Representa la posición comercial y conversacional de una familia.

    Este contrato no genera respuestas, no modifica la base de datos
    y no ejecuta transiciones. Solamente establece una estructura
    común para las siguientes fases del nuevo flujo.
    """

    version: str = "1.0"

    etapa_conversacional: str = "CONTACTO_INICIAL"
    estado_comercial: str = "PROSPECTO_NUEVO"
    objetivo_pendiente: str = ""

    hitos_comerciales: List[str] = Field(
        default_factory=list
    )
    
    nombre_tutor: str = ""
    zona_interes: str = ""

    alumnos: List[Dict[str, Any]] = Field(
        default_factory=list
    )

    referencia_colegio: str = ""

    temas_explicados: List[str] = Field(
        default_factory=list
    )

    areas_interes: List[str] = Field(
        default_factory=list
    )

    objeciones_detectadas: List[str] = Field(
        default_factory=list
    )

    fecha_ultima_interaccion: str = ""
    resumen_relacion: str = ""

    historial_completo_disponible: bool = False


def crear_contexto_comercial_vacio() -> Dict[str, Any]:
    """
    Devuelve un contexto comercial seguro con valores iniciales.

    No consulta la base de datos.
    No modifica contactos.
    No altera FLOW_STATE.
    """
    return ContextoComercialConversacion().model_dump()

# ============================================================
# PISO MONOTÓNICO DEL EMBUDO COMERCIAL
# ============================================================
#
# Principio:
# una vez consumada una etapa comercial, una interpretación
# posterior nunca puede devolver automáticamente a la familia
# a una etapa anterior.
#
# Gemini interpreta el significado.
# Python protege el progreso ya consumado.
# ============================================================

NIVELES_ACCIONES_EMBUDO = {
    "PEDIR_ZONA": 10,
    "PEDIR_REFERENCIA": 25,
    "PRESENTAR_PROPUESTA_VALOR": 30,
    "EXPLICAR_METODO_FILADELFIA": 40,
    "PREGUNTAR_AREA_INTERES": 50,
    "PROFUNDIZAR_AREA_INTERES": 60,
    "RESPONDER_COSTOS": 70,
    "INVITAR_CITA": 70,
    "PEDIR_FECHA_CITA": 80,
    "PEDIR_HORA_CITA": 80,
    "CONFIRMAR_FECHA_CITA": 80,
}


def obtener_piso_progreso_comercial(
    contexto_comercial: Optional[
        Dict[str, Any]
    ],
) -> Dict[str, Any]:
    """
    Obtiene el punto mínimo del embudo que ya fue consumado.

    Se apoya principalmente en hitos autoritativos, pero también
    utiliza etapa y estado persistidos como evidencia adicional.

    No modifica BD.
    No consulta Gemini.
    """

    contexto = (
        contexto_comercial
        if isinstance(
            contexto_comercial,
            dict,
        )
        else {}
    )

    hitos_raw = contexto.get(
        "hitos_comerciales",
        [],
    )

    if not isinstance(
        hitos_raw,
        list,
    ):
        hitos_raw = []

    hitos = {
        str(
            hito or ""
        ).strip().upper()
        for hito in hitos_raw
        if str(
            hito or ""
        ).strip()
    }

    etapa = str(
        contexto.get(
            "etapa_conversacional",
            "",
        )
        or ""
    ).strip().upper()

    estado = str(
        contexto.get(
            "estado_comercial",
            "",
        )
        or ""
    ).strip().upper()

    objetivo = str(
        contexto.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    # --------------------------------------------------------
    # 90 - CITA CONFIRMADA
    # --------------------------------------------------------

    if (
        "CITA_CONFIRMADA" in hitos
        or etapa == "VISITA_CONFIRMADA"
        or estado == "VISITA_CONFIRMADA"
    ):
        return {
            "nivel": 90,
            "piso": "CITA_CONFIRMADA",
            "etapa": "VISITA_CONFIRMADA",
            "estado": "VISITA_CONFIRMADA",
            "objetivo": (
                objetivo
                or "OBTENER_DATOS_CITA"
            ),
        }

    # --------------------------------------------------------
    # 80 - CITA EN NEGOCIACIÓN / CONFIRMACIÓN
    # --------------------------------------------------------

    if (
        "CITA_SOLICITADA" in hitos
        or etapa
        in {
            "NEGOCIACION_CITA",
            "ESPERANDO_CONFIRMACION_ADMIN",
        }
        or estado
        in {
            "PENDIENTE_DE_AGENDAR",
            "CITA_PENDIENTE_CONFIRMACION",
        }
    ):
        return {
            "nivel": 80,
            "piso": "CITA_EN_PROCESO",
            "etapa": etapa,
            "estado": estado,
            "objetivo": objetivo,
        }

    # --------------------------------------------------------
    # 70 - POST COSTOS / INVITACIÓN A VISITA
    # --------------------------------------------------------

    if (
        "RECIBIO_COSTOS" in hitos
        or "RECIBIO_OPCIONES_PAGO" in hitos
        or etapa == "INVITACION_VISITA"
        or estado == "COSTOS_PRESENTADOS"
    ):
        return {
            "nivel": 70,
            "piso": "POST_COSTOS_VISITA",
            "etapa": "INVITACION_VISITA",
            "estado": "COSTOS_PRESENTADOS",
            "objetivo": "OBTENER_DECISION_VISITA",
        }

    # --------------------------------------------------------
    # 60 - RESPUESTA PERSONALIZADA YA ENTREGADA
    # --------------------------------------------------------

    if (
        "RECIBIO_RESPUESTA_PERSONALIZADA"
        in hitos
        or etapa == "PROFUNDIZACION_INTERES"
    ):
        return {
            "nivel": 60,
            "piso": "RESPUESTA_PERSONALIZADA",
            "etapa": etapa,
            "estado": estado,
            "objetivo": (
                objetivo
                or "OBTENER_CONFIRMACION_INTERES"
            ),
        }

    # --------------------------------------------------------
    # 50 - ÁREA DE INTERÉS
    # --------------------------------------------------------

    if (
        "EXPRESO_AREA_INTERES" in hitos
        or etapa == "IDENTIFICACION_INTERES"
    ):
        return {
            "nivel": 50,
            "piso": "AREA_INTERES",
            "etapa": etapa,
            "estado": estado,
            "objetivo": (
                objetivo
                or "OBTENER_AREA_INTERES"
            ),
        }

    # --------------------------------------------------------
    # 40 - MÉTODO YA EXPLICADO
    # --------------------------------------------------------

    if (
        "RECIBIO_EXPLICACION_METODO"
        in hitos
        or etapa == "EXPLICACION_METODO"
    ):
        return {
            "nivel": 40,
            "piso": "METODO_EXPLICADO",
            "etapa": etapa,
            "estado": estado,
            "objetivo": (
                objetivo
                or "OBTENER_AREA_INTERES"
            ),
        }

    # --------------------------------------------------------
    # 30 - PROPUESTA DE VALOR YA PRESENTADA
    # --------------------------------------------------------

    if (
        "RECIBIO_PRESENTACION_VALOR"
        in hitos
        or etapa == "PRESENTACION_VALOR"
    ):
        return {
            "nivel": 30,
            "piso": "VALOR_PRESENTADO",
            "etapa": etapa,
            "estado": estado,
            "objetivo": (
                objetivo
                or "OBTENER_RESPUESTA_METODO"
            ),
        }

    # --------------------------------------------------------
    # 25 - REFERENCIA YA RESPONDIDA
    # --------------------------------------------------------

    if "RESPONDIO_REFERENCIA" in hitos:
        return {
            "nivel": 25,
            "piso": "REFERENCIA_RESPONDIDA",
            "etapa": etapa,
            "estado": estado,
            "objetivo": objetivo,
        }

    # --------------------------------------------------------
    # 20 - ZONA YA VALIDADA
    # --------------------------------------------------------

    if "ZONA_VALIDADA" in hitos:
        return {
            "nivel": 20,
            "piso": "ZONA_VALIDADA",
            "etapa": etapa,
            "estado": estado,
            "objetivo": objetivo,
        }

    return {
        "nivel": 0,
        "piso": "SIN_PISO",
        "etapa": etapa,
        "estado": estado,
        "objetivo": objetivo,
    }


def aplicar_candado_progreso_comercial(
    decision: Dict[str, Any],
    analisis: Dict[str, Any],
    contexto_comercial: Optional[
        Dict[str, Any]
    ],
) -> Dict[str, Any]:
    """
    Arbitra la decisión ANTES de redactar la respuesta.

    Objetivos:
    1. impedir regresiones del embudo;
    2. responder preguntas paralelas sin perder progreso;
    3. proteger el flujo incluso si Gemini falla o devuelve
       una interpretación incompleta.

    No modifica BD.
    """

    decision_segura = (
        dict(decision)
        if isinstance(
            decision,
            dict,
        )
        else {}
    )

    analisis_seguro = (
        analisis
        if isinstance(
            analisis,
            dict,
        )
        else {}
    )

    contexto = (
        contexto_comercial
        if isinstance(
            contexto_comercial,
            dict,
        )
        else {}
    )

    datos_decision = decision_segura.get(
        "datos_detectados",
        {},
    )

    if not isinstance(
        datos_decision,
        dict,
    ):
        datos_decision = {}

    else:
        datos_decision = dict(
            datos_decision
        )

    decision_segura[
        "datos_detectados"
    ] = datos_decision

    piso = obtener_piso_progreso_comercial(
        contexto
    )

    nivel_piso = int(
        piso.get(
            "nivel",
            0,
        )
        or 0
    )

    accion_original = str(
        decision_segura.get(
            "accion",
            "CONTINUAR_CONVERSACION",
        )
        or "CONTINUAR_CONVERSACION"
    ).strip().upper()

    nivel_accion = (
        NIVELES_ACCIONES_EMBUDO.get(
            accion_original
        )
    )

    relacion_objetivo = str(
        analisis_seguro.get(
            "relacion_con_objetivo_pendiente",
            "SIN_OBJETIVO",
        )
        or "SIN_OBJETIVO"
    ).strip().upper()

    intencion_principal = str(
        analisis_seguro.get(
            "intencion_principal",
            "",
        )
        or ""
    ).strip().upper()

    tema_interes = str(
        analisis_seguro.get(
            "tema_interes",
            "",
        )
        or ""
    ).strip()

    es_consulta_paralela = bool(
        analisis_seguro.get(
            "pregunta_paralela",
            False,
        )
        or relacion_objetivo
        == "NO_AFECTA_OBJETIVO"
        or intencion_principal
        == "PREGUNTAR_TEMA_EDUCATIVO"
        or tema_interes
    )

    solicitud_costos_explicita = bool(
        analisis_seguro.get(
            "pide_costos",
            False,
        )
        or intencion_principal
        == "PEDIR_COSTOS"
    )

    solicitud_cita_explicita = bool(
        analisis_seguro.get(
            "pide_cita",
            False,
        )
        or intencion_principal
        in {
            "PEDIR_CITA",
            "PROPONER_FECHA_CITA",
            "PROPONER_HORA_CITA",
        }
    )

    acciones_explicitas_protegidas = {
        "RESPONDER_COSTOS",
        "PEDIR_NIVEL_COSTOS",
        "RESPONDER_HORARIOS",
        "RESPONDER_UBICACION",
        "PEDIR_FECHA_CITA",
        "PEDIR_HORA_CITA",
        "CONFIRMAR_FECHA_CITA",
        "CONSULTAR_ADMIN",
        "PEDIR_DATOS_CITA",
        "REGISTRAR_DATOS_CITA",
        "CITA_DIA_NO_LABORAL",
        "CITA_FUERA_HORARIO",
        "SEGUIMIENTO",
        "RECHAZAR_CAMPUS",
        "ORIENTAR_PRE_KINDER",
        "PEDIR_FECHA_NACIMIENTO",
    }

    acciones_blandas_embudo = {
        "PEDIR_ZONA",
        "PEDIR_REFERENCIA",
        "PRESENTAR_PROPUESTA_VALOR",
        "EXPLICAR_METODO_FILADELFIA",
        "PREGUNTAR_AREA_INTERES",
        "PROFUNDIZAR_AREA_INTERES",
        "INVITAR_CITA",
        "CONTINUAR_INFORMES",
        "RESPONDER_TEMA",
        "CONTINUAR_CONVERSACION",
    }

    # --------------------------------------------------------
    # PREGUNTA PARALELA:
    # se responde sin consumir ni reiniciar el funnel.
    # --------------------------------------------------------

    if (
        es_consulta_paralela
        and accion_original
        in acciones_blandas_embudo
        and not solicitud_costos_explicita
        and not solicitud_cita_explicita
    ):
        decision_segura[
            "accion"
        ] = "RESPONDER_TEMA"

        if nivel_piso >= 70:
            decision_segura[
                "puede_compartir_costos"
            ] = True

        decision_segura[
            "motivo"
        ] = (
            "El prospecto realizó una consulta paralela. "
            "Se responde directamente sin modificar el "
            "progreso comercial ya alcanzado."
        )

        objetivo_contexto = str(
            contexto.get(
                "objetivo_pendiente",
                "",
            )
            or ""
        ).strip().upper()

        objetivo_piso = str(
            piso.get(
                "objetivo",
                "",
            )
            or ""
        ).strip().upper()

        if objetivo_contexto in {
            "",
            "ESPERAR_REACTIVACION_PROSPECTO",
        }:
            objetivo_retorno = objetivo_piso
        else:
            objetivo_retorno = objetivo_contexto

        datos_decision.update({
            "preservar_progreso_comercial": True,
            "piso_comercial": piso.get(
                "piso",
                "",
            ),
            "objetivo_retorno": (
                objetivo_retorno
            ),
        })
        

        print(
            "🧭 CONSULTA PARALELA SIN REGRESIÓN: "
            f"accion_original={accion_original}, "
            f"piso={piso.get('piso')}"
        )

        return decision_segura

    # --------------------------------------------------------
    # ACCIONES QUE NO REPRESENTAN PROGRESO LINEAL
    # --------------------------------------------------------

    if (
        accion_original
        in acciones_explicitas_protegidas
    ):
        return decision_segura

    if nivel_accion is None:
        return decision_segura

    # --------------------------------------------------------
    # SIN REGRESIÓN
    # --------------------------------------------------------

    if nivel_accion >= nivel_piso:
        return decision_segura

    # --------------------------------------------------------
    # REGRESIÓN DETECTADA
    # --------------------------------------------------------

    if nivel_piso >= 70:
        nueva_accion = "RESPONDER_TEMA"

    elif nivel_piso >= 60:
        nueva_accion = "INVITAR_CITA"

    elif nivel_piso >= 50:
        nueva_accion = (
            "PROFUNDIZAR_AREA_INTERES"
        )

    elif nivel_piso >= 40:
        nueva_accion = (
            "PREGUNTAR_AREA_INTERES"
        )

    elif nivel_piso >= 30:
        nueva_accion = (
            "EXPLICAR_METODO_FILADELFIA"
        )

    elif nivel_piso >= 25:
        nueva_accion = (
            "PRESENTAR_PROPUESTA_VALOR"
        )

    elif nivel_piso >= 20:
        nueva_accion = "PEDIR_REFERENCIA"

    else:
        nueva_accion = accion_original

    decision_segura[
        "accion"
    ] = nueva_accion

    decision_segura[
        "motivo"
    ] = (
        "La acción originalmente propuesta implicaba "
        "regresar a una etapa comercial ya consumada. "
        "Python aplicó el piso monotónico del embudo."
    )

    datos_decision.update({
        "regresion_comercial_bloqueada": True,
        "accion_original_bloqueada": (
            accion_original
        ),
        "piso_comercial": piso.get(
            "piso",
            "",
        ),
        "nivel_piso_comercial": nivel_piso,
        "objetivo_retorno": str(
            contexto.get(
                "objetivo_pendiente",
                "",
            )
            or piso.get(
                "objetivo",
                "",
            )
            or ""
        ).strip().upper(),
    })

    if nueva_accion == "RESPONDER_TEMA":
        datos_decision[
            "preservar_progreso_comercial"
        ] = True

    print(
        "🛡️ REGRESIÓN COMERCIAL BLOQUEADA: "
        f"propuesta={accion_original}, "
        f"piso={piso.get('piso')}, "
        f"accion_corregida={nueva_accion}"
    )

    return decision_segura
    

# ============================================================
# CONTRATO DE MEMORIA HISTÓRICA DE LA CONVERSACIÓN
# ============================================================

class MemoriaHistoricaConversacion(BaseModel):
    """
    Representa la información comercial recuperada a partir
    del historial completo de una familia.

    Este contrato:
    - no genera respuestas;
    - no modifica contactos;
    - no modifica mensajes;
    - no cambia FLOW_STATE;
    - no guarda información;
    - solamente valida una extracción estructurada.
    """

    version: str = "1.0"

    nombre_tutor: str = ""

    alumnos: List[Dict[str, Any]] = Field(
        default_factory=list
    )

    zona_interes: str = ""
    referencia_colegio: str = ""

    niveles_interes: List[str] = Field(
        default_factory=list
    )

    grados_interes: List[str] = Field(
        default_factory=list
    )

    areas_interes: List[str] = Field(
        default_factory=list
    )

    temas_explicados: List[str] = Field(
        default_factory=list
    )

    objeciones_detectadas: List[str] = Field(
        default_factory=list
    )

    hitos_comerciales: List[str] = Field(
        default_factory=list
    )

    solicito_costos: bool = False
    costos_presentados: bool = False

    acepto_visita: bool = False
    cita_solicitada: bool = False
    cita_confirmada: bool = False

    fecha_cita_texto: str = ""
    fecha_cita_iso: str = ""

    hora_cita_texto: str = ""
    hora_cita_24h: str = ""

    ultimo_mensaje_prospecto: str = ""
    ultima_respuesta_asistente: str = ""

    etapa_conversacional_sugerida: str = (
        "CONTACTO_INICIAL"
    )

    estado_comercial_sugerido: str = (
        "PROSPECTO_NUEVO"
    )

    resumen_relacion: str = ""

    datos_confirmados: List[str] = Field(
        default_factory=list
    )

    datos_inciertos: List[str] = Field(
        default_factory=list
    )

    confianza: float = 0.0


def crear_memoria_historica_vacia() -> Dict[str, Any]:
    """
    Devuelve una memoria histórica segura y vacía.

    No consulta la base de datos.
    No llama a Gemini.
    No modifica contactos.
    No guarda información.
    """
    return MemoriaHistoricaConversacion().model_dump()

class AnalisisMensajeProspecto(BaseModel):
    """
    Contrato interno del análisis estructurado producido por Gemini.

    Esta clase no decide reglas críticas del negocio.
    Solamente representa y valida la interpretación del mensaje.
    """

    version: str = "1.0"

    saludo: bool = False
    saludo_simple: bool = False

    intencion_principal: str = "OTRO"
    intenciones_secundarias: List[str] = Field(default_factory=list)

    campus_mencionado: str = ""
    campus_externo: bool = False

    zona_mencionada: str = ""
    clasificacion_zona: str = "NO_MENCIONADA"

    nivel: str = ""
    grado: str = ""

    edad_alumno: Optional[int] = None
    fecha_nacimiento_texto: str = ""
    fecha_nacimiento_iso: str = ""

    nivel_actual: str = ""
    ultimo_grado_cursado: str = ""
    grado_solicitado: str = ""

    requiere_validar_pre_kinder: bool = False

    tema_interes: str = ""

    pide_costos: bool = False
    pide_cita: bool = False

    seguimiento_cita: bool = False
    solicitud_confirmacion_cita: bool = False
    cambio_fecha_cita: bool = False
    cancelacion_cita: bool = False
    desistimiento_temporal: bool = False
    asume_cita_confirmada: bool = False
    pregunta_paralela: bool = False
    reclamo_demora: bool = False
    contexto_cita_pendiente_reconocido: bool = False
    requiere_admin_contextual: bool = False

    fecha_cita_texto: str = ""
    hora_cita_texto: str = ""
    fecha_cita_iso: str = ""
    hora_cita_24h: str = ""
    dia_no_laboral: bool = False

    nombre_tutor: str = ""
    nombre_alumno: str = ""

    alumnos: List[Dict[str, Any]] = Field(
        default_factory=list
    )

    pausa_conversacion: bool = False

    # Relación semántica entre el mensaje actual y el objetivo
    # conversacional que ya estaba pendiente.
    relacion_con_objetivo_pendiente: str = "SIN_OBJETIVO"

    datos_detectados: List[str] = Field(default_factory=list)
    datos_faltantes: List[str] = Field(default_factory=list)

    accion_recomendada: str = "CONTINUAR_CONVERSACION"
    confianza: float = 0.0

def crear_analisis_mensaje_vacio() -> Dict[str, Any]:
    """
    Devuelve un análisis seguro con todos los campos y valores predeterminados.

    Se utiliza como fallback cuando Gemini falla, devuelve texto inválido
    o produce un JSON que no cumple el contrato.
    """
    return AnalisisMensajeProspecto().model_dump()


def normalizar_lista_textos(valor: Any) -> List[str]:
    """
    Convierte un valor recibido en una lista limpia de textos.

    Evita que Gemini entregue null, un string simple u otros tipos
    donde el contrato espera una lista.
    """
    if valor is None:
        return []

    if isinstance(valor, list):
        resultado = []

        for elemento in valor:
            texto = str(elemento or "").strip()

            if texto and texto not in resultado:
                resultado.append(texto)

        return resultado

    if isinstance(valor, str):
        texto = valor.strip()
        return [texto] if texto else []

    return []


def normalizar_booleano(valor: Any, predeterminado: bool = False) -> bool:
    """
    Normaliza booleanos aunque Gemini los entregue como texto o número.
    """
    if isinstance(valor, bool):
        return valor

    if isinstance(valor, int):
        return valor == 1

    if isinstance(valor, str):
        texto = valor.strip().lower()

        if texto in ["true", "1", "yes", "si", "sí", "verdadero"]:
            return True

        if texto in ["false", "0", "no", "falso"]:
            return False

    return predeterminado


def normalizar_entero_opcional(valor: Any) -> Optional[int]:
    """
    Convierte una edad a entero cuando sea posible.
    """
    if valor is None or valor == "":
        return None

    try:
        edad = int(valor)

        if 0 <= edad <= 25:
            return edad

    except (TypeError, ValueError):
        pass

    return None


def normalizar_confianza(valor: Any) -> float:
    """
    Garantiza que confianza quede entre 0.0 y 1.0.
    """
    try:
        confianza = float(valor)
    except (TypeError, ValueError):
        return 0.0

    return max(0.0, min(confianza, 1.0))


def normalizar_analisis_mensaje_ia(
    datos_crudos: Any
) -> Dict[str, Any]:
    """
    Limpia, completa y valida el JSON devuelto por Gemini.

    Importante:
    - No confía ciegamente en las etiquetas de la IA.
    - Sustituye valores desconocidos por valores seguros.
    - No aplica todavía las reglas críticas del negocio.
    - Siempre devuelve un diccionario con el contrato completo.
    """
    base = crear_analisis_mensaje_vacio()

    if not isinstance(datos_crudos, dict):
        print(
            "⚠️ El análisis IA no es un diccionario. "
            "Se utilizará el contrato vacío."
        )
        return base

    intencion_principal = str(
        datos_crudos.get("intencion_principal", "OTRO") or "OTRO"
    ).strip().upper()

    if intencion_principal not in INTENCIONES_PRINCIPALES_VALIDAS:
        intencion_principal = "OTRO"

    intenciones_secundarias_crudas = normalizar_lista_textos(
        datos_crudos.get("intenciones_secundarias")
    )

    intenciones_secundarias = []

    for intencion in intenciones_secundarias_crudas:
        intencion_normalizada = intencion.strip().upper()

        if (
            intencion_normalizada in INTENCIONES_PRINCIPALES_VALIDAS
            and intencion_normalizada != intencion_principal
            and intencion_normalizada not in intenciones_secundarias
        ):
            intenciones_secundarias.append(intencion_normalizada)

    clasificacion_zona = str(
        datos_crudos.get(
            "clasificacion_zona",
            "NO_MENCIONADA"
        ) or "NO_MENCIONADA"
    ).strip().upper()

    if clasificacion_zona not in CLASIFICACIONES_ZONA_VALIDAS:
        clasificacion_zona = "NO_MENCIONADA"

    nivel = str(
        datos_crudos.get("nivel", "") or ""
    ).strip()

    equivalencias_nivel = {
        "kinder": "Kínder",
        "kínder": "Kínder",
        "preescolar": "Kínder",
        "primaria": "Primaria",
        "secundaria": "Secundaria",
    }

    nivel_normalizado = equivalencias_nivel.get(
        nivel.lower(),
        nivel
    )

    if nivel_normalizado not in NIVELES_OFICIALES_VALIDOS:
        nivel_normalizado = ""

    tema_interes = str(
        datos_crudos.get("tema_interes", "") or ""
    ).strip().lower()

    if tema_interes not in TEMAS_INTERES_VALIDOS:
        tema_interes = ""

    accion_recomendada = str(
        datos_crudos.get(
            "accion_recomendada",
            "CONTINUAR_CONVERSACION"
        ) or "CONTINUAR_CONVERSACION"
    ).strip().upper()

    if accion_recomendada not in ACCIONES_RECOMENDADAS_VALIDAS:
        accion_recomendada = "CONTINUAR_CONVERSACION"

    relacion_con_objetivo_pendiente = str(
        datos_crudos.get(
            "relacion_con_objetivo_pendiente",
            "SIN_OBJETIVO",
        )
        or "SIN_OBJETIVO"
    ).strip().upper()

    if (
        relacion_con_objetivo_pendiente
        not in RELACIONES_OBJETIVO_VALIDAS
    ):
        relacion_con_objetivo_pendiente = (
            "SIN_OBJETIVO"
        )

    datos_detectados_crudos = normalizar_lista_textos(
        datos_crudos.get("datos_detectados")
    )

    equivalencias_datos_detectados = {
        "referencia": "referencia_colegio",
        "referencia_colegio": "referencia_colegio",
        "referencia_del_colegio": "referencia_colegio",

        "zona": "zona_interes",
        "zona_interes": "zona_interes",
        "localidad": "zona_interes",
        "municipio": "zona_interes",

        "nivel": "nivel",
        "nivel_interes": "nivel",

        "grado": "grado_solicitado",
        "grado_interes": "grado_solicitado",
        "grado_solicitado": "grado_solicitado",
    }

    datos_detectados_normalizados = []

    for dato in datos_detectados_crudos:
        clave_dato = re.sub(
            r"[^a-z0-9_]+",
            "_",
            unicodedata.normalize(
                "NFD",
                str(dato or "").strip().lower(),
            ).encode(
                "ascii",
                "ignore",
            ).decode(
                "ascii"
            ),
        ).strip("_")

        dato_canonico = equivalencias_datos_detectados.get(
            clave_dato,
            clave_dato,
        )

        if (
            dato_canonico
            and dato_canonico
            not in datos_detectados_normalizados
        ):
            datos_detectados_normalizados.append(
                dato_canonico
            )

    # --------------------------------------------------------
    # UNO O VARIOS ALUMNOS DETECTADOS EN EL MENSAJE
    # --------------------------------------------------------

    alumnos_crudos = datos_crudos.get(
        "alumnos",
        [],
    )

    alumnos_normalizados = []

    if isinstance(
        alumnos_crudos,
        list,
    ):

        for alumno_crudo in alumnos_crudos:

            if not isinstance(
                alumno_crudo,
                dict,
            ):
                continue

            nombre = str(
                alumno_crudo.get(
                    "nombre",
                    "",
                )
                or ""
            ).strip()

            nivel = str(
                alumno_crudo.get(
                    "nivel",
                    alumno_crudo.get(
                        "nivel_interes",
                        "",
                    ),
                )
                or ""
            ).strip()

            nivel = equivalencias_nivel.get(
                nivel.lower(),
                nivel,
            )

            if (
                nivel
                not in NIVELES_OFICIALES_VALIDOS
            ):
                nivel = ""

            grado = str(
                alumno_crudo.get(
                    "grado",
                    alumno_crudo.get(
                        "grado_interes",
                        "",
                    ),
                )
                or ""
            ).strip()

            edad = normalizar_entero_opcional(
                alumno_crudo.get(
                    "edad",
                    alumno_crudo.get(
                        "edad_alumno",
                    ),
                )
            )

            fecha_nacimiento = str(
                alumno_crudo.get(
                    "fecha_nacimiento",
                    alumno_crudo.get(
                        "fecha_nacimiento_iso",
                        "",
                    ),
                )
                or ""
            ).strip()

            if not any(
                [
                    nombre,
                    nivel,
                    grado,
                    edad is not None,
                    fecha_nacimiento,
                ]
            ):
                continue

            alumnos_normalizados.append({
                "nombre": nombre,
                "nivel": nivel,
                "grado": grado,
                "edad": edad,
                "fecha_nacimiento": (
                    fecha_nacimiento
                ),
            })

    analisis_normalizado = {
        "version": "1.0",

        "saludo": normalizar_booleano(
            datos_crudos.get("saludo")
        ),
        "saludo_simple": normalizar_booleano(
            datos_crudos.get("saludo_simple")
        ),

        "intencion_principal": intencion_principal,
        "intenciones_secundarias": intenciones_secundarias,

        "campus_mencionado": str(
            datos_crudos.get("campus_mencionado", "") or ""
        ).strip(),
        "campus_externo": normalizar_booleano(
            datos_crudos.get("campus_externo")
        ),

        "zona_mencionada": str(
            datos_crudos.get("zona_mencionada", "") or ""
        ).strip(),
        "clasificacion_zona": clasificacion_zona,

        "nivel": nivel_normalizado,
        "grado": str(
            datos_crudos.get("grado", "") or ""
        ).strip(),

        "edad_alumno": normalizar_entero_opcional(
            datos_crudos.get("edad_alumno")
        ),

        "fecha_nacimiento_texto": str(
            datos_crudos.get(
                "fecha_nacimiento_texto",
                "",
            ) or ""
        ).strip(),

        "fecha_nacimiento_iso": str(
            datos_crudos.get(
                "fecha_nacimiento_iso",
                "",
            ) or ""
        ).strip(),

        "nivel_actual": str(
            datos_crudos.get(
                "nivel_actual",
                "",
            ) or ""
        ).strip(),

        "ultimo_grado_cursado": str(
            datos_crudos.get(
                "ultimo_grado_cursado",
                "",
            ) or ""
        ).strip(),

        "grado_solicitado": str(
            datos_crudos.get(
                "grado_solicitado",
                "",
            ) or ""
        ).strip(),

        "requiere_validar_pre_kinder": normalizar_booleano(
            datos_crudos.get(
                "requiere_validar_pre_kinder"
            )
        ),

        "tema_interes": tema_interes,

        "pide_costos": normalizar_booleano(
            datos_crudos.get("pide_costos")
        ),
        "pide_cita": normalizar_booleano(
            datos_crudos.get("pide_cita")
        ),

        "seguimiento_cita": normalizar_booleano(
            datos_crudos.get("seguimiento_cita")
        ),
        "solicitud_confirmacion_cita": normalizar_booleano(
            datos_crudos.get(
                "solicitud_confirmacion_cita"
            )
        ),
        "cambio_fecha_cita": normalizar_booleano(
            datos_crudos.get("cambio_fecha_cita")
        ),
        "cancelacion_cita": normalizar_booleano(
            datos_crudos.get("cancelacion_cita")
        ),
        "desistimiento_temporal": normalizar_booleano(
            datos_crudos.get("desistimiento_temporal")
        ),
        "asume_cita_confirmada": normalizar_booleano(
            datos_crudos.get("asume_cita_confirmada")
        ),
        "pregunta_paralela": normalizar_booleano(
            datos_crudos.get("pregunta_paralela")
        ),
        "reclamo_demora": normalizar_booleano(
            datos_crudos.get("reclamo_demora")
        ),
        "contexto_cita_pendiente_reconocido": (
            normalizar_booleano(
                datos_crudos.get(
                    "contexto_cita_pendiente_reconocido"
                )
            )
        ),
        "requiere_admin_contextual": normalizar_booleano(
            datos_crudos.get(
                "requiere_admin_contextual"
            )
        ),

        "fecha_cita_texto": str(
            datos_crudos.get("fecha_cita_texto", "") or ""
        ).strip(),
        "hora_cita_texto": str(
            datos_crudos.get("hora_cita_texto", "") or ""
        ).strip(),
        "fecha_cita_iso": str(
            datos_crudos.get("fecha_cita_iso", "") or ""
        ).strip(),
        "hora_cita_24h": str(
            datos_crudos.get("hora_cita_24h", "") or ""
        ).strip(),
        "dia_no_laboral": normalizar_booleano(
            datos_crudos.get("dia_no_laboral")
        ),

        "nombre_tutor": str(
            datos_crudos.get("nombre_tutor", "") or ""
        ).strip(),

        "nombre_alumno": str(
            datos_crudos.get("nombre_alumno", "") or ""
        ).strip(),

        "alumnos": alumnos_normalizados,

        "pausa_conversacion":
            normalizar_booleano(
            datos_crudos.get("pausa_conversacion")
        ),

        "relacion_con_objetivo_pendiente": (
            relacion_con_objetivo_pendiente
        ),

        "datos_detectados": datos_detectados_normalizados,
        "datos_faltantes": normalizar_lista_textos(
            datos_crudos.get("datos_faltantes")
        ),

        "accion_recomendada": accion_recomendada,
        "confianza": normalizar_confianza(
            datos_crudos.get("confianza")
        ),
    }

    try:
        analisis_validado = AnalisisMensajeProspecto.model_validate(
            analisis_normalizado
        )

        return analisis_validado.model_dump()

    except Exception as e:
        print(f"⚠️ Error validando contrato de análisis IA: {e}")
        return base
        

def obtener_modelos_gemini():
    """
    Devuelve el modelo principal + modelos de respaldo configurados en Railway.
    """
    modelo_principal = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()

    modelos_respaldo = [
        model.strip()
        for model in os.getenv(
            "GEMINI_FALLBACK_MODELS",
            ""
        ).split(",")
        if model.strip()
    ]

    modelos = []

    if modelo_principal:
        modelos.append(modelo_principal)

    for modelo in modelos_respaldo:
        if modelo not in modelos:
            modelos.append(modelo)

    return modelos


# ============================================================
# POLÍTICA CENTRAL DE EJECUCIÓN GEMINI
# ============================================================

IA_PRESUPUESTOS_SALIDA = {
    # Respuestas que legítimamente deben ser muy pequeñas.
    "ETIQUETA": {
        "base": 1000,
        "maximo": 2000,
    },

    # Fecha, hora o pequeños conjuntos de datos.
    "EXTRACCION_CORTA": {
        "base": 2000,
        "maximo": 4000,
    },

    # Contratos JSON, análisis y extracción estructurada.
    "JSON_ESTRUCTURADO": {
        "base": 8000,
        "maximo": 16000,
    },

    # Respuestas naturales dirigidas a personas.
    "RESPUESTA_CONVERSACIONAL": {
        "base": 8000,
        "maximo": 16000,
    },

    # Reconstrucción o resumen de conversaciones extensas.
    "MEMORIA_HISTORICA": {
        "base": 12000,
        "maximo": 20000,
    },
}


def clasificar_tipo_tarea_gemini(
    tarea: str,
) -> str:
    """
    Determina el presupuesto técnico apropiado según la naturaleza
    de la tarea.

    No interpreta mensajes del usuario.
    No modifica el flujo comercial.
    """

    tarea_normalizada = str(
        tarea or ""
    ).strip().lower()

    if any(
        expresion in tarea_normalizada
        for expresion in [
            "clasificación de alcance campus",
            "clasificacion de alcance campus",
            "clasificación de intención",
            "clasificacion de intención",
            "clasificación respuesta admin cita",
            "clasificacion respuesta admin cita",
            "clasificación resolución admin zona",
            "clasificacion resolucion admin zona",
        ]
    ):
        return "ETIQUETA"

    if any(
        expresion in tarea_normalizada
        for expresion in [
            "extracción hora cita",
            "extraccion hora cita",
        ]
    ):
        return "EXTRACCION_CORTA"

    if any(
        expresion in tarea_normalizada
        for expresion in [
            "análisis estructurado",
            "analisis estructurado",
            "clasificación de alcance ia",
            "clasificacion de alcance ia",
            "extracción datos cita",
            "extraccion datos cita",
        ]
    ):
        return "JSON_ESTRUCTURADO"

    if any(
        expresion in tarea_normalizada
        for expresion in [
            "memoria histórica",
            "memoria historica",
        ]
    ):
        return "MEMORIA_HISTORICA"

    return "RESPUESTA_CONVERSACIONAL"


def extraer_valor_generation_config(
    generation_config,
    campo: str,
    predeterminado=None,
):
    """
    Lee de forma tolerante un valor de GenerationConfig,
    independientemente de que llegue como objeto o diccionario.
    """

    if generation_config is None:
        return predeterminado

    try:
        if isinstance(
            generation_config,
            dict,
        ):
            return generation_config.get(
                campo,
                predeterminado,
            )

        valor = getattr(
            generation_config,
            campo,
            predeterminado,
        )

        return (
            predeterminado
            if valor is None
            else valor
        )

    except Exception:
        return predeterminado


def construir_generation_config_gemini(
    generation_config=None,
    max_output_tokens: Optional[int] = None,
):
    """
    Construye una configuración compatible con las llamadas actuales.

    Conserva los parámetros de generación ya utilizados por el bot,
    pero permite administrar centralmente el techo de salida.
    """

    temperatura = extraer_valor_generation_config(
        generation_config,
        "temperature",
        None,
    )

    argumentos = {}

    if temperatura is not None:
        argumentos["temperature"] = temperatura

    if max_output_tokens is not None:
        argumentos[
            "max_output_tokens"
        ] = int(max_output_tokens)

    return genai.types.GenerationConfig(
        **argumentos
    )


def normalizar_finish_reason_gemini(
    valor,
) -> str:
    """
    Convierte FinishReason a una etiqueta estable como:
    STOP, MAX_TOKENS, SAFETY, OTHER, etc.
    """

    if valor is None:
        return ""

    try:
        nombre = getattr(
            valor,
            "name",
            None,
        )

        if nombre:
            return str(nombre).strip().upper()
    except Exception:
        pass

    texto = str(valor or "").strip().upper()

    if "." in texto:
        texto = texto.rsplit(
            ".",
            1,
        )[-1]

    return texto


def extraer_metricas_respuesta_gemini(
    response,
) -> Dict[str, Any]:
    """
    Recupera motivo de finalización y consumo real de tokens
    sin afectar la respuesta.

    Todos los campos son tolerantes a SDKs/versiones donde algún
    atributo no esté disponible.
    """

    metricas = {
        "finish_reason": "",
        "finish_message": "",
        "prompt_tokens": None,
        "output_tokens": None,
        "thoughts_tokens": None,
        "cached_tokens": None,
        "total_tokens": None,
    }

    try:
        candidates = getattr(
            response,
            "candidates",
            None,
        ) or []

        if candidates:
            candidate = candidates[0]

            metricas[
                "finish_reason"
            ] = normalizar_finish_reason_gemini(
                getattr(
                    candidate,
                    "finish_reason",
                    None,
                )
            )

            metricas[
                "finish_message"
            ] = str(
                getattr(
                    candidate,
                    "finish_message",
                    "",
                )
                or ""
            ).strip()

    except Exception:
        pass

    try:
        usage = getattr(
            response,
            "usage_metadata",
            None,
        )

        if usage is not None:
            try:
                print(
                    "🔬 GEMINI_USAGE_DEBUG: "
                    + json.dumps(
                        {
                            "response_type": (
                                type(response).__name__
                            ),
                            "usage_type": (
                                type(usage).__name__
                            ),
                            "usage_repr": repr(usage),
                            "usage_dict": (
                                getattr(
                                    usage,
                                    "__dict__",
                                    None,
                                )
                            ),
                            "usage_dir_filtrado": [
                                atributo
                                for atributo in dir(usage)
                                if (
                                    "token" in atributo.lower()
                                    or "prompt" in atributo.lower()
                                    or "candidate" in atributo.lower()
                                    or "cached" in atributo.lower()
                                    or "thought" in atributo.lower()
                                )
                            ],
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )
            except Exception as e:
                print(
                    "⚠️ No se pudo imprimir "
                    f"GEMINI_USAGE_DEBUG: {e}"
                )
            campos_usage = {
                "prompt_tokens": (
                    "prompt_token_count"
                ),
                "output_tokens": (
                    "candidates_token_count"
                ),
                "thoughts_tokens": (
                    "thoughts_token_count"
                ),
                "cached_tokens": (
                    "cached_content_token_count"
                ),
                "total_tokens": (
                    "total_token_count"
                ),
            }

            for destino, origen in (
                campos_usage.items()
            ):
                try:
                    valor = getattr(
                        usage,
                        origen,
                        None,
                    )

                    if valor is not None:
                        metricas[destino] = int(
                            valor
                        )

                except Exception:
                    pass

    except Exception:
        pass

    return metricas


def generar_con_gemini_con_fallback(
    contenido,
    generation_config=None,
    tarea: str = "gemini",
):
    """
    Administrador central de ejecución Gemini.

    Principios:
    - El límite de salida es un fusible técnico, no una forma
      de recortar respuestas legítimas.
    - Registra consumo real y motivo de finalización.
    - Si Gemini termina por MAX_TOKENS, amplía automáticamente
      el presupuesto antes de considerar fallida la generación.
    - Mantiene fallback entre modelos.
    """

    ultimo_error = None

    tipo_tarea = clasificar_tipo_tarea_gemini(
        tarea
    )

    politica = IA_PRESUPUESTOS_SALIDA.get(
        tipo_tarea,
        IA_PRESUPUESTOS_SALIDA[
            "RESPUESTA_CONVERSACIONAL"
        ],
    )

    presupuesto_base = int(
        politica["base"]
    )

    presupuesto_maximo = int(
        politica["maximo"]
    )

    for model_name in obtener_modelos_gemini():

        presupuesto_actual = (
            presupuesto_base
        )

        intento_tokens = 0

        while True:
            intento_tokens += 1

            try:
                print(
                    "🧠 Probando Gemini: "
                    f"tarea={tarea}, "
                    f"modelo={model_name}, "
                    f"tipo={tipo_tarea}, "
                    f"max_output_tokens="
                    f"{presupuesto_actual}, "
                    f"intento_tokens={intento_tokens}"
                )

                model = genai.GenerativeModel(
                    model_name
                )

                config_efectiva = (
                    construir_generation_config_gemini(
                        generation_config=(
                            generation_config
                        ),
                        max_output_tokens=(
                            presupuesto_actual
                        ),
                    )
                )

                response = model.generate_content(
                    contenido,
                    generation_config=(
                        config_efectiva
                    ),
                )

                metricas = (
                    extraer_metricas_respuesta_gemini(
                        response
                    )
                )

                metricas_log = {
                    "tarea": tarea,
                    "modelo": model_name,
                    "tipo_tarea": tipo_tarea,
                    "max_output_tokens": (
                        presupuesto_actual
                    ),
                    **metricas,
                }

                print(
                    "📊 GEMINI_METRICAS: "
                    + json.dumps(
                        metricas_log,
                        ensure_ascii=False,
                        default=str,
                    )
                )

                finish_reason = str(
                    metricas.get(
                        "finish_reason",
                        "",
                    )
                    or ""
                ).strip().upper()

                if (
                    finish_reason
                    == "MAX_TOKENS"
                ):
                    if (
                        presupuesto_actual
                        < presupuesto_maximo
                    ):
                        nuevo_presupuesto = min(
                            presupuesto_actual * 2,
                            presupuesto_maximo,
                        )

                        print(
                            "⚠️ Gemini alcanzó "
                            "MAX_TOKENS. "
                            "Se regenerará la respuesta "
                            "con mayor margen: "
                            f"{presupuesto_actual} → "
                            f"{nuevo_presupuesto}"
                        )

                        presupuesto_actual = (
                            nuevo_presupuesto
                        )

                        continue

                    ultimo_error = RuntimeError(
                        "Gemini alcanzó MAX_TOKENS "
                        "incluso con el presupuesto "
                        "máximo de seguridad "
                        f"({presupuesto_maximo})."
                    )

                    print(
                        "⚠️ "
                        f"{ultimo_error}"
                    )

                    break

                print(
                    "✅ Gemini usado para "
                    f"{tarea}: {model_name}"
                )

                return response, model_name

            except Exception as e:
                ultimo_error = e

                print(
                    "⚠️ Falló Gemini para "
                    f"{tarea} con {model_name}: {e}"
                )

                break

    raise RuntimeError(
        "Todos los modelos Gemini fallaron "
        f"para {tarea}. "
        f"Último error: {ultimo_error}"
    )

def extraer_texto_respuesta_gemini(response) -> str:
    """
    Extrae texto de una respuesta Gemini aunque no venga como response.text simple.
    """
    try:
        if hasattr(response, "text") and response.text:
            return response.text.strip()
    except Exception:
        pass

    try:
        parts = response.candidates[0].content.parts
        texto = "".join(
            part.text for part in parts
            if hasattr(part, "text") and part.text
        )
        return texto.strip()
    except Exception:
        return ""

def extraer_json_de_texto(texto: str) -> Optional[Dict[str, Any]]:
    """
    Extrae un objeto JSON de una respuesta de Gemini.

    Tolera:
    - JSON directo.
    - Bloques ```json ... ```.
    - Texto adicional antes o después del objeto.
    """
    contenido = (texto or "").strip()

    if not contenido:
        return None

    # Elimina cercas Markdown frecuentes.
    if contenido.startswith("```"):
        contenido = re.sub(
            r"^```(?:json)?\s*",
            "",
            contenido,
            flags=re.IGNORECASE,
        )
        contenido = re.sub(
            r"\s*```$",
            "",
            contenido,
        )
        contenido = contenido.strip()

    # Primer intento: todo el contenido es JSON.
    try:
        datos = json.loads(contenido)

        if isinstance(datos, dict):
            return datos

    except json.JSONDecodeError:
        pass

    # Segundo intento: extraer desde la primera llave hasta la última.
    inicio = contenido.find("{")
    fin = contenido.rfind("}")

    if inicio == -1 or fin == -1 or fin <= inicio:
        return None

    fragmento = contenido[inicio:fin + 1]

    try:
        datos = json.loads(fragmento)

        if isinstance(datos, dict):
            return datos

    except json.JSONDecodeError:
        return None

    return None

# ============================================================
# CLASIFICACIÓN IA DEL ALCANCE GENERAL DE LA CONVERSACIÓN
# ============================================================

def detectar_admisiones_evidentes_para_alcance(
    mensaje_usuario: str,
) -> bool:
    """
    Detecta solicitudes explícitas e inequívocas de admisiones.

    Sólo evita la clasificación IA cuando el mensaje contiene
    evidencia suficientemente clara de interés escolar/admisiones.

    No modifica estado, CRM ni base de datos.
    """

    mensaje_normalizado = (
        normalizar_texto_para_deteccion(
            mensaje_usuario
        )
    )

    if not mensaje_normalizado:
        return False

    # --------------------------------------------------------
    # EXPRESIONES INEQUÍVOCAS DE INFORMES ESCOLARES
    # --------------------------------------------------------

    expresiones_directas = [
        "mas informacion",
        "más información",
        "quiero mas informacion",
        "quiero más información",
        "quisiera mas informacion",
        "quisiera más información",
        "necesito mas informacion",
        "necesito más información",
        "quiero informes",
        "quisiera informes",
        "solicito informes",
        "necesito informes",
        "me puede dar informes",
        "me pueden dar informes",
        "me interesa el colegio",
        "informacion sobre primaria",
        "informacion sobre secundaria",
        "informacion sobre kinder",
        "informacion de primaria",
        "informacion de secundaria",
        "informacion de kinder",
        "me interesa primaria",
        "me interesa secundaria",
        "me interesa kinder",
        "agendar una cita",
        "agendar cita",
        "agendar una visita",
        "agendar visita",
        "programar una cita",
        "programar cita",
        "programar una visita",
        "programar visita",
        "reservar una cita",
        "reservar cita",
        "reservar una visita",
        "reservar visita",
        "quiero reservar una cita",
        "quisiera reservar una cita",
        "puedo reservar una cita",
        "se puede reservar una cita",
        "quiero una cita",
        "quisiera una cita",
        "quiero agendar",
        "quisiera agendar",
        "quiero visitar el colegio",
        "quisiera visitar el colegio",
    ]

    if any(
        expresion in mensaje_normalizado
        for expresion in expresiones_directas
    ):
        return True

    # --------------------------------------------------------
    # "INFORMACIÓN" GENÉRICA SÓLO CUANDO EXISTE CONTEXTO
    # ESCOLAR O DE ADMISIÓN
    # --------------------------------------------------------

    pide_informacion_generica = any(
        expresion in mensaje_normalizado
        for expresion in [
            "quiero informacion",
            "quisiera informacion",
            "necesito informacion",
            "solicito informacion",
        ]
    )

    if not pide_informacion_generica:
        return False

    indicadores_admisiones = [
        "inscribir",
        "inscripcion",
        "admision",
        "nuevo ingreso",
        "alumno",
        "hijo",
        "hija",
        "niño",
        "niña",
        "colegio",
        "escuela",
        "kinder",
        "preescolar",
        "primaria",
        "secundaria",
        "colegiatura",
        "beca",
        "grado",
    ]

    return any(
        indicador in mensaje_normalizado
        for indicador in indicadores_admisiones
    )
    
def clasificar_alcance_conversacion_con_ia(
    mensaje_usuario: str,
    historial_lista: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Clasifica el motivo general por el que una persona
    se comunica con el colegio.

    Esta función se ejecutará antes del análisis comercial
    de admisiones.

    No genera una respuesta para el contacto.
    No modifica la base de datos.
    No modifica el CRM.
    No cambia FLOW_STATE.
    No crea tareas administrativas.
    No envía mensajes por Twilio.
    """

    resultado_fallo = {
        "exitoso": False,
        "alcance": crear_alcance_conversacion_vacio(),
        "modelo_usado": "",
        "intentos_realizados": 0,
        "errores": [],
    }

    mensaje = str(
        mensaje_usuario or ""
    ).strip()

    if not mensaje:
        resultado_fallo["errores"].append(
            "MENSAJE_USUARIO_VACIO"
        )

        return resultado_fallo

    historial_seguro = (
        historial_lista
        if isinstance(historial_lista, list)
        else []
    )

    historial_limpio = []

    for elemento in historial_seguro[-10:]:
        texto = str(
            elemento or ""
        ).strip()

        if texto:
            historial_limpio.append(
                texto
            )

    historial_texto = (
        "\n".join(historial_limpio)
        if historial_limpio
        else "Sin historial previo disponible."
    )

    contrato_json = json.dumps(
        crear_alcance_conversacion_vacio(),
        ensure_ascii=False,
        indent=2,
    )

    categorias_json = json.dumps(
        sorted(
            ALCANCES_CONVERSACION_VALIDOS
        ),
        ensure_ascii=False,
    )

    prompt_base = f"""
Eres un clasificador de alcance conversacional para el
Colegio Valle de Filadelfia Campus Santa Cruz Atizapán.

Tu única tarea es identificar el motivo general por el que
la persona se comunica.

No redactes una respuesta para la persona.
No analices todavía zona, nivel escolar, grado, costos,
referencias del colegio ni etapas del proceso de admisiones.
No decidas todavía qué mensaje debe enviar el bot.

CATEGORÍAS PERMITIDAS:

{categorias_json}

DEFINICIÓN DE CADA CATEGORÍA:

1. AMBIGUO

Utiliza AMBIGUO cuando todavía no sea posible conocer
el motivo real del contacto.

Ejemplos:

- Hola.
- Buenas tardes.
- ¿Es el canal del colegio?
- Vi su anuncio.
- Quisiera información.

Una frase prellenada automáticamente por una campaña de
Facebook o WhatsApp no confirma por sí sola que la persona
busque admisiones.

2. ADMISIONES

Utiliza ADMISIONES cuando exista intención de solicitar
información para inscribir o evaluar el ingreso de un alumno
a Maternal, Kínder, Primaria o Secundaria.

También corresponde cuando solicitan información sobre:

- colegiaturas para nuevo ingreso;
- inscripción;
- requisitos de admisión;
- modelo educativo;
- instalaciones;
- horarios escolares;
- becas de nuevo ingreso;
- visita para conocer el colegio;
- niveles o grados para un futuro alumno.

3. EMPLEO

Utiliza EMPLEO cuando la persona busque:

- una vacante;
- trabajo;
- empleo;
- entregar currículum;
- participar en reclutamiento;
- información sobre contratación;
- trabajar como docente o personal administrativo.

Una expresión como "profesor de secundaria" corresponde
a EMPLEO y no significa interés de admisiones en Secundaria.

4. ALUMNOS_ACTUALES

Utiliza ALUMNOS_ACTUALES cuando la persona indique
claramente que ya es:

- madre, padre o tutor de un alumno inscrito;
- alumno actual;
- integrante de una familia actualmente inscrita.

Puede preguntar sobre pagos, horarios, actividades,
plataformas, uniformes, materiales, profesores o asuntos
cotidianos del ciclo escolar.

La condición de familia actual tiene prioridad sobre otras
categorías administrativas.

5. TRAMITES_ADMINISTRATIVOS

Utiliza TRAMITES_ADMINISTRATIVOS cuando se solicite un
trámite institucional concreto, por ejemplo:

- constancias;
- facturas;
- recibos;
- documentos;
- certificados;
- bajas;
- aclaraciones administrativas;
- referencias de pago;
- asuntos de control escolar.

Si la persona confirma que es familia de un alumno actual,
prefiere ALUMNOS_ACTUALES y describe el trámite en
"motivo_principal".

6. PROVEEDORES

Utiliza PROVEEDORES cuando una persona, negocio o empresa
esté ofreciendo al colegio un producto, servicio, cotización,
venta, suministro, colaboración comercial o reunión de trabajo.

Incluye expresamente casos como:

- ofrecer productos;
- ofrecer servicios profesionales;
- ofrecer uniformes escolares;
- ofrecer mobiliario, tecnología, alimentos, materiales o insumos;
- presentar una cotización;
- mejorar precios o costos actuales del colegio;
- solicitar una cita para presentar un producto o servicio;
- solicitar contacto para ventas;
- participar como proveedor;
- establecer una alianza comercial.

REGLA CRÍTICA:

Si la persona está tratando de vender, ofrecer o presentar algo
AL COLEGIO, la categoría debe ser PROVEEDORES.

La palabra "cita" no convierte este caso en ADMISIONES.

La palabra "uniformes" no convierte este caso en
ALUMNOS_ACTUALES.

La palabra "costos", "precios" o "presupuesto" no significa que
esté solicitando colegiaturas cuando el contexto indica que está
ofreciendo un producto o servicio.

No utilices AMBIGUO cuando el mensaje deja claro que la persona
está ofreciendo comercialmente algo al colegio.

7. OTRO_CONFIGURADO

Utiliza OTRO_CONFIGURADO únicamente cuando el motivo
no sea admisiones, empleo, alumnos actuales, trámites o
proveedores, pero exista una ruta institucional conocida
y segura que pueda atenderse automáticamente.

No utilices esta categoría solamente para evitar
SIN_RUTA_CONFIGURADA.

8. SIN_RUTA_CONFIGURADA

Utiliza SIN_RUTA_CONFIGURADA cuando:

- el motivo ya es comprensible;
- no corresponde a ninguna ruta definida;
- requiere una decisión humana;
- faltan políticas institucionales confirmadas;
- responder obligaría a inventar datos o procedimientos;
- existe una situación inusual que debe revisar el
  administrador.

REGLAS DE CLASIFICACIÓN:

1. Analiza el mensaje actual junto con el historial reciente.

2. El mensaje actual tiene prioridad, pero debes utilizar
el historial para resolver referencias como "sí", "eso",
"la vacante", "mi hijo" o "el pago".

2-A. La ruta identifica el DOMINIO de la conversación, no si existe
una nueva solicitud que deba resolverse.

Si el historial muestra que existe una conversación de ADMISIONES
ya establecida, también pertenecen a ADMISIONES las respuestas que:

- aceptan o rechazan continuar;
- cancelan o posponen el proceso;
- indican que ya eligieron o inscribieron al alumno en otra escuela;
- agradecen y dan por terminada la conversación;
- responden a una decisión previamente comunicada por el colegio;
- cierran naturalmente el proceso de admisiones.

Un cierre, desistimiento, negativa o agradecimiento dentro de una
conversación de admisiones NO es una nueva ruta y NO debe
clasificarse como SIN_RUTA_CONFIGURADA solamente porque ya no
requiera más información.

SIN_RUTA_CONFIGURADA se utiliza para un motivo real distinto,
comprensible y sin ruta institucional definida; no para cerrar una
ruta que ya estaba identificada.

3. No clasifiques automáticamente como ADMISIONES porque
la conversación provenga de una campaña publicitaria.

4. No extraigas niveles escolares cuando el contexto sea
EMPLEO, PROVEEDORES, TRÁMITES o cualquier categoría
distinta de ADMISIONES.

5. Si existen dos motivos, identifica como categoría principal
el motivo que la persona está intentando resolver ahora.

6. "ruta_configurada" debe ser true exclusivamente para:

- ADMISIONES
- EMPLEO
- ALUMNOS_ACTUALES
- TRAMITES_ADMINISTRATIVOS
- PROVEEDORES
- OTRO_CONFIGURADO

7. La categoría AMBIGUO puede representar dos situaciones distintas:

A. APERTURA SOCIAL SIN SOLICITUD

Ejemplos conceptuales:

- un saludo;
- una cortesía;
- una apertura social;
- una expresión que todavía no contiene ninguna solicitud.

En estos casos:

- "alcance_conversacion" debe ser "AMBIGUO";
- "requiere_aclaracion" debe ser false;
- "motivo_principal" debe indicar que se trata de una apertura
  social sin solicitud concreta;
- no inventes que la persona está pidiendo informes;
- el sistema podrá responder de forma conversacional y natural.

B. CONSULTA INCOMPLETA O MOTIVO INDEFINIDO

Se presenta cuando la persona sí parece intentar iniciar una
consulta, pero todavía no permite saber si corresponde a
admisiones, empleo, proveedores, alumnos actuales, trámites
administrativos u otro motivo.

En estos casos:

- "alcance_conversacion" debe ser "AMBIGUO";
- "requiere_aclaracion" debe ser true;
- "motivo_principal" debe explicar qué parte de la intención
  permanece indefinida.

No utilices listas rígidas de frases para decidir entre ambos
casos. Interpreta conversacionalmente si existe una solicitud
real o solamente una apertura social.

8. "requiere_admin" debe ser true cuando la clasificación sea
SIN_RUTA_CONFIGURADA.

9. En "motivo_principal", describe el motivo de contacto en
una frase breve y concreta.

10. En "resumen_solicitud", resume únicamente lo que la
persona está solicitando, sin inventar información.

11. En "motivo_escalacion", explica brevemente por qué se
requiere revisión humana. Déjalo vacío si no se requiere.

12. "confianza" debe ser un número entre 0.0 y 1.0.

13. Devuelve exclusivamente un objeto JSON válido.

No uses Markdown.
No agregues explicaciones.
No escribas texto antes ni después del JSON.

CONTRATO OBLIGATORIO:

{contrato_json}

HISTORIAL RECIENTE:

{historial_texto}

MENSAJE ACTUAL:

{mensaje}
"""

    instrucciones_reintento = """

REINTENTO SEMÁNTICO OBLIGATORIO:

La respuesta anterior fue recibida técnicamente, pero no pudo
validarse correctamente como una clasificación de alcance.

Vuelve a interpretar el MENSAJE ACTUAL junto con el HISTORIAL
RECIENTE.

Recuerda especialmente:

- El mensaje actual tiene prioridad.
- El historial sirve para resolver respuestas breves o referencias.
- Un saludo simple puede ser AMBIGUO sin requerir aclaración.
- Una solicitud incompleta puede ser AMBIGUO y sí requerir aclaración.
- No clasifiques automáticamente como ADMISIONES solo porque
  el contacto provenga de una campaña.
- No confundas una vacante docente con interés en un nivel escolar.
- Si la persona ya dejó claro el motivo actual, conserva esa ruta
  aunque el mensaje actual sea breve, como "sí", "no", "eso",
  "perfecto", "gracias" o una precisión relacionada.
- No inventes una ruta que el contexto no respalde.

Devuelve exclusivamente un objeto JSON válido.
Respeta exactamente el contrato indicado.
No uses Markdown.
No agregues explicaciones.
No redactes una respuesta para el contacto.
No escribas texto antes ni después del JSON.
"""

    total_intentos_semanticos = 2

    for numero_intento in range(
        1,
        total_intentos_semanticos + 1,
    ):
        resultado_fallo[
            "intentos_realizados"
        ] += 1

        prompt_intento = prompt_base

        if numero_intento > 1:
            prompt_intento += (
                instrucciones_reintento
            )

        try:
            print(
                "🧭 Clasificación de alcance IA: "
                f"intento_semantico={numero_intento}"
            )

            response, modelo_usado = (
                generar_con_gemini_con_fallback(
                    prompt_intento,
                    generation_config=(
                        genai.types.GenerationConfig(
                            temperature=0.0,
                        )
                    ),
                    tarea=(
                        "clasificación de alcance IA "
                        f"intento {numero_intento}"
                    ),
                )
            )

            texto_respuesta = (
                extraer_texto_respuesta_gemini(
                    response
                )
            )

            if not texto_respuesta:
                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "RESPUESTA_VACIA"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                continue

            datos_crudos = (
                extraer_json_de_texto(
                    texto_respuesta
                )
            )

            if datos_crudos is None:
                muestra_inicio = (
                    texto_respuesta[:500]
                    .replace(
                        "\n",
                        "\\n",
                    )
                )

                muestra_final = (
                    texto_respuesta[-500:]
                    .replace(
                        "\n",
                        "\\n",
                    )
                )

                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "JSON_INVALIDO"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                print(
                    "⚠️ Inicio respuesta alcance: "
                    f"{muestra_inicio}"
                )

                print(
                    "⚠️ Final respuesta alcance: "
                    f"{muestra_final}"
                )

                continue

            alcance_normalizado = (
                normalizar_alcance_conversacion(
                    datos_crudos
                )
            )

            alcance_detectado = str(
                alcance_normalizado.get(
                    "alcance_conversacion",
                    "AMBIGUO",
                )
                or "AMBIGUO"
            ).strip().upper()

            confianza = normalizar_confianza(
                alcance_normalizado.get(
                    "confianza"
                )
            )

            if (
                alcance_detectado
                not in ALCANCES_CONVERSACION_VALIDOS
            ):
                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "ALCANCE_NO_VALIDO"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                continue

            print(
                "✅ Alcance conversacional válido: "
                f"{alcance_detectado}, "
                f"confianza={confianza}, "
                f"modelo={modelo_usado}, "
                f"intento_semantico={numero_intento}"
            )

            return {
                "exitoso": True,
                "alcance": alcance_normalizado,
                "modelo_usado": modelo_usado,
                "intentos_realizados": (
                    resultado_fallo[
                        "intentos_realizados"
                    ]
                ),
                "errores": (
                    resultado_fallo[
                        "errores"
                    ]
                ),
            }

        except Exception as e:
            error = (
                "intento semántico "
                f"{numero_intento}: "
                f"FALLO_TECNICO_GEMINI: {e}"
            )

            resultado_fallo[
                "errores"
            ].append(
                error
            )

            print(
                "⚠️ Error técnico clasificando "
                "alcance: "
                f"{error}"
            )

            continue

    return resultado_fallo 

# ============================================================
# EXTRACCIÓN IA DE MEMORIA HISTÓRICA
# ============================================================

def normalizar_memoria_historica_ia(
    datos_crudos: Any,
) -> Dict[str, Any]:
    """
    Limpia y valida la memoria histórica producida por Gemini.

    No modifica contactos.
    No guarda información.
    No cambia FLOW_STATE.
    """

    base = crear_memoria_historica_vacia()

    if not isinstance(datos_crudos, dict):
        return base

    etapa_sugerida = str(
        datos_crudos.get(
            "etapa_conversacional_sugerida",
            "CONTACTO_INICIAL",
        )
        or "CONTACTO_INICIAL"
    ).strip().upper()

    if etapa_sugerida not in ETAPAS_CONVERSACIONALES_VALIDAS:
        etapa_sugerida = "CONTACTO_INICIAL"

    estado_sugerido = str(
        datos_crudos.get(
            "estado_comercial_sugerido",
            "PROSPECTO_NUEVO",
        )
        or "PROSPECTO_NUEVO"
    ).strip().upper()

    if estado_sugerido not in ESTADOS_COMERCIALES_VALIDOS:
        estado_sugerido = "PROSPECTO_NUEVO"

    hitos_crudos = normalizar_lista_textos(
        datos_crudos.get(
            "hitos_comerciales"
        )
    )

    hitos_validos = []

    for hito in hitos_crudos:
        hito_normalizado = str(
            hito or ""
        ).strip().upper()

        if (
            hito_normalizado
            in HITOS_COMERCIALES_VALIDOS
            and hito_normalizado
            not in hitos_validos
        ):
            hitos_validos.append(
                hito_normalizado
            )

    niveles_crudos = normalizar_lista_textos(
        datos_crudos.get(
            "niveles_interes"
        )
    )

    equivalencias_nivel = {
        "kinder": "Kínder",
        "kínder": "Kínder",
        "preescolar": "Kínder",
        "primaria": "Primaria",
        "secundaria": "Secundaria",
    }

    niveles_validos = []

    for nivel in niveles_crudos:
        nivel_limpio = str(
            nivel or ""
        ).strip()

        nivel_normalizado = (
            equivalencias_nivel.get(
                nivel_limpio.lower(),
                nivel_limpio,
            )
        )

        if (
            nivel_normalizado
            in NIVELES_OFICIALES_VALIDOS
            and nivel_normalizado
            and nivel_normalizado
            not in niveles_validos
        ):
            niveles_validos.append(
                nivel_normalizado
            )

    alumnos_crudos = datos_crudos.get(
        "alumnos",
        [],
    )

    alumnos_normalizados = []

    if isinstance(alumnos_crudos, list):
        for alumno in alumnos_crudos:
            if not isinstance(alumno, dict):
                continue

            nombre = str(
                alumno.get(
                    "nombre",
                    "",
                )
                or ""
            ).strip()

            nivel = str(
                alumno.get(
                    "nivel_interes",
                    alumno.get(
                        "nivel",
                        "",
                    ),
                )
                or ""
            ).strip()

            nivel = equivalencias_nivel.get(
                nivel.lower(),
                nivel,
            )

            if (
                nivel
                not in NIVELES_OFICIALES_VALIDOS
            ):
                nivel = ""

            registro_alumno = {
                "nombre": nombre,
                "nivel_interes": nivel,
                "grado_interes": str(
                    alumno.get(
                        "grado_interes",
                        alumno.get(
                            "grado",
                            "",
                        ),
                    )
                    or ""
                ).strip(),
                "edad": normalizar_entero_opcional(
                    alumno.get(
                        "edad"
                    )
                ),
                "fecha_nacimiento": str(
                    alumno.get(
                        "fecha_nacimiento",
                        "",
                    )
                    or ""
                ).strip(),
            }

            if any(
                valor not in [
                    "",
                    None,
                    [],
                ]
                for valor in (
                    registro_alumno.values()
                )
            ):
                alumnos_normalizados.append(
                    registro_alumno
                )

    memoria_normalizada = {
        "version": "1.0",

        "nombre_tutor": str(
            datos_crudos.get(
                "nombre_tutor",
                "",
            )
            or ""
        ).strip(),

        "alumnos": alumnos_normalizados,

        "zona_interes": str(
            datos_crudos.get(
                "zona_interes",
                "",
            )
            or ""
        ).strip(),

        "referencia_colegio": str(
            datos_crudos.get(
                "referencia_colegio",
                "",
            )
            or ""
        ).strip(),

        "niveles_interes": niveles_validos,

        "grados_interes": (
            normalizar_lista_textos(
                datos_crudos.get(
                    "grados_interes"
                )
            )
        ),

        "areas_interes": (
            normalizar_lista_textos(
                datos_crudos.get(
                    "areas_interes"
                )
            )
        ),

        "temas_explicados": (
            normalizar_lista_textos(
                datos_crudos.get(
                    "temas_explicados"
                )
            )
        ),

        "objeciones_detectadas": (
            normalizar_lista_textos(
                datos_crudos.get(
                    "objeciones_detectadas"
                )
            )
        ),

        "hitos_comerciales": hitos_validos,

        "solicito_costos": normalizar_booleano(
            datos_crudos.get(
                "solicito_costos"
            )
        ),

        "costos_presentados": normalizar_booleano(
            datos_crudos.get(
                "costos_presentados"
            )
        ),

        "acepto_visita": normalizar_booleano(
            datos_crudos.get(
                "acepto_visita"
            )
        ),

        "cita_solicitada": normalizar_booleano(
            datos_crudos.get(
                "cita_solicitada"
            )
        ),

        "cita_confirmada": normalizar_booleano(
            datos_crudos.get(
                "cita_confirmada"
            )
        ),

        "fecha_cita_texto": str(
            datos_crudos.get(
                "fecha_cita_texto",
                "",
            )
            or ""
        ).strip(),

        "fecha_cita_iso": str(
            datos_crudos.get(
                "fecha_cita_iso",
                "",
            )
            or ""
        ).strip(),

        "hora_cita_texto": str(
            datos_crudos.get(
                "hora_cita_texto",
                "",
            )
            or ""
        ).strip(),

        "hora_cita_24h": str(
            datos_crudos.get(
                "hora_cita_24h",
                "",
            )
            or ""
        ).strip(),

        "ultimo_mensaje_prospecto": str(
            datos_crudos.get(
                "ultimo_mensaje_prospecto",
                "",
            )
            or ""
        ).strip(),

        "ultima_respuesta_asistente": str(
            datos_crudos.get(
                "ultima_respuesta_asistente",
                "",
            )
            or ""
        ).strip(),

        "etapa_conversacional_sugerida": (
            etapa_sugerida
        ),

        "estado_comercial_sugerido": (
            estado_sugerido
        ),

        "resumen_relacion": str(
            datos_crudos.get(
                "resumen_relacion",
                "",
            )
            or ""
        ).strip(),

        "datos_confirmados": (
            normalizar_lista_textos(
                datos_crudos.get(
                    "datos_confirmados"
                )
            )
        ),

        "datos_inciertos": (
            normalizar_lista_textos(
                datos_crudos.get(
                    "datos_inciertos"
                )
            )
        ),

        "confianza": normalizar_confianza(
            datos_crudos.get(
                "confianza"
            )
        ),
    }

    try:
        memoria_validada = (
            MemoriaHistoricaConversacion.model_validate(
                memoria_normalizada
            )
        )

        return memoria_validada.model_dump()

    except Exception as e:
        print(
            "⚠️ Error validando memoria histórica: "
            f"{e}"
        )

        return base

def memoria_historica_contiene_informacion(
    memoria: Dict[str, Any],
) -> bool:
    """
    Determina si una memoria histórica normalizada contiene
    información comercial o conversacional realmente útil.

    No considera como información suficiente:
    - valores predeterminados del contrato;
    - confianza por sí sola;
    - CONTACTO_INICIAL;
    - PROSPECTO_NUEVO.

    No modifica datos ni ejecuta acciones.
    """

    if not isinstance(memoria, dict):
        return False

    campos_texto = [
        "nombre_tutor",
        "zona_interes",
        "referencia_colegio",
        "fecha_cita_texto",
        "fecha_cita_iso",
        "hora_cita_texto",
        "hora_cita_24h",
        "ultimo_mensaje_prospecto",
        "ultima_respuesta_asistente",
        "resumen_relacion",
    ]

    for campo in campos_texto:
        if str(
            memoria.get(
                campo,
                "",
            )
            or ""
        ).strip():
            return True

    campos_lista = [
        "alumnos",
        "niveles_interes",
        "grados_interes",
        "areas_interes",
        "temas_explicados",
        "objeciones_detectadas",
        "hitos_comerciales",
        "datos_confirmados",
        "datos_inciertos",
    ]

    for campo in campos_lista:
        valor = memoria.get(
            campo,
            [],
        )

        if isinstance(valor, list) and valor:
            return True

    campos_booleanos = [
        "solicito_costos",
        "costos_presentados",
        "acepto_visita",
        "cita_solicitada",
        "cita_confirmada",
    ]

    for campo in campos_booleanos:
        if memoria.get(campo) is True:
            return True

    etapa = str(
        memoria.get(
            "etapa_conversacional_sugerida",
            "",
        )
        or ""
    ).strip().upper()

    if (
        etapa
        and etapa != "CONTACTO_INICIAL"
    ):
        return True

    estado = str(
        memoria.get(
            "estado_comercial_sugerido",
            "",
        )
        or ""
    ).strip().upper()

    if (
        estado
        and estado != "PROSPECTO_NUEVO"
    ):
        return True

    return False

def extraer_memoria_historica_con_ia(
    texto_conversacion: str,
) -> Dict[str, Any]:
    """
    Analiza el historial completo con Gemini y devuelve
    una memoria histórica validada.

    Responsabilidades de esta función:
    - Construir el prompt de memoria histórica.
    - Realizar hasta dos intentos SEMÁNTICOS.
    - Validar que Gemini entregue texto.
    - Validar que el texto contenga JSON.
    - Normalizar la memoria histórica.
    - Rechazar memorias técnicamente válidas pero vacías.

    Responsabilidades delegadas al administrador central Gemini:
    - Selección de modelo.
    - Fallback técnico entre modelos.
    - Presupuesto de tokens.
    - Detección de MAX_TOKENS.
    - Ampliación automática del presupuesto.
    - Registro de métricas técnicas.

    Esta función:
    - no modifica la base de datos;
    - no guarda notes;
    - no cambia contact.status;
    - no cambia FLOW_STATE;
    - no envía mensajes;
    - no crea tareas administrativas.
    """

    resultado_fallo = {
        "exitoso": False,
        "memoria": (
            crear_memoria_historica_vacia()
        ),
        "modelo_usado": "",
        "intentos_realizados": 0,
        "errores": [],
    }

    historial = str(
        texto_conversacion or ""
    ).strip()

    if not historial:
        resultado_fallo["errores"].append(
            "HISTORIAL_VACIO"
        )
        return resultado_fallo

    api_key = (
        os.getenv(
            "GOOGLE_AI_API_KEY"
        )
        or os.getenv(
            "GEMINI_API_KEY"
        )
    )

    if not api_key:
        resultado_fallo["errores"].append(
            "GEMINI_API_KEY_NO_CONFIGURADA"
        )
        return resultado_fallo

    genai.configure(
        api_key=api_key
    )

    contrato_json = json.dumps(
        crear_memoria_historica_vacia(),
        ensure_ascii=False,
        indent=2,
    )

    prompt_base = f"""
Eres un analizador de memoria comercial para el proceso de admisiones
del Colegio Valle de Filadelfia, Campus Santa Cruz Atizapán.

Tu tarea es leer el historial completo de una conversación y extraer
exclusivamente información explícita o razonablemente confirmada.

No debes redactar una respuesta para el prospecto.

REGLAS OBLIGATORIAS:

1. No inventes nombres, edades, grados, fechas ni zonas.

2. Distingue entre lo dicho por el prospecto y lo dicho por el asistente.

3. Una pregunta del asistente no confirma un dato.

4. Una afirmación del prospecto sí puede confirmar un dato.

5. "costos_presentados" solamente será true cuando el asistente haya
compartido efectivamente una cantidad, colegiatura, inscripción u otra
información económica concreta.

6. "cita_confirmada" solamente será true cuando exista una confirmación
explícita de disponibilidad. Una frase como "permítame verificar" no
confirma la cita.

7. "cita_solicitada" será true cuando el prospecto ya haya proporcionado
o propuesto fecha u hora para una visita.

8. Los niveles oficiales son:
- Kínder
- Primaria
- Secundaria

9. Las etapas conversacionales permitidas son:
{json.dumps(
    sorted(
        ETAPAS_CONVERSACIONALES_VALIDAS
    ),
    ensure_ascii=False
)}

10. Los estados comerciales permitidos son:
{json.dumps(
    sorted(
        ESTADOS_COMERCIALES_VALIDOS
    ),
    ensure_ascii=False
)}

11. Los hitos comerciales permitidos son:
{json.dumps(
    sorted(
        HITOS_COMERCIALES_VALIDOS
    ),
    ensure_ascii=False
)}

12. En "datos_confirmados" incluye únicamente hechos claramente
respaldados por la conversación.

13. En "datos_inciertos" incluye información ambigua, incompleta o
pendiente de confirmación.

14. "confianza" debe ser un número entre 0.0 y 1.0.

15. Devuelve exclusivamente un objeto JSON válido.

No uses Markdown.
No agregues explicaciones.
No escribas texto antes ni después del JSON.

CONTRATO OBLIGATORIO:
{contrato_json}

HISTORIAL COMPLETO:
{historial}
"""

    instrucciones_reintento = """

REINTENTO SEMÁNTICO OBLIGATORIO:

La respuesta anterior fue recibida técnicamente, pero no pudo
validarse como una memoria histórica útil.

Vuelve a revisar TODO el historial de la conversación.

Busca especialmente hechos que ya hayan quedado confirmados y que
deben mantenerse disponibles aunque hayan ocurrido muchos mensajes atrás:

- nombre del tutor;
- nombre o nombres de los alumnos;
- nivel o niveles de interés;
- grado solicitado;
- zona;
- referencia del colegio;
- áreas de interés;
- temas ya explicados;
- costos ya solicitados o presentados;
- aceptación de una visita;
- fecha y hora propuestas;
- cita pendiente o confirmada;
- hitos comerciales alcanzados;
- último mensaje del prospecto;
- última respuesta del asistente.

No confundas preguntas del asistente con datos confirmados.

No inventes información que no aparezca en la conversación.

Devuelve exclusivamente un objeto JSON válido que respete
exactamente el contrato solicitado.

No uses Markdown.
No agregues explicaciones.
No escribas texto antes ni después del JSON.
No dejes la memoria vacía si el historial contiene información
comercial confirmada.
"""

    total_intentos_semanticos = 2

    for numero_intento in range(
        1,
        total_intentos_semanticos + 1,
    ):
        resultado_fallo[
            "intentos_realizados"
        ] += 1

        prompt_intento = prompt_base

        if numero_intento > 1:
            prompt_intento += (
                instrucciones_reintento
            )

        try:
            print(
                "🧠 Memoria histórica IA: "
                f"intento_semantico={numero_intento}"
            )

            response, modelo_usado = (
                generar_con_gemini_con_fallback(
                    prompt_intento,
                    generation_config=(
                        genai.types.GenerationConfig(
                            temperature=0.0,
                        )
                    ),
                    tarea=(
                        "memoria histórica "
                        f"intento {numero_intento}"
                    ),
                )
            )

            texto_respuesta = (
                extraer_texto_respuesta_gemini(
                    response
                )
            )

            if not texto_respuesta:
                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "RESPUESTA_VACIA"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                continue

            datos_crudos = (
                extraer_json_de_texto(
                    texto_respuesta
                )
            )

            if datos_crudos is None:
                muestra_inicio = (
                    texto_respuesta[:500]
                    .replace(
                        "\n",
                        "\\n",
                    )
                )

                muestra_final = (
                    texto_respuesta[-500:]
                    .replace(
                        "\n",
                        "\\n",
                    )
                )

                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "JSON_INVALIDO"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    "⚠️ JSON histórico no válido | "
                    f"caracteres={len(texto_respuesta)}"
                )

                print(
                    "⚠️ Inicio respuesta memoria: "
                    f"{muestra_inicio}"
                )

                print(
                    "⚠️ Final respuesta memoria: "
                    f"{muestra_final}"
                )

                continue

            memoria = (
                normalizar_memoria_historica_ia(
                    datos_crudos
                )
            )

            if not (
                memoria_historica_contiene_informacion(
                    memoria
                )
            ):
                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "MEMORIA_VACIA"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                continue

            print(
                "✅ Memoria histórica válida: "
                f"modelo={modelo_usado}, "
                f"intento_semantico="
                f"{numero_intento}"
            )

            return {
                "exitoso": True,
                "memoria": memoria,
                "modelo_usado": modelo_usado,
                "intentos_realizados": (
                    resultado_fallo[
                        "intentos_realizados"
                    ]
                ),
                "errores": (
                    resultado_fallo[
                        "errores"
                    ]
                ),
            }

        except Exception as e:
            error = (
                "intento semántico "
                f"{numero_intento}: "
                f"FALLO_TECNICO_GEMINI: {e}"
            )

            resultado_fallo[
                "errores"
            ].append(
                error
            )

            print(
                "⚠️ Error técnico en memoria "
                "histórica IA: "
                f"{error}"
            )

            continue

    return resultado_fallo
    
    

def analisis_estructurado_contiene_informacion(
    analisis: Dict[str, Any],
) -> bool:
    """
    Determina si el análisis normalizado contiene información útil.

    Evita aceptar como válido el contrato vacío que se utiliza
    como respaldo cuando Gemini no devuelve un JSON aprovechable.
    """
    if not isinstance(analisis, dict):
        return False

    if (
        analisis.get("intencion_principal")
        and analisis.get("intencion_principal") != "OTRO"
    ):
        return True

    campos_texto = [
        "campus_mencionado",
        "zona_mencionada",
        "nivel",
        "grado",
        "fecha_nacimiento_texto",
        "fecha_nacimiento_iso",
        "nivel_actual",
        "ultimo_grado_cursado",
        "grado_solicitado",
        "tema_interes",
        "fecha_cita_texto",
        "hora_cita_texto",
        "fecha_cita_iso",
        "hora_cita_24h",
        "nombre_tutor",
        "nombre_alumno",
    ]

    if any(
        str(analisis.get(campo, "") or "").strip()
        for campo in campos_texto
    ):
        return True

    campos_booleanos = [
        "saludo",
        "saludo_simple",
        "campus_externo",
        "requiere_validar_pre_kinder",
        "pide_costos",
        "pide_cita",
        "dia_no_laboral",
        "pausa_conversacion",
    ]

    if any(
        bool(analisis.get(campo))
        for campo in campos_booleanos
    ):
        return True

    if analisis.get("edad_alumno") is not None:
        return True

    if analisis.get("intenciones_secundarias"):
        return True

    if analisis.get("datos_detectados"):
        return True

    return False

def ejecutar_analisis_estructurado_con_reintentos(
    prompt_analisis: str,
) -> Dict[str, Any]:
    """
    Ejecuta el análisis estructurado con recuperación automática.

    Responsabilidades de esta función:
    - Realizar hasta dos intentos SEMÁNTICOS.
    - Validar que Gemini entregue texto.
    - Validar que el texto contenga JSON.
    - Normalizar el contrato.
    - Rechazar contratos semánticamente vacíos.

    Responsabilidades delegadas al administrador central Gemini:
    - Selección de modelo.
    - Fallback técnico entre modelos.
    - Presupuesto de tokens.
    - Detección de MAX_TOKENS.
    - Ampliación automática del presupuesto.
    - Registro de métricas técnicas.

    Esto evita multiplicar bucles de modelos, reintentos
    semánticos y reintentos técnicos.
    """

    resultado_fallo = {
        "exitoso": False,
        "analisis": crear_analisis_mensaje_vacio(),
        "modelo_usado": "",
        "intentos_realizados": 0,
        "errores": [],
    }

    instrucciones_reintento = """

REINTENTO SEMÁNTICO OBLIGATORIO:

La respuesta anterior fue recibida técnicamente, pero no pudo
validarse como un análisis estructurado útil.

Revisa nuevamente TODO el contexto proporcionado, especialmente:

- el mensaje actual del prospecto;
- la última pregunta del asistente;
- el historial reciente;
- el contexto comercial enriquecido;
- los datos previamente confirmados;
- la etapa actual de la conversación.

Si el mensaje actual es breve, como "sí", "no", "claro",
"perfecto", una fecha, una hora, un nombre o una localidad,
interpreta su significado como continuación de la conversación.

Devuelve exclusivamente un objeto JSON válido que cumpla
exactamente el contrato solicitado.

No uses Markdown.
No agregues explicaciones.
No escribas texto antes ni después del JSON.
No dejes la respuesta vacía.
No devuelvas un contrato neutral o vacío si el contexto permite
determinar el significado del mensaje actual.
"""

    total_intentos_semanticos = 2

    for numero_intento in range(
        1,
        total_intentos_semanticos + 1,
    ):
        resultado_fallo[
            "intentos_realizados"
        ] += 1

        prompt_intento = prompt_analisis

        if numero_intento > 1:
            prompt_intento += instrucciones_reintento

        try:
            print(
                "🧠 Análisis estructurado: "
                f"intento_semantico={numero_intento}"
            )

            response, modelo_usado = (
                generar_con_gemini_con_fallback(
                    prompt_intento,
                    generation_config=(
                        genai.types.GenerationConfig(
                            temperature=0.0,
                        )
                    ),
                    tarea=(
                        "análisis estructurado "
                        f"intento {numero_intento}"
                    ),
                )
            )

            texto_respuesta = (
                extraer_texto_respuesta_gemini(
                    response
                )
            )

            if not texto_respuesta:
                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "RESPUESTA_VACIA"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                continue

            datos_crudos = (
                extraer_json_de_texto(
                    texto_respuesta
                )
            )

            if datos_crudos is None:
                muestra_inicio = (
                    texto_respuesta[:500]
                    .replace(
                        "\n",
                        "\\n",
                    )
                )

                muestra_final = (
                    texto_respuesta[-500:]
                    .replace(
                        "\n",
                        "\\n",
                    )
                )

                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "JSON_INVALIDO"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                print(
                    "⚠️ Inicio respuesta análisis: "
                    f"{muestra_inicio}"
                )

                print(
                    "⚠️ Final respuesta análisis: "
                    f"{muestra_final}"
                )

                continue

            analisis = (
                normalizar_analisis_mensaje_ia(
                    datos_crudos
                )
            )

            if not (
                analisis_estructurado_contiene_informacion(
                    analisis
                )
            ):
                error = (
                    f"{modelo_usado}: "
                    f"intento semántico "
                    f"{numero_intento}: "
                    "CONTRATO_VACIO"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    f"⚠️ {error}"
                )

                continue

            print(
                "✅ Análisis estructurado válido: "
                f"modelo={modelo_usado}, "
                f"intento_semantico="
                f"{numero_intento}"
            )

            return {
                "exitoso": True,
                "analisis": analisis,
                "modelo_usado": modelo_usado,
                "intentos_realizados": (
                    resultado_fallo[
                        "intentos_realizados"
                    ]
                ),
                "errores": (
                    resultado_fallo[
                        "errores"
                    ]
                ),
            }

        except Exception as e:
            error = (
                "intento semántico "
                f"{numero_intento}: "
                f"FALLO_TECNICO_GEMINI: {e}"
            )

            resultado_fallo[
                "errores"
            ].append(
                error
            )

            print(
                "⚠️ Error técnico en análisis "
                "estructurado: "
                f"{error}"
            )

            continue

    return resultado_fallo
    
def crear_analisis_determinista_basico(
    mensaje_usuario: str,
) -> Dict[str, Any]:
    """
    Construye un análisis mínimo y seguro cuando Gemini
    no devuelve JSON válido.

    No sustituye el análisis semántico completo.
    Solamente recupera intenciones y datos explícitos
    que pueden identificarse sin ambigüedad.
    """

    analisis = crear_analisis_mensaje_vacio()

    mensaje_original = str(
        mensaje_usuario or ""
    ).strip()

    if not mensaje_original:
        return analisis

    mensaje_normalizado = (
        unicodedata.normalize(
            "NFD",
            mensaje_original.lower(),
        )
    )

    mensaje_normalizado = "".join(
        caracter
        for caracter in mensaje_normalizado
        if unicodedata.category(caracter) != "Mn"
    )

    mensaje_normalizado = re.sub(
        r"[^a-z0-9\s]",
        " ",
        mensaje_normalizado,
    )

    mensaje_normalizado = re.sub(
        r"\s+",
        " ",
        mensaje_normalizado,
    ).strip()

    # ========================================================
    # SALUDO
    # ========================================================

    expresiones_saludo = [
        "hola",
        "buenos dias",
        "buenas tardes",
        "buenas noches",
        "que tal",
    ]

    tiene_saludo = any(
        mensaje_normalizado == expresion
        or mensaje_normalizado.startswith(
            f"{expresion} "
        )
        for expresion in expresiones_saludo
    )

    analisis["saludo"] = tiene_saludo

    # ========================================================
    # NIVEL EDUCATIVO EXPLÍCITO
    # ========================================================

    if (
        "primaria"
        in mensaje_normalizado
    ):
        analisis["nivel"] = "Primaria"

    elif (
        "secundaria"
        in mensaje_normalizado
    ):
        analisis["nivel"] = "Secundaria"

    elif any(
        expresion in mensaje_normalizado
        for expresion in [
            "kinder",
            "preescolar",
        ]
    ):
        analisis["nivel"] = "Kínder"

    # ========================================================
    # SOLICITUD DE UBICACIÓN INSTITUCIONAL
    # ========================================================
    #
    # Esta es una intención logística inequívoca.
    # Si Gemini falla, Python puede recuperarla sin alterar
    # el embudo comercial.
    # ========================================================

    if detectar_solicitud_ubicacion_institucional(
        mensaje_original
    ):
        analisis.update({
            "intencion_principal": (
                "PEDIR_UBICACION"
            ),
            "accion_recomendada": (
                "RESPONDER_UBICACION"
            ),
            "pregunta_paralela": True,
            "confianza": 0.98,
        })

        analisis["datos_detectados"] = [
            "solicitud_ubicacion_institucional",
        ]

        return analisis

    # ========================================================
    # SOLICITUD DE INFORMES
    # ========================================================

    expresiones_informes = [
        "informes",
        "informacion",
        "quiero saber",
        "quisiera saber",
        "me interesa",
        "solicito informacion",
    ]

    pide_informes = any(
        expresion in mensaje_normalizado
        for expresion in expresiones_informes
    )

    if pide_informes:
        analisis.update({
            "intencion_principal": "PEDIR_INFORMES",
            "accion_recomendada": "CONTINUAR_INFORMES",
            "confianza": 0.90,
        })

        analisis["datos_detectados"] = [
            "solicitud_informes",
        ]

        if analisis.get("nivel"):
            analisis[
                "datos_detectados"
            ].append(
                "nivel"
            )

        return analisis

    # ========================================================
    # SOLICITUD EXPLÍCITA DE COSTOS
    # ========================================================

    expresiones_costos = [
        "costo",
        "costos",
        "precio",
        "colegiatura",
        "inscripcion",
        "cuanto cuesta",
        "cuanto esta",
    ]

    pide_costos = any(
        expresion in mensaje_normalizado
        for expresion in expresiones_costos
    )

    if pide_costos:
        analisis.update({
            "intencion_principal": "PEDIR_COSTOS",
            "pide_costos": True,
            "tema_interes": "costos",
            "accion_recomendada": "RESPONDER_COSTOS",
            "confianza": 0.90,
        })

        analisis["datos_detectados"] = [
            "solicitud_costos",
        ]

        return analisis

    # ========================================================
    # SALUDO SIMPLE
    # ========================================================

    if tiene_saludo:
        analisis.update({
            "saludo_simple": True,
            "intencion_principal": "SALUDO",
            "accion_recomendada": "RESPONDER_SALUDO",
            "confianza": 0.95,
        })

        return analisis

    return analisis

def analizar_mensaje_prospecto_con_ia(
    mensaje_usuario: str,
    contact=None,
    history=None,
    contexto_comercial: Optional[
        Dict[str, Any]
    ] = None,
) -> Dict[str, Any]:
    """
    Analiza integralmente el mensaje actual del prospecto con Gemini.

    Esta función:
    - Interpreta todas las intenciones presentes.
    - Extrae datos útiles.
    - Devuelve un JSON normalizado.
    - No redacta una respuesta para el prospecto.
    - No modifica la base de datos.
    - No cambia el estado del flujo.
    """
    mensaje = (mensaje_usuario or "").strip()

    if not mensaje:
        print("⚠️ Análisis estructurado: mensaje vacío")
        return crear_analisis_mensaje_vacio()

    api_key = (
        os.getenv("GOOGLE_AI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )

    if not api_key:
        print(
            "⚠️ No hay clave de Gemini disponible para "
            "el análisis estructurado"
        )
        return crear_analisis_mensaje_vacio()

    genai.configure(api_key=api_key)

    historial_lineas = []

    if history:
        for item in history[-8:]:
            direccion = getattr(item, "direction", "")
            contenido = str(
                getattr(item, "content", "") or ""
            ).strip()

            if not contenido:
                continue

            if direccion == "incoming":
                emisor = "Prospecto"
            elif direccion == "outgoing":
                emisor = "Asistente"
            else:
                emisor = "Conversación"

            historial_lineas.append(
                f"{emisor}: {contenido}"
            )

    historial_texto = (
        "\n".join(historial_lineas)
        if historial_lineas
        else "Sin historial reciente."
    )

    estado_actual = ""

    if contact is not None:
        try:
            estado_actual = get_flow_state(contact)
        except Exception:
            estado_actual = ""

    notas_contacto = str(
        getattr(contact, "notes", "") or ""
    ).strip() if contact is not None else ""

    estatus_contacto = str(
        getattr(contact, "status", "") or ""
    ).strip() if contact is not None else ""

    fecha_actual = datetime.now(LOCAL_TZ)

    fecha_actual_texto = fecha_actual.strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    contrato_json = json.dumps(
        crear_analisis_mensaje_vacio(),
        ensure_ascii=False,
        indent=2,
    )

    contexto_comercial_seguro = (
        contexto_comercial
        if isinstance(
            contexto_comercial,
            dict,
        )
        else crear_contexto_comercial_vacio()
    )

    contexto_comercial_json = json.dumps(
        contexto_comercial_seguro,
        ensure_ascii=False,
        indent=2,
    )

    prompt_analisis = f"""
Eres el analizador semántico de un bot de admisiones del
Colegio Valle de Filadelfia, Campus Santa Cruz Atizapán.

Tu única tarea es interpretar el mensaje del prospecto y devolver
un objeto JSON. No debes redactar una respuesta para el usuario.

FECHA Y HORA LOCAL ACTUAL:
{fecha_actual_texto}

ESTADO CONVERSACIONAL ACTUAL:
{estado_actual or "No disponible"}

ESTATUS DEL CONTACTO:
{estatus_contacto or "No disponible"}

DATOS PREVIOS GUARDADOS:
{notas_contacto or "Sin datos previos guardados."}

CONTEXTO COMERCIAL ENRIQUECIDO:
{contexto_comercial_json}

HISTORIAL RECIENTE:
{historial_texto}

MENSAJE ACTUAL DEL PROSPECTO:
{mensaje}

IMPORTANTE SOBRE EL MENSAJE ACTUAL:

El bloque anterior puede contener UNO O VARIOS mensajes consecutivos
del mismo prospecto que todavía forman una sola unidad conversacional
pendiente de respuesta.

Si contiene varias líneas:

- interprétalas conjuntamente y en orden cronológico;
- no las trates como conversaciones independientes;
- identifica todas las intenciones relevantes presentes;
- un mensaje posterior puede complementar, precisar o corregir uno anterior;
- si un mensaje posterior modifica claramente un dato anterior sobre el
  mismo asunto, utiliza la información más reciente;
- si los mensajes contienen temas paralelos, conserva ambos sin perder
  el objetivo comercial pendiente;
- distingue entre una corrección, información adicional, una pregunta
  paralela y un cambio real de intención;
- no ignores información útil simplemente porque apareció en una línea
  anterior del mismo bloque.

Tu resultado debe representar el significado global de toda la unidad
conversacional, no sólo de la última línea.

REGLAS INSTITUCIONALES PARA INTERPRETACIÓN:

1. Este canal atiende únicamente al Campus Santa Cruz Atizapán.

2. Zonas válidas o cercanas:
- Santa Cruz Atizapán
- Santiago Tianguistenco
- Tianguistenco
- Capulhuac
- Capulhuac de Mirafuentes
- San Pedro
- Xalatlaco
- Almoloya
- Buen Suceso
- Tlazala
- Almaya
- localidades claramente cercanas a Santa Cruz Atizapán

3. Campus o zonas externas:
- Metepec
- Toluca
- Atlacomulco
- cualquier otro campus del colegio

4. Niveles oficiales:
- Kínder
- Primaria
- Secundaria

No clasifiques Maternal ni Pre-kínder como niveles oficiales.
Cuando el alumno parezca demasiado pequeño, activa:
"requiere_validar_pre_kinder": true

Cuando se mencione una fecha de nacimiento:

- conserva la expresión original en "fecha_nacimiento_texto";
- conviértela al formato YYYY-MM-DD en "fecha_nacimiento_iso";
- no inventes el día, mes o año cuando falte alguno;
- si la fecha es ambigua, deja "fecha_nacimiento_iso" vacío.

Cuando el prospecto mencione antecedentes escolares:

- guarda el nivel en "nivel_actual";
- guarda el último grado concluido en "ultimo_grado_cursado";
- guarda el grado que desea cursar en "grado_solicitado".

Ejemplos:

"Actualmente cursa segundo de primaria"
nivel_actual: "Primaria"

"Terminó segundo de primaria"
ultimo_grado_cursado: "2 de Primaria"

"Busco tercero de primaria"
grado_solicitado: "3 de Primaria"

No determines automáticamente grados posteriores únicamente por edad.
Para primaria y secundaria, considera también el antecedente escolar.

5. Un mismo mensaje puede contener varias intenciones.
Selecciona la más importante como "intencion_principal" y coloca
las demás en "intenciones_secundarias".

6. Si pregunta costo, colegiatura, inscripción o precio:
"pide_costos": true

7. Si pide visitar, conocer, agendar o tener una cita nueva:
"pide_cita": true

8. Si solicita la ubicación física del colegio, la dirección,
un enlace de Maps, indicaciones para llegar o pregunta dónde
se encuentra el campus:

"intencion_principal": "PEDIR_UBICACION"

o, si existe otra intención claramente más importante,
incluye "PEDIR_UBICACION" en "intenciones_secundarias".

Esta regla aplica aunque la cita ya esté confirmada y aunque
la solicitud de ubicación aparezca como una pregunta paralela.

No clasifiques una solicitud clara de ubicación únicamente como:
"OTRO"
"PREGUNTAR_TEMA_EDUCATIVO"
o una pregunta paralela genérica.

Nunca inventes una dirección, coordenadas, Place ID ni enlace
de Google Maps. Tu responsabilidad aquí es únicamente detectar
la intención PEDIR_UBICACION. Python proporcionará la ubicación
institucional autorizada.

9. Debes interpretar el mensaje actual como continuación de la
conversación, nunca como un mensaje aislado.

Usa conjuntamente:

- el contexto comercial enriquecido;
- la etapa conversacional;
- los hitos comerciales;
- los datos ya confirmados;
- el estado conversacional actual;
- el estatus comercial;
- los datos previos guardados;
- el historial reciente;
- el último mensaje del asistente;
- la última pregunta formulada por el asistente;
- la fecha y hora de cita recuperadas del contexto;
- el mensaje actual.

REGLA CONTEXTUAL ABSOLUTA:

Cuando el mensaje actual sea breve, ambiguo o no tenga significado
suficiente por sí solo, interprétalo como respuesta a la última
pregunta del asistente.

Una respuesta breve puede confirmar, negar, precisar, corregir o
completar el dato que estaba pendiente.

No exijas que el prospecto repita el tema de la pregunta.

No devuelvas un contrato vacío únicamente porque el mensaje actual
sea breve. Recupera su significado del historial, de la última
pregunta y del contexto comercial enriquecido.

Los hitos y datos del contexto comercial representan información
ya recuperada de la conversación. No los ignores ni obligues a la
familia a responder nuevamente algo que ya quedó confirmado.

RELACIÓN DEL MENSAJE CON EL OBJETIVO PENDIENTE:

El campo "objetivo_pendiente" del CONTEXTO COMERCIAL ENRIQUECIDO
representa aquello que la conversación estaba intentando obtener,
confirmar o completar antes del mensaje actual.

Debes interpretar semánticamente qué relación tiene el mensaje
actual con ese objetivo y completar:

"relacion_con_objetivo_pendiente"

Utiliza exclusivamente uno de estos valores:

"SIN_OBJETIVO"
cuando no existe un objetivo pendiente real en el contexto.

"RESPONDE_OBJETIVO"
cuando el mensaje actual responde, aporta, confirma, niega o completa
lo que se estaba esperando, aunque la respuesta sea breve, indirecta,
coloquial o contenga además otra información.

"NO_AFECTA_OBJETIVO"
cuando el prospecto introduce una pregunta, comentario, duda o tema
paralelo que puede atenderse, pero que no responde, modifica ni
cancela lo que estaba pendiente.

"MODIFICA_OBJETIVO"
cuando el mensaje cambia de manera explícita información directamente
relacionada con el objetivo pendiente o reemplaza una decisión
anterior relevante para ese objetivo.

"CANCELA_OBJETIVO"
cuando el prospecto manifiesta claramente que ya no desea continuar
con aquello que estaba pendiente.

REGLAS IMPORTANTES:

- No clasifiques por palabras exactas.
- Interpreta intención, contexto e historial.
- Una pregunta paralela no elimina el objetivo pendiente.
- Que el mensaje contenga información útil no significa automáticamente
  que haya respondido el objetivo pendiente.
- Si el prospecto responde el objetivo pendiente y además hace otra
  pregunta, utiliza "RESPONDE_OBJETIVO".
- No obligues al prospecto a repetir información que ya se encuentra
  confirmada en el contexto.
- El objetivo pendiente tiene prioridad como referencia conversacional,
  pero no impide que la familia hable naturalmente de otros temas.

Activa los siguientes campos según el significado integral:

"seguimiento_cita": true
cuando la familia pregunta, reclama o busca saber qué ocurrió con
una visita o cita previamente solicitada.

"solicitud_confirmacion_cita": true
cuando necesita saber si la cita, visita, fecha u horario ya fueron
confirmados.

"cambio_fecha_cita": true
cuando desea reemplazar una fecha u hora previamente propuesta.

"cancelacion_cita": true
cuando manifiesta claramente que quiere cancelar la visita.

"desistimiento_temporal": true
cuando indica que por ahora no asistirá, que retomará después o que
ya no desea continuar en este momento, sin tratarse necesariamente
de una cancelación definitiva.

"asume_cita_confirmada": true
cuando la familia habla como si la visita ya estuviera confirmada,
aunque el historial indique que todavía estaba pendiente.

"pregunta_paralela": true
cuando existe una cita pendiente de confirmación y la familia hace
una pregunta adicional que no cambia, cancela ni solicita confirmar
la cita.

Incluye preguntas informativas o logísticas relacionadas o no con
la visita, por ejemplo:
- si debe llevar identificación o algún documento;
- si el alumno debe asistir;
- si puede ir acompañado;
- dónde puede estacionarse;
- cuál es la dirección;
- cómo llegar;
- costos;
- idiomas;
- horarios;
- requisitos;
- cualquier otra duda que pueda responderse sin modificar la cita.

Una pregunta paralela NO significa que la familia haya abandonado
la cita ni que deba reiniciarse el flujo comercial.

"reclamo_demora": true
cuando expresa molestia, preocupación o insistencia por el tiempo
transcurrido sin respuesta.

"contexto_cita_pendiente_reconocido": true
cuando los datos previos o el historial muestran que la cita sigue
esperando confirmación administrativa.

"requiere_admin_contextual": true
cuando, considerando el historial y el mensaje actual, la respuesta
depende de una confirmación, disponibilidad, cancelación o cambio
que debe revisar una persona administradora.

Estos campos no dependen de palabras exactas. Interpreta expresiones
directas, indirectas, breves, coloquiales y con errores ortográficos.

Ejemplos equivalentes de seguimiento:

- "¿Qué pasó con mi cita?"
- "¿Ya quedó?"
- "Sigo pendiente de lo que iban a revisar."
- "¿Ya pudieron ver si me reciben?"
- "Nada más quería saber si sí quedó lo del lunes."

Si el estado indica CITA_PENDIENTE_CONFIRMACION o
ESPERANDO_CONFIRMACION_ADMIN:

- no interpretes la cita como confirmada;
- no supongas que la directora ya espera a la familia;
- no conviertas una frase ambigua en una confirmación;
- reconoce ese estado en
  "contexto_cita_pendiente_reconocido".

9. Si menciona una fecha relativa como hoy, mañana o un día de la
semana:
- conserva la expresión original en "fecha_cita_texto"
- conviértela a una fecha calendario YYYY-MM-DD en "fecha_cita_iso"
  cuando exista una interpretación razonable a partir de la fecha
  actual proporcionada en el contexto
- no confundas una expresión de fecha con "hora_cita_texto"
- "hora_cita_texto" y "hora_cita_24h" se utilizan exclusivamente
  para información de horario
  

10. Las visitas sólo se realizan de lunes a viernes.
Marca "dia_no_laboral": true cuando la fecha propuesta sea sábado
o domingo.

10-B. IDENTIFICACIÓN DE UNO O VARIOS ALUMNOS

Utiliza el campo "alumnos" para representar de forma independiente
a cada hijo, hija o futuro alumno mencionado o claramente recuperable
del contexto de la conversación.

Cada elemento debe utilizar esta estructura:

{{
  "nombre": "",
  "nivel": "",
  "grado": "",
  "edad": null,
  "fecha_nacimiento": ""
}}

REGLAS:

- Si existe un solo alumno, "alumnos" puede contener un elemento.
- Si existen dos o más alumnos, crea un elemento independiente
  para cada uno.
- No combines dos alumnos en un mismo elemento.
- Un alumno puede existir aunque todavía no conozcas su nombre.
- Si la familia dice "tengo uno para primaria y otro para secundaria",
  deben existir dos elementos, aunque ambos nombres estén vacíos.
- Si dice "mi hija va a 3.º de primaria y mi hijo a 1.º de secundaria",
  deben existir dos elementos con sus respectivos niveles y grados.
- Si posteriormente proporciona el nombre de uno de ellos, utiliza
  el historial y contexto para asociarlo al alumno correcto cuando
  sea inequívoco.
- No inventes nombres, niveles, grados, edades ni relaciones.
- No elimines alumnos previamente identificados sólo porque el
  mensaje actual se refiera únicamente a uno de ellos.
- "nombre_alumno" se conserva únicamente como campo de compatibilidad.
  Si existe un único alumno identificado, puede contener su nombre.
  Si existen varios, no combines sus nombres dentro de
  "nombre_alumno".

11. No inventes nombres, zonas, fechas, niveles, grados o edades.
Cuando un dato no esté expresado ni pueda recuperarse claramente
del historial, usa el valor vacío correspondiente.

12. Los campos "clasificacion_zona", "dia_no_laboral" y
"accion_recomendada" son orientativos. Posteriormente serán
validados por código.

13. "confianza" debe ser un número entre 0.0 y 1.0.

14. Devuelve exclusivamente JSON válido.
No uses Markdown.
No agregues explicaciones.
No escribas texto antes ni después del JSON.

CONTRATO OBLIGATORIO:
{contrato_json}
"""

    resultado_analisis = (
        ejecutar_analisis_estructurado_con_reintentos(
            prompt_analisis
        )
    )

    if not resultado_analisis.get("exitoso"):
        print(
            "⚠️ Todos los intentos del análisis "
            "estructurado fallaron."
        )

        errores_analisis_texto = json.dumps(
            resultado_analisis.get(
                "errores",
                [],
            ),
            ensure_ascii=False,
        )

        print(
            "📋 Errores de análisis: "
            f"{errores_analisis_texto}"
        )

        analisis_respaldo = (
            crear_analisis_determinista_basico(
                mensaje
            )
        )

        if (
            analisis_estructurado_contiene_informacion(
                analisis_respaldo
            )
        ):
            analisis_respaldo_texto = json.dumps(
                analisis_respaldo,
                ensure_ascii=False,
            )

            print(
                "✅ Se utilizó análisis determinista "
                "de respaldo: "
                f"{analisis_respaldo_texto}"
            )
            
            return analisis_respaldo

        return crear_analisis_mensaje_vacio()

    analisis = resultado_analisis.get(
        "analisis",
        crear_analisis_mensaje_vacio(),
    )

    print(
        "✅ Análisis estructurado completado "
        f"con {resultado_analisis.get('modelo_usado')}; "
        f"intentos: "
        f"{resultado_analisis.get('intentos_realizados')}"
    )

    analisis_normalizado_texto = json.dumps(
        analisis,
        ensure_ascii=False,
    )

    print(
        "🧠 Análisis normalizado: "
        f"{analisis_normalizado_texto}"
    )

    return analisis
        
# ============================================================
# REGLAS DETERMINISTAS DEL NUEVO FLUJO ESTRUCTURADO
# ============================================================

def crear_decision_negocio_vacia() -> Dict[str, Any]:
    """
    Devuelve una decisión segura y neutral.

    Esta estructura representa la decisión tomada por Python,
    no la recomendación entregada por Gemini.
    """
    return {
        "accion": "CONTINUAR_CONVERSACION",
        "motivo": "No se identificó una regla prioritaria.",
        "requiere_admin": False,
        "puede_compartir_costos": False,
        "zona_validada": False,
        "debe_finalizar_conversacion": False,
        "datos_detectados": {},
    }

# ============================================================
# CATÁLOGO GEOGRÁFICO DEL FLUJO ESTRUCTURADO
# ============================================================

ZONAS_VALIDAS_DIRECTAS = {
    "santa cruz atizapan",
    "santa cruz",
    "santacruz",
    "santiago tianguistenco",
    "tianguistenco",
    "capulhuac",
    "capulhuac de mirafuentes",
    "san pedro tlatizapan",
    "san pedro",
    "tlatizapan",
    "xalatlaco",
    "almoloya",
    "almoloya del rio",
    "buen suceso",
    "tlazala",
    "almaya",
}


ZONAS_VALIDAS_AMBIGUAS = {
    "santiago",
}

ZONAS_VALIDAS_POR_CONECTIVIDAD = {
    "mixicaltzingo",
    "mexicaltzingo",
    "mixcalcingo",
    "calimaya",
    "san andres ocotlan",
    "rancho el meson",
    "el meson",
    "tenango",
    "tenango del valle",
    "san antonio la isla",
    "ocoyoacac",
    "jajalpa",
    "los encinos",
}


ZONAS_EXTERNAS_CONOCIDAS = {
    "metepec",
    "toluca",
    "atlacomulco",
}


REFERENCIAS_CAMPUS_EXTERNOS = {
    "campus metepec",
    "campus de metepec",
    "campus toluca",
    "campus de toluca",
    "campus atlacomulco",
    "campus de atlacomulco",
    "otro campus",
    "otro plantel",
    "campus diferente",
    "plantel diferente",
}


def normalizar_texto_geografico(
    valor: Any,
) -> str:
    """
    Normaliza texto para comparar nombres geográficos.

    - Convierte a minúsculas.
    - Elimina acentos.
    - Sustituye signos por espacios.
    - Elimina espacios repetidos.
    """
    texto = str(valor or "").strip().lower()

    if not texto:
        return ""

    texto = unicodedata.normalize(
        "NFD",
        texto,
    )

    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )

    texto = re.sub(
        r"[^a-z0-9ñ]+",
        " ",
        texto,
    )

    texto = re.sub(
        r"\s+",
        " ",
        texto,
    )

    return texto.strip()

def detectar_solicitud_ubicacion_institucional(
    mensaje_usuario: str,
) -> bool:
    """
    Detecta de forma determinista una solicitud inequívoca
    de ubicación física del campus.

    Esta función NO decide el flujo comercial.
    Solamente protege una intención logística crítica cuando
    Gemini la clasifica como OTRO o pregunta paralela.

    Principios:
    - Detecta intención, no frases exactas.
    - Distingue entre informar la ubicación del prospecto
      y solicitar la ubicación del colegio.
    - Gemini sigue siendo el intérprete semántico principal;
      esta función actúa como red de seguridad determinista.
    """

    texto = normalizar_texto_geografico(
        mensaje_usuario
    )

    if not texto:
        return False

    # --------------------------------------------------------
    # SOLICITUDES DIRECTAS
    # --------------------------------------------------------

    solicitudes_directas = {
        "ubicacion",
        "direccion",
        "localizacion",
        "maps",
        "google maps",
        "mapa",
    }

    if texto in solicitudes_directas:
        return True

    # --------------------------------------------------------
    # PREGUNTAS DIRECTAS SOBRE DÓNDE ESTÁ EL COLEGIO
    # --------------------------------------------------------

    patrones_directos = [
        r"\bdonde estan\b",
        r"\bdonde estan ubicados\b",
        r"\bdonde se ubican\b",
        r"\bdonde queda\b",
        r"\bdonde queda el colegio\b",
        r"\bdonde esta el colegio\b",
        r"\bcual es su direccion\b",
        r"\bcual es la direccion\b",
        r"\bcomo llegar\b",
        r"\bcomo llego\b",
        r"\bcomo puedo llegar\b",
    ]

    if any(
        re.search(
            patron,
            texto,
        )
        for patron in patrones_directos
    ):
        return True

    # --------------------------------------------------------
    # TÉRMINOS QUE REPRESENTAN LA UBICACIÓN DEL CAMPUS
    # --------------------------------------------------------

    tiene_termino_ubicacion = bool(
        re.search(
            (
                r"\b("
                r"ubicacion|"
                r"direccion|"
                r"localizacion|"
                r"maps|"
                r"google maps|"
                r"mapa"
                r")\b"
            ),
            texto,
        )
    )

    if not tiene_termino_ubicacion:
        return False

    # --------------------------------------------------------
    # VERBOS DE SOLICITUD
    # --------------------------------------------------------
    #
    # En lugar de enumerar frases completas como
    # "me podrías pasar tu ubicación", detectamos la estructura:
    #
    # solicitud + verbo de entrega + concepto de ubicación
    #
    # Esto cubre variantes naturales sin convertir la lógica
    # en una colección infinita de frases.
    # --------------------------------------------------------

    tiene_verbo_solicitud = bool(
        re.search(
            (
                r"\b("
                r"mand[a-z]*|"
                r"envi[a-z]*|"
                r"pas[a-z]*|"
                r"compart[a-z]*|"
                r"dame|dar|"
                r"necesito|quiero"
                r")\b"
            ),
            texto,
        )
    )

    tiene_modal_solicitud = bool(
        re.search(
            (
                r"\b("
                r"puedes|"
                r"podrias|"
                r"podria|"
                r"puede"
                r")\b"
            ),
            texto,
        )
    )

    tiene_pronombre_solicitud = bool(
        re.search(
            r"\b(me|nos)\b",
            texto,
        )
    )

    if (
        tiene_termino_ubicacion
        and (
            tiene_verbo_solicitud
            or (
                tiene_modal_solicitud
                and tiene_pronombre_solicitud
            )
        )
    ):
        return True

    return False
    
def texto_contiene_alias_geografico(
    texto_normalizado: str,
    aliases: set,
) -> bool:
    """
    Busca nombres geográficos completos, evitando coincidencias
    parciales dentro de otras palabras.
    """
    if not texto_normalizado:
        return False

    texto_delimitado = (
        f" {texto_normalizado} "
    )

    aliases_ordenados = sorted(
        aliases,
        key=len,
        reverse=True,
    )

    for alias in aliases_ordenados:
        alias_normalizado = (
            normalizar_texto_geografico(alias)
        )

        if not alias_normalizado:
            continue

        if (
            f" {alias_normalizado} "
            in texto_delimitado
        ):
            return True

    return False

def texto_confirma_zona_ambigua(
    mensaje_usuario: str,
    zona_mencionada: str,
    alias: str,
) -> bool:
    """
    Confirma un alias geográfico ambiguo, como "Santiago".

    El alias se acepta cuando:
    1. Gemini lo extrajo expresamente como zona; o
    2. Aparece en el mensaje acompañado de una expresión
       claramente geográfica.

    No se acepta solamente porque aparezca como nombre personal.
    """
    mensaje_normalizado = normalizar_texto_geografico(
        mensaje_usuario
    )

    zona_normalizada = normalizar_texto_geografico(
        zona_mencionada
    )

    alias_normalizado = normalizar_texto_geografico(
        alias
    )

    if not alias_normalizado:
        return False

    if zona_normalizada == alias_normalizado:
        return True

    patrones_geograficos = [
        rf"\bsoy de {re.escape(alias_normalizado)}\b",
        rf"\bsomos de {re.escape(alias_normalizado)}\b",
        rf"\bvivo en {re.escape(alias_normalizado)}\b",
        rf"\bvivimos en {re.escape(alias_normalizado)}\b",
        rf"\bradico en {re.escape(alias_normalizado)}\b",
        rf"\bradicamos en {re.escape(alias_normalizado)}\b",
        rf"\bresido en {re.escape(alias_normalizado)}\b",
        rf"\bresidimos en {re.escape(alias_normalizado)}\b",
        rf"\bvenimos de {re.escape(alias_normalizado)}\b",
        rf"\bestoy en {re.escape(alias_normalizado)}\b",
        rf"\bestamos en {re.escape(alias_normalizado)}\b",
        rf"\bnuestra zona es {re.escape(alias_normalizado)}\b",
        rf"\bnuestra localidad es {re.escape(alias_normalizado)}\b",
        rf"\bnuestra comunidad es {re.escape(alias_normalizado)}\b",
    ]

    return any(
        re.search(
            patron,
            mensaje_normalizado,
        )
        for patron in patrones_geograficos
    )


def buscar_localidad_google_places(
    localidad: str,
) -> Dict[str, Any]:
    """
    Busca una localidad mediante Google Places API (New).

    Reglas de seguridad geográfica:
    - La búsqueda se orienta hacia el entorno del colegio.
    - Se solicitan varios candidatos, no solamente el primero.
    - Se rechazan explícitamente resultados de CDMX.
    - Solamente se acepta automáticamente un resultado cuya
      dirección sea compatible con el Estado de México.
    - Si existe ambigüedad, falla de forma segura.
    """

    resultado = {
        "encontrado": False,
        "consulta": "",
        "nombre": "",
        "direccion_formateada": "",
        "place_id": "",
        "latitud": None,
        "longitud": None,
        "candidatos_evaluados": 0,
        "error": "",
    }

    localidad_limpia = str(
        localidad or ""
    ).strip()

    if not localidad_limpia:
        resultado["error"] = "LOCALIDAD_VACIA"
        return resultado

    api_key = str(
        os.getenv(
            "GOOGLE_MAPS_API_KEY",
            "",
        )
        or ""
    ).strip()

    if not api_key:
        resultado[
            "error"
        ] = "GOOGLE_MAPS_API_KEY_NO_CONFIGURADA"

        return resultado

    consulta = (
        f"{localidad_limpia}, "
        "Estado de México, México"
    )

    resultado["consulta"] = consulta

    url = (
        "https://places.googleapis.com/"
        "v1/places:searchText"
    )

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.location"
        ),
    }

    payload = {
        "textQuery": consulta,
        "languageCode": "es",
        "regionCode": "MX",
        "pageSize": 5,
    }

    # --------------------------------------------------------
    # SESGO GEOGRÁFICO HACIA EL ENTORNO DEL COLEGIO
    # --------------------------------------------------------
    #
    # No sustituye la validación posterior de la dirección.
    # Únicamente ayuda a Google a priorizar homónimos cercanos.
    # --------------------------------------------------------

    try:
        colegio_latitud = float(
            os.getenv(
                "COLEGIO_LATITUD",
                "",
            )
        )

        colegio_longitud = float(
            os.getenv(
                "COLEGIO_LONGITUD",
                "",
            )
        )

        if (
            -90 <= colegio_latitud <= 90
            and -180 <= colegio_longitud <= 180
        ):
            payload["locationBias"] = {
                "circle": {
                    "center": {
                        "latitude": colegio_latitud,
                        "longitude": colegio_longitud,
                    },
                    "radius": 50000.0,
                }
            }

    except (TypeError, ValueError):
        pass

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10,
        )

    except requests.RequestException as e:
        resultado["error"] = (
            "ERROR_CONEXION_GOOGLE_PLACES: "
            f"{e}"
        )

        return resultado

    if response.status_code != 200:
        detalle_error = ""

        try:
            respuesta_error = response.json()

            detalle_error = str(
                respuesta_error.get(
                    "error",
                    {},
                ).get(
                    "message",
                    "",
                )
                or ""
            ).strip()

        except (
            ValueError,
            TypeError,
            AttributeError,
        ):
            detalle_error = str(
                response.text or ""
            ).strip()

        resultado["error"] = (
            "GOOGLE_PLACES_HTTP_"
            f"{response.status_code}"
        )

        if detalle_error:
            resultado["error"] += (
                f": {detalle_error[:300]}"
            )

        return resultado

    try:
        datos = response.json()

    except ValueError:
        resultado["error"] = (
            "RESPUESTA_GOOGLE_PLACES_NO_JSON"
        )

        return resultado

    lugares = datos.get(
        "places",
        [],
    )

    if (
        not isinstance(lugares, list)
        or not lugares
    ):
        resultado["error"] = (
            "LOCALIDAD_NO_ENCONTRADA"
        )

        return resultado

    # --------------------------------------------------------
    # SELECCIÓN SEGURA DE CANDIDATO
    # --------------------------------------------------------

    lugar_seleccionado = None

    for lugar in lugares:

        if not isinstance(
            lugar,
            dict,
        ):
            continue

        resultado[
            "candidatos_evaluados"
        ] += 1

        direccion = str(
            lugar.get(
                "formattedAddress",
                "",
            )
            or ""
        ).strip()

        direccion_normalizada = (
            normalizar_texto_geografico(
                direccion
            )
        )

        # -----------------------------------------------
        # CDMX nunca debe aceptarse automáticamente como
        # si fuera Estado de México.
        # -----------------------------------------------

        if (
            "ciudad de mexico"
            in direccion_normalizada
            or "cdmx"
            in direccion_normalizada
        ):
            continue

        # -----------------------------------------------
        # Confirmación explícita de Estado de México.
        #
        # Google suele devolver:
        # "Estado de México"
        # o la abreviatura postal "Méx."
        # -----------------------------------------------

        direccion_minusculas = (
            direccion.lower()
        )

        pertenece_estado_mexico = bool(
            "estado de méxico"
            in direccion_minusculas
            or "estado de mexico"
            in direccion_normalizada
            or re.search(
                r"(?:^|,\s*)méx\.(?:,|$)",
                direccion_minusculas,
            )
        )

        if not pertenece_estado_mexico:
            continue

        location = lugar.get(
            "location",
            {},
        )

        if not isinstance(
            location,
            dict,
        ):
            continue

        latitud = location.get(
            "latitude"
        )

        longitud = location.get(
            "longitude"
        )

        try:
            latitud = float(
                latitud
            )

            longitud = float(
                longitud
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if not (
            -90 <= latitud <= 90
            and -180 <= longitud <= 180
        ):
            continue

        if not str(
            lugar.get(
                "id",
                "",
            )
            or ""
        ).strip():
            continue

        lugar_seleccionado = (
            lugar,
            latitud,
            longitud,
            direccion,
        )

        break

    if lugar_seleccionado is None:
        resultado["error"] = (
            "SIN_CANDIDATO_CONFIABLE_EN_ESTADO_DE_MEXICO"
        )

        return resultado

    (
        lugar,
        latitud,
        longitud,
        direccion,
    ) = lugar_seleccionado

    display_name = lugar.get(
        "displayName",
        {},
    )

    if isinstance(
        display_name,
        dict,
    ):
        nombre = str(
            display_name.get(
                "text",
                "",
            )
            or ""
        ).strip()

    else:
        nombre = ""

    resultado.update({
        "encontrado": True,
        "nombre": nombre,
        "direccion_formateada": direccion,
        "place_id": str(
            lugar.get(
                "id",
                "",
            )
            or ""
        ).strip(),
        "latitud": latitud,
        "longitud": longitud,
        "error": "",
    })

    return resultado
    
def calcular_ruta_google_routes(
    latitud_origen: Any,
    longitud_origen: Any,
) -> Dict[str, Any]:
    """
    Calcula una ruta en automóvil desde una localidad
    hasta el Colegio Valle de Filadelfia Campus Santa Cruz.

    Esta función:
    - utiliza coordenadas aproximadas de una localidad;
    - no solicita la dirección exacta del prospecto;
    - no modifica la base de datos;
    - no cambia decisiones del flujo;
    - no envía mensajes;
    - devuelve una estructura segura cuando ocurre un error.
    """
    resultado = {
        "ruta_encontrada": False,
        "latitud_origen": None,
        "longitud_origen": None,
        "latitud_destino": None,
        "longitud_destino": None,
        "distancia_metros": None,
        "distancia_km": None,
        "duracion_segundos": None,
        "duracion_minutos": None,
        "limite_km": None,
        "dentro_del_limite": None,
        "error": "",
    }

    api_key = str(
        os.getenv(
            "GOOGLE_MAPS_API_KEY",
            "",
        ) or ""
    ).strip()

    if not api_key:
        resultado["error"] = (
            "GOOGLE_MAPS_API_KEY_NO_CONFIGURADA"
        )
        return resultado

    try:
        latitud_origen_float = float(
            latitud_origen
        )

        longitud_origen_float = float(
            longitud_origen
        )

    except (TypeError, ValueError):
        resultado["error"] = (
            "COORDENADAS_ORIGEN_INVALIDAS"
        )
        return resultado

    try:
        latitud_destino = float(
            os.getenv(
                "COLEGIO_LATITUD",
                "",
            )
        )

        longitud_destino = float(
            os.getenv(
                "COLEGIO_LONGITUD",
                "",
            )
        )

    except (TypeError, ValueError):
        resultado["error"] = (
            "COORDENADAS_COLEGIO_NO_CONFIGURADAS"
        )
        return resultado

    try:
        limite_km = float(
            os.getenv(
                "GOOGLE_MAPS_MAX_ROUTE_KM",
                "15",
            )
        )

    except (TypeError, ValueError):
        limite_km = 15.0

    if not (
        -90 <= latitud_origen_float <= 90
        and -180 <= longitud_origen_float <= 180
    ):
        resultado["error"] = (
            "COORDENADAS_ORIGEN_FUERA_DE_RANGO"
        )
        return resultado

    if not (
        -90 <= latitud_destino <= 90
        and -180 <= longitud_destino <= 180
    ):
        resultado["error"] = (
            "COORDENADAS_COLEGIO_FUERA_DE_RANGO"
        )
        return resultado

    resultado.update({
        "latitud_origen": latitud_origen_float,
        "longitud_origen": longitud_origen_float,
        "latitud_destino": latitud_destino,
        "longitud_destino": longitud_destino,
        "limite_km": limite_km,
    })

    url = (
        "https://routes.googleapis.com/"
        "directions/v2:computeRoutes"
    )

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "routes.distanceMeters,"
            "routes.duration"
        ),
    }

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": latitud_origen_float,
                    "longitude": longitud_origen_float,
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": latitud_destino,
                    "longitude": longitud_destino,
                }
            }
        },
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "routeModifiers": {
            "avoidTolls": False,
            "avoidHighways": False,
            "avoidFerries": False,
        },
        "languageCode": "es-MX",
        "units": "METRIC",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=15,
        )

    except requests.RequestException as e:
        resultado["error"] = (
            "ERROR_CONEXION_GOOGLE_ROUTES: "
            f"{e}"
        )
        return resultado

    if response.status_code != 200:
        detalle_error = ""

        try:
            respuesta_error = response.json()

            detalle_error = str(
                respuesta_error.get(
                    "error",
                    {},
                ).get(
                    "message",
                    "",
                )
                or ""
            ).strip()

        except (
            ValueError,
            TypeError,
            AttributeError,
        ):
            detalle_error = str(
                response.text or ""
            ).strip()

        resultado["error"] = (
            "GOOGLE_ROUTES_HTTP_"
            f"{response.status_code}"
        )

        if detalle_error:
            resultado["error"] += (
                f": {detalle_error[:300]}"
            )

        return resultado

    try:
        datos = response.json()

    except ValueError:
        resultado["error"] = (
            "RESPUESTA_GOOGLE_ROUTES_NO_JSON"
        )
        return resultado

    rutas = datos.get(
        "routes",
        [],
    )

    if not isinstance(rutas, list) or not rutas:
        resultado["error"] = (
            "RUTA_NO_ENCONTRADA"
        )
        return resultado

    ruta = rutas[0]

    if not isinstance(ruta, dict):
        resultado["error"] = (
            "FORMATO_RUTA_INVALIDO"
        )
        return resultado

    distancia_metros = ruta.get(
        "distanceMeters"
    )

    duracion_texto = str(
        ruta.get(
            "duration",
            "",
        )
        or ""
    ).strip()

    try:
        distancia_metros = int(
            distancia_metros
        )

    except (TypeError, ValueError):
        resultado["error"] = (
            "DISTANCIA_RUTA_INVALIDA"
        )
        return resultado

    duracion_segundos = None

    if duracion_texto.endswith("s"):
        try:
            duracion_segundos = float(
                duracion_texto[:-1]
            )

        except (TypeError, ValueError):
            duracion_segundos = None

    distancia_km = round(
        distancia_metros / 1000,
        3,
    )

    duracion_minutos = (
        round(
            duracion_segundos / 60,
            1,
        )
        if duracion_segundos is not None
        else None
    )

    resultado.update({
        "ruta_encontrada": True,
        "distancia_metros": distancia_metros,
        "distancia_km": distancia_km,
        "duracion_segundos": duracion_segundos,
        "duracion_minutos": duracion_minutos,
        "dentro_del_limite": (
            distancia_km <= limite_km
        ),
    })

    return resultado

def validar_zona_desconocida_con_google(
    localidad: str,
) -> Dict[str, Any]:
    """
    Resuelve una zona desconocida mediante Google Places
    y Google Routes.

    Reglas:
    - Hasta el límite configurado: zona válida por ruta.
    - Por encima del límite: requiere revisión humana.
    - Si Google falla: requiere revisión humana.
    - No rechaza automáticamente ninguna localidad.
    - No modifica base de datos ni flujo conversacional.
    """
    resultado = {
        "clasificacion": "ZONA_REQUIERE_REVISION",
        "zona_validada": False,
        "requiere_admin": True,
        "localidad_consultada": "",
        "places": None,
        "ruta": None,
        "motivo": "",
    }

    localidad_limpia = str(
        localidad or ""
    ).strip()

    resultado["localidad_consultada"] = (
        localidad_limpia
    )

    if not localidad_limpia:
        resultado["motivo"] = (
            "No existe una localidad suficiente para "
            "realizar la validación geográfica."
        )
        return resultado

    resultado_places = (
        buscar_localidad_google_places(
            localidad_limpia
        )
    )

    resultado["places"] = resultado_places

    if not resultado_places.get("encontrado"):
        resultado["motivo"] = (
            "Google Places no pudo localizar la zona; "
            "debe revisarla una persona administradora."
        )
        return resultado

    resultado_ruta = calcular_ruta_google_routes(
        latitud_origen=resultado_places.get(
            "latitud"
        ),
        longitud_origen=resultado_places.get(
            "longitud"
        ),
    )

    resultado["ruta"] = resultado_ruta

    if not resultado_ruta.get(
        "ruta_encontrada"
    ):
        resultado["motivo"] = (
            "Google Routes no pudo calcular la ruta; "
            "debe revisarla una persona administradora."
        )
        return resultado

    if resultado_ruta.get(
        "dentro_del_limite"
    ) is True:
        resultado.update({
            "clasificacion": "ZONA_VALIDA_POR_RUTA",
            "zona_validada": True,
            "requiere_admin": False,
            "motivo": (
                "La localidad se encuentra dentro del "
                "límite máximo de distancia por carretera."
            ),
        })

        return resultado

    resultado["motivo"] = (
        "La localidad supera el límite configurado de "
        "distancia por carretera y requiere revisión "
        "administrativa; no debe rechazarse automáticamente."
    )

    return resultado


def clasificar_zona_determinista(
    mensaje_usuario: str = "",
    zona_mencionada: str = "",
    campus_mencionado: str = "",
) -> Dict[str, Any]:
    """
    Clasifica la ubicación mediante catálogos institucionales.

    Esta función no consulta Gemini ni servicios externos.

    Prioridad:
    1. Campus externo explícito.
    2. Zona externa conocida.
    3. Zona válida directa.
    4. Zona válida por conectividad.
    5. Zona desconocida pendiente de validación geográfica.
    6. Sin zona mencionada.
    """
    texto_completo = " ".join(
        [
            str(mensaje_usuario or ""),
            str(zona_mencionada or ""),
            str(campus_mencionado or ""),
        ]
    )

    texto_normalizado = (
        normalizar_texto_geografico(
            texto_completo
        )
    )

    zona_normalizada = (
        normalizar_texto_geografico(
            zona_mencionada
        )
    )

    campus_normalizado = (
        normalizar_texto_geografico(
            campus_mencionado
        )
    )

    resultado = {
        "clasificacion": "NO_MENCIONADA",
        "zona_normalizada": zona_normalizada,
        "campus_normalizado": campus_normalizado,
        "requiere_validacion_geografica": False,
        "es_zona_validada": False,
        "es_zona_externa": False,
        "es_campus_externo": False,
    }

    if texto_contiene_alias_geografico(
        texto_normalizado,
        REFERENCIAS_CAMPUS_EXTERNOS,
    ):
        resultado.update({
            "clasificacion": "CAMPUS_EXTERNO",
            "es_campus_externo": True,
        })

        return resultado

    if texto_contiene_alias_geografico(
        texto_normalizado,
        ZONAS_EXTERNAS_CONOCIDAS,
    ):
        resultado.update({
            "clasificacion": "ZONA_EXTERNA",
            "es_zona_externa": True,
        })

        return resultado

    if texto_contiene_alias_geografico(
        texto_normalizado,
        ZONAS_VALIDAS_DIRECTAS,
    ):
        resultado.update({
            "clasificacion": "ZONA_VALIDA_DIRECTA",
            "es_zona_validada": True,
        })

        return resultado

    for alias_ambiguo in ZONAS_VALIDAS_AMBIGUAS:
        if texto_confirma_zona_ambigua(
            mensaje_usuario=mensaje_usuario,
            zona_mencionada=zona_mencionada,
            alias=alias_ambiguo,
        ):
            resultado.update({
                "clasificacion": "ZONA_VALIDA_DIRECTA",
                "es_zona_validada": True,
            })

            return resultado
            
    if texto_contiene_alias_geografico(
        texto_normalizado,
        ZONAS_VALIDAS_POR_CONECTIVIDAD,
    ):
        resultado.update({
            "clasificacion": (
                "ZONA_VALIDA_POR_CONECTIVIDAD"
            ),
            "es_zona_validada": True,
        })

        return resultado

    if zona_normalizada or campus_normalizado:
        resultado.update({
            "clasificacion": "ZONA_DESCONOCIDA",
            "requiere_validacion_geografica": True,
        })

        return resultado

    return resultado
        

def zona_previamente_validada_en_flujo(
    contact=None,
    zona_actual: str = "",
) -> bool:
    """
    Determina si la MISMA zona actual ya fue validada
    de forma autoritativa.

    Prioridad:
    1. ZONA_VALIDADA_AUTORITATIVA.
    2. Compatibilidad histórica con ZONA_VALIDADA +
       ZONA_INTERES.

    Una zona distinta nunca hereda automáticamente
    la autorización de otra.
    """

    if contact is None:
        return False

    try:
        zona_actual_normalizada = (
            normalizar_texto_geografico(
                zona_actual
            )
        )

        # ----------------------------------------------------
        # 1. AUTORIDAD ESPECÍFICA DE ZONA
        # ----------------------------------------------------

        zona_autoritativa = str(
            get_note_value(
                contact,
                "ZONA_VALIDADA_AUTORITATIVA",
            )
            or ""
        ).strip()

        if zona_autoritativa:
            zona_autoritativa_normalizada = (
                normalizar_texto_geografico(
                    zona_autoritativa
                )
            )

            if not zona_actual_normalizada:
                return True

            return (
                zona_actual_normalizada
                == zona_autoritativa_normalizada
            )

        # ----------------------------------------------------
        # 2. COMPATIBILIDAD CON CONTACTOS ANTERIORES
        # ----------------------------------------------------

        hitos_raw = get_note_value(
            contact,
            "HITOS_COMERCIALES",
        )

        if not hitos_raw:
            return False

        try:
            hitos = json.loads(
                hitos_raw
            )

        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return False

        if not isinstance(hitos, list):
            return False

        hitos_normalizados = {
            str(hito or "")
            .strip()
            .upper()
            for hito in hitos
            if str(hito or "").strip()
        }

        if (
            "ZONA_VALIDADA"
            not in hitos_normalizados
        ):
            return False

        zona_interes = str(
            get_note_value(
                contact,
                "ZONA_INTERES",
            )
            or ""
        ).strip()

        if not zona_interes:
            return False

        zona_interes_normalizada = (
            normalizar_texto_geografico(
                zona_interes
            )
        )

        if not zona_actual_normalizada:
            return False

        return (
            zona_actual_normalizada
            == zona_interes_normalizada
        )

    except Exception as e:
        print(
            "⚠️ No fue posible determinar "
            "si la zona estaba previamente validada: "
            f"{e}"
        )

        return False
def construir_datos_detectados_para_decision(
    analisis: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Reúne solamente datos útiles que posteriormente podrían guardarse.

    Esta función no escribe todavía en contact.notes.
    """

    datos = {}

    if analisis.get("zona_mencionada"):
        datos["zona_mencionada"] = analisis[
            "zona_mencionada"
        ]

    if analisis.get("nivel"):
        datos["nivel"] = analisis[
            "nivel"
        ]

    if analisis.get("grado"):
        datos["grado"] = analisis[
            "grado"
        ]

    if analisis.get("edad_alumno") is not None:
        datos["edad_alumno"] = analisis[
            "edad_alumno"
        ]

    if analisis.get("fecha_nacimiento_texto"):
        datos["fecha_nacimiento_texto"] = analisis[
            "fecha_nacimiento_texto"
        ]

    if analisis.get("fecha_nacimiento_iso"):
        datos["fecha_nacimiento_iso"] = analisis[
            "fecha_nacimiento_iso"
        ]

    if analisis.get("nivel_actual"):
        datos["nivel_actual"] = analisis[
            "nivel_actual"
        ]

    if analisis.get("ultimo_grado_cursado"):
        datos["ultimo_grado_cursado"] = analisis[
            "ultimo_grado_cursado"
        ]

    if analisis.get("grado_solicitado"):
        datos["grado_solicitado"] = analisis[
            "grado_solicitado"
        ]

    if analisis.get("nombre_tutor"):
        datos["nombre_tutor"] = analisis[
            "nombre_tutor"
        ]

    if analisis.get("nombre_alumno"):
        datos["nombre_alumno"] = analisis[
            "nombre_alumno"
        ]

    if analisis.get("fecha_cita_iso"):
        datos["fecha_cita_iso"] = analisis[
            "fecha_cita_iso"
        ]

    if analisis.get("hora_cita_24h"):
        datos["hora_cita_24h"] = analisis[
            "hora_cita_24h"
        ]

    return datos

def existe_mensaje_entrante_posterior_al_turno(
    db: Session,
    contact,
    max_message_id: Optional[int],
) -> bool:
    """
    Determina si, mientras se procesaba el turno actual,
    llegó un mensaje entrante más reciente del mismo contacto.

    Se utiliza para impedir que una respuesta ya obsoleta
    sea enviada al prospecto.
    """

    if (
        db is None
        or contact is None
        or not isinstance(max_message_id, int)
        or max_message_id <= 0
    ):
        return False

    try:
        mensaje_mas_reciente = (
            db.query(Message.id)
            .filter(
                Message.contact_id == contact.id,
                Message.direction == "incoming",
                Message.id > max_message_id,
            )
            .order_by(
                Message.id.asc()
            )
            .first()
        )

        return (
            mensaje_mas_reciente
            is not None
        )

    except Exception as e:
        print(
            "⚠️ No fue posible comprobar "
            "si el turno seguía vigente: "
            f"{e}"
        )

        return False

def existe_mensaje_saliente_posterior_al_turno(
    db: Session,
    contact,
    max_message_id: Optional[int],
) -> bool:
    """
    Determina si, mientras se procesaba el turno actual,
    apareció un mensaje saliente más reciente del mismo contacto.

    Un outbound posterior puede provenir de:
    - una respuesta administrativa;
    - una respuesta manual desde el panel;
    - otro procesamiento que ya tomó autoridad.

    Si existe, la respuesta que este turno estaba preparando
    debe considerarse obsoleta.
    """

    if (
        db is None
        or contact is None
        or not isinstance(max_message_id, int)
        or max_message_id <= 0
    ):
        return False

    try:
        mensaje_mas_reciente = (
            db.query(Message.id)
            .filter(
                Message.contact_id == contact.id,
                Message.direction == "outgoing",
                Message.id > max_message_id,
            )
            .order_by(
                Message.id.asc()
            )
            .first()
        )

        return (
            mensaje_mas_reciente
            is not None
        )

    except Exception as e:
        print(
            "⚠️ No fue posible comprobar "
            "si apareció un outbound posterior "
            f"al turno: {e}"
        )

        return False


def obtener_unidad_semantica_pendiente_desde_bd(
    db: Session,
    contact,
    max_message_id: Optional[int] = None,
    mensaje_fallback: str = "",
    max_mensajes: int = 12,
) -> Dict[str, Any]:
    """
    Reconstruye desde PostgreSQL todos los mensajes INCOMING
    que siguen conversacionalmente pendientes hasta el corte
    del turno actual.

    Regla:
    - buscamos el último OUTGOING anterior al corte;
    - tomamos todos los INCOMING posteriores a ese OUTGOING;
    - nunca tomamos INCOMING posteriores al corte actual;
    - preservamos el orden cronológico.

    Esto permite que, si una respuesta anterior fue suprimida
    porque llegaron nuevos mensajes mientras se procesaba,
    el siguiente turno vuelva a considerar también esos mensajes
    que realmente nunca recibieron respuesta.
    """

    fallback = str(
        mensaje_fallback or ""
    ).strip()

    resultado = {
        "texto": fallback,
        "message_ids": [],
        "cantidad": 0,
        "ultimo_outgoing_id": None,
        "ultimo_inbound_procesado_id": None,
        "corte_message_id": None,
        "uso_fallback": True,
    }

    if (
        db is None
        or contact is None
    ):
        return resultado

    try:

        # ----------------------------------------------------
        # 1. DETERMINAR CORTE AUTORITATIVO DEL TURNO
        # ----------------------------------------------------

        corte_efectivo = (
            max_message_id
            if (
                isinstance(max_message_id, int)
                and max_message_id > 0
            )
            else None
        )

        # Algunas rutas internas de prueba pueden llamar al
        # procesador sin max_message_id.
        #
        # En ese caso usamos el último inbound persistido.
        if corte_efectivo is None:

            ultimo_incoming = (
                db.query(Message)
                .filter(
                    Message.contact_id
                    == contact.id,
                    Message.direction
                    == "incoming",
                )
                .order_by(
                    Message.id.desc()
                )
                .first()
            )

            if ultimo_incoming is not None:
                corte_efectivo = (
                    ultimo_incoming.id
                )

        resultado[
            "corte_message_id"
        ] = corte_efectivo

        if (
            not isinstance(
                corte_efectivo,
                int,
            )
            or corte_efectivo <= 0
        ):
            return resultado

        # ----------------------------------------------------
        # 2. ÚLTIMO OUTGOING ANTERIOR AL CORTE
        # ----------------------------------------------------
        #
        # Ésta es la frontera conversacional:
        # todo inbound posterior todavía puede pertenecer
        # a la misma unidad pendiente.
        # ----------------------------------------------------

        ultimo_outgoing = (
            db.query(Message)
            .filter(
                Message.contact_id
                == contact.id,
                Message.direction
                == "outgoing",
                Message.id
                < corte_efectivo,
            )
            .order_by(
                Message.id.desc()
            )
            .first()
        )

        ultimo_outgoing_id = (
            ultimo_outgoing.id
            if ultimo_outgoing is not None
            else None
        )

        resultado[
            "ultimo_outgoing_id"
        ] = ultimo_outgoing_id

        ultimo_procesado_id = (
            obtener_ultimo_inbound_procesado_id(
                contact
            )
        )

        resultado[
            "ultimo_inbound_procesado_id"
        ] = ultimo_procesado_id

        frontera_inferior = max(
            [
                valor
                for valor in [
                    ultimo_outgoing_id,
                    ultimo_procesado_id,
                ]
                if isinstance(
                    valor,
                    int,
                )
            ],
            default=None,
        )

        # ----------------------------------------------------
        # 3. INBOUNDS NO RESPONDIDOS HASTA EL CORTE
        # ----------------------------------------------------

        query_pendientes = (
            db.query(Message)
            .filter(
                Message.contact_id
                == contact.id,
                Message.direction
                == "incoming",
                Message.id
                <= corte_efectivo,
            )
        )

        if (
            isinstance(
                frontera_inferior,
                int,
            )
            and frontera_inferior > 0
        ):
            query_pendientes = (
                query_pendientes.filter(
                    Message.id
                    > frontera_inferior
                )
            )

        # Nos quedamos con los últimos N para impedir que una
        # conversación extremadamente larga genere un bloque
        # descontrolado.
        mensajes_pendientes = (
            query_pendientes
            .order_by(
                Message.id.desc()
            )
            .limit(
                max(
                    1,
                    int(max_mensajes or 12),
                )
            )
            .all()
        )

        mensajes_pendientes = list(
            reversed(
                mensajes_pendientes
            )
        )

        # ----------------------------------------------------
        # 4. CONSTRUIR UNIDAD SEMÁNTICA
        # ----------------------------------------------------

        contenidos = []
        message_ids = []

        for mensaje_db in mensajes_pendientes:

            contenido = str(
                getattr(
                    mensaje_db,
                    "content",
                    "",
                )
                or ""
            ).strip()

            if not contenido:
                continue

            contenidos.append(
                contenido
            )

            message_id = getattr(
                mensaje_db,
                "id",
                None,
            )

            if isinstance(
                message_id,
                int,
            ):
                message_ids.append(
                    message_id
                )

        if not contenidos:
            return resultado

        texto_unidad = "\n".join(
            contenidos
        ).strip()

        resultado.update({
            "texto": texto_unidad,
            "message_ids": message_ids,
            "cantidad": len(contenidos),
            "uso_fallback": False,
        })

        return resultado

    except Exception as e:

        print(
            "⚠️ No fue posible reconstruir "
            "la unidad semántica pendiente: "
            f"contact_id="
            f"{getattr(contact, 'id', None)}, "
            f"error={e}"
        )

        return resultado

def obtener_ultimo_inbound_procesado_id(
    contact,
) -> Optional[int]:

    if contact is None:
        return None

    valor = str(
        get_note_value(
            contact,
            "ULTIMO_INBOUND_PROCESADO_ID",
        )
        or ""
    ).strip()

    if not valor:
        return None

    try:
        resultado = int(valor)

        return (
            resultado
            if resultado > 0
            else None
        )

    except (
        TypeError,
        ValueError,
    ):
        return None


def marcar_inbound_procesado_hasta(
    contact,
    message_id: Optional[int],
):

    if (
        contact is None
        or not isinstance(message_id, int)
        or message_id <= 0
    ):
        return

    anterior = (
        obtener_ultimo_inbound_procesado_id(
            contact
        )
    )

    if (
        isinstance(anterior, int)
        and anterior >= message_id
    ):
        return

    set_note_value(
        contact,
        "ULTIMO_INBOUND_PROCESADO_ID",
        str(message_id),
    )

def consumir_turno_sin_respuesta(
    db: Session,
    contact,
    max_message_id: Optional[int],
    motivo: str = "",
) -> bool:
    """
    Marca como consumido semánticamente un turno que Python
    resolvió deliberadamente sin enviar un mensaje al prospecto.

    NO debe utilizarse para:
    - turnos obsoletos por inbound posterior;
    - turnos obsoletos por outbound autoritativo;
    - errores técnicos;
    - fallos de Twilio.

    En esos casos el contenido debe seguir disponible para
    procesamiento posterior.
    """

    if (
        db is None
        or contact is None
        or not isinstance(max_message_id, int)
        or max_message_id <= 0
    ):
        return False

    marcar_inbound_procesado_hasta(
        contact,
        max_message_id,
    )

    db.commit()

    print(
        "✅ TURNO CONSUMIDO SIN RESPUESTA: "
        f"contact_id={contact.id}, "
        f"hasta={max_message_id}, "
        f"motivo={str(motivo or '').strip()}"
    )

    return True
    
        
def detectar_pausa_conversacion_simple(
    mensaje: str,
) -> bool:
    """
    Detecta expresiones frecuentes con las que el prospecto
    indica que revisará la información o responderá después.
    """
    texto = (mensaje or "").lower().strip()

    frases_pausa = [
        "después les aviso",
        "despues les aviso",
        "luego les aviso",
        "más tarde les aviso",
        "mas tarde les aviso",
        "yo les aviso",
        "les confirmo después",
        "les confirmo despues",
        "lo voy a revisar",
        "lo revisaré",
        "lo revisare",
        "déjeme revisarlo",
        "dejeme revisarlo",
        "lo platico con",
        "lo consultaré con",
        "lo consultare con",
        "lo veo con mi esposo",
        "lo veo con mi esposa",
        "lo veo con mi familia",
        "lo reviso con mi esposo",
        "lo reviso con mi esposa",
        "por el momento no",
        "más adelante",
        "mas adelante",
    ]

    return any(
        frase in texto
        for frase in frases_pausa
    )

def fecha_cita_es_no_laboral(
    fecha_cita_iso: str,
) -> bool:
    """
    Determina mediante Python si una fecha de cita corresponde
    a sábado o domingo.

    Espera el formato YYYY-MM-DD.
    Si la fecha está vacía o es inválida, devuelve False.
    """
    fecha_texto = str(
        fecha_cita_iso or ""
    ).strip()

    if not fecha_texto:
        return False

    try:
        fecha_cita = datetime.strptime(
            fecha_texto,
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return False

    return fecha_cita.weekday() in {
        5,
        6,
    }

def fecha_cita_requiere_confirmacion_calendario(
    fecha_cita_texto: str,
) -> bool:
    """
    Determina si la fecha propuesta por el prospecto fue expresada
    de forma relativa o potencialmente ambigua y, por lo tanto,
    debe confirmarse mediante una fecha calendario explícita antes
    de consultar disponibilidad con administración.

    Ejemplos que requieren confirmación:
    - viernes
    - este viernes
    - el viernes
    - próximo viernes
    - el próximo viernes
    - mañana
    - pasado mañana

    Ejemplos que no requieren confirmación:
    - 28 de agosto
    - viernes 28 de agosto
    - 28/08/2026
    - 2026-08-28
    """

    texto = normalizar_texto_para_deteccion(
        fecha_cita_texto
    )

    if not texto:
        return False

    # --------------------------------------------------------
    # FECHAS CALENDARIO EXPLÍCITAS
    # --------------------------------------------------------

    # YYYY-MM-DD
    if re.search(
        r"\b20\d{2}-\d{1,2}-\d{1,2}\b",
        texto,
    ):
        return False

    # DD/MM/YYYY, DD-MM-YYYY o variantes sin año.
    if re.search(
        r"\b\d{1,2}[/-]\d{1,2}"
        r"(?:[/-]\d{2,4})?\b",
        texto,
    ):
        return False

    meses = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]

    menciona_mes = any(
        mes in texto
        for mes in meses
    )

    menciona_dia_numerico = bool(
        re.search(
            r"\b(?:[1-9]|[12]\d|3[01])\b",
            texto,
        )
    )

    if (
        menciona_mes
        and menciona_dia_numerico
    ):
        return False

    # --------------------------------------------------------
    # EXPRESIONES RELATIVAS
    # --------------------------------------------------------

    if any(
        expresion in texto
        for expresion in [
            "hoy",
            "manana",
            "pasado manana",
        ]
    ):
        return True

    dias_semana = [
        "lunes",
        "martes",
        "miercoles",
        "jueves",
        "viernes",
        "sabado",
        "domingo",
    ]

    if any(
        dia in texto
        for dia in dias_semana
    ):
        return True

    return False


def respuesta_afirmativa_confirmacion_cita(
    mensaje_usuario: str,
) -> bool:
    """
    Detecta respuestas afirmativas simples cuando el bot está
    esperando que el prospecto confirme una fecha calendario.

    Sólo debe utilizarse cuando el objetivo pendiente sea
    CONFIRMAR_FECHA_CITA_CALENDARIO.
    """

    texto = normalizar_texto_para_deteccion(
        mensaje_usuario
    )

    if not texto:
        return False

    respuestas_afirmativas = {
        "si",
        "si correcto",
        "si es correcto",
        "correcto",
        "correcta",
        "asi es",
        "exacto",
        "exactamente",
        "de acuerdo",
        "confirmo",
        "confirmado",
        "esta bien",
        "esta perfecto",
        "perfecto",
    }

    return texto in respuestas_afirmativas

def respuesta_negativa_confirmacion_cita(
    mensaje_usuario: str,
) -> bool:
    """
    Detecta una negativa simple cuando el bot está esperando
    confirmación de una fecha calendario interpretada.

    Se usa únicamente dentro del objetivo:
    CONFIRMAR_FECHA_CITA_CALENDARIO.
    """

    texto = normalizar_texto_para_deteccion(
        mensaje_usuario
    )

    if not texto:
        return False

    respuestas_negativas = {
        "no",
        "no es correcto",
        "no correcto",
        "incorrecto",
        "incorrecta",
        "no seria",
        "no sería",
        "no esa fecha",
        "esa no",
        "no es esa",
    }

    return texto in respuestas_negativas
    

def formatear_fecha_cita_calendario(
    fecha_cita_iso: str,
) -> str:
    """
    Convierte una fecha YYYY-MM-DD a una representación
    inequívoca y natural para confirmar una cita.

    Ejemplo:
    2026-08-28 -> viernes 28 de agosto
    """

    fecha_texto = str(
        fecha_cita_iso or ""
    ).strip()

    if not fecha_texto:
        return ""

    try:
        fecha = datetime.strptime(
            fecha_texto,
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return ""

    dias = [
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    ]

    meses = [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ]

    return (
        f"{dias[fecha.weekday()]} "
        f"{fecha.day} de "
        f"{meses[fecha.month - 1]}"
    )

def formatear_hora_cita_12h(
    hora_cita_24h: str,
) -> str:
    """
    Convierte HH:MM de 24 horas a formato compacto:
    11:00 -> 11:00am
    14:00 -> 2:00pm
    """

    hora_texto = str(
        hora_cita_24h or ""
    ).strip()

    if not hora_texto:
        return ""

    try:
        hora = datetime.strptime(
            hora_texto,
            "%H:%M",
        )
    except ValueError:
        return hora_texto

    periodo = (
        "am"
        if hora.hour < 12
        else "pm"
    )

    hora_12 = hora.hour % 12

    if hora_12 == 0:
        hora_12 = 12

    return (
        f"{hora_12}:"
        f"{hora.minute:02d}"
        f"{periodo}"
    )

def validar_momento_cita(
    fecha_cita_iso: str,
    hora_cita_24h: str,
) -> Dict[str, Any]:
    """
    Valida de forma determinista que una cita corresponda
    a un momento futuro real en la zona horaria local.

    Estados:
    - INVALIDO
    - PASADO
    - HOY_SIN_HORARIO_DISPONIBLE
    - FUTURO_VALIDO

    Esta función no decide disponibilidad administrativa.
    Únicamente protege la integridad temporal.
    """

    resultado = {
        "estado": "INVALIDO",
        "valido": False,
        "fecha": str(fecha_cita_iso or "").strip(),
        "hora": str(hora_cita_24h or "").strip(),
        "motivo": "",
    }

    fecha_texto = resultado["fecha"]
    hora_texto = resultado["hora"]

    if not fecha_texto or not hora_texto:
        resultado["motivo"] = (
            "Falta fecha u hora para validar la cita."
        )
        return resultado

    try:
        fecha = datetime.strptime(
            fecha_texto,
            "%Y-%m-%d",
        ).date()

        hora = datetime.strptime(
            hora_texto,
            "%H:%M",
        ).time()

    except ValueError:
        resultado["motivo"] = (
            "La fecha o la hora no tienen un formato válido."
        )
        return resultado

    ahora_local = datetime.now(
        LOCAL_TZ
    )

    momento_cita = datetime.combine(
        fecha,
        hora,
    ).replace(
        tzinfo=LOCAL_TZ
    )

    # --------------------------------------------------------
    # FECHA/HORA YA PASADA
    # --------------------------------------------------------

    if momento_cita <= ahora_local:

        resultado.update({
            "estado": "PASADO",
            "valido": False,
            "motivo": (
                "La fecha y hora propuestas ya ocurrieron."
            ),
        })

        return resultado

    # --------------------------------------------------------
    # HOY, PERO YA TERMINÓ LA VENTANA OPERATIVA
    # --------------------------------------------------------

    if (
        fecha == ahora_local.date()
        and ahora_local.time()
        > datetime.strptime(
            "16:00",
            "%H:%M",
        ).time()
    ):
        resultado.update({
            "estado": "HOY_SIN_HORARIO_DISPONIBLE",
            "valido": False,
            "motivo": (
                "Ya terminó la ventana máxima de atención "
                "para visitas del día de hoy."
            ),
        })

        return resultado

    resultado.update({
        "estado": "FUTURO_VALIDO",
        "valido": True,
        "motivo": (
            "La fecha y hora corresponden a un momento futuro."
        ),
    })

    return resultado

def clasificar_horario_cita(
    hora_cita_24h: str,
) -> str:
    """
    Clasifica el horario propuesto para una visita.

    REGULAR:
    - De 08:00 a 13:00.

    EVALUAR:
    - Después de las 13:00 y hasta las 16:00.
    - Requiere consulta con administración.

    FUERA:
    - Antes de las 08:00 o después de las 16:00.

    INVALIDO:
    - Hora vacía o con formato distinto de HH:MM.
    """
    hora_texto = str(
        hora_cita_24h or ""
    ).strip()

    if not hora_texto:
        return "INVALIDO"

    try:
        hora_cita = datetime.strptime(
            hora_texto,
            "%H:%M",
        ).time()
    except ValueError:
        return "INVALIDO"

    inicio_regular = datetime.strptime(
        "08:00",
        "%H:%M",
    ).time()

    fin_regular = datetime.strptime(
        "13:00",
        "%H:%M",
    ).time()

    fin_evaluable = datetime.strptime(
        "16:00",
        "%H:%M",
    ).time()

    if inicio_regular <= hora_cita <= fin_regular:
        return "REGULAR"

    if fin_regular < hora_cita <= fin_evaluable:
        return "EVALUAR"

    return "FUERA"

def obtener_anio_inicio_ciclo_escolar() -> int:
    """
    Obtiene el año de inicio del ciclo escolar.

    Puede configurarse en Railway mediante:
    CICLO_ESCOLAR_ANIO_INICIO=2026

    Si la variable no existe o es inválida, utiliza el año local actual.
    """
    valor_configurado = str(
        os.getenv(
            "CICLO_ESCOLAR_ANIO_INICIO",
            "",
        ) or ""
    ).strip()

    if valor_configurado:
        try:
            anio = int(valor_configurado)

            if 2020 <= anio <= 2100:
                return anio
        except ValueError:
            pass

    return datetime.now(LOCAL_TZ).year


def clasificar_nivel_por_fecha_nacimiento(
    fecha_nacimiento_iso: str,
    anio_inicio_ciclo: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Clasifica el nivel sugerido con base en la edad que el alumno
    tendrá al 31 de diciembre del año en que inicia el ciclo escolar.

    Esta función solamente determina ingreso inicial por edad.

    No asigna automáticamente grados posteriores de Primaria
    o Secundaria, porque requieren antecedente escolar.
    """
    resultado = {
        "clasificacion": "SIN_FECHA",
        "edad_al_corte": None,
        "fecha_corte": "",
        "fecha_nacimiento_valida": False,
        "requiere_antecedente_escolar": False,
    }

    fecha_texto = str(
        fecha_nacimiento_iso or ""
    ).strip()

    if not fecha_texto:
        return resultado

    try:
        fecha_nacimiento = datetime.strptime(
            fecha_texto,
            "%Y-%m-%d",
        ).date()
    except ValueError:
        resultado["clasificacion"] = "FECHA_INVALIDA"
        return resultado

    if anio_inicio_ciclo is None:
        anio_inicio_ciclo = (
            obtener_anio_inicio_ciclo_escolar()
        )

    try:
        fecha_corte = datetime(
            anio_inicio_ciclo,
            12,
            31,
        ).date()
    except ValueError:
        resultado["clasificacion"] = "ANIO_CICLO_INVALIDO"
        return resultado

    if fecha_nacimiento > fecha_corte:
        resultado["clasificacion"] = "FECHA_INVALIDA"
        return resultado

    edad_al_corte = (
        fecha_corte.year
        - fecha_nacimiento.year
        - (
            (
                fecha_corte.month,
                fecha_corte.day,
            )
            < (
                fecha_nacimiento.month,
                fecha_nacimiento.day,
            )
        )
    )

    resultado.update({
        "edad_al_corte": edad_al_corte,
        "fecha_corte": fecha_corte.isoformat(),
        "fecha_nacimiento_valida": True,
    })

    if edad_al_corte < 3:
        resultado["clasificacion"] = "PRE_KINDER"
        return resultado

    if edad_al_corte == 3:
        resultado["clasificacion"] = "KINDER_1"
        return resultado

    if edad_al_corte == 4:
        resultado["clasificacion"] = "KINDER_2"
        return resultado

    if edad_al_corte == 5:
        resultado["clasificacion"] = "KINDER_3"
        return resultado

    if edad_al_corte == 6:
        resultado["clasificacion"] = "PRIMARIA_1"
        return resultado

    resultado.update({
        "clasificacion": "REQUIERE_ANTECEDENTE_ESCOLAR",
        "requiere_antecedente_escolar": True,
    })

    return resultado

def detectar_saludo_simple_estructurado(
    mensaje_usuario: str,
) -> str:
    """
    Detecta determinísticamente cuando TODO el mensaje
    contiene únicamente saludos.

    Soporta también varios saludos consecutivos agrupados
    por el buffer, por ejemplo:

    hola
    buenas noches

    Si existe cualquier contenido sustantivo adicional,
    devuelve cadena vacía para que el mensaje continúe
    al análisis normal de intención.
    """

    mensaje_original = str(
        mensaje_usuario or ""
    ).strip()

    if not mensaje_original:
        return ""

    mensaje_normalizado = unicodedata.normalize(
        "NFD",
        mensaje_original.lower(),
    )

    mensaje_normalizado = "".join(
        caracter
        for caracter in mensaje_normalizado
        if unicodedata.category(caracter) != "Mn"
    )

    mensaje_normalizado = re.sub(
        r"[^a-z0-9\s]",
        " ",
        mensaje_normalizado,
    )

    mensaje_normalizado = re.sub(
        r"\s+",
        " ",
        mensaje_normalizado,
    ).strip()

    if not mensaje_normalizado:
        return ""

    equivalencias_saludo = {
        "hola": "Hola",
        "holaa": "Hola",
        "holaaa": "Hola",
        "buen dia": "Buenos días",
        "buenos dias": "Buenos días",
        "buenas tardes": "Buenas tardes",
        "buena tarde": "Buenas tardes",
        "buenas noches": "Buenas noches",
        "buena noche": "Buenas noches",
        "que tal": "Hola",
    }

    # Coincidencia simple exacta.
    saludo_directo = equivalencias_saludo.get(
        mensaje_normalizado
    )

    if saludo_directo:
        return saludo_directo

    # --------------------------------------------------------
    # SALUDO COMPUESTO
    # --------------------------------------------------------
    #
    # Eliminamos únicamente expresiones reconocidas como
    # saludo. Si al terminar queda alguna palabra sustantiva,
    # ya no se considera saludo simple.
    # --------------------------------------------------------

    expresiones_ordenadas = sorted(
        equivalencias_saludo.keys(),
        key=len,
        reverse=True,
    )

    restante = mensaje_normalizado
    saludos_detectados = []

    hubo_cambio = True

    while restante and hubo_cambio:

        hubo_cambio = False

        for expresion in expresiones_ordenadas:

            patron = (
                r"(?:^|\s)"
                + re.escape(expresion)
                + r"(?:\s|$)"
            )

            coincidencia = re.search(
                patron,
                restante,
            )

            if not coincidencia:
                continue

            saludos_detectados.append(
                equivalencias_saludo[expresion]
            )

            inicio, fin = coincidencia.span()

            restante = (
                restante[:inicio]
                + " "
                + restante[fin:]
            )

            restante = re.sub(
                r"\s+",
                " ",
                restante,
            ).strip()

            hubo_cambio = True
            break

    if restante:
        # Existe intención/contenido adicional.
        return ""

    if not saludos_detectados:
        return ""

    # Preferimos el saludo contextual más específico y reciente.
    for saludo in reversed(saludos_detectados):
        if saludo in {
            "Buenos días",
            "Buenas tardes",
            "Buenas noches",
        }:
            return saludo

    return "Hola"

def crear_respuesta_saludo_simple_estructurado(
    mensaje_usuario: str,
) -> str:
    """
    Genera una respuesta institucional corta y abierta
    cuando el mensaje contiene únicamente saludo(s).

    No presenta el colegio.
    No pide nombre, nivel, zona ni grado.
    No utiliza Gemini.
    """

    saludo_contextual = (
        detectar_saludo_simple_estructurado(
            mensaje_usuario
        )
    )

    if not saludo_contextual:
        return ""

    if saludo_contextual == "Hola":
        return (
            "¡Hola! Gracias por contactarnos 😃\n"
            "¿En qué le podemos ayudar?"
        )

    return (
        f"¡Hola, {saludo_contextual.lower()}! "
        "Gracias por contactarnos 😃\n"
        "¿En qué le podemos ayudar?"
    )

def obtener_niveles_costos_solicitados(
    analisis: Dict[str, Any],
    decision: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Obtiene uno o varios niveles escolares relevantes para
    una solicitud de costos.

    Prioridad:
    1. nivel explícito del análisis;
    2. niveles de los alumnos detectados;
    3. niveles previamente preservados en la decisión.

    El resultado nunca contiene duplicados ni niveles
    fuera del catálogo autorizado.
    """

    analisis_seguro = (
        analisis
        if isinstance(analisis, dict)
        else {}
    )

    decision_segura = (
        decision
        if isinstance(decision, dict)
        else {}
    )

    equivalencias = {
        "kinder": "Kínder",
        "kínder": "Kínder",
        "preescolar": "Kínder",
        "primaria": "Primaria",
        "secundaria": "Secundaria",
    }

    niveles_validos = {
        "Kínder",
        "Primaria",
        "Secundaria",
    }

    niveles = []

    def agregar_nivel(valor):
        texto = str(
            valor or ""
        ).strip()

        if not texto:
            return

        nivel_normalizado = equivalencias.get(
            texto.lower(),
            texto,
        )

        if (
            nivel_normalizado in niveles_validos
            and nivel_normalizado not in niveles
        ):
            niveles.append(
                nivel_normalizado
            )

    # Nivel principal.
    agregar_nivel(
        analisis_seguro.get(
            "nivel",
            "",
        )
    )

    # Uno o varios alumnos.
    alumnos = analisis_seguro.get(
        "alumnos",
        [],
    )

    if isinstance(alumnos, list):
        for alumno in alumnos:
            if not isinstance(
                alumno,
                dict,
            ):
                continue

            agregar_nivel(
                alumno.get(
                    "nivel",
                    "",
                )
            )

    # Datos conservados por Python.
    datos_decision = decision_segura.get(
        "datos_detectados",
        {},
    )

    if isinstance(
        datos_decision,
        dict,
    ):
        niveles_previos = datos_decision.get(
            "niveles_costos",
            [],
        )

        if isinstance(
            niveles_previos,
            list,
        ):
            for nivel in niveles_previos:
                agregar_nivel(
                    nivel
                )

        agregar_nivel(
            datos_decision.get(
                "nivel_costos",
                "",
            )
        )

    return niveles
    

def aplicar_reglas_negocio_estructuradas(
    analisis: Dict[str, Any],
    contact=None,
    mensaje_usuario: str = "",
    contexto_comercial: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Aplica las reglas críticas del colegio sobre el análisis de Gemini.

    Orden de prioridad:
    1. Campus externo o zona no atendida.
    2. Cita en día no laborable.
    3. Evaluación de alumno menor a Kínder.
    4. Pausa o cierre de conversación.
    5. Saludo simple.
    6. Protección de costos mediante validación de zona.
    7. Flujo de cita.
    8. Registro de datos de cita.
    9. Temas educativos e informes generales.

    Esta función:
    - No redacta respuestas.
    - No guarda información.
    - No envía mensajes.
    - No avisa al administrador.
    - No cambia el FLOW_STATE.
    """
    analisis_seguro = normalizar_analisis_mensaje_ia(
        analisis
    )

    decision = crear_decision_negocio_vacia()

    decision["datos_detectados"] = (
        construir_datos_detectados_para_decision(
            analisis_seguro
        )
    )

    zona_mencionada = str(
        analisis_seguro.get(
            "zona_mencionada",
            "",
        ) or ""
    ).strip()

    contexto_zona = (
        contexto_comercial
        if isinstance(
            contexto_comercial,
            dict,
        )
        else {}
    )

    zona_contexto = str(
        contexto_zona.get(
            "zona_interes",
            "",
        )
        or ""
    ).strip()

    # El dato del mensaje actual tiene prioridad.
    # Si el usuario ya había proporcionado su zona,
    # se reutiliza el dato confirmado del contexto.
    zona_para_decision = (
        zona_mencionada
        or zona_contexto
    )

    campus_mencionado = str(
        analisis_seguro.get(
            "campus_mencionado",
            "",
        ) or ""
    ).strip()


    clasificacion_zona_determinista = (
        clasificar_zona_determinista(
            mensaje_usuario=mensaje_usuario,
            zona_mencionada=zona_para_decision,
            campus_mencionado=campus_mencionado,
        )
    )

    decision["datos_detectados"][
        "clasificacion_zona_determinista"
    ] = clasificacion_zona_determinista

    # ========================================================
    # AUTORIDAD PERSISTENTE DE ZONA
    # ========================================================
    #
    # Antes de volver a consultar Google, comprobamos si
    # exactamente esta misma localidad ya fue autorizada.
    # ========================================================

    zona_valida_previamente = (
        zona_previamente_validada_en_flujo(
            contact=contact,
            zona_actual=zona_para_decision,
        )
    )

    validacion_geografica = None

    if zona_valida_previamente:

        clasificacion_zona_determinista[
            "clasificacion"
        ] = "ZONA_VALIDADA_PREVIAMENTE"

        clasificacion_zona_determinista[
            "es_zona_validada"
        ] = True

        clasificacion_zona_determinista[
            "requiere_validacion_geografica"
        ] = False

        decision["datos_detectados"][
            "zona_validada_previamente"
        ] = True

        print(
            "✅ ZONA AUTORITATIVA REUTILIZADA: "
            f"contact_id={getattr(contact, 'id', None)}, "
            f"zona={zona_para_decision!r}"
        )

    elif clasificacion_zona_determinista.get(
        "requiere_validacion_geografica",
        False,
    ):

        localidad_para_validar = (
            zona_para_decision
            or campus_mencionado
        )

        validacion_geografica = (
            validar_zona_desconocida_con_google(
                localidad_para_validar
            )
        )

        decision["datos_detectados"][
            "validacion_geografica_google"
        ] = validacion_geografica

        if validacion_geografica.get(
            "zona_validada"
        ):

            clasificacion_zona_determinista[
                "clasificacion"
            ] = "ZONA_VALIDA_POR_RUTA"

            clasificacion_zona_determinista[
                "es_zona_validada"
            ] = True

            clasificacion_zona_determinista[
                "requiere_validacion_geografica"
            ] = False

    zona_valida_en_mensaje = bool(
        clasificacion_zona_determinista.get(
            "es_zona_validada",
            False,
        )
    )

    zona_validada = bool(
        zona_valida_en_mensaje
        or zona_valida_previamente
    )


    campus_externo_determinista = bool(
        clasificacion_zona_determinista.get(
            "es_campus_externo",
            False,
        )
    )

    zona_externa_determinista = bool(
        clasificacion_zona_determinista.get(
            "es_zona_externa",
            False,
        )
    )

    decision["zona_validada"] = zona_validada

    # ========================================================
    # CAMBIO AMBIGUO DE NIVEL O POSIBLE NUEVO ALUMNO
    # ========================================================

    contexto_seguro = (
        contexto_comercial
        if isinstance(contexto_comercial, dict)
        else {}
    )

    nivel_actual_mensaje = str(
        analisis_seguro.get(
            "nivel",
            "",
        )
        or analisis_seguro.get(
            "grado_solicitado",
            "",
        )
        or ""
    ).strip()

    alumnos_previos = contexto_seguro.get(
        "alumnos",
        [],
    )

    if not isinstance(alumnos_previos, list):
        alumnos_previos = []

    niveles_previos = []
    nombres_alumnos_previos = []

    for alumno_previo in alumnos_previos:
        if not isinstance(alumno_previo, dict):
            continue

        nivel_previo = str(
            alumno_previo.get(
                "nivel_interes",
                "",
            )
            or ""
        ).strip()

        nombre_previo = str(
            alumno_previo.get(
                "nombre",
                "",
            )
            or ""
        ).strip()

        if (
            nivel_previo
            and nivel_previo not in niveles_previos
        ):
            niveles_previos.append(
                nivel_previo
            )

        if (
            nombre_previo
            and nombre_previo
            not in nombres_alumnos_previos
        ):
            nombres_alumnos_previos.append(
                nombre_previo
            )

    nivel_nuevo_distinto = bool(
        nivel_actual_mensaje
        and niveles_previos
        and nivel_actual_mensaje
        not in niveles_previos
    )

    mensaje_normalizado = (
        normalizar_texto_para_deteccion(
            mensaje_usuario
        )
    )

    nombre_alumno_detectado = str(
        analisis_seguro.get(
            "nombre_alumno",
            "",
        )
        or ""
    ).strip()

    nombre_alumno_normalizado = (
        normalizar_texto_para_deteccion(
            nombre_alumno_detectado
        )
    )

    nombre_alumno_mencionado_en_mensaje = bool(
        nombre_alumno_normalizado
        and nombre_alumno_normalizado
        in mensaje_normalizado
    )
    
    menciona_otro_alumno = any(
        expresion in mensaje_normalizado
        for expresion in [
            "otro hijo",
            "otra hija",
            "otro alumno",
            "otra alumna",
            "mi otro hijo",
            "mi otra hija",
        ]
    )

    confirma_mismo_alumno = any(
        expresion in mensaje_normalizado
        for expresion in [
            "es para el mismo",
            "es para la misma",
            "tambien es para",
            "también es para",
            "para el mismo hijo",
            "para la misma hija",
        ]
    )

    cambio_nivel_ambiguo = bool(
        nivel_nuevo_distinto
        and not nombre_alumno_mencionado_en_mensaje
        and not menciona_otro_alumno
        and not confirma_mismo_alumno
    )
    
    if cambio_nivel_ambiguo:
        decision.update({
            "accion": "CONTINUAR_CONVERSACION",
            "motivo": (
                "El prospecto mencionó un nivel distinto al "
                "registrado anteriormente, pero no está claro "
                "si se refiere al mismo alumno o a otro."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "requiere_aclarar_alumno": True,
            "nivel_actual_mensaje": (
                nivel_actual_mensaje
            ),
            "niveles_previos": niveles_previos,
            "nombres_alumnos_previos": (
                nombres_alumnos_previos
            ),
            "pregunta_aclaratoria_sugerida": (
                "¿Los informes de "
                f"{nivel_actual_mensaje} son para el alumno "
                "que ya tenemos registrado o para otro alumno?"
            ),
        })

        return decision
        
    
    # ========================================================
    # 1. CAMPUS EXTERNO O ZONA NO ATENDIDA
    # ========================================================

    if (
        campus_externo_determinista
        or zona_externa_determinista
    ):
        decision.update({
            "accion": "RECHAZAR_CAMPUS",
            "motivo": (
                "El prospecto busca otro campus o una zona "
                "que no corresponde a Santa Cruz Atizapán."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
            "debe_finalizar_conversacion": True,
        })

        return decision

    if (
        validacion_geografica
        and validacion_geografica.get(
            "clasificacion"
        ) == "ZONA_REQUIERE_REVISION"
    ):
        decision.update({
            "accion": "CONSULTAR_ADMIN",
            "motivo": (
                validacion_geografica.get(
                    "motivo"
                )
                or (
                    "La zona requiere revisión "
                    "administrativa."
                )
            ),
            "requiere_admin": True,
            "puede_compartir_costos": False,
            "zona_validada": False,
            "debe_finalizar_conversacion": False,
        })

        return decision    

    # ========================================================
    # 2. CITA EN SÁBADO O DOMINGO
    # ========================================================

    fecha_cita_iso = str(
        analisis_seguro.get(
            "fecha_cita_iso",
            "",
        ) or ""
    ).strip()

    cita_en_dia_no_laboral = (
        fecha_cita_es_no_laboral(
            fecha_cita_iso
        )
    )

    if cita_en_dia_no_laboral:
        decision.update({
            "accion": "CITA_DIA_NO_LABORAL",
            "motivo": (
                "La fecha propuesta corresponde a sábado "
                "o domingo."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
        })

        return decision
        
    # ========================================================
    # 3. CLASIFICACIÓN DETERMINISTA POR EDAD
    # ========================================================

    fecha_nacimiento_iso = str(
        analisis_seguro.get(
            "fecha_nacimiento_iso",
            "",
        ) or ""
    ).strip()

    edad_alumno = analisis_seguro.get(
        "edad_alumno"
    )

    parece_menor_de_kinder = (
        analisis_seguro.get(
            "requiere_validar_pre_kinder"
        )
        or (
            edad_alumno is not None
            and edad_alumno < 3
        )
    )

    if parece_menor_de_kinder and not fecha_nacimiento_iso:
        decision.update({
            "accion": "PEDIR_FECHA_NACIMIENTO",
            "motivo": (
                "Se requiere la fecha de nacimiento completa "
                "para calcular la edad del alumno al corte "
                "del ciclo escolar."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
        })

        return decision

    clasificacion_edad = (
        clasificar_nivel_por_fecha_nacimiento(
            fecha_nacimiento_iso
        )
    )

    decision["datos_detectados"][
        "clasificacion_edad"
    ] = clasificacion_edad

    if (
        clasificacion_edad.get("clasificacion")
        == "PRE_KINDER"
    ):
        decision.update({
            "accion": "ORIENTAR_PRE_KINDER",
            "motivo": (
                "De acuerdo con su fecha de nacimiento, "
                "el alumno tendrá menos de 3 años al "
                "31 de diciembre del año de inicio del ciclo."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
        })

        return decision

    # ========================================================
    # 4. SEGUIMIENTO CONTEXTUAL DE CITA PENDIENTE
    # ========================================================

    estado_contacto = str(
        getattr(
            contact,
            "status",
            "",
        )
        or ""
    ).strip().upper()

    etapa_contacto = ""

    if contact is not None:
        try:
            etapa_contacto = str(
                get_note_value(
                    contact,
                    "ETAPA_CONVERSACIONAL",
                )
                or get_flow_state(contact)
                or ""
            ).strip().upper()

        except Exception:
            etapa_contacto = ""

    contexto_cita_pendiente_determinista = (
        estado_contacto
        == "CITA_PENDIENTE_CONFIRMACION"
        or etapa_contacto
        == "ESPERANDO_CONFIRMACION_ADMIN"
    )

    contexto_cita_pendiente_ia = bool(
        analisis.get(
            "contexto_cita_pendiente_reconocido",
            False,
        )
    )

    seguimiento_cita_ia = bool(
        analisis.get(
            "seguimiento_cita",
            False,
        )
    )

    solicitud_confirmacion_cita_ia = bool(
        analisis.get(
            "solicitud_confirmacion_cita",
            False,
        )
    )

    requiere_admin_contextual = bool(
        analisis.get(
            "requiere_admin_contextual",
            False,
        )
    )

    seguimiento_contextual_detectado = (
        seguimiento_cita_ia
        or solicitud_confirmacion_cita_ia
    )

    # La espera administrativa es un estado autoritativo.
    # Gemini puede reconocer semántica de seguimiento,
    # pero no puede crear por inferencia una autoridad
    # administrativa que PostgreSQL todavía no posee.
    contexto_cita_pendiente = (
        contexto_cita_pendiente_determinista
    )

    if (
        contexto_cita_pendiente
        and seguimiento_contextual_detectado
        and requiere_admin_contextual
    ):
        decision.update({
            "accion": "CONSULTAR_ADMIN",
            "motivo": (
                "La IA reconoció que el prospecto está "
                "dando seguimiento a una visita cuya "
                "confirmación administrativa continúa "
                "pendiente."
            ),
            "requiere_admin": True,
            "puede_compartir_costos": zona_validada,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "contexto_cita_pendiente_determinista": (
                contexto_cita_pendiente_determinista
            ),
            "contexto_cita_pendiente_ia": (
                contexto_cita_pendiente_ia
            ),
            "seguimiento_cita_ia": (
                seguimiento_cita_ia
            ),
            "solicitud_confirmacion_cita_ia": (
                solicitud_confirmacion_cita_ia
            ),
            "requiere_admin_contextual": (
                requiere_admin_contextual
            ),
        })

        return decision

    # ========================================================
    # 4-B. PREGUNTA PARALELA MIENTRAS LA CITA ESPERA ADMIN
    # ========================================================
    #
    # Una pregunta adicional del prospecto no debe sacar
    # la conversación del estado de espera administrativa.
    #
    # Ejemplos:
    # - ¿Debo llevar identificación?
    # - ¿Mi hijo debe acompañarme?
    # - ¿Dónde me estaciono?
    # - ¿Cuál es la dirección?
    # - ¿Puedo ir con mi esposo?
    #
    # La pregunta puede ser respondida normalmente, pero
    # NO debe reabrirse el embudo comercial ni modificarse
    # el objetivo pendiente de la cita.
    # ========================================================

    pregunta_paralela_cita = bool(
        analisis_seguro.get(
            "pregunta_paralela",
            False,
        )
    )

    if (
        contexto_cita_pendiente
        and pregunta_paralela_cita
    ):
        decision.update({
            "accion": "CONTINUAR_CONVERSACION",
            "motivo": (
                "La familia realizó una pregunta paralela "
                "mientras la cita continúa pendiente de "
                "confirmación administrativa. Debe atenderse "
                "la pregunta sin alterar el estado de la cita."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "preservar_estado_cita_pendiente": True,
            "contexto_cita_pendiente_determinista": (
                contexto_cita_pendiente_determinista
            ),
            "pregunta_paralela_cita": True,
        })

        return decision

    # ========================================================
    # 5. PAUSA O CIERRE TEMPORAL
    # ========================================================


    if (
        analisis_seguro.get("desistimiento_temporal")
        or analisis_seguro.get("pausa_conversacion")
        or analisis_seguro.get("intencion_principal")
        == "PAUSAR_CONVERSACION"
        or detectar_pausa_conversacion_simple(
            mensaje_usuario
        )
    ):
        decision.update({
            "accion": "SEGUIMIENTO",
            "motivo": (
                "El prospecto indicó que revisará la "
                "información o retomará posteriormente."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
        })

        return decision

    # ========================================================
    # 6. SALUDO SIMPLE
    # ========================================================

    saludo_simple_determinista = (
        detectar_saludo_simple_estructurado(
            mensaje_usuario
        )
    )

    if saludo_simple_determinista:
        decision.update({
            "accion": "RESPONDER_SALUDO",
            "motivo": (
                "El mensaje contiene únicamente un saludo."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
        })

        decision["datos_detectados"][
            "saludo_contextual"
        ] = saludo_simple_determinista

        return decision

    # ========================================================
    # PRIORIDAD: SOLICITUD DE UBICACIÓN INSTITUCIONAL
    # ========================================================
    #
    # Una consulta logística directa no debe obligar al
    # prospecto a recorrer etapas comerciales pendientes.
    #
    # Gemini identifica la intención.
    # Python decide la acción.
    # Python proporcionará posteriormente el enlace autorizado.
    # ========================================================

    intencion_principal_actual = str(
        analisis_seguro.get(
            "intencion_principal",
            "",
        )
        or ""
    ).strip().upper()

    intenciones_secundarias_actuales = (
        analisis_seguro.get(
            "intenciones_secundarias",
            [],
        )
    )

    if not isinstance(
        intenciones_secundarias_actuales,
        list,
    ):
        intenciones_secundarias_actuales = []

    solicitud_ubicacion_determinista = (
        detectar_solicitud_ubicacion_institucional(
            mensaje_usuario
        )
    )

    solicita_ubicacion = bool(
        intencion_principal_actual
        == "PEDIR_UBICACION"
        or "PEDIR_UBICACION"
        in intenciones_secundarias_actuales
        or solicitud_ubicacion_determinista
    )

    if solicita_ubicacion:
        decision.update({
            "accion": "RESPONDER_UBICACION",
            "motivo": (
                "El prospecto solicitó directamente "
                "la ubicación institucional del campus."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "solicitud_ubicacion_institucional": True,
            "deteccion_ubicacion_determinista": (
                solicitud_ubicacion_determinista
            ),
        })

        print(
            "📍 SOLICITUD DE UBICACIÓN PRIORIZADA: "
            f"intencion_ia={intencion_principal_actual}, "
            f"determinista={solicitud_ubicacion_determinista}"
        )

        return decision

    # ========================================================
    # CONFIRMACIÓN DE FECHA CALENDARIO PENDIENTE
    # ========================================================
    #
    # Si el bot acaba de convertir una expresión relativa
    # como "el próximo viernes" en una fecha calendario
    # concreta y está esperando confirmación, una respuesta
    # afirmativa simple debe continuar directamente hacia
    # consulta administrativa.
    #
    # No dependemos de que Gemini vuelva a extraer fecha/hora
    # del mensaje "sí".
    # ========================================================

    contexto_confirmacion_cita = (
        contexto_comercial
        if isinstance(
            contexto_comercial,
            dict,
        )
        else {}
    )

    objetivo_confirmacion_cita = str(
        contexto_confirmacion_cita.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    if (
        objetivo_confirmacion_cita
        == "CONFIRMAR_FECHA_CITA_CALENDARIO"
        and respuesta_afirmativa_confirmacion_cita(
            mensaje_usuario
        )
    ):
        fecha_cita_persistida = ""
        hora_cita_persistida = ""

        if contact is not None:
            try:
                fecha_cita_persistida = str(
                    get_note_value(
                        contact,
                        "FECHA_CITA",
                    )
                    or get_note_value(
                        contact,
                        "FECHA_CITA_ISO",
                    )
                    or get_note_value(
                        contact,
                        "FECHA_CITA_TEXTO",
                    )
                    or ""
                ).strip()

                hora_cita_persistida = str(
                    get_note_value(
                        contact,
                        "HORA_CITA",
                    )
                    or get_note_value(
                        contact,
                        "HORA_CITA_24H",
                    )
                    or get_note_value(
                        contact,
                        "HORA_CITA_TEXTO",
                    )
                    or ""
                ).strip()

            except Exception as e:
                print(
                    "⚠️ No fue posible recuperar fecha/hora "
                    "para confirmar cita: "
                    f"{e}"
                )

        if (
            fecha_cita_persistida
            and hora_cita_persistida
        ):
            validacion_momento_confirmado = (
                validar_momento_cita(
                    fecha_cita_persistida,
                    hora_cita_persistida,
                )
            )

            if not validacion_momento_confirmado.get(
                "valido",
                False,
            ):
                decision.update({
                    "accion": "PEDIR_FECHA_CITA",
                    "motivo": (
                        "La fecha y hora previamente propuestas "
                        "ya no corresponden a un momento futuro. "
                        "Debe solicitarse una nueva opción."
                    ),
                    "requiere_admin": False,
                    "puede_compartir_costos": zona_validada,
                    "debe_finalizar_conversacion": False,
                })

                decision["datos_detectados"].update({
                    "momento_cita_invalido": True,
                    "estado_momento_cita": (
                        validacion_momento_confirmado.get(
                            "estado",
                            "INVALIDO",
                        )
                    ),
                    "fecha_cita_rechazada": (
                        fecha_cita_persistida
                    ),
                    "hora_cita_rechazada": (
                        hora_cita_persistida
                    ),
                })

                return decision

            clasificacion_horario_confirmado = (
                clasificar_horario_cita(
                    hora_cita_persistida
                )
            )
            
            if (
                clasificacion_horario_confirmado
                in {
                    "REGULAR",
                    "EVALUAR",
                }
            ):
                decision.update({
                    "accion": "CONSULTAR_ADMIN",
                    "motivo": (
                        "El prospecto confirmó expresamente "
                        "la fecha calendario y horario que "
                        "habían sido interpretados previamente."
                    ),
                    "requiere_admin": True,
                    "puede_compartir_costos": (
                        zona_validada
                    ),
                    "debe_finalizar_conversacion": False,
                })

                decision["datos_detectados"].update({
                    "fecha_cita_confirmada_calendario": (
                        fecha_cita_persistida
                    ),
                    "hora_cita_confirmada_calendario": (
                        hora_cita_persistida
                    ),
                    "confirmacion_calendario_explicita": True,
                })

                return decision

    if (
        objetivo_confirmacion_cita
        == "CONFIRMAR_FECHA_CITA_CALENDARIO"
        and respuesta_negativa_confirmacion_cita(
            mensaje_usuario
        )
    ):
        decision.update({
            "accion": "PEDIR_FECHA_CITA",
            "motivo": (
                "El prospecto indicó que la fecha calendario "
                "interpretada no corresponde a la que desea. "
                "Debe solicitarse nuevamente el día y horario."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "correccion_fecha_cita": True,
        })

        return decision

    # ========================================================
    # CORRECCIÓN COMPUESTA DE FECHA/HORA DE CITA
    # ========================================================
    #
    # Cuando estamos esperando confirmar una fecha calendario,
    # el prospecto puede responder con una corrección completa:
    #
    # "No, me refería al viernes siguiente."
    # "No, mejor el lunes."
    # "No, mejor a las 12."
    #
    # En esos casos conservamos del intento anterior únicamente
    # el dato que el prospecto NO está modificando.
    # ========================================================

    correccion_cita_pendiente = False

    if (
        objetivo_confirmacion_cita
        == "CONFIRMAR_FECHA_CITA_CALENDARIO"
    ):
        nueva_fecha_texto = str(
            analisis_seguro.get(
                "fecha_cita_texto",
                "",
            )
            or ""
        ).strip()

        nueva_fecha_iso = str(
            analisis_seguro.get(
                "fecha_cita_iso",
                "",
            )
            or ""
        ).strip()

        nueva_hora_texto = str(
            analisis_seguro.get(
                "hora_cita_texto",
                "",
            )
            or ""
        ).strip()

        nueva_hora_24h = str(
            analisis_seguro.get(
                "hora_cita_24h",
                "",
            )
            or ""
        ).strip()

        cambio_fecha_detectado = bool(
            analisis_seguro.get(
                "cambio_fecha_cita"
            )
            or nueva_fecha_texto
            or nueva_fecha_iso
            or nueva_hora_texto
            or nueva_hora_24h
        )

        if cambio_fecha_detectado:

            fecha_cita_anterior = ""
            hora_cita_anterior = ""

            if contact is not None:
                try:
                    fecha_cita_anterior = str(
                        get_note_value(
                            contact,
                            "FECHA_CITA",
                        )
                        or get_note_value(
                            contact,
                            "FECHA_CITA_ISO",
                        )
                        or ""
                    ).strip()

                    hora_cita_anterior = str(
                        get_note_value(
                            contact,
                            "HORA_CITA",
                        )
                        or get_note_value(
                            contact,
                            "HORA_CITA_24H",
                        )
                        or ""
                    ).strip()

                except Exception as e:
                    print(
                        "⚠️ No fue posible recuperar "
                        "fecha/hora anterior durante "
                        "la corrección de cita: "
                        f"{e}"
                    )

            # Si corrigió solamente la fecha,
            # conservamos el horario anterior.
            if (
                (nueva_fecha_iso or nueva_fecha_texto)
                and not (
                    nueva_hora_24h
                    or nueva_hora_texto
                )
                and hora_cita_anterior
            ):
                analisis_seguro[
                    "hora_cita_24h"
                ] = hora_cita_anterior

            # Si corrigió solamente la hora,
            # conservamos la fecha anterior.
            if (
                (nueva_hora_24h or nueva_hora_texto)
                and not (
                    nueva_fecha_iso
                    or nueva_fecha_texto
                )
                and fecha_cita_anterior
            ):
                analisis_seguro[
                    "fecha_cita_iso"
                ] = fecha_cita_anterior

            correccion_cita_pendiente = True
            
    

    # ========================================================
    # 7. PRIORIDAD: SOLICITUD EXPLÍCITA DE CITA
    # ========================================================
    #
    # Si el prospecto solicita expresamente una visita,
    # el agendamiento tiene prioridad sobre las etapas
    # informativas pendientes.
    #
    # No se obliga a completar previamente:
    # - propuesta general de valor;
    # - explicación del Método Filadelfia;
    # - identificación de área de interés;
    # - profundización de interés.
    #
    # La validación de zona sigue siendo una regla crítica.
    # ========================================================

    intenciones_cita = {
        "PEDIR_CITA",
        "PROPONER_FECHA_CITA",
        "PROPONER_HORA_CITA",
    }

    tiene_intencion_cita = bool(
        correccion_cita_pendiente
        or objetivo_confirmacion_cita
        == "OBTENER_ZONA_PARA_CITA"
        or analisis_seguro.get(
            "pide_cita"
        )
        or analisis_seguro.get(
            "intencion_principal"
        ) in intenciones_cita
        or any(
            intencion in intenciones_cita
            for intencion in analisis_seguro.get(
                "intenciones_secundarias",
                [],
            )
        )
    )


    if tiene_intencion_cita:

        # ----------------------------------------------------
        # LA ZONA SIGUE SIENDO VALIDACIÓN CRÍTICA
        # ----------------------------------------------------

        if not zona_validada:
            decision.update({
                "accion": "PEDIR_ZONA",
                "motivo": (
                    "El prospecto solicitó una visita, "
                    "pero todavía es necesario validar "
                    "la localidad antes de continuar "
                    "con el agendamiento."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "objetivo_pendiente_sugerido"
            ] = "OBTENER_ZONA_PARA_CITA"

            return decision

        fecha_cita = str(
            analisis_seguro.get(
                "fecha_cita_iso",
                "",
            )
            or ""
        ).strip()

        hora_cita = str(
            analisis_seguro.get(
                "hora_cita_24h",
                "",
            )
            or ""
        ).strip()

        # ----------------------------------------------------
        # FALTA FECHA
        # ----------------------------------------------------

        if not fecha_cita:
            decision.update({
                "accion": "PEDIR_FECHA_CITA",
                "motivo": (
                    "El prospecto solicitó explícitamente "
                    "una visita. Se omiten las etapas "
                    "informativas pendientes y se comienza "
                    "el agendamiento."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            return decision

        # ----------------------------------------------------
        # VALIDACIÓN DE FECHA AUNQUE TODAVÍA NO EXISTA HORA
        # ----------------------------------------------------
        #
        # Evita preguntar una hora para una fecha que ya pasó
        # o para "hoy" cuando ya terminó la ventana máxima
        # de atención.
        # ----------------------------------------------------

        try:
            fecha_cita_sola = datetime.strptime(
                fecha_cita,
                "%Y-%m-%d",
            ).date()

        except ValueError:
            decision.update({
                "accion": "PEDIR_FECHA_CITA",
                "motivo": (
                    "La fecha proporcionada no pudo "
                    "interpretarse correctamente."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "momento_cita_invalido": True,
                "estado_momento_cita": "INVALIDO",
                "fecha_cita_rechazada": fecha_cita,
            })

            return decision

        ahora_local_cita = datetime.now(
            LOCAL_TZ
        )

        fecha_hoy_local = (
            ahora_local_cita.date()
        )

        hora_limite_hoy = datetime.strptime(
            "16:00",
            "%H:%M",
        ).time()

        if fecha_cita_sola < fecha_hoy_local:
            decision.update({
                "accion": "PEDIR_FECHA_CITA",
                "motivo": (
                    "La fecha propuesta ya ocurrió. "
                    "Debe solicitarse una fecha futura."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "momento_cita_invalido": True,
                "estado_momento_cita": "PASADO",
                "fecha_cita_rechazada": fecha_cita,
            })

            return decision

        if (
            fecha_cita_sola == fecha_hoy_local
            and ahora_local_cita.time()
            > hora_limite_hoy
        ):
            decision.update({
                "accion": "PEDIR_FECHA_CITA",
                "motivo": (
                    "La familia propuso una visita para hoy, "
                    "pero ya terminó la ventana máxima de "
                    "atención del día. Debe solicitarse una "
                    "fecha posterior."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "momento_cita_invalido": True,
                "estado_momento_cita": (
                    "HOY_SIN_HORARIO_DISPONIBLE"
                ),
                "fecha_cita_rechazada": fecha_cita,
            })

            return decision

        # ----------------------------------------------------
        # FALTA HORA
        # ----------------------------------------------------

        if not hora_cita:
            decision.update({
                "accion": "PEDIR_HORA_CITA",
                "motivo": (
                    "El prospecto ya proporcionó fecha "
                    "para la visita, pero todavía falta "
                    "definir el horario."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            return decision

        # ----------------------------------------------------
        # VALIDAR HORARIO
        # ----------------------------------------------------

        # ----------------------------------------------------
        # AUTORIDAD TEMPORAL DE LA CITA
        # ----------------------------------------------------

        validacion_momento_cita = (
            validar_momento_cita(
                fecha_cita,
                hora_cita,
            )
        )

        if not validacion_momento_cita.get(
            "valido",
            False,
        ):
            estado_momento = str(
                validacion_momento_cita.get(
                    "estado",
                    "INVALIDO",
                )
                or "INVALIDO"
            ).strip().upper()

            decision.update({
                "accion": "PEDIR_FECHA_CITA",
                "motivo": (
                    "La fecha y hora propuestas no corresponden "
                    "a un momento futuro disponible. Debe "
                    "solicitarse una nueva fecha y horario."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "momento_cita_invalido": True,
                "estado_momento_cita": (
                    estado_momento
                ),
                "fecha_cita_rechazada": (
                    fecha_cita
                ),
                "hora_cita_rechazada": (
                    hora_cita
                ),
            })

            return decision
        
        clasificacion_horario = (
            clasificar_horario_cita(
                hora_cita
            )
        )

        if clasificacion_horario == "INVALIDO":
            decision.update({
                "accion": "PEDIR_HORA_CITA",
                "motivo": (
                    "El horario proporcionado no pudo "
                    "interpretarse claramente."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            return decision

        if clasificacion_horario == "FUERA":
            decision.update({
                "accion": "CITA_FUERA_HORARIO",
                "motivo": (
                    "El horario solicitado está fuera "
                    "del rango disponible para visitas."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            return decision

        # ----------------------------------------------------
        # CONFIRMAR FECHA CALENDARIO SI LA EXPRESIÓN ES RELATIVA
        # ----------------------------------------------------

        fecha_cita_texto_original = str(
            analisis_seguro.get(
                "fecha_cita_texto",
                "",
            )
            or ""
        ).strip()

        contexto_seguro_cita = (
            contexto_comercial
            if isinstance(
                contexto_comercial,
                dict,
            )
            else {}
        )

        objetivo_pendiente_actual = str(
            contexto_seguro_cita.get(
                "objetivo_pendiente",
                "",
            )
            or ""
        ).strip().upper()

        esperando_confirmacion_fecha = (
            objetivo_pendiente_actual
            == "CONFIRMAR_FECHA_CITA_CALENDARIO"
        )

        confirmacion_afirmativa = (
            esperando_confirmacion_fecha
            and respuesta_afirmativa_confirmacion_cita(
                mensaje_usuario
            )
        )

        if (
            fecha_cita_requiere_confirmacion_calendario(
                fecha_cita_texto_original
            )
            and not confirmacion_afirmativa
        ):
            decision.update({
                "accion": "CONFIRMAR_FECHA_CITA",
                "motivo": (
                    "El prospecto proporcionó una expresión "
                    "relativa de fecha. Antes de consultar "
                    "disponibilidad con administración debe "
                    "confirmarse la fecha calendario interpretada."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "fecha_cita_texto_original": (
                    fecha_cita_texto_original
                ),
                "fecha_cita_iso_confirmar": (
                    fecha_cita
                ),
                "hora_cita_confirmar": (
                    hora_cita
                ),
            })

            return decision
            

        if clasificacion_horario == "EVALUAR":
            decision.update({
                "accion": "CONSULTAR_ADMIN",
                "motivo": (
                    "El horario solicitado requiere "
                    "validación administrativa antes "
                    "de confirmarse."
                ),
                "requiere_admin": True,
                "puede_compartir_costos": zona_validada,
                "debe_finalizar_conversacion": False,
            })

            return decision

        # ----------------------------------------------------
        # FECHA Y HORARIO REGULARES
        # ----------------------------------------------------

        decision.update({
            "accion": "CONSULTAR_ADMIN",
            "motivo": (
                "El prospecto solicitó explícitamente "
                "una visita y proporcionó fecha y horario. "
                "La disponibilidad debe confirmarla "
                "administración."
            ),
            "requiere_admin": True,
            "puede_compartir_costos": zona_validada,
            "debe_finalizar_conversacion": False,
        })

        return decision

    # ========================================================
    # 8. SECUENCIA COMERCIAL NORMAL
    # ========================================================

    contexto_secuencial = (
        contexto_comercial
        if isinstance(
            contexto_comercial,
            dict,
        )
        else {}
    )

    hitos_comerciales = (
        contexto_secuencial.get(
            "hitos_comerciales",
            [],
        )
    )

    if not isinstance(
        hitos_comerciales,
        list,
    ):
        hitos_comerciales = []

    hitos_comerciales = {
        str(hito or "").strip().upper()
        for hito in hitos_comerciales
        if str(hito or "").strip()
    }

    # --------------------------------------------------------
    # BARRERA POST-CITA CONFIRMADA
    # --------------------------------------------------------
    #
    # Una cita confirmada es un punto de no retorno para el
    # embudo comercial previo a la visita.
    #
    # Desde este momento ya no se deben ejecutar automáticamente:
    # - referencia;
    # - propuesta general de valor;
    # - Método Filadelfia;
    # - área de interés;
    # - profundización;
    # - invitación a visita.
    #
    # La familia todavía puede hacer preguntas concretas como
    # costos, ubicación u otros temas, pero esas solicitudes se
    # atienden por sus rutas específicas, no reabriendo el embudo.
    # --------------------------------------------------------

    etapa_comercial_actual = str(
        contexto_secuencial.get(
            "etapa_conversacional",
            "",
        )
        or ""
    ).strip().upper()

    estado_comercial_actual = str(
        contexto_secuencial.get(
            "estado_comercial",
            "",
        )
        or ""
    ).strip().upper()

    cita_ya_confirmada = bool(
        "CITA_CONFIRMADA"
        in hitos_comerciales
        or etapa_comercial_actual
        == "VISITA_CONFIRMADA"
        or estado_comercial_actual
        == "VISITA_CONFIRMADA"
    )

    referencia_previa = str(
        contexto_secuencial.get(
            "referencia_colegio",
            "",
        )
        or ""
    ).strip()

    areas_interes_previas = (
        contexto_secuencial.get(
            "areas_interes",
            [],
        )
    )

    if not isinstance(
        areas_interes_previas,
        list,
    ):
        areas_interes_previas = []

    intencion_principal = str(
        analisis_seguro.get(
            "intencion_principal",
            "",
        )
        or ""
    ).strip().upper()

    intenciones_secundarias = (
        analisis_seguro.get(
            "intenciones_secundarias",
            [],
        )
    )

    if not isinstance(
        intenciones_secundarias,
        list,
    ):
        intenciones_secundarias = []

    # ========================================================
    # PRIORIDAD: PREGUNTA EXPLÍCITA SOBRE HORARIOS
    # ========================================================
    #
    # Las preguntas institucionales concretas deben responderse
    # antes de continuar con pasos blandos del embudo como
    # referencia, propuesta de valor o área de interés.
    # ========================================================

    if detectar_solicitud_horarios(
        mensaje_usuario
    ):

        niveles_horarios = []

        alumnos_horarios = (
            contexto_secuencial.get(
                "alumnos",
                [],
            )
        )

        if isinstance(
            alumnos_horarios,
            list,
        ):
            for alumno in alumnos_horarios:

                if not isinstance(
                    alumno,
                    dict,
                ):
                    continue

                nivel = str(
                    alumno.get(
                        "nivel_interes",
                        "",
                    )
                    or alumno.get(
                        "nivel",
                        "",
                    )
                    or ""
                ).strip()

                if (
                    nivel
                    and nivel
                    not in niveles_horarios
                ):
                    niveles_horarios.append(
                        nivel
                    )

        nivel_turno = str(
            analisis_seguro.get(
                "nivel",
                "",
            )
            or ""
        ).strip()

        if (
            nivel_turno
            and nivel_turno
            not in niveles_horarios
        ):
            niveles_horarios.append(
                nivel_turno
            )

        decision.update({
            "accion": "RESPONDER_HORARIOS",
            "motivo": (
                "La familia realizó una pregunta explícita "
                "sobre los horarios escolares."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "niveles_horarios": (
                niveles_horarios
            ),
        })

        return decision
    
    # ========================================================
    # ESTRATEGIA DE SOLICITUD DE COSTOS
    # ========================================================

    objetivo_pendiente_actual = str(
        contexto_secuencial.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    solicitud_costos_explicita = bool(
        analisis_seguro.get("pide_costos")
        or intencion_principal == "PEDIR_COSTOS"
        or "PEDIR_COSTOS"
        in intenciones_secundarias
    )

    continuacion_zona_costos = (
        objetivo_pendiente_actual
        == "OBTENER_ZONA_PARA_COSTOS"
    )

    continuacion_nivel_costos = (
        objetivo_pendiente_actual
        == "OBTENER_NIVEL_PARA_COSTOS"
    )

    continuacion_solicitud_costos = bool(
        continuacion_zona_costos
        or continuacion_nivel_costos
    )

    continuacion_costos_sin_nueva_solicitud = bool(
        continuacion_solicitud_costos
        and not solicitud_costos_explicita
    )

    solicito_costos_previamente = (
        "SOLICITO_COSTOS_INICIAL"
        in hitos_comerciales
    )

    es_continuacion_primera_solicitud_costos = bool(
        continuacion_costos_sin_nueva_solicitud
        and "INSISTIO_COSTOS"
        not in hitos_comerciales
    )

    if (
        solicitud_costos_explicita
        or continuacion_solicitud_costos
    ):
        
        # ----------------------------------------------------
        # ----------------------------------------------------
        # PRIMERA SOLICITUD DE COSTOS
        # ----------------------------------------------------
        #
        # Se conserva la estrategia comercial de contextualizar
        # el precio antes de compartirlo, pero nunca repetimos
        # información que la familia ya recibió.
        # ----------------------------------------------------

        if (
            not solicito_costos_previamente
            or es_continuacion_primera_solicitud_costos
        ):

            if not zona_validada:
                decision.update({
                    "accion": "PEDIR_ZONA",
                    "motivo": (
                        "Primera solicitud de costos. "
                        "Primero corresponde validar la zona."
                    ),
                    "requiere_admin": False,
                    "puede_compartir_costos": False,
                    "debe_finalizar_conversacion": False,
                })

                decision["datos_detectados"].update({
                    "objetivo_pendiente_sugerido": (
                        "OBTENER_ZONA_PARA_COSTOS"
                    ),
                    "registrar_solicitud_costos_inicial": True,
                })

                return decision

            ya_recibio_valor = (
                "RECIBIO_PRESENTACION_VALOR"
                in hitos_comerciales
            )

            ya_recibio_metodo = (
                "RECIBIO_EXPLICACION_METODO"
                in hitos_comerciales
            )

            # Si todavía no recibió la propuesta de valor,
            # se presenta antes del precio.
            if not ya_recibio_valor:

                decision.update({
                    "accion": "PRESENTAR_PROPUESTA_VALOR",
                    "motivo": (
                        "Es la primera solicitud de costos "
                        "y la familia todavía no ha recibido "
                        "la propuesta general de valor."
                    ),
                    "requiere_admin": False,
                    "puede_compartir_costos": False,
                    "debe_finalizar_conversacion": False,
                })

                decision["datos_detectados"].update({
                    "registrar_solicitud_costos_inicial": True,
                    "etapa_secuencial": (
                        "PRESENTACION_VALOR"
                    ),
                })

                return decision

            # Si ya conoce el valor general pero todavía
            # no recibió la explicación del Método,
            # se explica una sola vez.
            if not ya_recibio_metodo:

                decision.update({
                    "accion": "EXPLICAR_METODO_FILADELFIA",
                    "motivo": (
                        "La familia ya recibió la propuesta "
                        "general de valor, pero todavía no "
                        "la explicación del Método Filadelfia."
                    ),
                    "requiere_admin": False,
                    "puede_compartir_costos": False,
                    "debe_finalizar_conversacion": False,
                })

                decision["datos_detectados"].update({
                    "registrar_solicitud_costos_inicial": True,
                    "etapa_secuencial": (
                        "EXPLICACION_METODO"
                    ),
                })

                return decision

            # Si la familia ya recibió valor Y Método,
            # no existe razón comercial para obligarla a
            # volver a pedir los costos.
            niveles_costos = (
                obtener_niveles_costos_solicitados(
                    analisis_seguro,
                    decision,
                )
            )

            if not niveles_costos:
                decision.update({
                    "accion": "PEDIR_NIVEL_COSTOS",
                    "motivo": (
                        "La familia ya recibió la información "
                        "estratégica y solicita costos, pero "
                        "falta identificar el nivel."
                    ),
                    "requiere_admin": False,
                    "puede_compartir_costos": False,
                    "debe_finalizar_conversacion": False,
                })

                decision["datos_detectados"].update({
                    "objetivo_pendiente_sugerido": (
                        "OBTENER_NIVEL_PARA_COSTOS"
                    ),
                    "registrar_solicitud_costos_inicial": True,
                })

                return decision

            decision.update({
                "accion": "RESPONDER_COSTOS",
                "motivo": (
                    "La familia ya recibió la propuesta de "
                    "valor y el Método Filadelfia. Su solicitud "
                    "de costos puede atenderse directamente."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": True,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "niveles_costos": niveles_costos,
                "nivel_costos": (
                    niveles_costos[0]
                    if len(niveles_costos) == 1
                    else ""
                ),
                "registrar_solicitud_costos_inicial": True,
            })

            return decision
            

        

        # ----------------------------------------------------
        # SEGUNDA SOLICITUD EXPLÍCITA:
        # ahora sí responder costos.
        # ----------------------------------------------------

        es_insistencia_costos = bool(
            solicitud_costos_explicita
            and solicito_costos_previamente
        )

        niveles_costos = (
            obtener_niveles_costos_solicitados(
                analisis_seguro,
                decision,
            )
        )

        if not zona_validada:
            decision.update({
                "accion": "PEDIR_ZONA",
                "motivo": (
                    "El prospecto insistió en costos, pero "
                    "todavía falta validar la zona."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "objetivo_pendiente_sugerido": (
                    "OBTENER_ZONA_PARA_COSTOS"
                ),
                "registrar_insistencia_costos": (
                    es_insistencia_costos
                ),
            })

            return decision

        if not niveles_costos:
            decision.update({
                "accion": "PEDIR_NIVEL_COSTOS",
                "motivo": (
                    "El prospecto insistió en costos y la zona "
                    "ya está validada, pero falta conocer el nivel."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "objetivo_pendiente_sugerido": (
                    "OBTENER_NIVEL_PARA_COSTOS"
                ),
                "registrar_insistencia_costos": (
                    es_insistencia_costos
                ),
            })

            return decision

        decision.update({
            "accion": "RESPONDER_COSTOS",
            "motivo": (
                "El prospecto volvió a solicitar costos. "
                "Corresponde atender la insistencia."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": True,
            "debe_finalizar_conversacion": False,
        })

        decision["datos_detectados"].update({
            "niveles_costos": niveles_costos,
            "nivel_costos": (
                niveles_costos[0]
                if len(niveles_costos) == 1
                else ""
            ),
        })

        return decision
        
    tiene_proceso_comercial_iniciado = bool(
        intencion_principal == "PEDIR_INFORMES"
        or "PEDIR_INFORMES"
        in intenciones_secundarias
        or "PIDIO_INFORMES"
        in hitos_comerciales
        or detectar_admisiones_evidentes_para_alcance(
            mensaje_usuario
        )
        or intencion_principal
        in {
            "RESPONDER_ZONA",
            "RESPONDER_REFERENCIA",
        }
        or zona_para_decision
        or analisis_seguro.get("nivel")
    )

    if (
        tiene_proceso_comercial_iniciado
        and not cita_ya_confirmada
    ):

        # ----------------------------------------------------
        # PASO 1: VALIDAR ZONA
        # ----------------------------------------------------

        zona_confirmada = bool(
            zona_validada
            or "ZONA_VALIDADA"
            in hitos_comerciales
        )

        if not zona_confirmada:
            decision.update({
                "accion": "PEDIR_ZONA",
                "motivo": (
                    "La familia confirmó que desea recibir "
                    "información del colegio y corresponde "
                    "validar primero su localidad."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "VALIDACION_ZONA"

            return decision

        # ----------------------------------------------------
        # PASO 2: REFERENCIA DEL COLEGIO
        # ----------------------------------------------------

        datos_detectados_analisis = (
            analisis_seguro.get(
                "datos_detectados",
                [],
            )
        )

        if not isinstance(
            datos_detectados_analisis,
            list,
        ):
            datos_detectados_analisis = []

        respondio_referencia_en_turno = (
            "referencia_colegio"
            in datos_detectados_analisis
        )

        referencia_confirmada = bool(
            referencia_previa
            or "RESPONDIO_REFERENCIA"
            in hitos_comerciales
            or respondio_referencia_en_turno
        )

        if (
            not referencia_confirmada
            and "SOLICITO_COSTOS_INICIAL"
            not in hitos_comerciales
        ):
            decision.update({
                "accion": "PEDIR_REFERENCIA",
                "motivo": (
                    "La localidad ya fue validada y ahora "
                    "corresponde conocer cómo supo del colegio "
                    "o qué referencias tiene."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "REFERENCIA_COLEGIO"

            return decision

        # ----------------------------------------------------
        # PASO 3: PROPUESTA GENERAL DE VALOR
        # ----------------------------------------------------

        if (
            "RECIBIO_PRESENTACION_VALOR"
            not in hitos_comerciales
        ):
            decision.update({
                "accion": "PRESENTAR_PROPUESTA_VALOR",
                "motivo": (
                    "La zona y la referencia ya fueron "
                    "confirmadas. Corresponde presentar la "
                    "propuesta general de valor."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "PRESENTACION_VALOR"

            return decision

        # ----------------------------------------------------
        # PASO 4: EXPLICAR MÉTODO FILADELFIA
        # ----------------------------------------------------

        if (
            "RECIBIO_EXPLICACION_METODO"
            not in hitos_comerciales
        ):
            decision.update({
                "accion": "EXPLICAR_METODO_FILADELFIA",
                "motivo": (
                    "La familia ya recibió la presentación "
                    "general. Corresponde explicar el "
                    "Método Filadelfia."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "EXPLICACION_METODO"

            return decision

        # ----------------------------------------------------
        # PASO 5: IDENTIFICAR ÁREA DE INTERÉS
        # ----------------------------------------------------

        area_interes_confirmada = bool(
            areas_interes_previas
            or "EXPRESO_AREA_INTERES"
            in hitos_comerciales
        )

        if not area_interes_confirmada:
            decision.update({
                "accion": "PREGUNTAR_AREA_INTERES",
                "motivo": (
                    "La familia ya conoce la propuesta y el "
                    "Método Filadelfia. Corresponde identificar "
                    "qué área desea fortalecer."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "IDENTIFICACION_INTERES"

            return decision

        # ----------------------------------------------------
        # PASO 6: RESPUESTA PERSONALIZADA
        # ----------------------------------------------------

        if (
            "RECIBIO_RESPUESTA_PERSONALIZADA"
            not in hitos_comerciales
        ):
            decision.update({
                "accion": "PROFUNDIZAR_AREA_INTERES",
                "motivo": (
                    "La familia expresó el área que desea "
                    "fortalecer. Corresponde responder con "
                    "información institucional relacionada."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "PROFUNDIZACION_INTERES"

            return decision

        # ----------------------------------------------------
        # INTENCIÓN EXPLÍCITA DE COSTOS TODAVÍA NO SATISFECHA
        # ----------------------------------------------------
        #
        # Si la familia pidió costos anteriormente y ya recibió
        # la contextualización comercial prevista, no debemos
        # avanzar a la visita dejando esa pregunta olvidada.
        # ----------------------------------------------------

        costos_pendientes = bool(
            "SOLICITO_COSTOS_INICIAL"
            in hitos_comerciales
            and "RECIBIO_COSTOS"
            not in hitos_comerciales
        )

        if costos_pendientes:

            niveles_costos_pendientes = (
                obtener_niveles_costos_solicitados(
                    analisis_seguro,
                    decision,
                )
            )

            # Recuperar también niveles ya conocidos
            # del contexto comercial persistido.
            if not niveles_costos_pendientes:

                alumnos_contexto = (
                    contexto_secuencial.get(
                        "alumnos",
                        [],
                    )
                    if isinstance(
                        contexto_secuencial,
                        dict,
                    )
                    else []
                )

                if isinstance(
                    alumnos_contexto,
                    list,
                ):
                    for alumno_contexto in (
                        alumnos_contexto
                    ):

                        if not isinstance(
                            alumno_contexto,
                            dict,
                        ):
                            continue

                        nivel_contexto = str(
                            alumno_contexto.get(
                                "nivel",
                                "",
                            )
                            or ""
                        ).strip()

                        if (
                            nivel_contexto
                            in {
                                "Kínder",
                                "Primaria",
                                "Secundaria",
                            }
                            and nivel_contexto
                            not in niveles_costos_pendientes
                        ):
                            niveles_costos_pendientes.append(
                                nivel_contexto
                            )

            if not niveles_costos_pendientes:

                decision.update({
                    "accion": "PEDIR_NIVEL_COSTOS",
                    "motivo": (
                        "La familia solicitó costos anteriormente "
                        "y esa intención sigue pendiente, pero falta "
                        "identificar el nivel."
                    ),
                    "requiere_admin": False,
                    "puede_compartir_costos": False,
                    "debe_finalizar_conversacion": False,
                })

                decision[
                    "datos_detectados"
                ].update({
                    "objetivo_pendiente_sugerido": (
                        "OBTENER_NIVEL_PARA_COSTOS"
                    ),
                    "intencion_costos_pendiente": True,
                })

                return decision

            decision.update({
                "accion": "RESPONDER_COSTOS",
                "motivo": (
                    "La familia solicitó costos previamente, "
                    "ya recibió la contextualización comercial "
                    "y corresponde atender esa intención antes "
                    "de continuar a la invitación de visita."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": True,
                "debe_finalizar_conversacion": False,
            })

            decision[
                "datos_detectados"
            ].update({
                "niveles_costos": (
                    niveles_costos_pendientes
                ),
                "nivel_costos": (
                    niveles_costos_pendientes[0]
                    if len(
                        niveles_costos_pendientes
                    ) == 1
                    else ""
                ),
                "intencion_costos_pendiente": False,
            })

            return decision

        # ----------------------------------------------------
        # PASO 7: INVITACIÓN A VISITA
        # ----------------------------------------------------

        if (
            "ACEPTO_VISITA"
            not in hitos_comerciales
            and "CITA_SOLICITADA"
            not in hitos_comerciales
            and "CITA_CONFIRMADA"
            not in hitos_comerciales
        ):
            decision.update({
                "accion": "INVITAR_CITA",
                "motivo": (
                    "La familia ya recibió la información "
                    "estratégica y corresponde invitarla "
                    "a conocer el colegio presencialmente."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"][
                "etapa_secuencial"
            ] = "INVITACION_VISITA"

            return decision

    # ========================================================
    # 9. COSTOS: SIEMPRE PROTEGIDOS POR ZONA
    # ========================================================
    
    if (
        analisis_seguro.get("pide_costos")
        or analisis_seguro.get("intencion_principal")
        == "PEDIR_COSTOS"
        or "PEDIR_COSTOS"
        in analisis_seguro.get(
            "intenciones_secundarias",
            [],
        )
    ):
        if not zona_validada:
            decision.update({
                "accion": "PEDIR_ZONA",
                "motivo": (
                    "El prospecto pidió costos, pero todavía "
                    "no se ha validado que corresponda al "
                    "Campus Santa Cruz Atizapán."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
            })

            decision["datos_detectados"].update({
                "objetivo_pendiente_sugerido": (
                    "OBTENER_ZONA_PARA_COSTOS"
                ),
                "intencion_costos_pendiente": True,
            })

            return decision

        niveles_costos = (
            obtener_niveles_costos_solicitados(
                analisis_seguro,
                decision,
            )
        )

        if not niveles_costos:
            decision.update({
                "accion": "PEDIR_NIVEL_COSTOS",
                "motivo": (
                    "La zona está validada y el prospecto "
                    "solicitó colegiaturas, pero todavía "
                    "no indicó el nivel escolar."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
                "debe_finalizar_conversacion": False,
            })

            decision["datos_detectados"].update({
                "objetivo_pendiente_sugerido": (
                    "OBTENER_NIVEL_PARA_COSTOS"
                ),
                "intencion_costos_pendiente": True,
            })

            return decision

        decision.update({
            "accion": "RESPONDER_COSTOS",
            "motivo": (
                "El prospecto pidió costos, la zona ya fue "
                "validada y se conocen los niveles solicitados."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": True,
        })

        decision["datos_detectados"].update({
            "niveles_costos": niveles_costos,
            "nivel_costos": (
                niveles_costos[0]
                if len(niveles_costos) == 1
                else ""
            ),
        })

        return decision



    # ========================================================
    # 10. DATOS POSTERIORES A LA CONFIRMACIÓN DE CITA
    # ========================================================

    if (
        analisis_seguro.get("intencion_principal")
        == "DAR_DATOS_CITA"
    ):
        nombre_tutor = analisis_seguro.get(
            "nombre_tutor",
            "",
        )

        nombre_alumno = analisis_seguro.get(
            "nombre_alumno",
            "",
        )

        nivel = analisis_seguro.get("nivel", "")
        grado = analisis_seguro.get("grado", "")

        if (
            nombre_tutor
            and nombre_alumno
            and (nivel or grado)
        ):
            decision.update({
                "accion": "REGISTRAR_DATOS_CITA",
                "motivo": (
                    "El prospecto proporcionó los datos "
                    "requeridos para completar el registro."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
            })

            return decision

        decision.update({
            "accion": "PEDIR_DATOS_CITA",
            "motivo": (
                "Faltan uno o más datos para completar "
                "el registro de la cita."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
        })

        return decision

    # ========================================================
    # 11. RESPUESTA DE ZONA VÁLIDA
    # ========================================================

    if (
        analisis_seguro.get("intencion_principal")
        == "RESPONDER_ZONA"
    ):
        if zona_valida_en_mensaje:
            decision.update({
                "accion": "CONTINUAR_INFORMES",
                "motivo": (
                    "La zona proporcionada corresponde al "
                    "Campus Santa Cruz Atizapán."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": True,
            })

            return decision

        decision.update({
            "accion": "PEDIR_ZONA",
            "motivo": (
                "La ubicación proporcionada no permite "
                "validar claramente la zona."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
        })

        return decision

    # ========================================================
    # 12. TEMA EDUCATIVO
    # ========================================================

    if (
        analisis_seguro.get("intencion_principal")
        == "PREGUNTAR_TEMA_EDUCATIVO"
        or analisis_seguro.get("tema_interes")
    ):
        decision.update({
            "accion": "RESPONDER_TEMA",
            "motivo": (
                "El prospecto preguntó sobre un tema "
                "educativo o institucional."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": zona_validada,
        })

        return decision

    # ========================================================
    # 13. INFORMES GENERALES
    # ========================================================

    intencion_principal = str(
        analisis_seguro.get(
            "intencion_principal",
            "",
        )
        or ""
    ).strip().upper()

    intenciones_secundarias = (
        analisis_seguro.get(
            "intenciones_secundarias",
            [],
        )
    )

    if not isinstance(
        intenciones_secundarias,
        list,
    ):
        intenciones_secundarias = []

    mensaje_normalizado = (
        normalizar_texto_geografico(
            mensaje_usuario
        )
    )

    nivel_detectado = str(
        analisis_seguro.get(
            "nivel",
            "",
        )
        or ""
    ).strip()

    grado_detectado = str(
        analisis_seguro.get(
            "grado",
            "",
        )
        or analisis_seguro.get(
            "grado_solicitado",
            "",
        )
        or ""
    ).strip()

    expresiones_solicitud_informes = [
        "quiero informes",
        "quisiera informes",
        "solicito informes",
        "necesito informes",
        "me puede dar informes",
        "me pueden dar informes",
        "quiero informacion",
        "quisiera informacion",
        "necesito informacion",
        "solicito informacion",
        "conocer mas informacion",
        "saber mas",
        "me interesa el colegio",
        "me interesa primaria",
        "me interesa secundaria",
        "me interesa kinder",
        "informacion sobre primaria",
        "informacion sobre secundaria",
        "informacion sobre kinder",
        "informacion de primaria",
        "informacion de secundaria",
        "informacion de kinder",
    ]

    solicitud_informes_por_texto = any(
        expresion in mensaje_normalizado
        for expresion
        in expresiones_solicitud_informes
    )

    solicitud_informes_por_intencion = (
        intencion_principal
        == "PEDIR_INFORMES"
        or "PEDIR_INFORMES"
        in intenciones_secundarias
    )

    solicitud_informes_por_contexto = (
        intencion_principal == "OTRO"
        and bool(
            nivel_detectado
            or grado_detectado
        )
        and any(
            palabra in mensaje_normalizado
            for palabra in [
                "informacion",
                "informes",
                "conocer",
                "interesa",
                "saber",
            ]
        )
    )

    solicita_informes_generales = (
        solicitud_informes_por_intencion
        or solicitud_informes_por_texto
        or solicitud_informes_por_contexto
    )

    if solicita_informes_generales:
        if not zona_validada:
            decision.update({
                "accion": "PEDIR_ZONA",
                "motivo": (
                    "El prospecto solicita informes y aún "
                    "no se ha validado su ubicación."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": False,
            })

            return decision

        decision.update({
            "accion": "CONTINUAR_INFORMES",
            "motivo": (
                "El prospecto solicita informes y la zona "
                "ya está validada."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": True,
        })

        return decision

    return decision


def construir_plan_respuesta_estructurada(
    analisis: Dict[str, Any],
    decision: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Traduce una decisión determinista en instrucciones de redacción.

    Esta función:
    - no llama a Gemini;
    - no redacta el mensaje final;
    - no consulta servicios externos;
    - no modifica la base de datos;
    - no envía mensajes;
    - no crea tareas administrativas.
    """
    analisis_seguro = (
        analisis
        if isinstance(analisis, dict)
        else {}
    )

    decision_segura = (
        decision
        if isinstance(decision, dict)
        else {}
    )

    accion = str(
        decision_segura.get(
            "accion",
            "CONTINUAR_CONVERSACION",
        )
        or "CONTINUAR_CONVERSACION"
    ).strip().upper()

    datos_detectados = decision_segura.get(
        "datos_detectados",
        {},
    )

    if not isinstance(datos_detectados, dict):
        datos_detectados = {}

    zona_mencionada = str(
        analisis_seguro.get(
            "zona_mencionada",
            "",
        )
        or datos_detectados.get(
            "zona_mencionada",
            "",
        )
        or ""
    ).strip()

    nivel = str(
        analisis_seguro.get(
            "nivel",
            "",
        )
        or datos_detectados.get(
            "nivel",
            "",
        )
        or ""
    ).strip()

    plan = {
        "accion": accion,
        "objetivo": (
            "Continuar la conversación de manera natural."
        ),
        "debe_incluir": [],
        "no_debe_incluir": [
            "Nombres internos de acciones o clasificaciones.",
            "Detalles técnicos de Google Places o Google Routes.",
            "Distancias, coordenadas, límites internos o cálculos.",
            "Explicaciones sobre reglas internas del sistema.",
            (
                "Repetir innecesariamente el nombre completo "
                "'Colegio Valle de Filadelfia Campus Santa Cruz "
                "Atizapán' o 'Campus Santa Cruz Atizapán'. "
                "Si el campus ya quedó identificado en la conversación, "
                "usar de forma natural expresiones como el colegio, "
                "nuestro colegio, el campus, nuestra propuesta o "
                "durante la visita. Repetir el nombre completo solamente "
                "cuando sea necesario aclarar qué campus se atiende, "
                "ante una duda de sede o al proporcionar información "
                "logística donde la ubicación sea relevante."
            ),
        ],
        "tono": (
            "Cordial, natural, breve y orientado a admisiones."
        ),
        "requiere_admin": bool(
            decision_segura.get(
                "requiere_admin",
                False,
            )
        ),
        "puede_compartir_costos": bool(
            decision_segura.get(
                "puede_compartir_costos",
                False,
            )
        ),
        "debe_finalizar_conversacion": bool(
            decision_segura.get(
                "debe_finalizar_conversacion",
                False,
            )
        ),
        "zona_mencionada": zona_mencionada,
        "nivel": nivel,
    }

    # ========================================================
    # BLOQUEO GLOBAL DE COSTOS
    # ========================================================

    if not plan["puede_compartir_costos"]:
        plan["no_debe_incluir"].extend([
            (
                "Cualquier cantidad, precio, costo, colegiatura, "
                "inscripción, mensualidad o importe."
            ),
            (
                "Menciones a becas, descuentos, promociones, "
                "planes de pago o formas de pago."
            ),
            (
                "Prometer que posteriormente se compartirán "
                "costos o información económica."
            ),
            (
                "Usar expresiones como información de costos, "
                "costos correspondientes, costos detallados, "
                "proceso de admisión y costos."
            ),
            (
                "Introducir el tema económico cuando el "
                "prospecto no lo ha solicitado."
            ),
        ])

    if accion == "RESPONDER_SALUDO":
        plan.update({
            "objetivo": (
                "Responder únicamente el saludo de manera "
                "natural y contextual."
            ),
            "debe_incluir": [
                "Un saludo acorde con el mensaje recibido.",
            ],
        })

        return plan

    if accion == "PEDIR_REFERENCIA":
        plan.update({
            "objetivo": (
                "Conocer si la familia ya tiene alguna "
                "referencia del Colegio Valle de Filadelfia "
                "Campus Santa Cruz."
            ),
            "debe_incluir": [
                (
                    "Una sola pregunta abierta para saber "
                    "si ya conoce o tiene alguna referencia "
                    "del colegio."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Pedir nombre, edad, grado o localidad.",
                    "Presentar todavía el modelo educativo.",
                    "Invitar todavía a una visita.",
                    "Formular más de una pregunta.",
                ]
            ),
        })

        return plan

    if accion == "PRESENTAR_PROPUESTA_VALOR":
        plan.update({
            "objetivo": (
                "Presentar la propuesta general de valor del "
                "colegio y cerrar obligatoriamente preguntando "
                "si conoce el Método Filadelfia."
            ),
            "debe_incluir": [
                (
                    "Iniciar con una transición natural equivalente "
                    "a: Permítame contarle brevemente por qué muchas "
                    "familias eligen Valle de Filadelfia Campus "
                    "Santa Cruz."
                ),
                (
                    "Explicar el Método Filadelfia como una enseñanza "
                    "activa y personalizada que potencia los talentos "
                    "de cada alumno y cuida su desarrollo físico, "
                    "emocional e intelectual."
                ),
                (
                    "Mencionar tecnología de vanguardia: iPads, "
                    "salones inteligentes, aplicaciones, videos, "
                    "realidad virtual y realidad aumentada."
                ),
                (
                    "Mencionar clases de inglés y francés desde "
                    "temprana edad, además de ciencias y finanzas "
                    "impartidas en inglés."
                ),
                (
                    "Mencionar actividades como judo, robótica, "
                    "violín Suzuki, aritmética mental ALOHA y LEGO."
                ),
                (
                    "Explicar que todo se desarrolla en un ambiente "
                    "seguro, colaborativo y lleno de entusiasmo."
                ),
                (
                    "Finalizar exactamente con la pregunta: "
                    "¿Ha escuchado hablar del Método Filadelfia?"
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Pedir nombre, edad, grado o fecha de nacimiento.",
                    "Preguntar qué busca en un colegio.",
                    "Preguntar todavía qué área desea fortalecer.",
                    "Hacer preguntas sobre experiencia escolar previa.",
                    "Profundizar únicamente en un tema mencionado antes.",
                    "Invitar todavía a una visita.",
                    "Ofrecer folletos, enlaces o llamadas.",
                    "Sustituir la pregunta final obligatoria.",
                    "Formular más de una pregunta.",
                    (
                        "Repetir innecesariamente el nombre completo "
                        "del colegio. Después de la primera mención, "
                        "usar el colegio, nuestro colegio, el campus "
                        "o nuestra propuesta."
                    ),
                ]
            ),
        })

        return plan

    if accion == "EXPLICAR_METODO_FILADELFIA":
        plan.update({
            "objetivo": (
                "Explicar obligatoriamente el Método Filadelfia, "
                "sin importar si el prospecto dijo que lo conoce "
                "o que no lo conoce."
            ),
            "debe_incluir": [
                (
                    "Explicar que el Método Filadelfia se centra "
                    "en cada niño o niña y adapta contenidos y "
                    "retos a sus necesidades."
                ),
                (
                    "Explicar que combina conocimientos, habilidades "
                    "y actitudes útiles para la vida diaria."
                ),
                (
                    "Explicar sus tres pilares: desarrollo lógico "
                    "matemático, estimulación artístico musical "
                    "y fortalecimiento físico de ligamentos "
                    "y articulaciones."
                ),
                (
                    "Mencionar desarrollo emocional, emprendimiento, "
                    "finanzas y salud física."
                ),
                (
                    "Mencionar apoyo neuromotor y el programa de "
                    "violín basado en el método Suzuki."
                ),
                (
                    "Explicar que el violín Suzuki favorece conexiones "
                    "cerebrales relacionadas con memoria, aprendizaje "
                    "y pensamiento ágil."
                ),
                (
                    "Transmitir que aprender se convierte en una "
                    "experiencia práctica, positiva y significativa."
                ),
                (
                    "Finalizar exactamente con la pregunta: "
                    "¿Qué área le interesa más fortalecer "
                    "en su hijo(a)?"
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Pedir nombre, edad, grado o fecha de nacimiento.",
                    "Preguntar si desea que se le explique el método.",
                    "Omitir la explicación porque el prospecto diga que sí lo conoce.",
                    "Hacer preguntas escolares adicionales.",
                    "Invitar todavía a una visita.",
                    "Ofrecer llamadas, folletos o enlaces.",
                    "Sustituir la pregunta final obligatoria.",
                    "Formular más de una pregunta.",
                    (
                        "Repetir el nombre completo del colegio. "
                        "Usar el colegio, nuestro colegio, "
                        "nuestro método o nuestra propuesta."
                    ),
                ]
            ),
        })

        return plan
        
    if accion == "PREGUNTAR_AREA_INTERES":
        plan.update({
            "objetivo": (
                "Identificar qué aspecto del desarrollo o "
                "aprendizaje del alumno es más importante "
                "para la familia."
            ),
            "debe_incluir": [
                (
                    "Una única pregunta abierta para conocer "
                    "qué le interesa fortalecer o qué busca "
                    "la familia en un colegio."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Enumerar demasiadas opciones.",
                    "Invitar todavía a una visita.",
                    "Pedir datos administrativos.",
                    "Formular más de una pregunta.",
                ]
            ),
        })

        return plan

    if accion == "PROFUNDIZAR_AREA_INTERES":
        plan.update({
            "objetivo": (
                "Responder de forma personalizada al área "
                "educativa que la familia indicó como prioritaria."
            ),
            "debe_incluir": [
                (
                    "Información institucional directamente "
                    "relacionada con el interés expresado."
                ),
                (
                    "Una pregunta abierta breve que permita "
                    "confirmar si esa propuesta responde a "
                    "lo que la familia busca."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Cambiar a un tema no solicitado.",
                    "Ofrecer llamadas, enlaces o folletos.",
                    "Pedir varios datos.",
                    "Formular más de una pregunta.",
                ]
            ),
        })

        return plan

    if accion == "INVITAR_CITA":
        plan.update({
            "objetivo": (
                "Invitar a la familia a conocer presencialmente "
                "el Campus Santa Cruz Atizapán."
            ),
            "debe_incluir": [
                (
                    "Una invitación natural y no agresiva "
                    "para realizar una visita presencial."
                ),
                (
                    "Una sola pregunta abierta para saber "
                    "si le gustaría conocer el campus."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Proponer llamadas telefónicas o videollamadas.",
                    "Ofrecer sesiones informativas remotas.",
                    "Inventar folletos o enlaces.",
                    "Pedir fecha y hora antes de que la familia acepte.",
                    "Formular más de una pregunta.",
                ]
            ),
        })

        return plan

    if accion == "PEDIR_ZONA":
        plan.update({
            "objetivo": (
                "Solicitar la localidad o municipio desde "
                "donde se comunica la familia."
            ),
            "debe_incluir": [
                (
                    "Una pregunta sencilla para conocer la "
                    "localidad o municipio de la familia."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Costos o colegiaturas antes de validar la zona.",
                    "Solicitud de dirección exacta, calle o ubicación GPS.",
                ]
            ),
        })

        return plan


    if accion == "RESPONDER_UBICACION":
        plan.update({
            "objetivo": (
                "Compartir directamente la ubicación "
                "institucional autorizada del campus."
            ),
            "debe_incluir": [
                (
                    "Únicamente una introducción breve "
                    "y el enlace institucional autorizado."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Inventar o reconstruir un enlace.",
                    "Inventar coordenadas.",
                    "Inventar un Place ID.",
                    "Solicitar información adicional.",
                    "Agregar una pregunta comercial.",
                    "Explicar Google Maps.",
                ]
            ),
        })

        return plan

    if accion == "PEDIR_NIVEL_COSTOS":
        plan.update({
            "objetivo": (
                "Solicitar únicamente el nivel escolar necesario "
                "para responder una solicitud de colegiaturas "
                "que ya está pendiente."
            ),
            "debe_incluir": [
                (
                    "Una sola pregunta breve para saber si la "
                    "información corresponde a Kínder, Primaria "
                    "o Secundaria."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Volver a pedir la zona.",
                    "Preguntar cómo conoció el colegio.",
                    "Presentar todavía la propuesta educativa.",
                    "Invitar a una visita.",
                    "Compartir costos antes de conocer el nivel.",
                    "Hacer más de una pregunta.",
                ]
            ),
        })

        return plan

    if accion == "RESPONDER_COSTOS":
        plan.update({
            "objetivo": (
                "Compartir la información de costos que "
                "corresponda al nivel solicitado."
            ),
            "debe_incluir": [
                (
                    "Los costos institucionales correspondientes "
                    "al nivel, utilizando únicamente la información "
                    "oficial disponible."
                ),
                (
                    "Una continuación natural de la conversación "
                    "después de proporcionar los costos."
                ),
            ],
        })

        return plan

    if accion == "CONSULTAR_ADMIN":
        validacion_google = datos_detectados.get(
            "validacion_geografica_google",
            {},
        )

        if not isinstance(validacion_google, dict):
            validacion_google = {}

        es_revision_geografica = (
            validacion_google.get(
                "clasificacion"
            )
            == "ZONA_REQUIERE_REVISION"
        )

        if es_revision_geografica:
            plan.update({
                "objetivo": (
                    "Informar que se revisará internamente la "
                    "posibilidad de atender a la familia desde "
                    "la zona indicada."
                ),
                "debe_incluir": [
                    (
                        "Una confirmación amable de que se "
                        "revisará la zona con el equipo del colegio."
                    ),
                    (
                        "Indicar que se dará respuesta en cuanto "
                        "se tenga la confirmación."
                    ),
                ],
                "no_debe_incluir": (
                    plan["no_debe_incluir"]
                    + [
                        "Rechazar automáticamente a la familia.",
                        "Compartir costos antes de la revisión.",
                        "Decir que la familia vive demasiado lejos.",
                    ]
                ),
            })

            return plan

        motivo_decision = str(
            decision_segura.get(
                "motivo",
                "",
            )
            or ""
        ).strip()

        motivo_normalizado = (
            normalizar_texto_geografico(
                motivo_decision
            )
        )

        es_seguimiento_cita_pendiente = (
            "visita" in motivo_normalizado
            and "confirmacion" in motivo_normalizado
            and "pendiente" in motivo_normalizado
        )

        if es_seguimiento_cita_pendiente:
            plan.update({
                "objetivo": (
                    "Responder al seguimiento de una familia que "
                    "continúa esperando la confirmación administrativa "
                    "de su visita."
                ),
                "debe_incluir": [
                    (
                        "Reconocer que la familia ya estaba esperando "
                        "la confirmación de su visita."
                    ),
                    (
                        "Ofrecer una disculpa breve y sincera por "
                        "la demora."
                    ),
                    (
                        "Indicar claramente que la visita continúa "
                        "pendiente de confirmación."
                    ),
                    (
                        "Explicar que se consultará nuevamente con "
                        "administración."
                    ),
                    (
                        "Indicar que se responderá en cuanto se obtenga "
                        "la confirmación."
                    ),
                ],
                "no_debe_incluir": (
                    plan["no_debe_incluir"]
                    + [
                        (
                            "Pedir que el prospecto vuelva a explicar "
                            "qué información necesita."
                        ),
                        (
                            "Volver a preguntar zona, nivel educativo "
                            "o área de interés."
                        ),
                        (
                            "Volver a solicitar el día y la hora "
                            "de la visita."
                        ),
                        (
                            "Hablar de costos, colegiaturas, inscripción "
                            "o formas de pago."
                        ),
                        (
                            "Presentar la visita como una solicitud nueva."
                        ),
                        (
                            "Afirmar que la visita ya está confirmada."
                        ),
                    ]
                ),
                "tono": (
                    "Cordial, empático, breve y responsable. "
                    "Debe reconocer la demora sin justificarla."
                ),
            })

            return plan

        plan.update({
            "objetivo": (
                "Informar que la solicitud debe ser confirmada "
                "por una persona administradora."
            ),
            "debe_incluir": [
                (
                    "Una confirmación amable de que se consultará "
                    "la disponibilidad o solicitud con administración."
                ),
                (
                    "Indicar que se responderá al obtener la confirmación."
                ),
            ],
        })

        return plan
        
    if accion == "RECHAZAR_CAMPUS":
        plan.update({
            "objetivo": (
                "Explicar amablemente que este canal atiende "
                "únicamente al Campus Santa Cruz Atizapán."
            ),
            "debe_incluir": [
                (
                    "Aclarar que la atención de este chat corresponde "
                    "exclusivamente al Campus Santa Cruz Atizapán."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Insistir en proporcionar información de Santa Cruz.",
                    "Compartir costos del Campus Santa Cruz.",
                    "Afirmar información de otros campus que no esté confirmada.",
                ]
            ),
        })

        return plan

    if accion == "CITA_DIA_NO_LABORAL":
        plan.update({
            "objetivo": (
                "Explicar que las visitas se realizan de lunes "
                "a viernes y solicitar otra fecha."
            ),
            "debe_incluir": [
                "Que las visitas se reciben de lunes a viernes.",
                "Una pregunta para elegir otra fecha.",
            ],
        })

        return plan

    if accion == "CITA_FUERA_HORARIO":
        plan.update({
            "objetivo": (
                "Explicar que el horario solicitado supera "
                "el límite máximo disponible para visitas "
                "y solicitar otro horario."
            ),
            "debe_incluir": [
                (
                    "Indicar amablemente que el horario máximo "
                    "en el que podemos recibir visitas es "
                    "a las 4:00 p. m."
                ),
                (
                    "Explicar que preferentemente recomendamos "
                    "un horario entre 8:00 a. m. y 1:00 p. m."
                ),
                (
                    "Aclarar expresamente que también pueden "
                    "considerarse horarios posteriores a la "
                    "1:00 p. m. y hasta las 4:00 p. m."
                ),
                (
                    "Cerrar con una sola pregunta para que "
                    "la familia proponga otro horario."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    (
                        "Presentar las 4:00 p. m. como si fuera "
                        "el único horario disponible por la tarde."
                    ),
                    (
                        "Dar a entender que entre la 1:00 p. m. "
                        "y las 4:00 p. m. no se reciben visitas."
                    ),
                    (
                        "Dar únicamente como alternativas "
                        "8:00 a. m. a 1:00 p. m. o exactamente "
                        "las 4:00 p. m."
                    ),
                ]
            ),
        })

        return plan

    if accion == "PEDIR_FECHA_CITA":
        plan.update({
            "objetivo": (
                "Solicitar en un solo mensaje el día y la hora "
                "en que la familia desea visitar el colegio."
            ),
            "debe_incluir": [
                (
                    "Indicar que las visitas se reciben de lunes "
                    "a viernes de 8:00 a. m. a 1:00 p. m."
                ),
                (
                    "Solicitar en una sola pregunta tanto el día "
                    "como la hora que le funcionan mejor."
                ),
                (
                    "Si por cuestiones laborales requiere un horario "
                    "posterior, indicar que puede evaluarse una "
                    "alternativa hasta máximo las 4:00 p. m."
                ),
            ],
        })

        return plan

    if accion == "PEDIR_HORA_CITA":
        plan.update({
            "objetivo": (
                "Solicitar el horario en que la familia desea "
                "realizar la visita, procurando orientar las citas "
                "preferentemente al horario de 8:00 a. m. a 1:00 p. m."
            ),
            "debe_incluir": [
                (
                    "Indicar de forma natural que preferentemente "
                    "recibimos las visitas entre 8:00 a. m. y "
                    "1:00 p. m., ya que es el horario más práctico "
                    "para brindarles una mejor atención."
                ),
                (
                    "Aclarar brevemente que, si lo requieren, "
                    "también podemos recibirles posteriormente, "
                    "hasta máximo las 4:00 p. m."
                ),
                (
                    "Cerrar con una sola pregunta para conocer "
                    "qué horario les funciona mejor."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    (
                        "Presentar las 4:00 p. m. como si fuera "
                        "el único horario disponible por la tarde."
                    ),
                    (
                        "Dar a entender que sólo existen dos bloques "
                        "rígidos: mañana de 8:00 a 1:00 y exactamente "
                        "4:00 p. m."
                    ),
                ]
            ),
        })

        return plan
        

    if accion == "CONFIRMAR_FECHA_CITA":

        fecha_iso_confirmar = str(
            datos_detectados.get(
                "fecha_cita_iso_confirmar",
                "",
            )
            or ""
        ).strip()

        hora_confirmar = str(
            datos_detectados.get(
                "hora_cita_confirmar",
                "",
            )
            or ""
        ).strip()

        hora_mostrable = (
            formatear_hora_cita_12h(
                hora_confirmar
            )
        )

        fecha_mostrable = (
            formatear_fecha_cita_calendario(
                fecha_iso_confirmar
            )
        )

        plan.update({
            "objetivo": (
                "Confirmar con la familia la fecha calendario "
                "y hora exactas interpretadas antes de consultar "
                "disponibilidad con administración."
            ),
            "debe_incluir": [
                (
                    f"La fecha calendario interpretada: "
                    f"{fecha_mostrable or fecha_iso_confirmar}."
                ),
                (
                    f"El horario interpretado: "
                    f"{hora_confirmar}."
                ),
                (
                    "Una única pregunta de confirmación equivalente "
                    "a: ¿correcto?"
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Decir todavía que la cita está confirmada.",
                    "Decir que administración ya fue consultada.",
                    "Prometer disponibilidad.",
                    "Pedir nuevamente día y hora.",
                ]
            ),
        })

        return plan

    if accion == "PEDIR_FECHA_NACIMIENTO":
        plan.update({
            "objetivo": (
                "Solicitar la fecha de nacimiento completa del alumno."
            ),
            "debe_incluir": [
                (
                    "Una pregunta para conocer día, mes y año "
                    "de nacimiento."
                ),
            ],
        })

        return plan

    if accion == "ORIENTAR_PRE_KINDER":
        plan.update({
            "objetivo": (
                "Explicar con sensibilidad que, por la edad del "
                "alumno, todavía no corresponde a Kínder."
            ),
            "debe_incluir": [
                (
                    "Una explicación amable basada en la edad "
                    "al inicio del ciclo escolar."
                ),
            ],
        })

        return plan

    if accion == "SEGUIMIENTO":
        plan.update({
            "objetivo": (
                "Cerrar temporalmente la conversación de forma amable."
            ),
            "debe_incluir": [
                (
                    "Confirmar que la familia puede retomar "
                    "la conversación posteriormente."
                ),
            ],
        })

        return plan

    if accion == "FALLBACK_CONVERSACIONAL":
        plan.update({
            "objetivo": (
                "Responder de forma segura y pedir una aclaración breve."
            ),
            "debe_incluir": [
                (
                    "Una pregunta sencilla para entender qué "
                    "información necesita la familia."
                ),
            ],
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    "Inventar datos o asumir la intención del prospecto.",
                ]
            ),
        })

        return plan

    if accion == "RESPONDER_TEMA":

        objetivo_retorno = str(
            datos_detectados.get(
                "objetivo_retorno",
                "",
            )
            or ""
        ).strip().upper()

        debe_incluir_tema = [
            (
                "Responder primero y directamente la "
                "pregunta o comentario actual del prospecto."
            ),
        ]

        if (
            objetivo_retorno
            == "OBTENER_DECISION_VISITA"
        ):
            debe_incluir_tema.append(
                (
                    "Después de responder la duda, retomar "
                    "de forma breve y natural la posibilidad "
                    "de visitar el colegio, sin repetir "
                    "información comercial anterior."
                )
            )

        plan.update({
            "objetivo": (
                "Resolver la consulta actual sin reiniciar "
                "ni hacer retroceder el embudo comercial."
            ),
            "debe_incluir": (
                debe_incluir_tema
            ),
            "no_debe_incluir": (
                plan["no_debe_incluir"]
                + [
                    (
                        "Volver a pedir información que ya "
                        "fue proporcionada anteriormente."
                    ),
                    (
                        "Volver a explicar etapas del embudo "
                        "que la familia ya superó."
                    ),
                    (
                        "Reiniciar la presentación general "
                        "del colegio."
                    ),
                    (
                        "Volver al Método Filadelfia, "
                        "referencia o área de interés sólo "
                        "para reconstruir artificialmente "
                        "el embudo."
                    ),
                ]
            ),
        })

        return plan

    if accion in [
        "CONTINUAR_INFORMES",
        "INVITAR_CITA",
        "CONTINUAR_CONVERSACION",
    ]:
        
        plan.update({
            "objetivo": (
                "Continuar atendiendo la intención del prospecto "
                "con información institucional pertinente."
            ),
            "debe_incluir": [
                (
                    "Una respuesta directa a la solicitud actual "
                    "y una continuación natural de la conversación."
                ),
            ],
        })

        return plan

    plan.update({
        "objetivo": (
            "Continuar la conversación sin inventar información."
        ),
        "debe_incluir": [
            (
                "Una respuesta coherente con la información "
                "detectada y la decisión de negocio."
            ),
        ],
    })

    return plan

def generar_respuesta_final_estructurada(
    mensaje_usuario: str,
    analisis: Dict[str, Any],
    decision: Dict[str, Any],
    plan_respuesta: Dict[str, Any],
    history=None,
) -> Dict[str, Any]:
    """
    Redacta y valida una respuesta conversacional mediante Gemini.

    Si la primera respuesta no cumple las condiciones mínimas:
    1. realiza un segundo intento con instrucciones reforzadas;
    2. si vuelve a fallar, utiliza una respuesta segura
       determinada por Python.

    Esta función:
    - no envía mensajes;
    - no modifica la base de datos;
    - no cambia FLOW_STATE;
    - no crea tareas administrativas;
    - no altera la decisión tomada por Python.
    """
    resultado = {
        "generada": False,
        "respuesta": "",
        "modelo_usado": "",
        "intentos": 0,
        "uso_fallback_seguro": False,
        "errores_validacion": [],
        "error": "",
    }

    mensaje = str(
        mensaje_usuario or ""
    ).strip()

    analisis_seguro = (
        analisis
        if isinstance(analisis, dict)
        else {}
    )

    decision_segura = (
        decision
        if isinstance(decision, dict)
        else {}
    )

    plan_seguro = (
        plan_respuesta
        if isinstance(plan_respuesta, dict)
        else {}
    )

    accion = str(
        decision_segura.get(
            "accion",
            "CONTINUAR_CONVERSACION",
        )
        or "CONTINUAR_CONVERSACION"
    ).strip().upper()

    zona_mencionada = str(
        plan_seguro.get(
            "zona_mencionada",
            "",
        )
        or analisis_seguro.get(
            "zona_mencionada",
            "",
        )
        or ""
    ).strip()

    nivel = str(
        plan_seguro.get(
            "nivel",
            "",
        )
        or analisis_seguro.get(
            "nivel",
            "",
        )
        or ""
    ).strip()

    if not mensaje:
        resultado["error"] = "MENSAJE_USUARIO_VACIO"
        return resultado

    # ========================================================
    # SALUDO SIMPLE DETERMINISTA
    # ========================================================

    if accion == "RESPONDER_SALUDO":
        respuesta_saludo = (
            crear_respuesta_saludo_simple_estructurado(
                mensaje
            )
        )

        if not respuesta_saludo:
            respuesta_saludo = (
                "Hola. ¿En qué podemos ayudarle?"
            )

        resultado.update({
            "generada": True,
            "respuesta": respuesta_saludo,
            "modelo_usado": "",
            "intentos": 0,
            "uso_fallback_seguro": False,
            "errores_validacion": [],
            "tipo_respuesta": (
                "SALUDO_SIMPLE_DETERMINISTA"
            ),
            "error": "",
        })

        print(
            "👋 Saludo simple estructurado. "
            "Se omite Gemini: "
            f"{respuesta_saludo}"
        )

        return resultado

    # ========================================================
    # UBICACIÓN INSTITUCIONAL DETERMINISTA
    # ========================================================

    if accion == "RESPONDER_UBICACION":

        ubicacion = (
            obtener_ubicacion_institucional_campus()
        )

        if not ubicacion.get(
            "configurada",
            False,
        ):
            resultado["error"] = (
                "UBICACION_INSTITUCIONAL_NO_CONFIGURADA"
            )

            print(
                "❌ No se enviará una ubicación porque "
                "faltan CAMPUS_MAPS_NAME o "
                "CAMPUS_MAPS_ADDRESS."
            )

            return resultado

        url_maps = str(
            ubicacion.get(
                "url",
                "",
            )
            or ""
        ).strip()

        respuesta_ubicacion = (
            "Con gusto, le comparto nuestra ubicación:"
            "\n\n"
            f"{url_maps}"
        )

        resultado.update({
            "generada": True,
            "respuesta": respuesta_ubicacion,
            "modelo_usado": "",
            "intentos": 0,
            "uso_fallback_seguro": False,
            "errores_validacion": [],
            "tipo_respuesta": (
                "UBICACION_INSTITUCIONAL_DETERMINISTA"
            ),
            "error": "",
        })

        print(
            "📍 Ubicación institucional generada "
            "sin intervención de Gemini."
        )

        return resultado

    # ========================================================
    # RESPUESTA DETERMINISTA DE HORARIOS
    # ========================================================

    if accion == "RESPONDER_HORARIOS":

        datos_decision = (
            decision_segura.get(
                "datos_detectados",
                {},
            )
        )

        if not isinstance(
            datos_decision,
            dict,
        ):
            datos_decision = {}

        niveles_horarios = (
            datos_decision.get(
                "niveles_horarios",
                [],
            )
        )

        respuesta_horarios = (
            construir_respuesta_horarios(
                niveles_horarios
            )
        )

        if not respuesta_horarios:
            resultado.update({
                "generada": True,
                "respuesta": (
                    "Permítame verificar los horarios "
                    "vigentes antes de proporcionarle "
                    "información incorrecta."
                ),
                "modelo_usado": "",
                "intentos": 0,
                "uso_fallback_seguro": True,
                "errores_validacion": [
                    "HORARIOS_AUTORIZADOS_NO_DISPONIBLES"
                ],
                "tipo_respuesta": (
                    "HORARIOS_FALLBACK_SEGURO"
                ),
                "error": (
                    "HORARIOS_AUTORIZADOS_NO_DISPONIBLES"
                ),
            })

            return resultado

        resultado.update({
            "generada": True,
            "respuesta": respuesta_horarios,
            "modelo_usado": "",
            "intentos": 0,
            "uso_fallback_seguro": False,
            "errores_validacion": [],
            "tipo_respuesta": (
                "HORARIOS_DETERMINISTAS"
            ),
            "error": "",
        })

        print(
            "🕒 Horarios institucionales respondidos "
            "sin intervención de Gemini."
        )

        return resultado

    # ========================================================
    # NIVEL PARA SOLICITUD DE COSTOS
    # ========================================================

    if accion == "PEDIR_NIVEL_COSTOS":

        respuesta_nivel_costos = (
            "Claro. ¿Para qué nivel requiere la información "
            "de colegiaturas: kínder, primaria o secundaria?"
        )

        resultado.update({
            "generada": True,
            "respuesta": respuesta_nivel_costos,
            "modelo_usado": "",
            "intentos": 0,
            "uso_fallback_seguro": False,
            "errores_validacion": [],
            "tipo_respuesta": (
                "SOLICITUD_NIVEL_COSTOS_DETERMINISTA"
            ),
            "error": "",
        })

        print(
            "💰 Nivel solicitado para continuar "
            "consulta pendiente de costos."
        )

        return resultado

    # ========================================================
    # RESPUESTA DETERMINISTA DE COSTOS
    # ========================================================

    if accion == "RESPONDER_COSTOS":

        niveles_costos = (
            obtener_niveles_costos_solicitados(
                analisis_seguro,
                decision_segura,
            )
        )

        if not niveles_costos:
            resultado.update({
                "generada": True,
                "respuesta": (
                    "Con gusto le comparto la información. "
                    "¿Para qué nivel requiere conocer la "
                    "colegiatura: kínder, primaria o secundaria?"
                ),
                "modelo_usado": "",
                "intentos": 0,
                "uso_fallback_seguro": True,
                "errores_validacion": [
                    "NIVEL_COSTOS_NO_DISPONIBLE"
                ],
                "tipo_respuesta": (
                    "COSTOS_FALLBACK_NIVEL"
                ),
                "error": (
                    "NIVEL_COSTOS_NO_DISPONIBLE"
                ),
            })

            return resultado

        precios_autorizados = []

        for nivel in niveles_costos:

            precio = (
                obtener_colegiatura_autorizada(
                    nivel
                )
            )

            if not precio:
                resultado.update({
                    "generada": True,
                    "respuesta": (
                        "En este momento no me es posible "
                        "consultar la colegiatura autorizada "
                        "para uno de los niveles solicitados. "
                        "Permítame verificar la información "
                        "antes de compartirle un monto."
                    ),
                    "modelo_usado": "",
                    "intentos": 0,
                    "uso_fallback_seguro": True,
                    "errores_validacion": [
                        "PRECIO_AUTORIZADO_NO_DISPONIBLE"
                    ],
                    "tipo_respuesta": (
                        "COSTOS_FALLBACK_SEGURO"
                    ),
                    "error": (
                        "PRECIO_AUTORIZADO_NO_DISPONIBLE"
                    ),
                })

                print(
                    "❌ RESPONDER_COSTOS bloqueado: "
                    "no existe colegiatura autorizada "
                    f"para nivel='{nivel}'."
                )

                return resultado

            precios_autorizados.append(
                precio
            )

        configuracion = (
            cargar_configuracion_precios()
        )

        opciones_comerciales = (
            configuracion.get(
                "opciones_comerciales",
                {},
            )
            if isinstance(
                configuracion,
                dict,
            )
            else {}
        )

        if not isinstance(
            opciones_comerciales,
            dict,
        ):
            opciones_comerciales = {}

        # ----------------------------------------------------
        # INTRODUCCIÓN COMERCIAL
        # ----------------------------------------------------

        partes_respuesta = [
            (
                "Entendemos que al elegir una escuela no "
                "solamente importa encontrar el mejor programa "
                "para sus hijos, sino también una opción que "
                "sea viable para la familia."
            ),
            (
                "Por eso, en Colegio Valle de Filadelfia "
                "contamos con diferentes alternativas que "
                "pueden hacer la colegiatura mucho más "
                "accesible de lo que inicialmente podría imaginar:"
            ),
        ]

        # ----------------------------------------------------
        # OPCIONES COMERCIALES AUTORIZADAS
        # ----------------------------------------------------

        opciones_texto = []

        if opciones_comerciales.get(
            "beca_alto_desempeno"
        ):
            opciones_texto.append(
                "- Opciones de beca de acuerdo con el perfil "
                "y desempeño del alumno."
            )

        if opciones_comerciales.get(
            "beca_hermanos"
        ):
            opciones_texto.append(
                "- Beneficios especiales para hermanos."
            )

        planes_pago = (
            opciones_comerciales.get(
                "planes_pago",
                [],
            )
        )

        if isinstance(
            planes_pago,
            list,
        ) and planes_pago:

            planes_limpios = [
                str(plan).strip()
                for plan in planes_pago
                if str(plan).strip()
            ]

            if planes_limpios:
                if len(planes_limpios) == 1:
                    planes_texto = (
                        planes_limpios[0]
                    )
                else:
                    planes_texto = (
                        ", ".join(
                            planes_limpios[:-1]
                        )
                        + " o "
                        + planes_limpios[-1]
                    )

                opciones_texto.append(
                    "- Planes de pago flexibles: "
                    f"{planes_texto}."
                )

        if opciones_comerciales.get(
            "descuento_pago_anticipado"
        ):
            opciones_texto.append(
                "- Descuentos por pago anticipado "
                "de anualidad."
            )

        medios_pago = (
            opciones_comerciales.get(
                "medios_pago",
                [],
            )
        )

        if isinstance(
            medios_pago,
            list,
        ) and medios_pago:

            medios_limpios = [
                str(medio).strip()
                for medio in medios_pago
                if str(medio).strip()
            ]

            if medios_limpios:
                if len(medios_limpios) == 1:
                    medios_texto = (
                        medios_limpios[0]
                    )
                else:
                    medios_texto = (
                        ", ".join(
                            medios_limpios[:-1]
                        )
                        + " y "
                        + medios_limpios[-1]
                    )

                opciones_texto.append(
                    "- Diferentes formas de pago: "
                    f"{medios_texto}."
                )

        if opciones_texto:
            partes_respuesta.append(
                "\n".join(
                    opciones_texto
                )
            )

        # ----------------------------------------------------
        # PRECIOS: ÚNICAMENTE NIVELES SOLICITADOS
        # ----------------------------------------------------

        lineas_precios = []

        for precio in precios_autorizados:

            nivel = str(
                precio.get(
                    "nivel",
                    "",
                )
                or ""
            ).strip()

            importe = precio.get(
                "importe"
            )

            moneda = str(
                precio.get(
                    "moneda",
                    "MXN",
                )
                or "MXN"
            ).strip()

            importe_formateado = (
                f"${importe:,.0f}"
            )

            nombre_mostrable = (
                "Preescolar"
                if nivel == "Kínder"
                else nivel
            )

            lineas_precios.append(
                f"{nombre_mostrable}: "
                f"aproximadamente "
                f"{importe_formateado} "
                f"{moneda} mensuales"
            )

        encabezado_precios = (
            "La colegiatura actualmente es:"
            if len(
                lineas_precios
            ) == 1
            else "Las colegiaturas actualmente son:"
        )

        partes_respuesta.append(
            encabezado_precios
            + "\n\n"
            + "\n".join(
                lineas_precios
            )
        )

        # ----------------------------------------------------
        # ENCUADRE FINAL
        # ----------------------------------------------------

        partes_respuesta.append(
            "Y algo importante: antes de pensar que el "
            "colegio puede estar fuera de su presupuesto, "
            "vale la pena conocer qué beneficios podrían "
            "aplicar en su caso."
        )

        partes_respuesta.append(
            "En una visita podemos explicarle personalmente "
            "nuestro modelo educativo, todo lo que incluye "
            "la colegiatura y revisar las opciones de beca, "
            "descuentos y forma de pago disponibles para "
            "su familia."
        )

        partes_respuesta.append(
            "¿Le gustaría agendar una visita y conocer "
            "más sobre becas y descuentos?"
        )

        respuesta_costos = "\n\n".join(
            partes_respuesta
        )

        resultado.update({
            "generada": True,
            "respuesta": respuesta_costos,
            "modelo_usado": "",
            "intentos": 0,
            "uso_fallback_seguro": False,
            "errores_validacion": [],
            "tipo_respuesta": (
                "COSTOS_AUTORIZADOS_DETERMINISTAS"
            ),
            "error": "",
        })

        print(
            "💰 Costos institucionales generados "
            "desde precios.json: "
            + ", ".join(
                (
                    f"{precio.get('nivel')}="
                    f"{precio.get('importe')}"
                )
                for precio
                in precios_autorizados
            )
        )

        return resultado

    # ========================================================
    # CONFIRMACIÓN DETERMINISTA DE FECHA DE CITA
    # ========================================================

    if accion == "CONFIRMAR_FECHA_CITA":

        datos_detectados = (
            decision_segura.get(
                "datos_detectados",
                {},
            )
        )

        if not isinstance(
            datos_detectados,
            dict,
        ):
            datos_detectados = {}

        fecha_iso_confirmar = str(
            datos_detectados.get(
                "fecha_cita_iso_confirmar",
                "",
            )
            or ""
        ).strip()

        hora_confirmar = str(
            datos_detectados.get(
                "hora_cita_confirmar",
                "",
            )
            or ""
        ).strip()

        hora_mostrable = (
            formatear_hora_cita_12h(
                hora_confirmar
            )
        )

        fecha_mostrable = (
            formatear_fecha_cita_calendario(
                fecha_iso_confirmar
            )
        )

        if not fecha_mostrable:
            resultado["error"] = (
                "FECHA_CITA_CONFIRMACION_INVALIDA"
            )
            return resultado

        if hora_confirmar:
            respuesta_confirmacion = (
                "Perfecto. Entonces sería para el "
                f"{fecha_mostrable} a las "
                f"{hora_mostrable}, ¿correcto?"
            )
        else:
            respuesta_confirmacion = (
                "Perfecto. Entonces sería para el "
                f"{fecha_mostrable}, ¿correcto?"
            )

        resultado.update({
            "generada": True,
            "respuesta": respuesta_confirmacion,
            "modelo_usado": "",
            "intentos": 0,
            "uso_fallback_seguro": False,
            "errores_validacion": [],
            "tipo_respuesta": (
                "CONFIRMACION_FECHA_CITA_DETERMINISTA"
            ),
            "error": "",
        })

        print(
            "📅 Fecha de cita enviada para confirmación "
            "calendario sin intervención de Gemini: "
            f"{respuesta_confirmacion}"
        )

        return resultado

    # ========================================================
    # RESPUESTA DETERMINISTA AL CONSULTAR DISPONIBILIDAD
    # DE UNA CITA
    # ========================================================

    if accion == "CONSULTAR_ADMIN":

        datos_detectados = (
            decision_segura.get(
                "datos_detectados",
                {},
            )
        )

        if not isinstance(
            datos_detectados,
            dict,
        ):
            datos_detectados = {}

        fecha_cita_admin = str(
            datos_detectados.get(
                "fecha_cita_confirmada_calendario",
                "",
            )
            or datos_detectados.get(
                "fecha_cita_iso",
                "",
            )
            or ""
        ).strip()

        hora_cita_admin = str(
            datos_detectados.get(
                "hora_cita_confirmada_calendario",
                "",
            )
            or datos_detectados.get(
                "hora_cita_24h",
                "",
            )
            or ""
        ).strip()

        if (
            fecha_cita_admin
            and hora_cita_admin
        ):
            respuesta_consulta = (
                "Permítame por favor, en lo que validamos "
                "la disponibilidad del día y hora que propone."
            )

            resultado.update({
                "generada": True,
                "respuesta": respuesta_consulta,
                "modelo_usado": "",
                "intentos": 0,
                "uso_fallback_seguro": False,
                "errores_validacion": [],
                "tipo_respuesta": (
                    "CONSULTA_CITA_ADMIN_DETERMINISTA"
                ),
                "error": "",
            })

            return resultado

    api_key = (
        os.getenv("GOOGLE_AI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )

    if not api_key:
        resultado["error"] = (
            "GOOGLE_AI_API_KEY_NO_CONFIGURADA"
        )
        return resultado

    genai.configure(api_key=api_key)

    def construir_fallback_seguro() -> str:
        """
        Devuelve una respuesta segura controlada por Python.
        """
        if accion == "CONSULTAR_ADMIN":
            motivo_decision = str(
                decision_segura.get(
                    "motivo",
                    "",
                )
                or ""
            ).strip().lower()

            es_seguimiento_cita_pendiente = (
                "visita"
                in motivo_decision
                and "confirmacion"
                in normalizar_texto_geografico(
                    motivo_decision
                )
                and "pendiente"
                in motivo_decision
            )

            if es_seguimiento_cita_pendiente:
                return (
                    "Le ofrezco una disculpa por la demora. "
                    "La confirmación de su visita continúa "
                    "pendiente. Permítame consultarlo nuevamente "
                    "con administración y en cuanto tenga respuesta "
                    "se la comparto."
                )

            if zona_mencionada:
                return (
                    "Con gusto. Permítame revisar internamente "
                    f"la atención desde {zona_mencionada}. "
                    "En cuanto tenga la confirmación, le comparto "
                    "la información correspondiente."
                )

            return (
                "Con gusto. Permítame revisar internamente su "
                "solicitud. En cuanto tenga la confirmación, "
                "le comparto la información correspondiente."
            )
            
        if accion == "RECHAZAR_CAMPUS":
            return (
                "Este canal brinda atención exclusivamente para "
                "el Colegio Valle de Filadelfia Campus Santa Cruz "
                "Atizapán."
            )

        if accion == "PEDIR_ZONA":
            return (
                "¿Desde qué localidad o municipio se comunica?"
            )

        if accion == "RESPONDER_SALUDO":
            return (
                crear_respuesta_saludo_simple_estructurado(
                    mensaje
                )
                or "Hola. ¿En qué podemos ayudarle?"
            )
            
        if accion == "PEDIR_FECHA_NACIMIENTO":
            return (
                "¿Me comparte, por favor, la fecha de nacimiento "
                "completa del alumno, incluyendo día, mes y año?"
            )

        if accion == "PEDIR_FECHA_CITA":
            return (
                "Con gusto podemos recibirle de lunes a viernes "
                "en un horario de 8:00 a. m. a 1:00 p. m. "
                "Si por cuestiones laborales necesita un horario "
                "posterior, podemos evaluar una alternativa hasta "
                "máximo las 4:00 p. m.\n\n"
                "¿Qué día y hora le funcionan mejor para su visita?"
            )

        if accion == "PEDIR_HORA_CITA":
            return (
                "¿En qué horario le gustaría realizar la visita?"
            )

        if accion == "CITA_DIA_NO_LABORAL":
            return (
                "Las visitas se realizan de lunes a viernes. "
                "¿Qué otro día le resultaría conveniente?"
            )

        if accion == "SEGUIMIENTO":
            return (
                "Con gusto. Puede retomar la conversación cuando "
                "lo considere conveniente y continuamos apoyándole."
            )

        if accion == "ORIENTAR_PRE_KINDER":
            return (
                "Por la edad del alumno, necesitamos revisar el "
                "nivel que le correspondería para el próximo ciclo "
                "escolar."
            )

        if accion == "RESPONDER_COSTOS":
            if nivel:
                return (
                    "Con gusto le compartimos la información de "
                    f"costos correspondiente a {nivel}."
                )

            return (
                "Con gusto le compartimos la información de costos. "
                "¿Para qué nivel escolar la requiere?"
            )

        return (
            "Con gusto le atendemos. ¿Podría compartirme un poco "
            "más sobre la información que necesita?"
        )
    def validar_respuesta_generada(
        texto: str,
    ) -> Dict[str, Any]:
        """
        Verifica que el texto sea una respuesta completa,
        coherente y adecuada para enviarse por WhatsApp.
        """
        respuesta_limpia = str(
            texto or ""
        ).strip()

        errores = []

        # ====================================================
        # INTEGRIDAD DE ENLACES GOOGLE MAPS
        # ====================================================

        urls_detectadas = re.findall(
            r"https?://[^\s]+",
            respuesta_limpia,
            flags=re.IGNORECASE,
        )

        urls_maps_detectadas = [
            url.rstrip(
                ".,;:!?)]}"
            )
            for url in urls_detectadas
            if (
                "google.com/maps"
                in url.lower()
                or "maps.app.goo.gl"
                in url.lower()
            )
        ]

        if urls_maps_detectadas:

            ubicacion_autorizada = (
                obtener_ubicacion_institucional_campus()
            )

            url_autorizada = str(
                ubicacion_autorizada.get(
                    "url",
                    "",
                )
                or ""
            ).strip()

            for url_detectada in (
                urls_maps_detectadas
            ):
                if (
                    not url_autorizada
                    or url_detectada
                    != url_autorizada
                ):
                    errores.append(
                        "ENLACE_MAPS_NO_AUTORIZADO"
                    )

        # ====================================================
        # FORMATO VISUAL PARA WHATSAPP
        # ====================================================

        signos_pregunta = (
            respuesta_limpia.count("?")
        )

        if signos_pregunta > 1:
            errores.append(
                "MAS_DE_UNA_PREGUNTA"
            )

        contiene_pregunta = (
            "?" in respuesta_limpia
        )

        if contiene_pregunta:
            posicion_pregunta = (
                respuesta_limpia.rfind("?")
            )

            texto_antes_pregunta = (
                respuesta_limpia[
                    :posicion_pregunta + 1
                ]
            )

            if (
                "\n\n" not in texto_antes_pregunta
                and len(respuesta_limpia) > 120
            ):
                errores.append(
                    "FALTA_SEPARACION_ANTES_PREGUNTA"
                )

        if not respuesta_limpia:
            errores.append("RESPUESTA_VACIA")

            return {
                "valida": False,
                "respuesta_limpia": "",
                "errores": errores,
            }

        if len(respuesta_limpia) < 20:
            errores.append(
                "RESPUESTA_DEMASIADO_CORTA"
            )

        if len(respuesta_limpia) > 1200:
            errores.append(
                "RESPUESTA_DEMASIADO_LARGA"
            )

        texto_normalizado = (
            normalizar_texto_geografico(
                respuesta_limpia
            )
        )

        # ====================================================
        # VALIDACIÓN GLOBAL DE REFERENCIAS ECONÓMICAS
        # ====================================================

        puede_compartir_costos = bool(
            decision_segura.get(
                "puede_compartir_costos",
                False,
            )
        )

        if not puede_compartir_costos:
            expresiones_economicas_prohibidas = [
                "costo",
                "costos",
                "precio",
                "precios",
                "colegiatura",
                "colegiaturas",
                "mensualidad",
                "mensualidades",
                "inscripcion",
                "importe",
                "beca",
                "becas",
                "descuento",
                "descuentos",
                "promocion",
                "promociones",
                "plan de pago",
                "planes de pago",
                "forma de pago",
                "formas de pago",
                "informacion de costos",
                "informacion detallada de costos",
                "costos correspondientes",
                "costos detallados",
                "compartir los costos",
                "brindarte los costos",
                "proceso de admision y costos",
            ]

            for expresion in (
                expresiones_economicas_prohibidas
            ):
                if expresion in texto_normalizado:
                    errores.append(
                        "MENCION_ECONOMICA_NO_AUTORIZADA:"
                        f"{expresion}"
                    )

        frases_prohibidas = [
            "refinar segun reglas",
            "analisis semantico",
            "decision obligatoria",
            "plan obligatorio",
            "reglas obligatorias",
            "razonamiento",
            "respuesta sugerida",
            "respuesta final",
            "accion consultar admin",
            "consultar admin",
            "zona requiere revision",
            "zona valida por ruta",
            "google places",
            "google routes",
            "coordenadas",
            "limite configurado",
            "distancia por carretera",
            "como modelo de lenguaje",
            "no puedo cumplir",
            "optional but good",
            "the plan says",
            "according to the plan",
            "based on the plan",
            "brief and natural",
            "breve y natural",
            "tono cordial",
            "refine the response",
            "internal rules",
            "final answer",
            "the user wants",
            "the prospect wants",
            "should include",
            "must include",
        ]

        for frase in frases_prohibidas:
            if frase in texto_normalizado:
                errores.append(
                    f"CONTENIDO_INTERNO:{frase}"
                )

        if "```" in respuesta_limpia:
            errores.append(
                "BLOQUE_MARKDOWN"
            )

        if re.search(
            r"^\s*(?:\*+:?|#+|\d+[.)])\s*",
            respuesta_limpia,
        ):
            errores.append(
                "INICIO_CON_FORMATO_INTERNO"
            )

        if re.search(
            r"\n\s*\d+[.)]\s+",
            respuesta_limpia,
        ):
            errores.append(
                "LISTA_NUMERADA_INTERNA"
            )

        if (
            respuesta_limpia.startswith("{")
            or respuesta_limpia.startswith("[")
        ):
            errores.append(
                "FORMATO_JSON_O_LISTA"
            )

        if respuesta_limpia.count("**") >= 2:
            errores.append(
                "MARKDOWN_EN_RESPUESTA"
            )

        if re.search(
            r"[*)]\s*:",
            respuesta_limpia,
        ):
            errores.append(
                "FORMATO_INTERNO_ANOMALO"
            )

        if respuesta_limpia.startswith(
            (
                "*",
                "#",
                ":",
                ")",
                "]",
                "}",
            )
        ):
            errores.append(
                "INICIO_ANOMALO"
            )

        palabras_ingles_internas = [
            "optional",
            "though",
            "plan",
            "should",
            "must",
            "response",
            "user",
            "prospect",
            "rules",
            "refine",
            "brief",
            "natural",
            "according",
            "include",
        ]

        cantidad_ingles_interno = sum(
            1
            for palabra in palabras_ingles_internas
            if re.search(
                rf"\b{re.escape(palabra)}\b",
                respuesta_limpia,
                flags=re.IGNORECASE,
            )
        )

        if cantidad_ingles_interno >= 2:
            errores.append(
                "CONTENIDO_INTERNO_EN_INGLES"
            )

        palabras_finales_incompletas = {
            "a",
            "al",
            "ante",
            "bajo",
            "con",
            "contra",
            "de",
            "del",
            "desde",
            "durante",
            "e",
            "el",
            "en",
            "entre",
            "hacia",
            "hasta",
            "la",
            "las",
            "lo",
            "los",
            "mediante",
            "o",
            "para",
            "pero",
            "por",
            "porque",
            "que",
            "segun",
            "sin",
            "sobre",
            "su",
            "sus",
            "tras",
            "tu",
            "un",
            "una",
            "y",
            "claro",
            "nuestra",
            "nuestro",
            "correspondiente",
            "informacion sobre",
        }

        texto_sin_signos_finales = re.sub(
            r"[.!?¡¿…,:;]+$",
            "",
            respuesta_limpia,
        ).strip()

        texto_para_validar_cierre = re.sub(
            r"\((?:a|o)\)",
            "",
            texto_sin_signos_finales,
            flags=re.IGNORECASE,
        )

        palabras_respuesta = re.findall(
            r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+",
            texto_para_validar_cierre,
        )

        ultima_palabra = (
            normalizar_texto_geografico(
                palabras_respuesta[-1]
            )
            if palabras_respuesta
            else ""
        )

        ultimas_dos_palabras = (
            normalizar_texto_geografico(
                " ".join(
                    palabras_respuesta[-2:]
                )
            )
            if len(palabras_respuesta) >= 2
            else ultima_palabra
        )

        if (
            ultima_palabra
            in palabras_finales_incompletas
            or ultimas_dos_palabras
            in palabras_finales_incompletas
        ):
            errores.append(
                "RESPUESTA_TERMINA_INCOMPLETA"
            )

        tiene_puntuacion_final = bool(
            re.search(
                r"[.!?…]$",
                respuesta_limpia,
            )
        )

        if not tiene_puntuacion_final:
            errores.append(
                "RESPUESTA_SIN_CIERRE"
            )

        acciones_que_requieren_pregunta = {
            "PEDIR_ZONA",
            "PEDIR_FECHA_NACIMIENTO",
            "PEDIR_FECHA_CITA",
            "PEDIR_HORA_CITA",
        }

        if (
            accion
            in acciones_que_requieren_pregunta
            and "?" not in respuesta_limpia
        ):
            errores.append(
                "ACCION_REQUIERE_PREGUNTA"
            )

        if accion == "RESPONDER_SALUDO":
            palabras_respuesta_saludo = (
                respuesta_limpia.split()
            )

            if len(
                palabras_respuesta_saludo
            ) > 15:
                errores.append(
                    "SALUDO_DEMASIADO_EXTENSO"
                )

            expresiones_no_permitidas_saludo = [
                "costos",
                "colegiatura",
                "inscripcion",
                "cita",
                "visita",
                "primaria",
                "secundaria",
                "kinder",
                "informacion sobre",
            ]

            for expresion in (
                expresiones_no_permitidas_saludo
            ):
                if expresion in texto_normalizado:
                    errores.append(
                        "SALUDO_AGREGA_INFORMACION:"
                        f"{expresion}"
                    )

        if accion == "CONSULTAR_ADMIN":
            palabras_requeridas = [
                "revis",
                "confirm",
                "consult",
            ]

            if not any(
                palabra in texto_normalizado
                for palabra in palabras_requeridas
            ):
                errores.append(
                    "NO_COMUNICA_REVISION_INTERNA"
                )

            palabras_prohibidas_admin = [
                "costo",
                "colegiatura",
                "inscripcion",
                "mensualidad",
                "kilometro",
                "lejos",
                "fuera de zona",
                "no podemos atender",
                "no es posible atender",
            ]

            for palabra in (
                palabras_prohibidas_admin
            ):
                if palabra in texto_normalizado:
                    errores.append(
                        "INCUMPLE_CONSULTAR_ADMIN:"
                        f"{palabra}"
                    )

        if accion == "RECHAZAR_CAMPUS":
            if (
                "campus santa cruz"
                not in texto_normalizado
                and "santa cruz atizapan"
                not in texto_normalizado
            ):
                errores.append(
                    "NO_ACLARA_CANAL_SANTA_CRUZ"
                )

        if accion == "PEDIR_ZONA":
            palabras_zona = [
                "localidad",
                "municipio",
                "zona",
                "donde",
            ]

            if not any(
                palabra in texto_normalizado
                for palabra in palabras_zona
            ):
                errores.append(
                    "NO_SOLICITA_LOCALIDAD"
                )

            datos_exactos_prohibidos = [
                "direccion exacta",
                "calle",
                "numero exterior",
                "ubicacion gps",
                "coordenadas",
                "compartir ubicacion",
            ]

            for expresion in (
                datos_exactos_prohibidos
            ):
                if expresion in texto_normalizado:
                    errores.append(
                        "PIDE_UBICACION_EXACTA:"
                        f"{expresion}"
                    )

        if accion == "PEDIR_FECHA_NACIMIENTO":
            palabras_fecha_nacimiento = [
                "fecha de nacimiento",
                "dia mes y año",
                "dia mes y ano",
                "nacimiento completa",
            ]

            if not any(
                expresion in texto_normalizado
                for expresion in (
                    palabras_fecha_nacimiento
                )
            ):
                errores.append(
                    "NO_SOLICITA_FECHA_NACIMIENTO"
                )

        if accion == "PEDIR_FECHA_CITA":
            palabras_fecha_cita = [
                "que dia",
                "que fecha",
                "cuando",
                "lunes a viernes",
                "dia le gustaria",
            ]

            if not any(
                expresion in texto_normalizado
                for expresion in palabras_fecha_cita
            ):
                errores.append(
                    "NO_SOLICITA_FECHA_CITA"
                )

        if accion == "PEDIR_HORA_CITA":
            palabras_hora = [
                "hora",
                "horario",
                "a que hora",
            ]

            if not any(
                expresion in texto_normalizado
                for expresion in palabras_hora
            ):
                errores.append(
                    "NO_SOLICITA_HORA_CITA"
                )

        if accion == "CITA_DIA_NO_LABORAL":
            menciona_dias_laborales = (
                "lunes a viernes"
                in texto_normalizado
                or "entre semana"
                in texto_normalizado
            )

            if not menciona_dias_laborales:
                errores.append(
                    "NO_ACLARA_DIAS_DE_VISITA"
                )

            if "?" not in respuesta_limpia:
                errores.append(
                    "NO_SOLICITA_OTRA_FECHA"
                )

        if accion == "SEGUIMIENTO":
            palabras_retorno = [
                "cuando guste",
                "cuando lo desee",
                "cuando lo considere",
                "retomar",
                "escribirnos",
                "contactarnos",
                "aqui estaremos",
                "aquí estaremos",
            ]

            if not any(
                expresion in texto_normalizado
                for expresion in palabras_retorno
            ):
                errores.append(
                    "NO_PERMITE_RETOMAR_CONVERSACION"
                )

        if accion == "ORIENTAR_PRE_KINDER":
            palabras_edad = [
                "edad",
                "años",
                "anos",
                "kinder",
                "nivel",
                "ciclo",
            ]

            if not any(
                palabra in texto_normalizado
                for palabra in palabras_edad
            ):
                errores.append(
                    "NO_ORIENTA_POR_EDAD"
                )

        if accion == "RESPONDER_COSTOS":
            palabras_costos = [
                "costo",
                "costos",
                "colegiatura",
                "colegiaturas",
                "inscripcion",
                "mensualidad",
                "precio",
                "importe",
                "informacion de costos",
            ]

            menciona_costos = any(
                expresion in texto_normalizado
                for expresion in palabras_costos
            )

            if not menciona_costos:
                errores.append(
                    "NO_RESPONDE_SOBRE_COSTOS"
                )

            respuestas_saludo_sin_contenido = [
                "hola",
                "que gusto saludarte",
                "mucho gusto",
                "buenos dias",
                "buenas tardes",
                "buenas noches",
            ]

            solo_saluda = (
                len(respuesta_limpia.split()) <= 12
                and any(
                    expresion in texto_normalizado
                    for expresion
                    in respuestas_saludo_sin_contenido
                )
                and not menciona_costos
            )

            if solo_saluda:
                errores.append(
                    "RESPUESTA_COSTOS_SOLO_SALUDA"
                )

            inicios_incompletos_costos = [
                "claro",
                "con gusto",
                "que gusto saludarte claro",
                "con gusto le comparto",
                "con gusto te comparto",
                "informacion sobre nuestra",
            ]

            if (
                len(respuesta_limpia) < 55
                and any(
                    texto_normalizado.endswith(
                        expresion
                    )
                    for expresion
                    in inicios_incompletos_costos
                )
            ):
                errores.append(
                    "RESPUESTA_COSTOS_INCOMPLETA"
                )
        return {
            "valida": not errores,
            "respuesta_limpia": (
                respuesta_limpia
            ),
            "errores": errores,
        }

    historial_lineas = []

    if history:
        for item in history[-6:]:
            direccion = str(
                getattr(
                    item,
                    "direction",
                    "",
                )
                or ""
            ).strip()

            contenido = str(
                getattr(
                    item,
                    "content",
                    "",
                )
                or ""
            ).strip()

            if not contenido:
                continue

            if direccion == "incoming":
                emisor = "Prospecto"
            elif direccion == "outgoing":
                emisor = "Asistente"
            else:
                emisor = "Conversación"

            historial_lineas.append(
                f"{emisor}: {contenido}"
            )

    historial_texto = (
        "\n".join(historial_lineas)
        if historial_lineas
        else "Sin historial previo disponible."
    )

    analisis_json = json.dumps(
        analisis_seguro,
        ensure_ascii=False,
        indent=2,
    )

    decision_json = json.dumps(
        decision_segura,
        ensure_ascii=False,
        indent=2,
    )

    plan_json = json.dumps(
        plan_seguro,
        ensure_ascii=False,
        indent=2,
    )

    ahora_local_respuesta = datetime.now(
        LOCAL_TZ
    )

    if ahora_local_respuesta.hour < 12:
        saludo_temporal = "buenos días"

    elif ahora_local_respuesta.hour < 19:
        saludo_temporal = "buenas tardes"

    else:
        saludo_temporal = "buenas noches"

    prompt_base = f"""
Eres el asistente de admisiones del Colegio Valle de Filadelfia,
Campus Santa Cruz Atizapán.

Redacta únicamente el mensaje final que se enviaría al prospecto
por WhatsApp.

MENSAJE DEL PROSPECTO:
{mensaje}

HISTORIAL RECIENTE:
{historial_texto}

ANÁLISIS:
{analisis_json}

DECISIÓN DEFINITIVA DE PYTHON:
{decision_json}

PLAN DE RESPUESTA:
{plan_json}

CONTEXTO TEMPORAL LOCAL:
En este momento corresponde decir "{saludo_temporal}".

Si el mensaje del prospecto justifica comenzar con un saludo,
cualquier saludo temporal debe ser coherente con este dato.
No digas "buen día" durante la noche ni "buenas noches" durante
la mañana.

INSTRUCCIONES:

- Respeta la decisión de Python.
- Cumple el objetivo y las restricciones del plan.
- Escribe una sola respuesta breve, cordial y natural.
- Usa trato institucional de usted, salvo que el historial
  muestre claramente que debe conservarse otro tratamiento.
- No agregues un saludo si el prospecto no saludó.
- No agradezcas automáticamente cada dato o respuesta del prospecto.
- Evita iniciar con "muchas gracias", "agradecemos",
  "gracias por compartir", "gracias por confirmar" o expresiones
  equivalentes, salvo que exista una razón conversacional real.
- Avanza directamente a la siguiente idea o pregunta del flujo.
- No repitas ni reformules innecesariamente la información que el
  prospecto acaba de proporcionar.
- Usa transiciones breves y naturales.
- No uses expresiones como "qué gusto saludarte",
  "qué gusto" o "mucho gusto" como frases de relleno.
- No muestres análisis, pasos, listas ni razonamientos.
- No uses encabezados.
- No uses Markdown.
- No uses numeraciones.
- No menciones acciones o clasificaciones internas.
- No menciones Google, rutas, coordenadas ni distancias.
- No inventes costos, fechas, disponibilidad ni datos.
- Devuelve exclusivamente el texto para WhatsApp.

REGLAS DE FORMATO PARA WHATSAPP:

- Divide la respuesta en párrafos breves.
- Cada párrafo debe expresar una sola idea.
- Cuando exista una explicación seguida de una pregunta,
  coloca una línea en blanco antes de la pregunta.
- La pregunta principal debe aparecer en el último párrafo.
- Formula como máximo una pregunta principal.
- Usa tantos párrafos breves como sean necesarios para que la
  respuesta sea fácil de leer en WhatsApp.
- En respuestas explicativas con varios conceptos o beneficios,
  separa cada idea principal en su propio párrafo.
- Prefiere claridad visual antes que compactar varias ideas en
  un solo bloque.
- No redactes bloques largos de texto.
- Evita respuestas visualmente densas o amontonadas.
- Conserva exactamente los saltos de línea en la respuesta final.
"""
    ultimo_modelo = ""
    errores_acumulados = []

    for numero_intento in range(1, 3):
        resultado["intentos"] = numero_intento

        prompt_intento = prompt_base

        if numero_intento == 2:
            prompt_intento += """

SEGUNDO INTENTO OBLIGATORIO:

La respuesta anterior fue rechazada porque parecía contenido
interno, incompleto o con formato incorrecto.

Genera de nuevo una sola respuesta final para WhatsApp.

No escribas pasos.
No escribas listas.
No escribas análisis.
No escribas encabezados.
No utilices asteriscos, numeración ni Markdown.
Comienza directamente con el mensaje dirigido a la familia.
"""

        try:
            response, modelo_usado = (
                generar_con_gemini_con_fallback(
                    prompt_intento,
                    generation_config=(
                        genai.types.GenerationConfig(
                            temperature=0.2,
                        )
                    ),
                    tarea=(
                        "redacción de respuesta "
                        f"estructurada intento {numero_intento}"
                    ),
                )
            )

            ultimo_modelo = modelo_usado

            respuesta_cruda = (
                extraer_texto_respuesta_gemini(
                    response
                ).strip()
            )

            validacion = validar_respuesta_generada(
                respuesta_cruda
            )

            if validacion["valida"]:
                resultado.update({
                    "generada": True,
                    "respuesta": validacion[
                        "respuesta_limpia"
                    ],
                    "modelo_usado": modelo_usado,
                    "uso_fallback_seguro": False,
                    "errores_validacion": (
                        errores_acumulados
                    ),
                    "error": "",
                })

                return resultado

            errores_intento = [
                (
                    f"INTENTO_{numero_intento}:"
                    f"{error}"
                )
                for error in validacion["errores"]
            ]

            errores_acumulados.extend(
                errores_intento
            )

            print(
                "⚠️ Respuesta estructurada rechazada: "
                f"intento={numero_intento}, "
                f"errores={errores_intento}, "
                f"respuesta={repr(respuesta_cruda)}"
            )

        except Exception as e:
            error_intento = (
                f"INTENTO_{numero_intento}:"
                f"ERROR_GENERACION:{e}"
            )

            errores_acumulados.append(
                error_intento
            )

            print(
                "⚠️ Error generando respuesta "
                f"estructurada: {error_intento}"
            )

    respuesta_fallback = construir_fallback_seguro()

    resultado.update({
        "generada": True,
        "respuesta": respuesta_fallback,
        "modelo_usado": ultimo_modelo,
        "uso_fallback_seguro": True,
        "errores_validacion": errores_acumulados,
        "error": "",
    })

    return resultado        

def procesar_mensaje_prospecto_estructurado(
    mensaje_usuario: str,
    contact=None,
    history=None,
    contexto_comercial: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Ejecuta el núcleo del nuevo flujo estructurado.

    Secuencia:
    1. Gemini interpreta el mensaje y devuelve el contrato estructurado.
    2. Python aplica las reglas deterministas del negocio.
    3. Se devuelven ambos resultados para auditoría y pruebas.

    Esta función todavía:
    - No redacta respuestas.
    - No envía mensajes por Twilio.
    - No modifica contact.notes.
    - No cambia el FLOW_STATE.
    - No crea tareas para el administrador.
    - No sustituye el flujo anterior.
    """
    mensaje = (mensaje_usuario or "").strip()

    if not mensaje:
        analisis_vacio = crear_analisis_mensaje_vacio()
        decision_vacia = crear_decision_negocio_vacia()

        decision_vacia["motivo"] = (
            "El mensaje recibido está vacío."
        )

        return {
            "version": "1.0",
            "flujo": "estructurado",
            "procesado": False,
            "analisis": analisis_vacio,
            "decision": decision_vacia,
            "error": "MENSAJE_VACIO",
        }

    try:

        analisis = analizar_mensaje_prospecto_con_ia(
            mensaje_usuario=mensaje,
            contact=contact,
            history=history or [],
            contexto_comercial=(
                contexto_comercial
            ),
        )
        
        analisis_fallo = (
            analisis.get("intencion_principal") == "OTRO"
            and analisis.get("confianza", 0.0) == 0.0
            and not analisis.get("datos_detectados")
            and not analisis.get("zona_mencionada")
            and not analisis.get("nivel")
        )
        
        if analisis_fallo:
            clasificacion_zona_respaldo = (
                clasificar_zona_determinista(
                    mensaje_usuario=mensaje,
                    zona_mencionada=mensaje,
                    campus_mencionado="",
                )
            )

            clasificacion_respaldo = str(
                clasificacion_zona_respaldo.get(
                    "clasificacion",
                    "",
                )
                or ""
            ).strip().upper()

            clasificaciones_geograficas_utiles = {
                "ZONA_VALIDA_DIRECTA",
                "ZONA_VALIDA_CONECTIVIDAD",
                "ZONA_EXTERNA",
                "CAMPUS_EXTERNO",
            }

            if (
                clasificacion_respaldo
                in clasificaciones_geograficas_utiles
            ):
                analisis = (
                    crear_analisis_mensaje_vacio()
                )

                analisis.update({
                    "intencion_principal": (
                        "RESPONDER_ZONA"
                    ),
                    "zona_mencionada": mensaje,
                    "clasificacion_zona": (
                        "VALIDA"
                        if clasificacion_zona_respaldo.get(
                            "es_zona_validada"
                        )
                        else "DUDOSA"
                    ),
                    "datos_detectados": [
                        "zona_interes"
                    ],
                    "accion_recomendada": (
                        "CONTINUAR_CONVERSACION"
                    ),
                    "confianza": 1.0,
                })

            else:
                contexto_rescate = (
                    contexto_comercial
                    if isinstance(
                        contexto_comercial,
                        dict,
                    )
                    else {}
                )

                hitos_contexto = (
                    contexto_rescate.get(
                        "hitos_comerciales",
                        [],
                    )
                )

                if not isinstance(
                    hitos_contexto,
                    list,
                ):
                    hitos_contexto = []

                etapa_contexto = str(
                    contexto_rescate.get(
                        "etapa_conversacional",
                        "",
                    )
                    or ""
                ).strip().upper()

                zona_contexto = str(
                    contexto_rescate.get(
                        "zona_interes",
                        "",
                    )
                    or ""
                ).strip()

                referencia_contexto = str(
                    contexto_rescate.get(
                        "referencia_colegio",
                        "",
                    )
                    or ""
                ).strip()

                alumnos_contexto = (
                    contexto_rescate.get(
                        "alumnos",
                        [],
                    )
                )

                if not isinstance(
                    alumnos_contexto,
                    list,
                ):
                    alumnos_contexto = []

                contexto_suficiente_para_continuar = bool(
                    hitos_contexto
                    or zona_contexto
                    or referencia_contexto
                    or alumnos_contexto
                    or etapa_contexto
                    not in {
                        "",
                        "CONTACTO_INICIAL",
                    }
                )

                if contexto_suficiente_para_continuar:
                    analisis = (
                        crear_analisis_mensaje_vacio()
                    )

                    analisis.update({
                        "accion_recomendada": (
                            "CONTINUAR_CONVERSACION"
                        ),
                        "datos_detectados": [
                            "contexto_recuperado"
                        ],
                        "confianza": 0.0,
                    })

                    print(
                        "🧠 Fallo técnico del análisis IA: "
                        "se conserva el contexto comercial "
                        "y Python continúa desde la etapa "
                        f"{etapa_contexto or 'NO_DEFINIDA'}."
                    )

                else:
                    decision_fallback = (
                        crear_decision_negocio_vacia()
                    )

                    decision_fallback.update({
                        "accion": (
                            "FALLBACK_CONVERSACIONAL"
                        ),
                        "motivo": (
                            "No existe análisis IA válido ni "
                            "contexto suficiente para continuar "
                            "la conversación con seguridad."
                        ),
                        "requiere_admin": False,
                        "puede_compartir_costos": False,
                        "zona_validada": False,
                        "debe_finalizar_conversacion": False,
                        "datos_detectados": {},
                    })

                    return {
                        "version": "1.0",
                        "flujo": "estructurado",
                        "procesado": True,
                        "analisis": analisis,
                        "decision": decision_fallback,
                        "error": (
                            "ANALISIS_IA_INVALIDO_SIN_CONTEXTO"
                        ),
                    }
                
            
        decision = aplicar_reglas_negocio_estructuradas(
            analisis=analisis,
            contact=contact,
            mensaje_usuario=mensaje,
            contexto_comercial=contexto_comercial,
        )

        # ----------------------------------------------------
        # ARBITRAJE MONOTÓNICO PRE-REDACCIÓN
        # ----------------------------------------------------
        #
        # Antes de permitir que Gemini redacte, Python verifica
        # que la decisión no regrese a una etapa ya consumada.
        # ----------------------------------------------------

        decision = aplicar_candado_progreso_comercial(
            decision=decision,
            analisis=analisis,
            contexto_comercial=contexto_comercial,
        )

        plan_respuesta = (
            construir_plan_respuesta_estructurada(
                analisis=analisis,
                decision=decision,
            )
        )
        

        generacion_respuesta = (
            generar_respuesta_final_estructurada(
                mensaje_usuario=mensaje,
                analisis=analisis,
                decision=decision,
                plan_respuesta=plan_respuesta,
                history=history or [],
            )
        )

        resultado = {
            "version": "1.0",
            "flujo": "estructurado",
            "procesado": True,
            "analisis": analisis,
            "decision": decision,
            "plan_respuesta": plan_respuesta,
            "generacion_respuesta": generacion_respuesta,
            "respuesta_generada": (
                generacion_respuesta.get(
                    "respuesta",
                    "",
                )
            ),
            "error": "",
        }

        print(
            "🧭 Resultado del orquestador estructurado: "
            f"{json.dumps(resultado, ensure_ascii=False)}"
        )

        return resultado

    except Exception as e:
        print(
            "⚠️ Error ejecutando el orquestador "
            f"estructurado: {e}"
        )

        analisis_vacio = crear_analisis_mensaje_vacio()
        decision_vacia = crear_decision_negocio_vacia()

        decision_vacia["motivo"] = (
            "Ocurrió un error interno durante el "
            "procesamiento estructurado."
        )

        return {
            "version": "1.0",
            "flujo": "estructurado",
            "procesado": False,
            "analisis": analisis_vacio,
            "decision": decision_vacia,
            "error": str(e),
        }
        
        
def descargar_media_twilio(media_url: str) -> bytes:
    """
    Descarga un archivo multimedia enviado por Twilio usando Basic Auth.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")

    if not all([account_sid, api_key, api_secret]):
        raise RuntimeError("Faltan credenciales de Twilio para descargar media")

    resp = requests.get(media_url, auth=(api_key, api_secret), timeout=30)
    resp.raise_for_status()
    return resp.content

def transcribir_audio_gemini(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """
    Usa Gemini para transcribir audio a texto en español.
    Aplica fallback de modelos si el modelo principal falla.
    """
    api_key = os.getenv("GOOGLE_AI_API_KEY")
    if not api_key:
        raise RuntimeError("Falta GOOGLE_AI_API_KEY")

    genai.configure(api_key=api_key)

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

    response, modelo_usado = generar_con_gemini_con_fallback(
        [
            {
                "mime_type": mime_type,
                "data": audio_b64
            },
            "Transcribe exactamente este audio a texto en español. Devuelve únicamente la transcripción, sin explicaciones."
        ],
        tarea="transcripción de audio"
    )

    print(f"🎙️ Modelo usado para transcripción: {modelo_usado}")

    texto = extraer_texto_respuesta_gemini(response)
    return texto
    
def es_audio_whatsapp(num_media: str, media_content_type: str) -> bool:
    """
    Determina si el mensaje entrante contiene audio.
    """
    try:
        total = int(num_media or "0")
    except ValueError:
        total = 0

    if total < 1:
        return False

    content_type = (media_content_type or "").lower()
    return content_type.startswith("audio/")


    

def get_note_value(contact, key: str) -> str:
    """
    Lee un valor guardado en contact.notes con formato KEY:valor.
    """
    notes = contact.notes or ""
    prefix = f"{key}:"

    for line in notes.splitlines():
        if line.startswith(prefix):
            return line.replace(prefix, "", 1).strip()

    return ""


def set_note_value(contact, key: str, value: str):
    """
    Guarda o actualiza un valor en contact.notes sin borrar otros datos.
    """
    notes = contact.notes or ""
    lines = notes.splitlines()
    prefix = f"{key}:"

    nuevas_lineas = []
    actualizado = False

    for line in lines:
        if line.startswith(prefix):
            nuevas_lineas.append(f"{key}:{value}")
            actualizado = True
        else:
            nuevas_lineas.append(line)

    if not actualizado:
        nuevas_lineas.append(f"{key}:{value}")

    contact.notes = "\n".join([line for line in nuevas_lineas if line.strip()])


def get_flow_state(contact) -> str:
    """
    Obtiene el estado conversacional actual guardado en notes.
    Compatible con notes de varias líneas.
    """
    estado = get_note_value(contact, "FLOW_STATE")

    if estado:
        return estado

    return "SALUDO_INICIAL"


def set_flow_state(contact, estado: str):
    """
    Guarda el estado conversacional actual sin borrar otros datos del prospecto.
    """
    set_note_value(contact, "FLOW_STATE", estado)

def construir_contexto_comercial_desde_contacto(
    contact,
) -> Dict[str, Any]:
    """
    Construye el contexto comercial y conversacional utilizando
    únicamente información ya guardada en el contacto.

    Esta función:
    - no modifica la base de datos;
    - no realiza commits;
    - no cambia FLOW_STATE;
    - no cambia contact.status;
    - no consulta Gemini;
    - no genera respuestas;
    - no elimina información existente.
    """

    contexto = crear_contexto_comercial_vacio()

    if contact is None:
        return contexto

    def leer_nota(
        *claves: str,
    ) -> str:
        """
        Devuelve el primer valor no vacío encontrado entre varias
        claves compatibles.
        """
        for clave in claves:
            valor = str(
                get_note_value(
                    contact,
                    clave,
                )
                or ""
            ).strip()

            if valor:
                return valor

        return ""

    def leer_lista_nota(
        *claves: str,
    ) -> List[str]:
        """
        Lee una lista guardada como JSON, texto separado por comas
        o texto separado por barras verticales.
        """
        valor = leer_nota(*claves)

        if not valor:
            return []

        try:
            datos = json.loads(valor)

            if isinstance(datos, list):
                resultado = []

                for elemento in datos:
                    texto = str(
                        elemento or ""
                    ).strip()

                    if (
                        texto
                        and texto not in resultado
                    ):
                        resultado.append(texto)

                return resultado

        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            pass

        separador = (
            "|"
            if "|" in valor
            else ","
        )

        resultado = []

        for elemento in valor.split(separador):
            texto = str(
                elemento or ""
            ).strip()

            if (
                texto
                and texto not in resultado
            ):
                resultado.append(texto)

        return resultado

    # --------------------------------------------------------
    # ESTADO COMERCIAL
    # --------------------------------------------------------

    estado_comercial = str(
        getattr(
            contact,
            "status",
            "",
        )
        or ""
    ).strip().upper()

    if (
        estado_comercial
        not in ESTADOS_COMERCIALES_VALIDOS
    ):
        estado_comercial = "PROSPECTO_NUEVO"

    contexto[
        "estado_comercial"
    ] = estado_comercial

    # --------------------------------------------------------
    # ETAPA CONVERSACIONAL
    # --------------------------------------------------------

    etapa_guardada = leer_nota(
        "ETAPA_CONVERSACIONAL",
    ).upper()

    if (
        etapa_guardada
        in ETAPAS_CONVERSACIONALES_VALIDAS
    ):
        etapa_conversacional = etapa_guardada

    else:
        flow_state_actual = str(
            get_flow_state(contact)
            or ""
        ).strip().upper()

        equivalencias_flow_state = {
            "SALUDO_INICIAL": "CONTACTO_INICIAL",
            "ESPERANDO_INTENCION": "REFERENCIA_COLEGIO",
            "ESPERANDO_REFERENCIA": "REFERENCIA_COLEGIO",
            "VALIDACION_ZONA": "VALIDACION_ZONA",
            "VALIDACION_ZONA_OBLIGATORIA": (
                "VALIDACION_ZONA"
            ),
            "ZONA_INVALIDA_POTENCIAL_METEPEC": (
                "VALIDACION_ZONA"
            ),
            "PRESENTACION_VALOR": (
                "PRESENTACION_VALOR"
            ),
            "EXPLICACION_METODO": (
                "EXPLICACION_METODO"
            ),
            "ESPERANDO_AREA_INTERES": (
                "IDENTIFICACION_INTERES"
            ),
            "PROFUNDIZACION_INTERES": (
                "PROFUNDIZACION_INTERES"
            ),
            "INVITACION_CITA": (
                "INVITACION_VISITA"
            ),
            "ESPERANDO_FECHA_CITA": (
                "NEGOCIACION_CITA"
            ),
            "ESPERANDO_HORA_CITA": (
                "NEGOCIACION_CITA"
            ),
            "CONSULTA_ADMIN_PENDIENTE": (
                "ESPERANDO_CONFIRMACION_ADMIN"
            ),
            "ESPERANDO_CONFIRMACION_ADMIN": (
                "ESPERANDO_CONFIRMACION_ADMIN"
            ),
            "ESPERANDO_DATOS_CITA": (
                "ESPERANDO_DATOS_CITA"
            ),
            "CITA_DATOS_COMPLETOS": (
                "VISITA_CONFIRMADA"
            ),
            "VISITA_AGENDADA": (
                "VISITA_CONFIRMADA"
            ),
            "SEGUIMIENTO_ACORDADO": (
                "SEGUIMIENTO"
            ),
        }

        etapa_conversacional = (
            equivalencias_flow_state.get(
                flow_state_actual,
                "CONTACTO_INICIAL",
            )
        )

    contexto[
        "etapa_conversacional"
    ] = etapa_conversacional

    # --------------------------------------------------------
    # OBJETIVO PENDIENTE
    # --------------------------------------------------------

    objetivo_pendiente = leer_nota(
        "OBJETIVO_PENDIENTE",
    ).upper()

    if (
        objetivo_pendiente
        not in OBJETIVOS_PENDIENTES_VALIDOS
    ):
        objetivo_pendiente = ""

    contexto[
        "objetivo_pendiente"
    ] = objetivo_pendiente

    # --------------------------------------------------------
    # DATOS DE LA FAMILIA
    # --------------------------------------------------------

    contexto["nombre_tutor"] = leer_nota(
        "NOMBRE_TUTOR",
        "NOMBRE_PADRE",
        "NOMBRE_MADRE",
    )

    contexto["zona_interes"] = leer_nota(
        "ZONA_INTERES",
        "ZONA",
    )

    contexto["referencia_colegio"] = leer_nota(
        "REFERENCIA_COLEGIO",
        "REFERENCIA",
    )

    # --------------------------------------------------------
    # DATOS DE UNO O VARIOS ALUMNOS
    # --------------------------------------------------------

    alumnos_estructurados_json = leer_nota(
        "ALUMNOS_ESTRUCTURADOS",
    )

    alumnos_contexto = []

    if alumnos_estructurados_json:

        try:
            alumnos_crudos = json.loads(
                alumnos_estructurados_json
            )

            if isinstance(
                alumnos_crudos,
                list,
            ):

                for alumno_crudo in alumnos_crudos:

                    if not isinstance(
                        alumno_crudo,
                        dict,
                    ):
                        continue

                    nombre = str(
                        alumno_crudo.get(
                            "nombre",
                            "",
                        )
                        or ""
                    ).strip()

                    nivel_interes = str(
                        alumno_crudo.get(
                            "nivel_interes",
                            alumno_crudo.get(
                                "nivel",
                                "",
                            ),
                        )
                        or ""
                    ).strip()

                    grado_interes = str(
                        alumno_crudo.get(
                            "grado_interes",
                            alumno_crudo.get(
                                "grado",
                                "",
                            ),
                        )
                        or ""
                    ).strip()

                    edad = alumno_crudo.get(
                        "edad"
                    )

                    fecha_nacimiento = str(
                        alumno_crudo.get(
                            "fecha_nacimiento",
                            "",
                        )
                        or ""
                    ).strip()

                    if not any(
                        [
                            nombre,
                            nivel_interes,
                            grado_interes,
                            edad is not None,
                            fecha_nacimiento,
                        ]
                    ):
                        continue

                    alumnos_contexto.append({
                        "nombre": nombre,
                        "nivel_interes": (
                            nivel_interes
                        ),
                        "grado_interes": (
                            grado_interes
                        ),
                        "edad": edad,
                        "fecha_nacimiento": (
                            fecha_nacimiento
                        ),
                    })

        except Exception as e:
            print(
                "⚠️ No se pudo leer "
                "ALUMNOS_ESTRUCTURADOS: "
                f"{e}"
            )

    # --------------------------------------------------------
    # COMPATIBILIDAD CON DATOS SINGULARES ANTERIORES
    # --------------------------------------------------------

    if not alumnos_contexto:

        nombre_alumno = leer_nota(
            "NOMBRE_ALUMNO",
        )

        nivel_interes = leer_nota(
            "NIVEL_INTERES",
            "NIVEL",
        )

        grado_interes = leer_nota(
            "GRADO_INTERES",
            "GRADO_SOLICITADO",
            "ULTIMO_GRADO_CURSADO",
        )

        edad_alumno = leer_nota(
            "EDAD_ALUMNO",
            "EDAD_AL_CORTE",
        )

        fecha_nacimiento = leer_nota(
            "FECHA_NACIMIENTO_ISO",
            "FECHA_NACIMIENTO",
        )

        if any(
            [
                nombre_alumno,
                nivel_interes,
                grado_interes,
                edad_alumno,
                fecha_nacimiento,
            ]
        ):

            alumnos_contexto.append({
                "nombre": nombre_alumno,
                "nivel_interes": nivel_interes,
                "grado_interes": grado_interes,
                "edad": edad_alumno,
                "fecha_nacimiento": (
                    fecha_nacimiento
                ),
            })

    contexto["alumnos"] = alumnos_contexto
    
    # --------------------------------------------------------
    # MEMORIA COMERCIAL YA DISPONIBLE
    # --------------------------------------------------------

    contexto[
        "hitos_comerciales"
    ] = [
        hito
        for hito in leer_lista_nota(
            "HITOS_COMERCIALES",
        )
        if hito in HITOS_COMERCIALES_VALIDOS
    ]

    contexto[
        "temas_explicados"
    ] = leer_lista_nota(
        "TEMAS_EXPLICADOS",
    )

    contexto[
        "areas_interes"
    ] = leer_lista_nota(
        "AREAS_INTERES",
        "AREA_INTERES",
    )

    contexto[
        "objeciones_detectadas"
    ] = leer_lista_nota(
        "OBJECIONES_DETECTADAS",
        "OBJECIONES",
    )

    contexto[
        "resumen_relacion"
    ] = leer_nota(
        "RESUMEN_RELACION",
        "RESUMEN_COMERCIAL",
    )

    # --------------------------------------------------------
    # ACTIVIDAD HISTÓRICA DEL CONTACTO
    # --------------------------------------------------------

    ultima_interaccion = getattr(
        contact,
        "last_contact",
        None,
    )

    if ultima_interaccion is not None:
        try:
            contexto[
                "fecha_ultima_interaccion"
            ] = ultima_interaccion.isoformat()

        except AttributeError:
            contexto[
                "fecha_ultima_interaccion"
            ] = str(
                ultima_interaccion
            ).strip()

    total_mensajes = getattr(
        contact,
        "total_messages",
        0,
    )

    try:
        total_mensajes = int(
            total_mensajes or 0
        )

    except (
        TypeError,
        ValueError,
    ):
        total_mensajes = 0

    contexto[
        "historial_completo_disponible"
    ] = total_mensajes > 0

    # --------------------------------------------------------
    # VALIDACIÓN FINAL DEL CONTRATO
    # --------------------------------------------------------

    try:
        contexto_validado = (
            ContextoComercialConversacion
            .model_validate(
                contexto
            )
        )

        return contexto_validado.model_dump()

    except Exception as e:
        print(
            "⚠️ Error construyendo contexto comercial: "
            f"{e}"
        )

        return crear_contexto_comercial_vacio()

def enriquecer_contexto_comercial_con_memoria(
    contexto_comercial: Dict[str, Any],
    resultado_memoria: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Combina el contexto estructurado guardado en el contacto
    con la memoria histórica recuperada por Gemini.

    Esta función:
    - no modifica la base de datos;
    - no modifica contact.notes;
    - no cambia contact.status;
    - no cambia FLOW_STATE;
    - no realiza commits;
    - no envía mensajes;
    - no elimina información estructurada existente.
    """

    if not isinstance(
        contexto_comercial,
        dict,
    ):
        contexto_base = (
            crear_contexto_comercial_vacio()
        )
    else:
        contexto_base = dict(
            contexto_comercial
        )

    if not isinstance(
        resultado_memoria,
        dict,
    ):
        return contexto_base

    if not resultado_memoria.get(
        "exitoso"
    ):
        return contexto_base

    memoria = resultado_memoria.get(
        "memoria"
    )

    if not isinstance(memoria, dict):
        return contexto_base

    # --------------------------------------------------------
    # ETAPA Y ESTADO COMERCIAL
    # --------------------------------------------------------
    #
    # La memoria IA NO tiene autoridad para modificar estos
    # campos. Son propiedad exclusiva del estado persistido y
    # de las transiciones deterministas del flujo.
    # --------------------------------------------------------

    etapa_sugerida_memoria = str(
        memoria.get(
            "etapa_conversacional_sugerida",
            "",
        )
        or ""
    ).strip().upper()

    estado_sugerido_memoria = str(
        memoria.get(
            "estado_comercial_sugerido",
            "",
        )
        or ""
    ).strip().upper()

    if (
        etapa_sugerida_memoria
        or estado_sugerido_memoria
    ):
        print(
            "🛡️ Sugerencia de estado de memoria IA "
            "ignorada por política determinista: "
            f"etapa={etapa_sugerida_memoria}, "
            f"estado={estado_sugerido_memoria}"
        )

    # --------------------------------------------------------
    # DATOS DE LA FAMILIA
    # --------------------------------------------------------

    campos_texto_completables = [
        "nombre_tutor",
        "zona_interes",
        "referencia_colegio",
    ]

    for campo in campos_texto_completables:
        valor_actual = str(
            contexto_base.get(
                campo,
                "",
            )
            or ""
        ).strip()

        valor_memoria = str(
            memoria.get(
                campo,
                "",
            )
            or ""
        ).strip()

        if (
            not valor_actual
            and valor_memoria
        ):
            contexto_base[
                campo
            ] = valor_memoria

    # --------------------------------------------------------
    # DATOS DEL ALUMNO
    # --------------------------------------------------------

    alumnos_actuales = contexto_base.get(
        "alumnos"
    )

    alumnos_memoria = memoria.get(
        "alumnos"
    )

    if (
        not alumnos_actuales
        and isinstance(
            alumnos_memoria,
            list,
        )
        and alumnos_memoria
    ):
        contexto_base[
            "alumnos"
        ] = alumnos_memoria

    # --------------------------------------------------------
    # LISTAS COMERCIALES
    # --------------------------------------------------------

    equivalencias_listas = {
        "temas_explicados": (
            "temas_explicados"
        ),
        "areas_interes": (
            "areas_interes"
        ),
        "objeciones_detectadas": (
            "objeciones_detectadas"
        ),
    }

    for (
        campo_contexto,
        campo_memoria,
    ) in equivalencias_listas.items():
        elementos_combinados = []

        for origen in [
            contexto_base.get(
                campo_contexto,
                [],
            ),
            memoria.get(
                campo_memoria,
                [],
            ),
        ]:
            for elemento in normalizar_lista_textos(
                origen
            ):
                if (
                    elemento
                    not in elementos_combinados
                ):
                    elementos_combinados.append(
                        elemento
                    )

        if campo_contexto == (
            "hitos_comerciales"
        ):
            elementos_combinados = [
                elemento
                for elemento
                in elementos_combinados
                if elemento
                in HITOS_COMERCIALES_VALIDOS
            ]

        contexto_base[
            campo_contexto
        ] = elementos_combinados

    # --------------------------------------------------------
    # RESUMEN DE LA RELACIÓN
    # --------------------------------------------------------

    resumen_memoria = str(
        memoria.get(
            "resumen_relacion",
            "",
        )
        or ""
    ).strip()

    if resumen_memoria:
        contexto_base[
            "resumen_relacion"
        ] = resumen_memoria

    contexto_base[
        "historial_completo_disponible"
    ] = True

    # --------------------------------------------------------
    # VALIDACIÓN FINAL
    # --------------------------------------------------------

    try:
        contexto_validado = (
            ContextoComercialConversacion
            .model_validate(
                contexto_base
            )
        )

        return contexto_validado.model_dump()

    except Exception as e:
        print(
            "⚠️ Error enriqueciendo contexto "
            "comercial: "
            f"{e}"
        )

        return contexto_comercial

def calcular_transicion_comercial_post_envio(
    resultado: Dict[str, Any],
    contexto_actual: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Calcula cómo debe evolucionar el contexto comercial DESPUÉS
    de que una respuesta haya sido enviada exitosamente.

    Importante:
    - No modifica la base de datos.
    - No modifica contact.
    - No realiza commits.
    - No cambia FLOW_STATE.
    - No asume que el mensaje fue enviado.
    - Solamente devuelve la transición propuesta.

    La persistencia real se realizará posteriormente, únicamente
    después de confirmar un envío exitoso por Twilio.
    """

    contexto = (
        contexto_actual
        if isinstance(contexto_actual, dict)
        else {}
    )

    resultado_seguro = (
        resultado
        if isinstance(resultado, dict)
        else {}
    )

    decision = resultado_seguro.get(
        "decision"
    )

    if not isinstance(decision, dict):
        decision = {}

    analisis = resultado_seguro.get(
        "analisis"
    )

    if not isinstance(analisis, dict):
        analisis = {}

    accion = str(
        decision.get(
            "accion",
            "CONTINUAR_CONVERSACION",
        )
        or "CONTINUAR_CONVERSACION"
    ).strip().upper()

    etapa_actual = str(
        contexto.get(
            "etapa_conversacional",
            "CONTACTO_INICIAL",
        )
        or "CONTACTO_INICIAL"
    ).strip().upper()

    estado_actual = str(
        contexto.get(
            "estado_comercial",
            "PROSPECTO_NUEVO",
        )
        or "PROSPECTO_NUEVO"
    ).strip().upper()

    hitos_previos = contexto.get(
        "hitos_comerciales",
        [],
    )

    if not isinstance(hitos_previos, list):
        hitos_previos = []

    hitos_resultado = []

    for hito in hitos_previos:
        hito_normalizado = str(
            hito or ""
        ).strip().upper()

        if (
            hito_normalizado
            in HITOS_COMERCIALES_VALIDOS
            and hito_normalizado
            not in hitos_resultado
        ):
            hitos_resultado.append(
                hito_normalizado
            )

    objetivo_actual = str(
        contexto.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    if (
        objetivo_actual
        not in OBJETIVOS_PENDIENTES_VALIDOS
    ):
        objetivo_actual = ""

    transicion = {
        "accion": accion,
        "etapa_anterior": etapa_actual,
        "estado_anterior": estado_actual,
        "etapa_conversacional": etapa_actual,
        "estado_comercial": estado_actual,
        "objetivo_pendiente": objetivo_actual,
        "hitos_comerciales": hitos_resultado,
        "hitos_nuevos": [],
        "transicion_aplicable": False,
        "motivo": "",
    }

    # ========================================================
    # CANDADO MONOTÓNICO FINAL DE PERSISTENCIA
    # ========================================================
    #
    # Es una segunda defensa independiente.
    # Aunque una ruta futura omitiera el arbitraje previo,
    # la máquina de estados no puede persistir una regresión.
    # ========================================================

    piso_persistencia = (
        obtener_piso_progreso_comercial(
            contexto
        )
    )

    nivel_piso_persistencia = int(
        piso_persistencia.get(
            "nivel",
            0,
        )
        or 0
    )

    nivel_accion_persistencia = (
        NIVELES_ACCIONES_EMBUDO.get(
            accion
        )
    )

    if (
        nivel_accion_persistencia
        is not None
        and nivel_accion_persistencia
        < nivel_piso_persistencia
    ):
        transicion.update({
            "etapa_conversacional": etapa_actual,
            "estado_comercial": estado_actual,
            "objetivo_pendiente": objetivo_actual,
            "transicion_aplicable": False,
            "motivo": (
                "Candado monotónico final: se bloqueó "
                "una transición que pretendía regresar "
                "a una etapa comercial ya consumada."
            ),
        })

        print(
            "🛡️ TRANSICIÓN REGRESIVA NO PERSISTIDA: "
            f"accion={accion}, "
            f"piso={piso_persistencia.get('piso')}"
        )

        return transicion
        

    # ========================================================
    # CANDADO DE INTEGRIDAD: CITA PENDIENTE DE ADMINISTRACIÓN
    # ========================================================
    #
    # Mientras una cita siga esperando confirmación humana,
    # ninguna respuesta paralela puede hacer retroceder el
    # embudo comercial.
    #
    # Esto es deliberadamente determinista y no depende de que
    # Gemini haya clasificado correctamente "pregunta_paralela".
    # ========================================================

    cita_pendiente_admin_actual = bool(
        objetivo_actual == "ESPERAR_CONFIRMACION_ADMIN"
        or etapa_actual == "ESPERANDO_CONFIRMACION_ADMIN"
        or estado_actual == "CITA_PENDIENTE_CONFIRMACION"
    )

    cambio_cita_explicito = bool(
        analisis.get(
            "cambio_fecha_cita",
            False,
        )
        or analisis.get(
            "cancelacion_cita",
            False,
        )
        or analisis.get(
            "desistimiento_temporal",
            False,
        )
    )

    if (
        cita_pendiente_admin_actual
        and not cambio_cita_explicito
        and accion != "CONSULTAR_ADMIN"
    ):
        transicion.update({
            "etapa_conversacional": (
                "ESPERANDO_CONFIRMACION_ADMIN"
            ),
            "estado_comercial": (
                "CITA_PENDIENTE_CONFIRMACION"
            ),
            "objetivo_pendiente": (
                "ESPERAR_CONFIRMACION_ADMIN"
            ),
            "transicion_aplicable": False,
            "motivo": (
                "Se preserva la cita pendiente de confirmación "
                "administrativa. El mensaje actual no puede "
                "reactivar ni hacer retroceder el embudo comercial."
            ),
        })

        return transicion

    # ========================================================
    # CONTINUIDAD GENÉRICA DEL OBJETIVO PENDIENTE
    # ========================================================
    #
    # NO_AFECTA_OBJETIVO protege conversaciones incidentales,
    # pero nunca puede invalidar una decisión comercial
    # explícita que Python ya tomó para este turno.
    #
    # Si decidir_siguiente_accion_estructurada() determinó una
    # acción que mueve el embudo, esa decisión es la autoridad.
    # ========================================================

    relacion_objetivo = str(
        analisis.get(
            "relacion_con_objetivo_pendiente",
            "SIN_OBJETIVO",
        )
        or "SIN_OBJETIVO"
    ).strip().upper()

    acciones_con_transicion_autoritativa = {
        "PEDIR_ZONA",
        "PEDIR_NIVEL_COSTOS",
        "PEDIR_REFERENCIA",
        "PRESENTAR_PROPUESTA_VALOR",
        "EXPLICAR_METODO_FILADELFIA",
        "PREGUNTAR_AREA_INTERES",
        "PROFUNDIZAR_AREA_INTERES",
        "RESPONDER_TEMA",
        "RESPONDER_COSTOS",
        "INVITAR_CITA",
        "PEDIR_FECHA_CITA",
        "PEDIR_HORA_CITA",
        "CONFIRMAR_FECHA_CITA",
        "CONSULTAR_ADMIN",
        "SEGUIMIENTO",
    }

    if (
        objetivo_actual
        and relacion_objetivo == "NO_AFECTA_OBJETIVO"
        and accion
        not in acciones_con_transicion_autoritativa
    ):
        transicion.update({
            "etapa_conversacional": etapa_actual,
            "estado_comercial": estado_actual,
            "objetivo_pendiente": objetivo_actual,
            "transicion_aplicable": False,
            "motivo": (
                "El mensaje actual fue interpretado como "
                "conversación paralela al objetivo pendiente. "
                "Se atiende el mensaje sin alterar la posición "
                "comercial ni el objetivo activo."
            ),
        })

        return transicion
    
    def agregar_hito(
        hito: str,
    ) -> None:
        hito_normalizado = str(
            hito or ""
        ).strip().upper()

        if (
            hito_normalizado
            not in HITOS_COMERCIALES_VALIDOS
        ):
            return

        if (
            hito_normalizado
            not in transicion["hitos_comerciales"]
        ):
            transicion[
                "hitos_comerciales"
            ].append(
                hito_normalizado
            )

        if (
            hito_normalizado
            not in transicion["hitos_nuevos"]
        ):
            transicion[
                "hitos_nuevos"
            ].append(
                hito_normalizado
            )

    # ========================================================
    # TRANSICIONES SEGURAS DERIVADAS DE LA RESPUESTA ENVIADA
    # ========================================================

    if accion == "PEDIR_ZONA":
        transicion.update({
            "etapa_conversacional": (
                "VALIDACION_ZONA"
            ),
            "estado_comercial": (
                "EN_CALIFICACION"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "Se solicitó validación de zona."
            ),
        })

        datos_decision_costos = decision.get(
            "datos_detectados",
            {},
        )

        if (
            isinstance(
                datos_decision_costos,
                dict,
            )
            and datos_decision_costos.get(
                "registrar_solicitud_costos_inicial"
            )
        ):
            agregar_hito(
                "SOLICITO_COSTOS_INICIAL"
            )

        if (
            isinstance(
                datos_decision_costos,
                dict,
            )
            and datos_decision_costos.get(
                "registrar_insistencia_costos"
            )
        ):
            agregar_hito(
                "INSISTIO_COSTOS"
            )

    elif accion == "PEDIR_NIVEL_COSTOS":
        transicion.update({
            "transicion_aplicable": True,
            "motivo": (
                "Se solicitó el nivel escolar para completar "
                "una consulta pendiente de costos."
            ),
        })

        datos_decision_costos = decision.get(
            "datos_detectados",
            {},
        )

        if isinstance(
            datos_decision_costos,
            dict,
        ):
            if datos_decision_costos.get(
                "registrar_solicitud_costos_inicial"
            ):
                agregar_hito(
                    "SOLICITO_COSTOS_INICIAL"
                )

            if datos_decision_costos.get(
                "registrar_insistencia_costos"
            ):
                agregar_hito(
                    "INSISTIO_COSTOS"
                )

    elif accion == "PEDIR_REFERENCIA":
        transicion.update({
            "etapa_conversacional": (
                "REFERENCIA_COLEGIO"
            ),
            "estado_comercial": (
                "EN_CALIFICACION"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "Se solicitó la referencia del colegio."
            ),
        })

    elif accion == "PRESENTAR_PROPUESTA_VALOR":
        transicion.update({
            "etapa_conversacional": (
                "PRESENTACION_VALOR"
            ),
            "estado_comercial": (
                "PROSPECTO_INFORMADO"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "La propuesta general de valor fue enviada."
            ),
        })

        datos_decision_costos = decision.get(
            "datos_detectados",
            {},
        )

        if (
            isinstance(
                datos_decision_costos,
                dict,
            )
            and datos_decision_costos.get(
                "registrar_solicitud_costos_inicial"
            )
        ):
            agregar_hito(
                "SOLICITO_COSTOS_INICIAL"
            )

        agregar_hito(
            "RECIBIO_PRESENTACION_VALOR"
        )

    elif accion == "EXPLICAR_METODO_FILADELFIA":
        transicion.update({
            "etapa_conversacional": (
                "EXPLICACION_METODO"
            ),
            "estado_comercial": (
                "PROSPECTO_INFORMADO"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "La explicación del Método Filadelfia "
                "fue enviada."
            ),
        })

        agregar_hito(
            "RECIBIO_EXPLICACION_METODO"
        )

    elif accion == "PREGUNTAR_AREA_INTERES":
        transicion.update({
            "etapa_conversacional": (
                "IDENTIFICACION_INTERES"
            ),
            "estado_comercial": (
                "PROSPECTO_INFORMADO"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "Se solicitó identificar el área de interés."
            ),
        })

    elif accion == "RESPONDER_HORARIOS":
        transicion.update({
            "transicion_aplicable": False,
            "motivo": (
                "Se respondió una pregunta institucional "
                "sobre horarios sin alterar la etapa "
                "comercial pendiente."
            ),
        })

    elif accion == "PROFUNDIZAR_AREA_INTERES":
        transicion.update({
            "etapa_conversacional": (
                "PROFUNDIZACION_INTERES"
            ),
            "estado_comercial": (
                "PROSPECTO_INFORMADO"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "Se envió una respuesta personalizada "
                "sobre el interés de la familia."
            ),
        })

        agregar_hito(
            "RECIBIO_RESPUESTA_PERSONALIZADA"
        )

    elif accion == "RESPONDER_TEMA":
        # ----------------------------------------------------
        # RESPUESTA INCIDENTAL SIN MOVER EL EMBUDO
        # ----------------------------------------------------
        #
        # Responder una duda concreta no significa entrar
        # nuevamente a PROFUNDIZACION_INTERES.
        #
        # Conservamos etapa, estado y objetivo actuales.
        # Marcamos la transición como aplicable para que el CRM
        # pueda abrir nuevamente la ventana de seguimiento desde
        # el objetivo que ya estaba pendiente.
        # ----------------------------------------------------

        transicion.update({
            "etapa_conversacional": etapa_actual,
            "estado_comercial": estado_actual,
            "objetivo_pendiente": objetivo_actual,
            "transicion_aplicable": True,
            "motivo": (
                "Se respondió una consulta paralela sin "
                "modificar la posición ni el objetivo "
                "comercial pendiente."
            ),
        })

    elif accion == "RESPONDER_COSTOS":
        transicion.update({
            "etapa_conversacional": (
                "INVITACION_VISITA"
            ),
            "estado_comercial": (
                "COSTOS_PRESENTADOS"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "Se compartieron los costos solicitados "
                "y quedó pendiente la decisión de visita "
                "de la familia."
            ),
        })
        
        datos_decision_costos = decision.get(
            "datos_detectados",
            {},
        )

        solicitud_inicial = bool(
            isinstance(
                datos_decision_costos,
                dict,
            )
            and datos_decision_costos.get(
                "registrar_solicitud_costos_inicial"
            )
        )

        if solicitud_inicial:
            agregar_hito(
                "SOLICITO_COSTOS_INICIAL"
            )
        else:
            agregar_hito(
                "INSISTIO_COSTOS"
            )

        agregar_hito(
            "RECIBIO_COSTOS"
        )

        agregar_hito(
            "RECIBIO_OPCIONES_PAGO"
        )
        
    elif accion == "INVITAR_CITA":
        transicion.update({
            "etapa_conversacional": (
                "INVITACION_VISITA"
            ),
            "estado_comercial": (
                "PENDIENTE_DE_AGENDAR"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "Se envió una invitación para visitar "
                "el campus."
            ),
        })

    elif accion in {
        "PEDIR_FECHA_CITA",
        "PEDIR_HORA_CITA",
        "CONFIRMAR_FECHA_CITA",
    }:
        transicion.update({
            "etapa_conversacional": (
                "NEGOCIACION_CITA"
            ),
            "estado_comercial": (
                "PENDIENTE_DE_AGENDAR"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "La conversación avanzó a negociación "
                "de fecha u horario de visita."
            ),
        })

    elif accion == "CONSULTAR_ADMIN":
        tiene_contexto_cita = bool(
            analisis.get("pide_cita")
            or analisis.get(
                "seguimiento_cita"
            )
            or analisis.get(
                "fecha_cita_texto"
            )
            or analisis.get(
                "fecha_cita_iso"
            )
            or analisis.get(
                "hora_cita_texto"
            )
            or analisis.get(
                "hora_cita_24h"
            )
            or objetivo_actual
            == "CONFIRMAR_FECHA_CITA_CALENDARIO"
            or (
                isinstance(
                    decision.get(
                        "datos_detectados",
                        {},
                    ),
                    dict,
                )
                and decision.get(
                    "datos_detectados",
                    {},
                ).get(
                    "confirmacion_calendario_explicita",
                    False,
                )
            )
        )
        

        if tiene_contexto_cita:
            transicion.update({
                "etapa_conversacional": (
                    "ESPERANDO_CONFIRMACION_ADMIN"
                ),
                "estado_comercial": (
                    "CITA_PENDIENTE_CONFIRMACION"
                ),
                "transicion_aplicable": True,
                "motivo": (
                    "La disponibilidad de la visita quedó "
                    "pendiente de confirmación administrativa."
                ),
            })

            agregar_hito(
                "CITA_SOLICITADA"
            )

        else:
            # ------------------------------------------------
            # REVISIÓN ADMINISTRATIVA NO RELACIONADA CON CITA
            # ------------------------------------------------
            #
            # Se conserva la etapa y estado comercial actuales
            # porque indican en qué punto surgió la revisión.
            #
            # Lo que sí cambia es la autoridad del siguiente
            # movimiento: ahora corresponde a administración.
            # ------------------------------------------------

            transicion.update({
                "etapa_conversacional": etapa_actual,
                "estado_comercial": estado_actual,
                "transicion_aplicable": True,
                "motivo": (
                    "La conversación quedó pendiente de "
                    "una decisión administrativa antes de "
                    "continuar el flujo comercial."
                ),
            })

    elif accion == "SEGUIMIENTO":
        transicion.update({
            "etapa_conversacional": (
                "SEGUIMIENTO"
            ),
            "transicion_aplicable": True,
            "motivo": (
                "La conversación quedó temporalmente "
                "en seguimiento."
            ),
        })

    # ========================================================
    # OBJETIVO PENDIENTE DERIVADO DE LA RESPUESTA ENVIADA
    # ========================================================

    if transicion.get(
        "transicion_aplicable",
        False,
    ):
        objetivos_por_accion = {
            "PEDIR_ZONA": (
                "OBTENER_ZONA"
            ),
        
            "PEDIR_REFERENCIA": (
                "OBTENER_REFERENCIA_COLEGIO"
            ),
        
            "PRESENTAR_PROPUESTA_VALOR": (
                "OBTENER_RESPUESTA_METODO"
            ),
        
            "EXPLICAR_METODO_FILADELFIA": (
                "OBTENER_AREA_INTERES"
            ),
        
            "PREGUNTAR_AREA_INTERES": (
                "OBTENER_AREA_INTERES"
            ),
        
            "PROFUNDIZAR_AREA_INTERES": (
                "OBTENER_CONFIRMACION_INTERES"
            ),
        
            "INVITAR_CITA": (
                "OBTENER_DECISION_VISITA"
            ),
        
            "RESPONDER_COSTOS": (
                "OBTENER_DECISION_VISITA"
            ),
        
            "PEDIR_FECHA_CITA": (
                "OBTENER_FECHA_CITA"
            ),
        
            "PEDIR_HORA_CITA": (
                "OBTENER_HORA_CITA"
            ),
        
            "CONFIRMAR_FECHA_CITA": (
                "CONFIRMAR_FECHA_CITA_CALENDARIO"
            ),
        
            "CONSULTAR_ADMIN": (
                "ESPERAR_CONFIRMACION_ADMIN"
            ),
        
            "SEGUIMIENTO": (
                "ESPERAR_REACTIVACION_PROSPECTO"
            ),
            "PEDIR_NIVEL_COSTOS": (
                "OBTENER_NIVEL_PARA_COSTOS"
            ),

        }

        datos_decision = decision.get(
            "datos_detectados",
            {},
        )

        if not isinstance(
            datos_decision,
            dict,
        ):
            datos_decision = {}

        objetivo_pendiente_sugerido = str(
            datos_decision.get(
                "objetivo_pendiente_sugerido",
                "",
            )
            or ""
        ).strip().upper()

        if (
            objetivo_pendiente_sugerido
            and objetivo_pendiente_sugerido
            in OBJETIVOS_PENDIENTES_VALIDOS
        ):
            transicion[
                "objetivo_pendiente"
            ] = objetivo_pendiente_sugerido

        else:
            if accion == "RESPONDER_TEMA":
                transicion[
                    "objetivo_pendiente"
                ] = objetivo_actual

            else:
                transicion[
                    "objetivo_pendiente"
                ] = objetivos_por_accion.get(
                    accion,
                    "",
                )

    # ========================================================
    # VALIDACIÓN FINAL
    # ========================================================

    etapa_resultante = str(
        transicion.get(
            "etapa_conversacional",
            "",
        )
        or ""
    ).strip().upper()

    if (
        etapa_resultante
        not in ETAPAS_CONVERSACIONALES_VALIDAS
    ):
        transicion[
            "etapa_conversacional"
        ] = etapa_actual

        transicion[
            "transicion_aplicable"
        ] = False

    estado_resultante = str(
        transicion.get(
            "estado_comercial",
            "",
        )
        or ""
    ).strip().upper()

    if (
        estado_resultante
        not in ESTADOS_COMERCIALES_VALIDOS
    ):
        transicion[
            "estado_comercial"
        ] = estado_actual

        transicion[
            "transicion_aplicable"
        ] = False

    return transicion

def persistir_transicion_comercial_post_envio(
    db: Session,
    contact,
    resultado: Dict[str, Any],
    contexto_actual: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Persiste la transición comercial calculada DESPUÉS de que
    una respuesta haya sido enviada exitosamente al prospecto.

    IMPORTANTE:
    - Esta función NO envía mensajes.
    - Esta función NO decide si Twilio tuvo éxito.
    - Debe llamarse únicamente después de confirmar envio_exitoso=True.
    - Utiliza calcular_transicion_comercial_post_envio() como
      única fuente para determinar la transición resultante.
    - Persiste etapa conversacional, estado comercial,
      FLOW_STATE compatible e hitos comerciales.
    - Realiza un solo commit al finalizar.
    """

    if contact is None:
        return {
            "persistido": False,
            "transicion_aplicada": False,
            "etapa_conversacional": "",
            "estado_comercial": "",
            "flow_state": "",
            "hitos_comerciales": [],
            "hitos_nuevos": [],
            "campos_actualizados": [],
            "error": "CONTACTO_NO_DISPONIBLE",
        }

    if not isinstance(resultado, dict):
        return {
            "persistido": False,
            "transicion_aplicada": False,
            "etapa_conversacional": "",
            "estado_comercial": "",
            "flow_state": "",
            "hitos_comerciales": [],
            "hitos_nuevos": [],
            "campos_actualizados": [],
            "error": "RESULTADO_INVALIDO",
        }

    contexto = (
        contexto_actual
        if isinstance(contexto_actual, dict)
        else construir_contexto_comercial_desde_contacto(
            contact
        )
    )

    transicion = (
        calcular_transicion_comercial_post_envio(
            resultado=resultado,
            contexto_actual=contexto,
        )
    )

    if not isinstance(transicion, dict):
        return {
            "persistido": False,
            "transicion_aplicada": False,
            "etapa_conversacional": "",
            "estado_comercial": "",
            "flow_state": "",
            "hitos_comerciales": [],
            "hitos_nuevos": [],
            "campos_actualizados": [],
            "error": "TRANSICION_INVALIDA",
        }

    if not transicion.get(
        "transicion_aplicable",
        False,
    ):
        return {
            "persistido": True,
            "transicion_aplicada": False,
            "etapa_conversacional": str(
                transicion.get(
                    "etapa_conversacional",
                    "",
                )
                or ""
            ),
            "estado_comercial": str(
                transicion.get(
                    "estado_comercial",
                    "",
                )
                or ""
            ),
            "flow_state": get_flow_state(contact),
            "hitos_comerciales": (
                transicion.get(
                    "hitos_comerciales",
                    [],
                )
                if isinstance(
                    transicion.get(
                        "hitos_comerciales",
                        [],
                    ),
                    list,
                )
                else []
            ),
            "hitos_nuevos": (
                transicion.get(
                    "hitos_nuevos",
                    [],
                )
                if isinstance(
                    transicion.get(
                        "hitos_nuevos",
                        [],
                    ),
                    list,
                )
                else []
            ),
            "campos_actualizados": [],
            "error": "",
        }

    etapa = str(
        transicion.get(
            "etapa_conversacional",
            "",
        )
        or ""
    ).strip().upper()

    estado = str(
        transicion.get(
            "estado_comercial",
            "",
        )
        or ""
    ).strip().upper()

    objetivo_pendiente = str(
        transicion.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    if (
        objetivo_pendiente
        not in OBJETIVOS_PENDIENTES_VALIDOS
    ):
        objetivo_pendiente = ""

    if etapa not in ETAPAS_CONVERSACIONALES_VALIDAS:
        return {
            "persistido": False,
            "transicion_aplicada": False,
            "etapa_conversacional": etapa,
            "estado_comercial": estado,
            "flow_state": "",
            "hitos_comerciales": [],
            "hitos_nuevos": [],
            "campos_actualizados": [],
            "error": "ETAPA_CONVERSACIONAL_INVALIDA",
        }

    if estado not in ESTADOS_COMERCIALES_VALIDOS:
        return {
            "persistido": False,
            "transicion_aplicada": False,
            "etapa_conversacional": etapa,
            "estado_comercial": estado,
            "flow_state": "",
            "hitos_comerciales": [],
            "hitos_nuevos": [],
            "campos_actualizados": [],
            "error": "ESTADO_COMERCIAL_INVALIDO",
        }

    hitos = transicion.get(
        "hitos_comerciales",
        [],
    )

    if not isinstance(hitos, list):
        hitos = []

    hitos_validos = []

    for hito in hitos:
        hito_normalizado = str(
            hito or ""
        ).strip().upper()

        if (
            hito_normalizado
            in HITOS_COMERCIALES_VALIDOS
            and hito_normalizado
            not in hitos_validos
        ):
            hitos_validos.append(
                hito_normalizado
            )

    hitos_nuevos = transicion.get(
        "hitos_nuevos",
        [],
    )

    if not isinstance(hitos_nuevos, list):
        hitos_nuevos = []

    hitos_nuevos_validos = []

    for hito in hitos_nuevos:
        hito_normalizado = str(
            hito or ""
        ).strip().upper()

        if (
            hito_normalizado
            in HITOS_COMERCIALES_VALIDOS
            and hito_normalizado
            not in hitos_nuevos_validos
        ):
            hitos_nuevos_validos.append(
                hito_normalizado
            )

    equivalencias_etapa_flow_state = {
        "CONTACTO_INICIAL": "SALUDO_INICIAL",
        "REFERENCIA_COLEGIO": "ESPERANDO_REFERENCIA",
        "VALIDACION_ZONA": "VALIDACION_ZONA",
        "PRESENTACION_VALOR": "PRESENTACION_VALOR",
        "EXPLICACION_METODO": "EXPLICACION_METODO",
        "IDENTIFICACION_INTERES": "ESPERANDO_AREA_INTERES",
        "PROFUNDIZACION_INTERES": "PROFUNDIZACION_INTERES",
        "INVITACION_VISITA": "INVITACION_CITA",
        "NEGOCIACION_CITA": "ESPERANDO_FECHA_CITA",
        "ESPERANDO_CONFIRMACION_ADMIN": (
            "ESPERANDO_CONFIRMACION_ADMIN"
        ),
        "ESPERANDO_DATOS_CITA": "ESPERANDO_DATOS_CITA",
        "VISITA_CONFIRMADA": "CITA_DATOS_COMPLETOS",
        "SEGUIMIENTO_VISITA": "SEGUIMIENTO_ACORDADO",
        "POST_VISITA_COSTOS": "SEGUIMIENTO_ACORDADO",
        "SEGUIMIENTO_INSCRIPCION": "SEGUIMIENTO_ACORDADO",
        "CIERRE_INSCRIPCION": "SEGUIMIENTO_ACORDADO",
        "SEGUIMIENTO": "SEGUIMIENTO_ACORDADO",
    }

    flow_state = equivalencias_etapa_flow_state.get(
        etapa,
        get_flow_state(contact),
    )

    if etapa == "NEGOCIACION_CITA":
        if (
            objetivo_pendiente
            == "OBTENER_HORA_CITA"
        ):
            flow_state = "ESPERANDO_HORA_CITA"

        elif (
            objetivo_pendiente
            == "OBTENER_FECHA_CITA"
        ):
            flow_state = "ESPERANDO_FECHA_CITA"

    campos_actualizados = []

    try:

        # ====================================================
        # SNAPSHOT ANTES DE UNA PAUSA TEMPORAL
        # ====================================================
        #
        # SEGUIMIENTO no sustituye la posición comercial.
        # Sólo la pausa.
        #
        # Guardamos el punto anterior para poder restaurarlo
        # cuando el prospecto vuelva a escribir.
        # ====================================================

        accion_transicion = str(
            transicion.get(
                "accion",
                "",
            )
            or ""
        ).strip().upper()

        if accion_transicion == "SEGUIMIENTO":

            etapa_pre_seguimiento = str(
                contexto.get(
                    "etapa_conversacional",
                    "",
                )
                or ""
            ).strip().upper()

            estado_pre_seguimiento = str(
                contexto.get(
                    "estado_comercial",
                    "",
                )
                or ""
            ).strip().upper()

            objetivo_pre_seguimiento = str(
                contexto.get(
                    "objetivo_pendiente",
                    "",
                )
                or ""
            ).strip().upper()

            if (
                etapa_pre_seguimiento
                and etapa_pre_seguimiento
                != "SEGUIMIENTO"
                and etapa_pre_seguimiento
                in ETAPAS_CONVERSACIONALES_VALIDAS
            ):
                set_note_value(
                    contact,
                    "ETAPA_ANTES_SEGUIMIENTO",
                    etapa_pre_seguimiento,
                )

                campos_actualizados.append(
                    "ETAPA_ANTES_SEGUIMIENTO"
                )

            if (
                estado_pre_seguimiento
                and estado_pre_seguimiento
                in ESTADOS_COMERCIALES_VALIDOS
            ):
                set_note_value(
                    contact,
                    "ESTADO_ANTES_SEGUIMIENTO",
                    estado_pre_seguimiento,
                )

                campos_actualizados.append(
                    "ESTADO_ANTES_SEGUIMIENTO"
                )

            if (
                objetivo_pre_seguimiento
                and objetivo_pre_seguimiento
                != "ESPERAR_REACTIVACION_PROSPECTO"
                and objetivo_pre_seguimiento
                in OBJETIVOS_PENDIENTES_VALIDOS
            ):
                set_note_value(
                    contact,
                    "OBJETIVO_ANTES_SEGUIMIENTO",
                    objetivo_pre_seguimiento,
                )

                campos_actualizados.append(
                    "OBJETIVO_ANTES_SEGUIMIENTO"
                )

            print(
                "📸 SNAPSHOT PRE-SEGUIMIENTO: "
                f"contact_id={contact.id}, "
                f"etapa={etapa_pre_seguimiento}, "
                f"estado={estado_pre_seguimiento}, "
                f"objetivo={objetivo_pre_seguimiento}"
            )
    
        etapa_anterior_guardada = get_note_value(
            contact,
            "ETAPA_CONVERSACIONAL",
        )

        if etapa_anterior_guardada != etapa:
            set_note_value(
                contact,
                "ETAPA_CONVERSACIONAL",
                etapa,
            )

            campos_actualizados.append(
                "ETAPA_CONVERSACIONAL"
            )

        estado_anterior = str(
            getattr(
                contact,
                "status",
                "",
            )
            or ""
        ).strip().upper()

        if estado_anterior != estado:
            contact.status = estado

            campos_actualizados.append(
                "contact.status"
            )

        flow_state_anterior = get_flow_state(
            contact
        )

        if flow_state_anterior != flow_state:
            set_flow_state(
                contact,
                flow_state,
            )

            campos_actualizados.append(
                "FLOW_STATE"
            )

        objetivo_anterior = get_note_value(
            contact,
            "OBJETIVO_PENDIENTE",
        ).strip().upper()

        if (
            objetivo_anterior
            != objetivo_pendiente
        ):
            set_note_value(
                contact,
                "OBJETIVO_PENDIENTE",
                objetivo_pendiente,
            )

            campos_actualizados.append(
                "OBJETIVO_PENDIENTE"
            )

        hitos_texto = json.dumps(
            hitos_validos,
            ensure_ascii=False,
        )

        hitos_anteriores_texto = get_note_value(
            contact,
            "HITOS_COMERCIALES",
        )

        if hitos_anteriores_texto != hitos_texto:
            set_note_value(
                contact,
                "HITOS_COMERCIALES",
                hitos_texto,
            )

            campos_actualizados.append(
                "HITOS_COMERCIALES"
            )

        db.commit()
        db.refresh(contact)

        return {
            "persistido": True,
            "transicion_aplicada": True,
            "accion": str(
                transicion.get(
                    "accion",
                    "",
                )
                or ""
            ),
            "etapa_conversacional": etapa,
            "estado_comercial": estado,
            "objetivo_pendiente": objetivo_pendiente,
            "flow_state": flow_state,
            "hitos_comerciales": hitos_validos,
            "hitos_nuevos": hitos_nuevos_validos,
            "campos_actualizados": (
                campos_actualizados
            ),
            "motivo": str(
                transicion.get(
                    "motivo",
                    "",
                )
                or ""
            ),
            "error": "",
        }

    except Exception as e:
        db.rollback()

        print(
            "⚠️ Error persistiendo transición "
            "comercial post-envío: "
            f"{e}"
        )

        return {
            "persistido": False,
            "transicion_aplicada": False,
            "accion": str(
                transicion.get(
                    "accion",
                    "",
                )
                or ""
            ),
            "etapa_conversacional": etapa,
            "estado_comercial": estado,
            "flow_state": flow_state,
            "hitos_comerciales": hitos_validos,
            "hitos_nuevos": hitos_nuevos_validos,
            "campos_actualizados": [],
            "error": str(e),
        }

# ============================================================
# PERSISTENCIA DEL NUEVO FLUJO ESTRUCTURADO
# ============================================================

def persistir_resultado_estructurado(
    db: Session,
    contact,
    resultado: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Guarda en contact.notes los datos útiles detectados por el
    nuevo flujo estructurado.

    Principios:
    - Guarda únicamente valores no vacíos.
    - No borra información previa válida.
    - No guarda mensajes de conversación.
    - No envía mensajes por Twilio.
    - No crea tareas administrativas.
    - No modifica todavía FLOW_STATE.
    - Realiza un solo commit al finalizar.
    """
    if contact is None:
        return {
            "persistido": False,
            "campos_actualizados": [],
            "error": "CONTACTO_NO_DISPONIBLE",
        }

    if not isinstance(resultado, dict):
        return {
            "persistido": False,
            "campos_actualizados": [],
            "error": "RESULTADO_INVALIDO",
        }

    analisis = resultado.get("analisis") or {}
    decision = resultado.get("decision") or {}

    if not isinstance(analisis, dict):
        analisis = {}

    if not isinstance(decision, dict):
        decision = {}

    campos_actualizados = []

    def guardar_valor(
        clave: str,
        valor: Any,
    ) -> None:
        """
        Guarda un valor únicamente cuando contiene información útil.
        """
        if valor is None:
            return

        if isinstance(valor, bool):
            valor_texto = "true" if valor else "false"
        else:
            valor_texto = str(valor).strip()

        if not valor_texto:
            return

        valor_anterior = get_note_value(
            contact,
            clave,
        )

        if valor_anterior == valor_texto:
            return

        set_note_value(
            contact,
            clave,
            valor_texto,
        )

        campos_actualizados.append(clave)

    try:
        # ----------------------------------------------------
        # DATOS DEL PROSPECTO Y DEL ALUMNO
        # ----------------------------------------------------

        guardar_valor(
            "ZONA_INTERES",
            analisis.get("zona_mencionada"),
        )

        guardar_valor(
            "NIVEL_INTERES",
            analisis.get("nivel"),
        )

        guardar_valor(
            "GRADO_INTERES",
            analisis.get("grado"),
        )

        guardar_valor(
            "EDAD_ALUMNO",
            analisis.get("edad_alumno"),
        )

        guardar_valor(
            "FECHA_NACIMIENTO",
            analisis.get("fecha_nacimiento_texto"),
        )
        
        guardar_valor(
            "FECHA_NACIMIENTO_ISO",
            analisis.get("fecha_nacimiento_iso"),
        )

        guardar_valor(
            "NIVEL_ACTUAL",
            analisis.get("nivel_actual"),
        )

        guardar_valor(
            "ULTIMO_GRADO_CURSADO",
            analisis.get("ultimo_grado_cursado"),
        )

        guardar_valor(
            "GRADO_SOLICITADO",
            analisis.get("grado_solicitado"),
        )

        clasificacion_edad = (
            decision.get("datos_detectados", {})
            .get("clasificacion_edad", {})
        )

        if isinstance(clasificacion_edad, dict):
            guardar_valor(
                "CLASIFICACION_EDAD",
                clasificacion_edad.get(
                    "clasificacion"
                ),
            )

            guardar_valor(
                "EDAD_AL_CORTE",
                clasificacion_edad.get(
                    "edad_al_corte"
                ),
            )

            guardar_valor(
                "FECHA_CORTE_ESCOLAR",
                clasificacion_edad.get(
                    "fecha_corte"
                ),
            )

        guardar_valor(
            "NOMBRE_TUTOR",
            analisis.get("nombre_tutor"),
        )

        # ----------------------------------------------------
        # UNO O VARIOS ALUMNOS DEL CONTEXTO COMERCIAL
        # ----------------------------------------------------

        alumnos_analisis = analisis.get(
            "alumnos",
            [],
        )

        if not isinstance(
            alumnos_analisis,
            list,
        ):
            alumnos_analisis = []

        alumnos_para_persistir = []

        for alumno_analisis in alumnos_analisis:

            if not isinstance(
                alumno_analisis,
                dict,
            ):
                continue

            nombre_alumno = str(
                alumno_analisis.get(
                    "nombre",
                    "",
                )
                or ""
            ).strip()

            nivel_alumno = str(
                alumno_analisis.get(
                    "nivel",
                    "",
                )
                or ""
            ).strip()

            grado_alumno = str(
                alumno_analisis.get(
                    "grado",
                    "",
                )
                or ""
            ).strip()

            edad_alumno = (
                alumno_analisis.get(
                    "edad"
                )
            )

            fecha_nacimiento_alumno = str(
                alumno_analisis.get(
                    "fecha_nacimiento",
                    "",
                )
                or ""
            ).strip()

            if not any(
                [
                    nombre_alumno,
                    nivel_alumno,
                    grado_alumno,
                    edad_alumno is not None,
                    fecha_nacimiento_alumno,
                ]
            ):
                continue

            alumnos_para_persistir.append({
                "nombre": nombre_alumno,
                "nivel_interes": nivel_alumno,
                "grado_interes": grado_alumno,
                "edad": edad_alumno,
                "fecha_nacimiento": (
                    fecha_nacimiento_alumno
                ),
            })

        if alumnos_para_persistir:

            guardar_valor(
                "ALUMNOS_ESTRUCTURADOS",
                json.dumps(
                    alumnos_para_persistir,
                    ensure_ascii=False,
                ),
            )

            # Compatibilidad con el código anterior.
            primer_alumno = (
                alumnos_para_persistir[0]
            )

            if primer_alumno.get("nombre"):
                guardar_valor(
                    "NOMBRE_ALUMNO",
                    primer_alumno.get(
                        "nombre"
                    ),
                )

        elif analisis.get("nombre_alumno"):

            # Compatibilidad defensiva con resultados
            # estructurados anteriores.
            guardar_valor(
                "NOMBRE_ALUMNO",
                analisis.get(
                    "nombre_alumno"
                ),
            )

        # ----------------------------------------------------
        # DATOS DE CITA
        # ----------------------------------------------------

        guardar_valor(
            "FECHA_CITA",
            analisis.get("fecha_cita_iso")
            or analisis.get("fecha_cita_texto"),
        )

        guardar_valor(
            "HORA_CITA",
            analisis.get("hora_cita_24h")
            or analisis.get("hora_cita_texto"),
        )

        # ----------------------------------------------------
        # DATOS DE ZONA Y CAMPUS
        # ----------------------------------------------------

        if decision.get("zona_validada") is True:
            guardar_valor(
                "ZONA_VALIDADA",
                True,
            )

            zona_validada_actual = str(
                analisis.get(
                    "zona_mencionada",
                    "",
                )
                or get_note_value(
                    contact,
                    "ZONA_INTERES",
                )
                or ""
            ).strip()

            if zona_validada_actual:

                guardar_valor(
                    "ZONA_VALIDADA_AUTORITATIVA",
                    zona_validada_actual,
                )

        if analisis.get("campus_externo") is True:
            guardar_valor(
                "CAMPUS_EXTERNO",
                True,
            )

        if analisis.get(
            "requiere_validar_pre_kinder"
        ) is True:
            guardar_valor(
                "REQUIERE_VALIDAR_PRE_KINDER",
                True,
            )

        # ----------------------------------------------------
        # AUDITORÍA DEL NUEVO FLUJO
        # ----------------------------------------------------

        guardar_valor(
            "ULTIMA_INTENCION_ESTRUCTURADA",
            analisis.get("intencion_principal"),
        )

        guardar_valor(
            "ULTIMA_ACCION_ESTRUCTURADA",
            decision.get("accion"),
        )

        confianza = analisis.get("confianza")

        if confianza is not None:
            guardar_valor(
                "ULTIMA_CONFIANZA_IA",
                confianza,
            )

        if campos_actualizados:
            db.commit()
            db.refresh(contact)

        return {
            "persistido": True,
            "campos_actualizados": campos_actualizados,
            "error": "",
        }

    except Exception as e:
        db.rollback()

        print(
            "⚠️ Error persistiendo resultado estructurado: "
            f"{e}"
        )

        return {
            "persistido": False,
            "campos_actualizados": [],
            "error": str(e),
        }

def es_zona_valida(mensaje: str) -> bool:
    msg = (mensaje or "").lower()
    zonas_validas = [
        "santiago", "tianguistenco", "santa cruz", "san pedro",
        "tlaltizapan", "tlaltizapán", "xalatlaco", "almoloya",
        "buen suceso", "capulhuac"
    ]
    return any(z in msg for z in zonas_validas)


def es_zona_invalida_probable(mensaje: str) -> bool:
    msg = (mensaje or "").lower()
    zonas_invalidas = ["metepec", "toluca"]
    return any(z in msg for z in zonas_invalidas)

def detecta_campus_externo(mensaje: str) -> bool:
    """
    Detecta cuando el usuario está buscando información de otro campus
    y no del Campus Santa Cruz Atizapán.
    """
    msg = (mensaje or "").lower().strip()

    frases = [
        "campus metepec",
        "del campus metepec",
        "de campus metepec",
        "teléfono del campus metepec",
        "telefono del campus metepec",
        "número del campus metepec",
        "numero del campus metepec",
        "contacto del campus metepec",
        "información del campus metepec",
        "informacion del campus metepec",
        "quiero informes de metepec",
        "quiero informes del campus metepec",
        "otro campus",
        "de otro campus"
    ]

    return any(frase in msg for frase in frases)

def clasificar_alcance_campus_con_ia(mensaje_usuario: str, history=None) -> str:
    """
    Usa IA para detectar si el usuario busca Campus Santa Cruz Atizapán
    o información de otro campus / zona no atendida.
    Devuelve: SANTA_CRUZ, OTRO_CAMPUS, FUERA_DE_ZONA, AMBIGUO
    """
    msg = (mensaje_usuario or "").strip()

    if not GEMINI_API_KEY:
        if detecta_campus_externo(msg) or es_zona_invalida_probable(msg):
            return "OTRO_CAMPUS"
        return "AMBIGUO"

    historial_lista = []
    if history:
        for item in history[-4:]:
            prefijo = "Usuario" if item.direction == "incoming" else "Asistente"
            historial_lista.append(f"{prefijo}: {item.content}")

    historial_texto = "\n".join(historial_lista) if historial_lista else "Sin historial reciente."

    prompt = f"""
Eres un clasificador estricto para un bot de WhatsApp del Colegio Valle de Filadelfia Campus Santa Cruz Atizapán.

CONTEXTO:
Este canal atiende únicamente al Campus Santa Cruz Atizapán.

ZONAS/CAMPUS QUE SÍ CORRESPONDEN:
- Santa Cruz Atizapán
- Santiago Tianguistenco
- Tianguistenco
- Capulhuac
- Xalatlaco
- Almoloya
- San Pedro
- Buen Suceso
- zonas cercanas al Campus Santa Cruz Atizapán

ZONAS/CAMPUS QUE NO DEBEN ATENDERSE EN ESTE CANAL:
- Metepec
- Toluca
- Atlacomulco
- cualquier otro campus
- cualquier zona donde el usuario claramente busca otra sede

HISTORIAL RECIENTE:
{historial_texto}

MENSAJE ACTUAL DEL USUARIO:
{msg}

TAREA:
Clasifica la intención del usuario en UNA sola etiqueta.

ETIQUETAS VÁLIDAS:
SANTA_CRUZ
OTRO_CAMPUS
FUERA_DE_ZONA
AMBIGUO

REGLAS:
- Si el usuario dice que busca Metepec, Toluca, Atlacomulco u otro campus, responde OTRO_CAMPUS.
- Si el usuario pide número, contacto, costos, dirección u horarios de otro campus, responde OTRO_CAMPUS.
- Si el usuario sólo menciona una zona lejana o no atendida, responde FUERA_DE_ZONA.
- Si el usuario busca Santa Cruz Atizapán o zonas cercanas, responde SANTA_CRUZ.
- Si no queda claro, responde AMBIGUO.
- Responde únicamente la etiqueta. No expliques nada.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.0,
            ),
            tarea="clasificación de alcance campus"
        )

        texto_respuesta = extraer_texto_respuesta_gemini(response)
        etiqueta = texto_respuesta.strip().upper()

        if etiqueta in ["SANTA_CRUZ", "OTRO_CAMPUS", "FUERA_DE_ZONA", "AMBIGUO"]:
            print(f"🏫 Alcance campus IA: {etiqueta} usando {modelo_usado}")
            return etiqueta

        return "AMBIGUO"

    except Exception as e:
        print(f"⚠️ Error clasificando alcance campus con IA: {e}")

        if detecta_campus_externo(msg) or es_zona_invalida_probable(msg):
            return "OTRO_CAMPUS"

        return "AMBIGUO"

def detecta_tema_interes_simple(mensaje: str) -> bool:
    msg = (mensaje or "").lower()
    temas = [
        "matem", "lógico", "logico", "artist", "musical", "motriz",
        "fisic", "ligamentos", "articulaciones", "emoc", "lectura",
        "leer", "pantalla", "ipad", "knotion"
    ]
    return any(t in msg for t in temas)


def detecta_costos(mensaje: str) -> bool:
    msg = (mensaje or "").lower()
    terminos = ["costo", "costos", "precio", "precios", "colegiatura", "colegiaturas", "inscripción", "inscripcion"]
    return any(t in msg for t in terminos)

def requiere_validacion_zona_antes_de_informes(estado_actual: str, mensaje_usuario: str, history=None) -> bool:
    """
    Bloquea información comercial sensible si todavía no se validó zona.
    Pero si el mismo mensaje ya incluye una zona válida, NO bloquea.
    """
    estados_previos_a_zona = [
        "ESPERANDO_INTENCION",
        "ESPERANDO_REFERENCIA",
        "VALIDACION_ZONA"
    ]

    if estado_actual not in estados_previos_a_zona:
        return False

    if not detecta_costos(mensaje_usuario):
        return False

    # Si el mismo mensaje ya trae una zona válida por regla rápida, no bloqueamos.
    if es_zona_valida(mensaje_usuario):
        return False

    # Si el mismo mensaje trae una zona externa o inválida, tampoco debe pasar a costos;
    # se manejará por el clasificador de campus / zona.
    if detecta_campus_externo(mensaje_usuario) or es_zona_invalida_probable(mensaje_usuario):
        return False

    # Si no hay zona clara, bloqueamos costos hasta validar ubicación.
    return True
def detectar_nivel_interes(mensaje: str) -> str:
    """
    Detecta el nivel o grado de interés mencionado por el prospecto.
    """
    msg = (mensaje or "").lower().strip()

    if any(x in msg for x in ["preescolar 2", "preescolar ii", "kinder 2", "kínder 2", "k2"]):
        return "Preescolar 2"

    if any(x in msg for x in ["preescolar 1", "preescolar i", "kinder 1", "kínder 1", "k1"]):
        return "Preescolar 1"

    if any(x in msg for x in ["preescolar 3", "preescolar iii", "kinder 3", "kínder 3", "k3"]):
        return "Preescolar 3"

    if "preescolar" in msg or "kinder" in msg or "kínder" in msg:
        return "Preescolar"

    if "primaria" in msg:
        return "Primaria"

    if "secundaria" in msg:
        return "Secundaria"

    return ""

def es_saludo_simple(mensaje: str) -> bool:
    """
    Detecta si el mensaje es únicamente un saludo simple.
    Tolera signos de puntuación como coma, punto, signos de admiración o pregunta.
    """
    msg = (mensaje or "").lower().strip()

    for caracter in [",", ".", "!", "¡", "?", "¿", ":", ";"]:
        msg = msg.replace(caracter, " ")

    msg = " ".join(msg.split())

    saludos = [
        "hola",
        "buenos días",
        "buenos dias",
        "buen día",
        "buen dia",
        "buenas tardes",
        "buenas noches",
        "hola buenos días",
        "hola buenos dias",
        "hola buen día",
        "hola buen dia",
        "hola buenas tardes",
        "hola buenas noches",
        "qué tal",
        "que tal"
    ]

    return msg in saludos    

def generar_saludo_inicial_contextual(mensaje: str) -> str:
    """
    Fallback simple para saludo inicial.
    La naturalidad principal del saludo se controla desde reglas_base.txt.
    """
    msg = (mensaje or "").lower().strip()

    if "buenos días" in msg or "buen dia" in msg or "buen día" in msg:
        return """Buenos días.

¿En qué podemos ayudarle?"""

    if "buenas tardes" in msg:
        return """Buenas tardes.

¿En qué podemos ayudarle?"""

    if "buenas noches" in msg:
        return """Buenas noches.

¿En qué podemos ayudarle?"""

    return """¡Hola!

¿En qué podemos ayudarle?"""

def es_saludo_repetido_temprano(estado_actual: str, mensaje: str) -> bool:
    """
    Detecta saludos simples enviados después del primer saludo,
    cuando el prospecto todavía no ha expresado una intención real.
    """
    if not es_saludo_simple(mensaje):
        return False

    return estado_actual in [
        "ESPERANDO_INTENCION",
        "ESPERANDO_REFERENCIA"
    ]


def responder_saludo_repetido_temprano(db: Session, contact, from_number: str, mensaje_usuario: str):
    """
    Responde un segundo saludo sin avanzar el embudo ni mandar a Gemini.
    """
    respuesta = generar_saludo_inicial_contextual(mensaje_usuario)

    resultado = enviar_respuesta_twilio(from_number, respuesta)

    twilio_sid = None
    if "SID:" in resultado:
        twilio_sid = resultado.split("SID: ")[1].strip()

    save_message(db, contact.id, "outgoing", respuesta, twilio_sid)

    # Dejamos al prospecto en espera de intención real.
    set_flow_state(contact, "ESPERANDO_INTENCION")
    db.commit()

    print("👋 Saludo repetido temprano detectado; no se avanzó el embudo")
    print(f"🤖 BOT: {respuesta}")
    print(f"📤 Estado: {resultado}")

    return {
        "status": "saludo_repetido_temprano",
        "contact_id": contact.id
    }

def detecta_intencion_cita(mensaje: str) -> bool:
    msg = (mensaje or "").lower().strip()
    terminos = [
        "cita", "visita", "agendar", "agendo", "quiero ir", "quiero conocer",
        "sí quiero", "si quiero", "sí", "si", "claro", "perfecto", "excelente",
        "me interesa", "quiero la cita", "quiero agendar"
    ]
    return any(t in msg for t in terminos)

def obtener_fecha_relativa_cita(mensaje: str):
    """
    Detecta referencias simples de fecha para cita:
    - hoy
    - mañana
    - lunes, martes, miércoles, jueves, viernes, sábado, domingo
    Devuelve un date o None.
    """
    msg = normalizar_texto_para_deteccion(mensaje)

    hoy = datetime.now(LOCAL_TZ).date()

    if "manana" in msg:
        return hoy + timedelta(days=1)

    if "hoy" in msg:
        return hoy

    dias = {
        "lunes": 0,
        "martes": 1,
        "miercoles": 2,
        "jueves": 3,
        "viernes": 4,
        "sabado": 5,
        "domingo": 6
    }

    for nombre_dia, weekday_objetivo in dias.items():
        if nombre_dia in msg:
            diferencia = (weekday_objetivo - hoy.weekday()) % 7

            # Si dice el mismo día de la semana, asumimos hoy.
            fecha_objetivo = hoy + timedelta(days=diferencia)
            return fecha_objetivo

    return None


def es_dia_laborable_para_visitas(fecha) -> bool:
    """
    El colegio recibe visitas de lunes a viernes.
    weekday(): lunes=0, domingo=6
    """
    if not fecha:
        return True

    return fecha.weekday() in [0, 1, 2, 3, 4]


def esta_en_contexto_de_agendar_cita(history=None) -> bool:
    """
    Detecta si la conversación reciente está en etapa de agendar visita.
    """
    if not history:
        return False

    textos = []

    for item in history[-6:]:
        contenido = (item.content or "").lower()
        textos.append(contenido)

    historial = "\n".join(textos)

    frases = [
        "agendar una visita",
        "agendamos su visita",
        "en qué día y hora",
        "que día y hora",
        "qué día y hora",
        "horario le gustaría asistir",
        "validaré la disponibilidad",
        "verificar si le podemos atender",
        "consultamos la disponibilidad",
        "directora de primaria",
        "directora de preescolar",
        "directora de secundaria"
    ]

    return any(frase in historial for frase in frases)


def detecta_propuesta_cita_en_dia_no_laboral(mensaje_usuario: str, history=None) -> bool:
    """
    Detecta cuando el prospecto propone una cita en sábado o domingo.
    """
    if not esta_en_contexto_de_agendar_cita(history):
        return False

    fecha = obtener_fecha_relativa_cita(mensaje_usuario)

    if not fecha:
        return False

    return not es_dia_laborable_para_visitas(fecha)
    

def detecta_pausa_o_cierre(mensaje: str) -> bool:
    """
    Detecta cuando el prospecto quiere pausar, revisar la información
    o cerrar temporalmente la conversación sin avanzar a cita.
    """
    msg = (mensaje or "").lower().strip()

    frases = [
        "no por el momento",
        "por el momento no",
        "lo reviso",
        "lo checo",
        "lo consulto",
        "lo platico",
        "lo veo con mi esposo",
        "lo veo con mi esposa",
        "lo reviso con mi esposo",
        "lo reviso con mi esposa",
        "lo consulto con mi esposo",
        "lo consulto con mi esposa",
        "lo revisamos en familia",
        "lo voy a pensar",
        "lo pensamos",
        "después les aviso",
        "despues les aviso",
        "después le aviso",
        "despues le aviso",
        "luego les aviso",
        "luego le aviso",
        "yo les aviso",
        "yo le aviso",
        "más adelante",
        "mas adelante",
        "ahorita no",
        "por ahora no",
        "necesito consultarlo con mi esposo",
        "necesito consultarlo con mi esposa",
        "tengo que consultarlo con mi esposo",
        "tengo que consultarlo con mi esposa",
        "voy a consultarlo con mi esposo",
        "voy a consultarlo con mi esposa",
        "quiero consultarlo con mi esposo",
        "quiero consultarlo con mi esposa",
        "debo consultarlo con mi esposo",
        "debo consultarlo con mi esposa",
    ]

    return any(frase in msg for frase in frases)

def detecta_condicion_consulta_admin(respuesta_bot: str) -> bool:
    """
    Detecta cuando el bot dejó una conversación en espera de confirmación humana,
    por ejemplo para validar disponibilidad de cita.
    """
    texto = (respuesta_bot or "").lower().strip()

    frases = [
        "permítame verificar",
        "permitame verificar",
        "consultamos la disponibilidad",
        "estamos consultando la disponibilidad",
        "consultar la disponibilidad",
        "verificar si le podemos atender",
        "en breve le confirmo",
        "en breve le confirmamos",
        "le pido un momento",
        "mientras consultamos",
        "queda pendiente de confirmación",
        "pendiente de confirmación"
    ]

    return any(frase in texto for frase in frases)



def es_numero_admin(from_number: str) -> bool:
    """
    Identifica si el mensaje entrante viene del administrador.
    Este número nunca debe entrar al flujo normal del bot.
    """
    remitente = normalizar_numero_whatsapp(from_number)

    admins = [
        "+5215546080064",
        "+525546080064"
    ]

    admin_env = normalizar_numero_whatsapp(os.getenv("ADMIN_WHATSAPP_NUMBER", ""))
    if admin_env:
        admins.append(admin_env)

    return remitente in admins

def determinar_estado_respuesta(estado_actual: str, mensaje_usuario: str, history=None) -> str:
    """
    Define con qué estado se debe RESPONDER el mensaje actual.
    """
    msg = (mensaje_usuario or "").lower().strip()

    # ===== CITA EN DÍA NO LABORABLE =====
    # Si el prospecto propone sábado/domingo, no se manda a admin.
    # Se responde directamente pidiendo una opción de lunes a viernes.
    if detecta_propuesta_cita_en_dia_no_laboral(mensaje_usuario, history):
        return "CITA_DIA_NO_LABORAL"

    # ===== BLOQUEO COMERCIAL HASTA VALIDAR ZONA =====
    # Si el prospecto pregunta colegiatura/costos antes de confirmar zona,
    # no se entrega información comercial ni se pregunta nivel todavía.
    if requiere_validacion_zona_antes_de_informes(estado_actual, mensaje_usuario, history):
        return "VALIDACION_ZONA_OBLIGATORIA"

        # ===== SALUDO INICIAL PURO =====
    # Si el primer mensaje es sólo un saludo, no debe pasar por IA ni avanzar el embudo.
    if estado_actual == "SALUDO_INICIAL" and es_saludo_simple(mensaje_usuario):
        return "SALUDO_INICIAL"

    # ===== CAMPUS EXTERNO / NO ATENDIBLE =====
    # Primero usamos reglas rápidas; después IA para interpretar variantes.
    if detecta_campus_externo(mensaje_usuario):
        return "CAMPUS_EXTERNO_NO_ATENDIBLE"
    
    clasificacion_campus = clasificar_alcance_campus_con_ia(
        mensaje_usuario=mensaje_usuario,
        history=history or []
    )
        
    if clasificacion_campus == "OTRO_CAMPUS":
        return "CAMPUS_EXTERNO_NO_ATENDIBLE"
    
    if clasificacion_campus == "FUERA_DE_ZONA":
        return "ZONA_INVALIDA_POTENCIAL_METEPEC"

    # ===== PAUSA / CIERRE GLOBAL =====
    # Si el prospecto pausa la conversación en una etapa avanzada,
    # se responde con cierre contextual y sin pregunta.
    if detecta_pausa_o_cierre(mensaje_usuario):
        if estado_actual not in [
            "SALUDO_INICIAL",
            "ESPERANDO_INTENCION",
            "ESPERANDO_REFERENCIA",
            "VALIDACION_ZONA"
        ]:
            return "SEGUIMIENTO_ACORDADO"

    # ===== ETAPAS TEMPRANAS DEL EMBUDO =====
    if estado_actual == "SALUDO_INICIAL":
        if es_saludo_simple(mensaje_usuario):
            return "SALUDO_INICIAL"
    
        # Si el primer mensaje ya trae intención, no responder sólo saludo.
        # Avanzamos directo a preguntar referencia.
        return "ESPERANDO_INTENCION"

    if estado_actual == "ESPERANDO_INTENCION":
        # Aunque pregunte costo desde el inicio, primero se fuerza referencia.
        return "ESPERANDO_REFERENCIA"

    if estado_actual == "ESPERANDO_REFERENCIA":
        return "VALIDACION_ZONA"

    # ===== VALIDACIÓN DE ZONA CON IA =====
    if estado_actual == "VALIDACION_ZONA":
        clasificacion = clasificar_intencion_en_estado(
            estado_actual=estado_actual,
            mensaje_usuario=mensaje_usuario,
            history=history or [],
        )
    
        if clasificacion == "ZONA_VALIDA":
            # Si el usuario ya dio zona válida y además preguntó costos,
            # no lo mandamos al discurso de método; respondemos sobre costos.
            if detecta_costos(mensaje_usuario):
                return "COSTOS_EN_ETAPA_AVANZADA"
    
            return "RESPUESTA_SOBRE_METODO"
    
        if clasificacion == "ZONA_INVALIDA":
            return "ZONA_INVALIDA_POTENCIAL_METEPEC"
    
        if clasificacion in ["ZONA_DUDOSA", "AMBIGUO"]:
            return "VALIDACION_ZONA"
        
    # ===== DESPUÉS DEL TEMA =====
    if estado_actual == "DESPUES_DEL_TEMA":
        clasificacion = clasificar_intencion_en_estado(
            estado_actual=estado_actual,
            mensaje_usuario=mensaje_usuario,
            history=history or [],
        )
    
        if clasificacion == "PIDE_COSTOS":
            return "COSTOS_EN_ETAPA_AVANZADA"
    
        if clasificacion == "ACEPTA_CITA":
            return "ESPERANDO_PROPUESTA_CITA"
    
        if clasificacion == "ACUERDO_SEGUIMIENTO":
            return "SEGUIMIENTO_ACORDADO"
    
        if clasificacion in ["REACCION_POSITIVA", "AMBIGUO"]:
            return "INVITACION_CITA"

    # ===== INVITACIÓN A CITA =====
    if estado_actual == "INVITACION_CITA":
        clasificacion = clasificar_intencion_en_estado(
            estado_actual=estado_actual,
            mensaje_usuario=mensaje_usuario,
            history=history or [],
        )
    
        if clasificacion == "ACEPTA_CITA":
            return "ESPERANDO_PROPUESTA_CITA"
    
        if clasificacion == "PIDE_COSTOS":
            return "INSISTE_COSTOS_ANTES_DE_AGENDAR"
    
        if clasificacion == "ACUERDO_SEGUIMIENTO":
            return "SEGUIMIENTO_ACORDADO"
    
        if clasificacion in ["DUDA", "AMBIGUO"]:
            return "INVITACION_CITA"

    return estado_actual

def clasificar_intencion_en_estado(
    estado_actual: str,
    mensaje_usuario: str,
    history,
) -> str:
    """
    Usa Gemini para clasificar la intención del usuario dentro de un estado ya definido.
    Devuelve una etiqueta corta controlada.
    """

    msg = (mensaje_usuario or "").strip()

    # Fallback rápido si no hay Gemini
    if not GEMINI_API_KEY:
        return clasificar_intencion_en_estado_fallback(estado_actual, msg)

    # Historial breve para contexto
    historial_lista = []
    if history:
        for item in history[-4:]:
            prefijo = "Usuario" if item.direction == "incoming" else "Asistente"
            historial_lista.append(f"{prefijo}: {item.content}")
    historial_texto = "\n".join(historial_lista) if historial_lista else "Sin historial reciente."

    # Etiquetas válidas por estado
    if estado_actual == "INVITACION_CITA":
        etiquetas_validas = [
            "ACEPTA_CITA",
            "PIDE_COSTOS",
            "DUDA",
            "ACUERDO_SEGUIMIENTO",
            "AMBIGUO",
        ]
    elif estado_actual == "DESPUES_DEL_TEMA":
        etiquetas_validas = [
            "ACEPTA_CITA",
            "PIDE_COSTOS",
            "REACCION_POSITIVA",
            "ACUERDO_SEGUIMIENTO",
            "AMBIGUO",
        ]
    elif estado_actual == "VALIDACION_ZONA":
        etiquetas_validas = [
            "ZONA_VALIDA",
            "ZONA_INVALIDA",
            "ZONA_DUDOSA",
            "AMBIGUO",
        ]
    else:
        return clasificar_intencion_en_estado_fallback(estado_actual, msg)

    prompt_clasificacion = f"""
Eres un clasificador estricto de intención conversacional.

ESTADO ACTUAL:
{estado_actual}

HISTORIAL RECIENTE:
{historial_texto}

MENSAJE DEL USUARIO:
{msg}

TAREA:
Clasifica el mensaje en UNA sola etiqueta válida.

REGLA ESPECIAL:
Si el usuario indica que revisará la información con otra persona (esposo, familia, etc.), 
que lo pensará, o que retomará después, clasifica como:
ACUERDO_SEGUIMIENTO

ETIQUETAS VÁLIDAS:
{", ".join(etiquetas_validas)}

REGLAS:
- Responde únicamente con una etiqueta.
- No expliques nada.
- No agregues puntuación.
- Si el estado actual es VALIDACION_ZONA y el usuario menciona una zona válida, clasifica como ZONA_VALIDA aunque también pregunte por costos, inscripción, colegiatura, nivel educativo o cita.
- Si el usuario menciona Capulhuac, Tlazala, Almaya, Santiago Tianguistenco, Tianguistenco, Xalatlaco, Almoloya, San Pedro, Buen Suceso o Santa Cruz Atizapán, clasifica como ZONA_VALIDA.
- Si no está claro, responde AMBIGUO.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt_clasificacion,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
            ),
            tarea="clasificación de intención"
        )
        
        print(f"🏷️ Modelo usado para clasificación: {modelo_usado}")

        etiqueta = ""

        try:
            if hasattr(response, "text") and response.text:
                etiqueta = response.text.strip().upper()
            else:
                parts = response.candidates[0].content.parts
                texto = "".join(
                    part.text for part in parts
                    if hasattr(part, "text") and part.text
                )
                etiqueta = texto.strip().upper()
        except Exception:
            etiqueta = ""

        if etiqueta in etiquetas_validas:
            return etiqueta

        return "AMBIGUO"

    except Exception as e:
        print(f"⚠️ Error clasificando intención con IA: {e}")
        return clasificar_intencion_en_estado_fallback(estado_actual, msg)

def clasificar_intencion_en_estado_fallback(estado_actual: str, mensaje_usuario: str) -> str:
    """
    Respaldo simple por keywords.
    """
    msg = (mensaje_usuario or "").lower().strip()

    if estado_actual == "INVITACION_CITA":
        if any(x in msg for x in [
            "lo reviso con mi esposo", "lo reviso con mi esposa",
            "lo veo con mi esposo", "lo veo con mi esposa",
            "lo consulto con mi esposo", "lo consulto con mi esposa",
            "lo platico con mi esposo", "lo platico con mi esposa",
            "lo revisamos y le avisamos", "después le escribo",
            "yo le aviso", "luego le aviso", "luego le escribo",
            "cuando lo revise le escribo", "cuando lo vea le escribo"
        ]):
            return "ACUERDO_SEGUIMIENTO"
        if any(x in msg for x in ["sí", "si", "claro", "excelente", "perfecto", "me interesa", "quiero", "agendar", "visita", "cita"]):
            return "ACEPTA_CITA"
        if detecta_costos(msg):
            return "PIDE_COSTOS"
        if any(x in msg for x in ["déjeme pensar", "lo voy a revisar", "más adelante", "no sé", "tal vez"]):
            return "DUDA"
        return "AMBIGUO"

    if estado_actual == "DESPUES_DEL_TEMA":
        if any(x in msg for x in [
            "lo reviso con mi esposo", "lo reviso con mi esposa",
            "lo veo con mi esposo", "lo veo con mi esposa",
            "lo consulto con mi esposo", "lo consulto con mi esposa",
            "lo platico con mi esposo", "lo platico con mi esposa",
            "lo revisamos y le avisamos", "después le escribo",
            "yo le aviso", "luego le aviso", "luego le escribo",
            "cuando lo revise le escribo", "cuando lo vea le escribo"
        ]):
            return "ACUERDO_SEGUIMIENTO"
        if detecta_costos(msg):
            return "PIDE_COSTOS"
        if any(x in msg for x in ["sí", "si", "claro", "excelente", "perfecto", "me interesa", "quiero", "agendar", "visita", "cita"]):
            return "ACEPTA_CITA"
        if any(x in msg for x in ["bien", "muy bien", "interesante", "excelente", "me gusta"]):
            return "REACCION_POSITIVA"
        return "AMBIGUO"

    if estado_actual == "VALIDACION_ZONA":
        if es_zona_valida(msg):
            return "ZONA_VALIDA"
        if es_zona_invalida_probable(msg):
            return "ZONA_INVALIDA"
        if any(x in msg for x in ["cerca", "como a 15 minutos", "por la zona", "no ubico", "colonia", "pueblo"]):
            return "ZONA_DUDOSA"
        return "AMBIGUO"

    return "AMBIGUO"

def determinar_estado_siguiente(estado_actual: str, mensaje_usuario: str) -> str:
    """
    Define el estado en el que quedará la conversación DESPUÉS de enviar
    la respuesta correspondiente al estado_actual.
    """
    if estado_actual == "SALUDO_INICIAL":
        return "ESPERANDO_INTENCION"

    if estado_actual == "CITA_DIA_NO_LABORAL":
        return "ESPERANDO_PROPUESTA_CITA"

    if estado_actual == "ESPERANDO_INTENCION":
        return "ESPERANDO_REFERENCIA"

    if estado_actual == "ESPERANDO_REFERENCIA":
        return "VALIDACION_ZONA"

    if estado_actual == "VALIDACION_ZONA":
        return "VALIDACION_ZONA"

    if estado_actual == "VALIDACION_ZONA_OBLIGATORIA":
        return "VALIDACION_ZONA"

    if estado_actual == "ZONA_INVALIDA_POTENCIAL_METEPEC":
        return "ZONA_INVALIDA_POTENCIAL_METEPEC"

    if estado_actual == "RESPUESTA_SOBRE_METODO":
        return "RESPUESTA_DE_INTERES"

    if estado_actual == "RESPUESTA_DE_INTERES":
        return "RESPUESTA_DE_INTERES"

    if estado_actual == "DESPUES_DEL_TEMA":
        return "INVITACION_CITA"

    if estado_actual == "INVITACION_CITA":
        return "INVITACION_CITA"

    if estado_actual == "COSTOS_EN_ETAPA_AVANZADA":
        return "COSTOS_EN_ETAPA_AVANZADA"

    if estado_actual == "INSISTE_COSTOS_ANTES_DE_AGENDAR":
        return "INSISTE_COSTOS_ANTES_DE_AGENDAR"

    if estado_actual == "ESPERANDO_PROPUESTA_CITA":
        return "ESPERANDO_PROPUESTA_CITA"

    if estado_actual == "SEGUIMIENTO_ACORDADO":
        return "SEGUIMIENTO_ACORDADO"

    if estado_actual == "CAMPUS_EXTERNO_NO_ATENDIBLE":
        return "CAMPUS_EXTERNO_NO_ATENDIBLE"

    return estado_actual


def convertir_a_hora_local(dt: datetime) -> datetime:
    """Convierte un datetime almacenado en BD a hora local de México."""
    if dt is None:
        return None

    # Si viene sin zona horaria, asumimos que está en UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(LOCAL_TZ)

def formatear_fecha_para_mensaje(dt: datetime) -> str:
    """Formatea fecha para mostrar en mensajes usando zona horaria local real."""
    dt_local = convertir_a_hora_local(dt)

    hoy_local = datetime.now(LOCAL_TZ)
    fecha_hoy = hoy_local.date()
    fecha_ayer = fecha_hoy - timedelta(days=1)
    fecha_msg = dt_local.date()

    hora = dt_local.hour
    minutos = dt_local.minute

    periodo = "a.m." if hora < 12 else "p.m."

    if hora == 0:
        hora_12 = 12
    elif hora > 12:
        hora_12 = hora - 12
    else:
        hora_12 = hora

    hora_str = f"{hora_12}:{minutos:02d} {periodo}"

    if fecha_msg == fecha_hoy:
        return f"Hoy {hora_str}"
    elif fecha_msg == fecha_ayer:
        return f"Ayer {hora_str}"
    else:
        meses = ["ene", "feb", "mar", "abr", "may", "jun",
                 "jul", "ago", "sep", "oct", "nov", "dic"]
        return f"{dt_local.day} {meses[dt_local.month - 1]} {hora_str}"
# ================= CONFIGURACIÓN DE BASE DE DATOS =================
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./whatsapp_bot.db")

# Crear enums para PostgreSQL
if DATABASE_URL.startswith("postgresql://"):
    # Definir tipos ENUM para PostgreSQL
    from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
    
    contact_status_enum = PG_ENUM(
        "PROSPECTO_NUEVO",
        "EN_CALIFICACION",
        "PROSPECTO_INFORMADO",
        "PENDIENTE_DE_AGENDAR",
        "CITA_PENDIENTE_CONFIRMACION",
        "VISITA_CONFIRMADA",
        "VISITA_AGENDADA",
        "VISITA_REALIZADA",
        "VISITA_NO_ASISTIO",
        "COSTOS_PRESENTADOS",
        "INSCRIPCION_PENDIENTE",
        "INSCRITO",
        "NO_INSCRITO",
        "DESCARTADO",
        "COMPETENCIA",
        "ALUMNO_ACTIVO",
        "ALUMNO_INACTIVO",
        "EX_ALUMNO",
        name="contact_status_enum",
        create_type=True
    )
    
    message_direction_enum = PG_ENUM(
        'incoming', 
        'outgoing', 
        name='message_direction_enum',
        create_type=True
    )
    
    # Modificar la URL para usar psycopg2
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(DATABASE_URL)
else:
    # Para SQLite, usar tipos String normales
    contact_status_enum = String(50)
    message_direction_enum = String(20)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ================= MODELOS DE BASE DE DATOS =================

class Contact(Base):
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(50), unique=True, index=True, nullable=False)
    
    # Usar el ENUM apropiado según la base de datos
    status = Column(contact_status_enum, default="PROSPECTO_NUEVO")
    
    first_contact = Column(DateTime, default=func.now())
    last_contact = Column(DateTime, default=func.now(), onupdate=func.now())
    total_messages = Column(Integer, default=0)
    notes = Column(Text, nullable=True)
    is_competitor = Column(Boolean, default=False)
    
    # Relación con mensajes
    messages = relationship("Message", back_populates="contact", cascade="all, delete-orphan")

class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"))
    
    # Usar el ENUM apropiado según la base de datos
    direction = Column(message_direction_enum, nullable=False)
    
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=func.now())
    twilio_sid = Column(String(50), nullable=True)
    
    # Relación con contacto
    contact = relationship("Contact", back_populates="messages")

# ============================================================
# CRM PERSISTENTE DE SEGUIMIENTO
# ============================================================

class ContactFollowUpState(Base):
    """
    Estado persistente actual del seguimiento comercial.

    IMPORTANTE:
    - Sólo se crea para contactos nuevos posteriores al rollout.
    - Los contactos históricos no reciben esta fila automáticamente.
    - No envía mensajes.
    """

    __tablename__ = "contact_followup_state"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    contact_id = Column(
        Integer,
        ForeignKey("contacts.id"),
        unique=True,
        nullable=False,
        index=True,
    )

    prospect_phone = Column(
        String(50),
        nullable=False,
        index=True,
    )

    # --------------------------------------------------------
    # CONTROL DE COHORTE / AUTOMATIZACIÓN
    # --------------------------------------------------------

    automation_enrolled_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    automation_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
    )

    # --------------------------------------------------------
    # ESTADO CRM DE LARGO PLAZO
    # --------------------------------------------------------

    commercial_goal = Column(
        String(100),
        nullable=False,
        default="",
    )

    journey_status = Column(
        String(50),
        nullable=False,
        default="NOT_ENROLLED",
        index=True,
    )

    # --------------------------------------------------------
    # CICLO / MINI ESTADO DE CONVERSACIÓN
    # --------------------------------------------------------

    conversation_cycle_id = Column(
        String(64),
        nullable=False,
        default="",
        index=True,
    )

    conversation_mode = Column(
        String(50),
        nullable=False,
        default="",
    )

    active_goal = Column(
        String(100),
        nullable=False,
        default="",
    )

    active_goal_status = Column(
        String(30),
        nullable=False,
        default="",
    )

    # --------------------------------------------------------
    # POSICIÓN ACTUAL DEL EMBUDO
    # --------------------------------------------------------

    current_objective = Column(
        String(100),
        nullable=False,
        default="",
    )

    current_stage = Column(
        String(100),
        nullable=False,
        default="",
    )

    current_commercial_status = Column(
        String(100),
        nullable=False,
        default="",
    )

    # --------------------------------------------------------
    # SEGUIMIENTO
    # --------------------------------------------------------

    followup_step = Column(
        Integer,
        nullable=False,
        default=0,
    )

    followup_count = Column(
        Integer,
        nullable=False,
        default=0,
    )

    last_inbound_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_outbound_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_followup_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    next_followup_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # --------------------------------------------------------
    # NURTURING
    # --------------------------------------------------------

    nurturing_started_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    next_nurturing_at = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
        onupdate=lambda: datetime.now(
            timezone.utc
        ),
    )


class FollowUpEvent(Base):
    """
    Bitácora histórica de ciclos, seguimientos y nurturing.

    No representa el estado actual.
    El estado actual vive en ContactFollowUpState.
    """

    __tablename__ = "followup_events"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    contact_id = Column(
        Integer,
        ForeignKey("contacts.id"),
        nullable=False,
        index=True,
    )

    conversation_cycle_id = Column(
        String(64),
        nullable=True,
        index=True,
    )

    event_type = Column(
        String(80),
        nullable=False,
        index=True,
    )

    step_number = Column(
        Integer,
        nullable=True,
    )

    objective = Column(
        String(100),
        nullable=True,
    )

    journey_status = Column(
        String(50),
        nullable=True,
    )

    scheduled_for = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    occurred_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
        nullable=False,
        index=True,
    )

    message_id = Column(
        Integer,
        ForeignKey("messages.id"),
        nullable=True,
    )

    twilio_sid = Column(
        String(100),
        nullable=True,
    )

    reason = Column(
        Text,
        nullable=True,
    )

    metadata_json = Column(
        Text,
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
    )

# ============================================================
# MODERACIÓN PERSISTENTE DE CONTACTOS
# ============================================================

class ContactModerationState(Base):
    """
    Estado independiente de moderación de un contacto.

    La ausencia de una fila equivale a CLEAR.

    Esta tabla no modifica Contact ni mezcla moderación
    con el estado comercial del prospecto.
    """

    __tablename__ = "contact_moderation_state"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    contact_id = Column(
        Integer,
        ForeignKey("contacts.id"),
        unique=True,
        nullable=False,
        index=True,
    )

    moderation_status = Column(
        String(30),
        nullable=False,
        default="CLEAR",
        index=True,
    )

    risk_category = Column(
        String(50),
        nullable=True,
    )

    block_reason = Column(
        Text,
        nullable=True,
    )

    source = Column(
        String(30),
        nullable=True,
    )

    last_flagged_message_id = Column(
        Integer,
        ForeignKey("messages.id"),
        nullable=True,
    )

    blocked_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    unblocked_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(
            timezone.utc
        ),
        onupdate=lambda: datetime.now(
            timezone.utc
        ),
    )

# ============================================================
# CONTROL CENTRAL DE MODERACIÓN
# ============================================================

MODERATION_STATUS_CLEAR = "CLEAR"
MODERATION_STATUS_REVIEW = "REVIEW"
MODERATION_STATUS_BLOCKED = "BLOCKED"


def obtener_estado_moderacion(
    db: Session,
    contact_id: int,
):
    """
    Obtiene moderación persistente.

    La ausencia de fila significa CLEAR.
    No crea estados innecesariamente.
    """

    return (
        db.query(ContactModerationState)
        .filter(
            ContactModerationState.contact_id
            == contact_id
        )
        .first()
    )


def contacto_esta_bloqueado(
    db: Session,
    contact_id: int,
) -> bool:

    estado = obtener_estado_moderacion(
        db,
        contact_id,
    )

    return bool(
        estado
        and estado.moderation_status
        == MODERATION_STATUS_BLOCKED
    )


def bloquear_contacto(
    db: Session,
    contact,
    reason: str,
    risk_category: str,
    source: str,
    message_id: Optional[int] = None,
):
    """
    Establece BLOCKED como autoridad persistente.

    También invalida cualquier programación comercial futura,
    pero no destruye el estado/ciclo CRM.
    """

    if contact is None:
        return None

    ahora = datetime.now(
        timezone.utc
    )

    estado = obtener_estado_moderacion(
        db,
        contact.id,
    )

    if estado is None:
        estado = ContactModerationState(
            contact_id=contact.id,
            created_at=ahora,
        )
        db.add(estado)

    estado.moderation_status = (
        MODERATION_STATUS_BLOCKED
    )

    estado.risk_category = str(
        risk_category or "SPAM"
    ).strip().upper()

    estado.block_reason = str(
        reason or ""
    ).strip()

    estado.source = str(
        source or "AUTO"
    ).strip().upper()

    estado.last_flagged_message_id = (
        message_id
    )

    estado.blocked_at = ahora
    estado.unblocked_at = None
    estado.updated_at = ahora

    # --------------------------------------------------------
    # INVALIDAR PROGRAMACIÓN CRM
    # --------------------------------------------------------
    #
    # No cambiamos el journey comercial.
    # La moderación es una autoridad independiente.
    # --------------------------------------------------------

    estado_crm = obtener_estado_followup_crm(
        db,
        contact.id,
    )

    if estado_crm is not None:
        estado_crm.next_followup_at = None
        estado_crm.next_nurturing_at = None
        estado_crm.updated_at = ahora

    # --------------------------------------------------------
    # CERRAR TAREAS ADMINISTRATIVAS QUE YA NO SON ACCIONABLES
    # --------------------------------------------------------

    tareas_admin_pendientes = (
        db.query(AdminPendingTask)
        .filter(
            AdminPendingTask.contact_id == contact.id,
            AdminPendingTask.status == "PENDIENTE",
        )
        .all()
    )

    ids_tareas_cerradas = []

    for tarea_admin in tareas_admin_pendientes:
        tarea_admin.status = "RESUELTA"
        tarea_admin.admin_response = (
            "CERRADA_POR_MODERACION"
        )
        tarea_admin.final_response = (
            "Contacto bloqueado. "
            "No se envió mensaje al prospecto."
        )
        tarea_admin.resolved_at = ahora

        ids_tareas_cerradas.append(
            tarea_admin.id
        )

    # También eliminamos cualquier selección temporal
    # del administrador que apunte a esas tareas.
    if ids_tareas_cerradas:
        for admin_key, tarea_id in list(
            ADMIN_SELECTED_TASKS.items()
        ):
            if tarea_id in ids_tareas_cerradas:
                ADMIN_SELECTED_TASKS.pop(
                    admin_key,
                    None,
                )

    db.commit()
    db.refresh(estado)

    print(
        "🚫 CONTACTO BLOQUEADO: "
        f"contact_id={contact.id}, "
        f"categoria={estado.risk_category}, "
        f"source={estado.source}"
    )

    return estado


def desbloquear_contacto(
    db: Session,
    contact,
):
    """
    Desbloquea exclusivamente mensajes futuros.

    No reproduce mensajes anteriores ni reactiva por sí misma
    programaciones canceladas.
    """

    if contact is None:
        return None

    estado = obtener_estado_moderacion(
        db,
        contact.id,
    )

    if estado is None:
        return None

    ahora = datetime.now(
        timezone.utc
    )

    estado.moderation_status = (
        MODERATION_STATUS_CLEAR
    )

    estado.unblocked_at = ahora
    estado.updated_at = ahora

    db.commit()
    db.refresh(estado)

    print(
        "✅ CONTACTO DESBLOQUEADO: "
        f"contact_id={contact.id}"
    )

    return estado


def evaluar_riesgo_mensaje_entrante(
    mensaje: str,
) -> Dict[str, Any]:
    """
    Evaluación determinista deliberadamente conservadora.

    Sólo genera bloqueo automático ante señales de muy alta
    confianza. No visita enlaces y no intenta determinar malware.
    """

    texto = str(
        mensaje or ""
    ).strip()

    texto_normalizado = (
        normalizar_texto_para_deteccion(
            texto
        )
    )

    contiene_url = bool(
        re.search(
            r"(https?://|www\.)\S+",
            texto,
            flags=re.IGNORECASE,
        )
    )

    indicadores_adulto_explicito = {
        "brazzers",
        "pornhub",
        "xvideos",
        "xnxx",
        "sitio porno",
        "sitio pornografico",
        "sitio pornográfico",
    }

    contenido_adulto_explicito = any(
        indicador in texto_normalizado
        for indicador
        in indicadores_adulto_explicito
    )

    # Bloqueo automático únicamente si se combinan
    # contenido explícito identificable + enlace.
    if (
        contiene_url
        and contenido_adulto_explicito
    ):
        return {
            "accion": "BLOCK",
            "categoria": (
                "CONTENIDO_INAPROPIADO"
            ),
            "motivo": (
                "Mensaje no solicitado con enlace y "
                "contenido adulto explícito."
            ),
        }

    return {
        "accion": "ALLOW",
        "categoria": "",
        "motivo": "",
    }

class AdminPendingTask(Base):
    __tablename__ = "admin_pending_tasks"

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    prospect_phone = Column(String(50), nullable=False)

    status = Column(String(30), default="PENDIENTE")  # PENDIENTE / RESUELTA
    trigger_message = Column(Text, nullable=True)
    bot_response = Column(Text, nullable=True)
    admin_response = Column(Text, nullable=True)
    final_response = Column(Text, nullable=True)

    created_at = Column(DateTime, default=func.now())
    resolved_at = Column(DateTime, nullable=True)

class AdminWhatsappState(Base):
    """
    Estado persistente del canal WhatsApp administrador.

    Se mantiene separado de Contact para evitar que el número
    administrador aparezca como prospecto dentro del CRM.
    """

    __tablename__ = "admin_whatsapp_state"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    admin_number = Column(
        String(50),
        unique=True,
        index=True,
        nullable=False,
    )

    last_inbound_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

# ================= MANEJO SEGURO DE LA CREACIÓN DE TABLAS =================
def setup_database():
    """Configura la base de datos de manera segura"""
    try:
        # Intentar crear tablas
        Base.metadata.create_all(bind=engine)
        print("✅ Tablas creadas exitosamente")
        
        # Si estamos en PostgreSQL, verificar que los ENUMs existan
        if DATABASE_URL.startswith("postgresql"):
            with engine.connect() as conn:
                # Verificar si existe el enum de contact_status
                result = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_type WHERE typname = 'contact_status_enum'
                    )
                """))
                if not result.scalar():
                    print("⚠️  El tipo ENUM 'contact_status_enum' no existe, creándolo...")
                    conn.execute(text("""
                        CREATE TYPE contact_status_enum AS ENUM (
                            'PROSPECTO_NUEVO', 
                            'PROSPECTO_INFORMADO', 
                            'VISITA_AGENDADA', 
                            'INSCRIPCION_PENDIENTE', 
                            'ALUMNO_ACTIVO', 
                            'ALUMNO_INACTIVO', 
                            'COMPETENCIA', 
                            'EX_ALUMNO'
                        )
                    """))
                # Mantener sincronizado el ENUM existente con
                # los estados comerciales actuales del CRM.
                valores_contact_status_requeridos = [
                    "PROSPECTO_NUEVO",
                    "EN_CALIFICACION",
                    "PROSPECTO_INFORMADO",
                    "PENDIENTE_DE_AGENDAR",
                    "CITA_PENDIENTE_CONFIRMACION",
                    "VISITA_CONFIRMADA",
                    "VISITA_AGENDADA",
                    "VISITA_REALIZADA",
                    "VISITA_NO_ASISTIO",
                    "COSTOS_PRESENTADOS",
                    "INSCRIPCION_PENDIENTE",
                    "INSCRITO",
                    "NO_INSCRITO",
                    "DESCARTADO",
                    "COMPETENCIA",
                    "ALUMNO_ACTIVO",
                    "ALUMNO_INACTIVO",
                    "EX_ALUMNO",
                ]

                for valor_status in valores_contact_status_requeridos:
                    conn.execute(
                        text(
                            "ALTER TYPE contact_status_enum "
                            f"ADD VALUE IF NOT EXISTS "
                            f"'{valor_status}'"
                        )
                    )

                conn.commit()

                print(
                    "✅ ENUM contact_status_enum "
                    "sincronizado correctamente"
                )
                
                # Verificar si existe el enum de message_direction
                result = conn.execute(text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_type WHERE typname = 'message_direction_enum'
                    )
                """))
                if not result.scalar():
                    print("⚠️  El tipo ENUM 'message_direction_enum' no existe, creándolo...")
                    conn.execute(text("""
                        CREATE TYPE message_direction_enum AS ENUM ('incoming', 'outgoing')
                    """))
                
                conn.commit()
                
    except Exception as e:
        print(f"⚠️  Error durante la configuración de la base de datos: {e}")
        print("⚠️  Intentando continuar...")

# Ejecutar configuración
setup_database()

# ================= DEPENDENCIA DE BASE DE DATOS =================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ============================================================
# CRM PERSISTENTE DE SEGUIMIENTO Y NURTURING
# ============================================================

CRM_COMMERCIAL_GOAL_VISITA = (
    "LOGRAR_VISITA_PRESENCIAL"
)

# ============================================================
# POLÍTICA DE FOLLOW-UP
# ============================================================

# F1: microseguimiento.
CRM_FOLLOWUP_INITIAL_MINUTES = 7

# F2: recuperación.
CRM_FOLLOWUP_SECOND_DELAY_HOURS = 4

# Horario silencioso para F2/F3.
CRM_FOLLOWUP_ACTIVE_START_HOUR = 7
CRM_FOLLOWUP_ACTIVE_END_HOUR = 21

# F3: antes de vencer la ventana WhatsApp.
CRM_FOLLOWUP_FINAL_LEAD_MINUTES = 90

# Si F3 caería de madrugada, adelantar al día anterior.
CRM_FOLLOWUP_FINAL_EVENING_HOUR = 20
CRM_FOLLOWUP_FINAL_EVENING_MINUTE = 30

# Frecuencia con la que el worker consulta pendientes.
CRM_FOLLOWUP_POLL_SECONDS = 30

# Ventana mínima para permitir que un inbound concurrente
# termine de persistirse antes del envío irreversible a Twilio.
CRM_FOLLOWUP_PRE_SEND_GRACE_SECONDS = 2

# Después del ciclo corto sin respuesta.
CRM_NURTURING_COOLDOWN_DAYS = 3

# Lock global PostgreSQL para evitar que dos instancias
# de Railway procesen el mismo lote simultáneamente.
CRM_FOLLOWUP_ADVISORY_LOCK_KEY = 26082026

# Interruptor maestro.
#
# IMPORTANTE:
# dejar FALSE durante el primer deploy.
# Lo activaremos en Railway después de validar.
FOLLOWUP_AUTOMATION_MASTER_ENABLED = (
    str(
        os.getenv(
            "FOLLOWUP_AUTOMATION_ENABLED",
            "false",
        )
        or "false"
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)



def registrar_evento_followup_crm(
    db: Session,
    estado_crm,
    event_type: str,
    reason: str = "",
    step_number: Optional[int] = None,
    scheduled_for=None,
    metadata: Optional[Dict[str, Any]] = None,
    message_id: Optional[int] = None,
    twilio_sid: Optional[str] = None,
):
    """
    Registra un evento histórico del ciclo CRM.

    No representa el estado actual.
    No realiza commit por sí sola.
    """

    if estado_crm is None:
        return None

    evento = FollowUpEvent(
        contact_id=estado_crm.contact_id,

        conversation_cycle_id=(
            estado_crm.conversation_cycle_id
            or None
        ),

        event_type=str(
            event_type or ""
        ).strip().upper(),

        step_number=step_number,

        objective=(
            estado_crm.current_objective
            or None
        ),

        journey_status=(
            estado_crm.journey_status
            or None
        ),

        scheduled_for=scheduled_for,

        message_id=message_id,

        twilio_sid=(
            str(
                twilio_sid
                or ""
            ).strip()
            or None
        ),

        occurred_at=datetime.now(
            timezone.utc
        ),

        reason=(
            str(reason or "").strip()
            or None
        ),

        metadata_json=(
            json.dumps(
                metadata,
                ensure_ascii=False,
                default=str,
            )
            if isinstance(metadata, dict)
            else None
        ),
    )

    db.add(evento)

    return evento


def crear_estado_crm_contacto_nuevo(
    db: Session,
    contact,
):
    """
    Crea la marca persistente de cohorte únicamente para
    un contacto que acaba de ser creado.

    IMPORTANTE:
    Esta función NO debe utilizarse para contactos históricos.
    """

    if contact is None:
        return None

    existente = (
        db.query(ContactFollowUpState)
        .filter(
            ContactFollowUpState.contact_id
            == contact.id
        )
        .first()
    )

    if existente:
        return existente

    ahora = datetime.now(
        timezone.utc
    )

    estado = ContactFollowUpState(
        contact_id=contact.id,
        prospect_phone=contact.phone_number,

        automation_enabled=(
            FOLLOWUP_AUTOMATION_MASTER_ENABLED
        ),
        automation_enrolled_at=None,

        commercial_goal="",
        journey_status="NOT_ENROLLED",

        conversation_cycle_id="",
        conversation_mode="",

        active_goal="",
        active_goal_status="",

        current_objective="",
        current_stage="",
        current_commercial_status="",

        followup_step=0,
        followup_count=0,

        last_inbound_at=None,
        last_outbound_at=None,
        last_followup_at=None,
        next_followup_at=None,

        nurturing_started_at=None,
        next_nurturing_at=None,

        created_at=ahora,
        updated_at=ahora,
    )

    db.add(estado)
    db.flush()

    registrar_evento_followup_crm(
        db=db,
        estado_crm=estado,
        event_type="CRM_STATE_CREATED",
        reason=(
            "Contacto nuevo posterior al rollout. "
            f"automation_enabled="
            f"{estado.automation_enabled}"
        ),
    )

    db.commit()
    db.refresh(estado)

    print(
        "🆕 CRM FOLLOWUP STATE CREADO: "
        f"contact_id={contact.id}, "
        "journey=NOT_ENROLLED, "
        f"automation_enabled="
        f"{estado.automation_enabled}"
    )

    return estado


def obtener_estado_followup_crm(
    db: Session,
    contact_id: int,
):
    """
    Obtiene el estado CRM existente.

    NO crea uno nuevo.
    Esto protege deliberadamente a contactos históricos.
    """

    return (
        db.query(ContactFollowUpState)
        .filter(
            ContactFollowUpState.contact_id
            == contact_id
        )
        .first()
    )

def normalizar_datetime_utc(
    valor,
):
    """
    Normaliza timestamps provenientes de PostgreSQL para
    realizar comparaciones seguras en UTC.
    """

    if valor is None:
        return None

    if valor.tzinfo is None:
        return valor.replace(
            tzinfo=timezone.utc
        )

    return valor.astimezone(
        timezone.utc
    )


def obtener_expiracion_ventana_whatsapp(
    last_inbound_at,
):
    """
    La ventana de servicio se calcula siempre desde el
    último mensaje INBOUND del prospecto.

    Nuestros mensajes outbound no la renuevan.
    """

    inbound_utc = (
        normalizar_datetime_utc(
            last_inbound_at
        )
    )

    if inbound_utc is None:
        return None

    return (
        inbound_utc
        + timedelta(hours=24)
    )


def ajustar_followup_a_horario_activo(
    fecha_utc,
):
    """
    F2 y F3 no deben despertar al prospecto.

    Horario permitido:
    07:00 <= hora < 21:00
    """

    if fecha_utc is None:
        return None

    fecha_utc = normalizar_datetime_utc(
        fecha_utc
    )

    local = fecha_utc.astimezone(
        LOCAL_TZ
    )

    # Antes de las 07:00 -> hoy a las 07:00.
    if (
        local.hour
        < CRM_FOLLOWUP_ACTIVE_START_HOUR
    ):
        local = local.replace(
            hour=(
                CRM_FOLLOWUP_ACTIVE_START_HOUR
            ),
            minute=0,
            second=0,
            microsecond=0,
        )

    # Desde las 21:00 -> mañana a las 07:00.
    elif (
        local.hour
        >= CRM_FOLLOWUP_ACTIVE_END_HOUR
    ):
        local = (
            local
            + timedelta(days=1)
        ).replace(
            hour=(
                CRM_FOLLOWUP_ACTIVE_START_HOUR
            ),
            minute=0,
            second=0,
            microsecond=0,
        )

    return local.astimezone(
        timezone.utc
    )


def calcular_momento_followup_final(
    last_inbound_at,
):
    """
    Calcula F3 aproximadamente 90 minutos antes del cierre
    de la ventana de 24 horas.

    Si el candidato cae:
    - antes de las 07:00 -> 20:30 del día anterior;
    - desde las 21:00 -> 20:30 del mismo día.

    Nunca debe quedar antes del último inbound.
    """

    inbound_utc = normalizar_datetime_utc(
        last_inbound_at
    )

    if inbound_utc is None:
        return None

    expiracion = (
        obtener_expiracion_ventana_whatsapp(
            inbound_utc
        )
    )

    if expiracion is None:
        return None

    candidato = (
        expiracion
        - timedelta(
            minutes=(
                CRM_FOLLOWUP_FINAL_LEAD_MINUTES
            )
        )
    )

    candidato_local = candidato.astimezone(
        LOCAL_TZ
    )

    # --------------------------------------------------------
    # MADRUGADA:
    # usar 20:30 del día anterior
    # --------------------------------------------------------

    if (
        candidato_local.hour
        < CRM_FOLLOWUP_ACTIVE_START_HOUR
    ):
        fecha_objetivo = (
            candidato_local.date()
            - timedelta(days=1)
        )

        candidato_local = datetime(
            year=fecha_objetivo.year,
            month=fecha_objetivo.month,
            day=fecha_objetivo.day,
            hour=(
                CRM_FOLLOWUP_FINAL_EVENING_HOUR
            ),
            minute=(
                CRM_FOLLOWUP_FINAL_EVENING_MINUTE
            ),
            tzinfo=LOCAL_TZ,
        )

    # --------------------------------------------------------
    # NOCHE:
    # usar 20:30 DEL MISMO DÍA
    # --------------------------------------------------------

    elif (
        candidato_local.hour
        >= CRM_FOLLOWUP_ACTIVE_END_HOUR
    ):
        fecha_objetivo = (
            candidato_local.date()
        )

        candidato_local = datetime(
            year=fecha_objetivo.year,
            month=fecha_objetivo.month,
            day=fecha_objetivo.day,
            hour=(
                CRM_FOLLOWUP_FINAL_EVENING_HOUR
            ),
            minute=(
                CRM_FOLLOWUP_FINAL_EVENING_MINUTE
            ),
            tzinfo=LOCAL_TZ,
        )

    candidato_utc = (
        candidato_local.astimezone(
            timezone.utc
        )
    )

    # Nunca programar F3 antes de que comenzara
    # la propia conversación.
    if candidato_utc <= inbound_utc:
        return None

    # Tampoco fuera de la ventana.
    if candidato_utc >= expiracion:
        return None

    return candidato_utc
def construir_followup_fallback(
    estado_crm,
    numero_followup: int,
) -> str:
    """
    Sólo se utiliza si Gemini no puede redactar.

    No intenta reconstruir el funnel.
    """
    objetivo = str(
        estado_crm.current_objective
        or ""
    ).strip().upper()

    if numero_followup == 1:

        if objetivo == "OBTENER_DECISION_VISITA":
            return (
                "Sólo quería confirmar si le gustaría "
                "que le ayudemos a coordinar una visita "
                "para conocer el colegio."
            )

        return (
            "Cuando tenga oportunidad, compártame "
            "el dato que quedó pendiente y con gusto "
            "continuamos."
        )

    if numero_followup == 2:
        return (
            "Buen día. Espero que se encuentre muy bien."
            "\n\n"
            "Retomo nuestra conversación por si todavía "
            "podemos ayudarle con la información que "
            "estaba revisando."
        )

    return (
        "Antes de cerrar por ahora esta conversación, "
        "queríamos saber si todavía desea que le "
        "ayudemos a continuar con la información. "
        "Con gusto podemos retomarla cuando lo necesite."
    )


def generar_followup_contextual_crm(
    db: Session,
    contact,
    estado_crm,
    numero_followup: int,
) -> str:
    """
    Gemini redacta; Python decide cuándo y por qué.

    El seguimiento debe conservar el contexto,
    pero nunca avanzar el estado por sí solo.
    """

    fallback = construir_followup_fallback(
        estado_crm,
        numero_followup,
    )

    if contact is None:
        return fallback

    try:
        historial = (
            obtener_historial_completo_contacto(
                db=db,
                contact=contact,
            )
        )

        conversacion = historial.get(
            "conversacion",
            [],
        )

        if not isinstance(
            conversacion,
            list,
        ):
            conversacion = []

        lineas = []

        for item in conversacion[-12:]:

            if not isinstance(
                item,
                dict,
            ):
                continue

            emisor = str(
                item.get(
                    "emisor",
                    "Conversación",
                )
                or "Conversación"
            ).strip()

            contenido = str(
                item.get(
                    "contenido",
                    "",
                )
                or ""
            ).strip()

            if contenido:
                lineas.append(
                    f"{emisor}: {contenido}"
                )

        historial_texto = (
            "\n".join(lineas)
            if lineas
            else "Sin historial disponible."
        )

        ahora_local = datetime.now(
            LOCAL_TZ
        )

        if ahora_local.hour < 12:
            momento_dia = "mañana"
        elif ahora_local.hour < 19:
            momento_dia = "tarde"
        else:
            momento_dia = "noche"

        prompt = f"""
Eres el asistente de admisiones del Colegio Valle de Filadelfia,
Campus Santa Cruz Atizapán.

Debes redactar un mensaje breve de seguimiento por WhatsApp porque
el prospecto dejó de responder temporalmente.

FOLLOW-UP:
{numero_followup} de 3

MOMENTO LOCAL:
{momento_dia}

OBJETIVO QUE SIGUE PENDIENTE:
{estado_crm.current_objective or "SIN_OBJETIVO"}

ETAPA:
{estado_crm.current_stage or "SIN_ETAPA"}

HISTORIAL:
{historial_texto}

REGLAS ABSOLUTAS:

- No inventes información.
- No cambies de tema.
- No avances artificialmente el embudo.
- No repitas una explicación larga ya enviada.
- No vuelvas a presentarte.
- No digas que eres una IA o bot.
- No menciones que es un mensaje automático.
- No menciones la ventana de 24 horas de WhatsApp.
- No presiones.
- No utilices urgencia artificial.
- Conserva trato institucional de usted.
- Usa el historial para continuar exactamente desde el punto pendiente.
- Si ya se hizo una pregunta concreta, puedes retomarla de manera natural.
- Como máximo una pregunta.
- Máximo 320 caracteres aproximadamente.
- Devuelve solamente el mensaje que se enviará.

TONO Y FORMATO SEGÚN EL PASO:

F1:
- Es una continuación inmediata de la conversación.
- NO vuelvas a saludar.
- NO comiences con "Buenos días", "Buenas tardes",
  "Buenas noches", "Hola" ni equivalentes.
- NO utilices frases formales como
  "Solo para dar seguimiento a su solicitud".
- NO anuncies que estás retomando la conversación.
- Debe sentirse como si la conversación simplemente
  continuara unos minutos después.
- Retoma naturalmente la pregunta o acción pendiente.
- Sé especialmente breve.

F2:
- Ya transcurrieron varias horas.
- Sí puedes iniciar con un saludo breve y natural
  acorde con el momento del día.
- Después del saludo o frase introductoria utiliza
  una línea en blanco antes del contenido principal.
- Debe sentirse como una reactivación cordial,
  no como una conversación nueva.
- Retoma exactamente el objetivo pendiente.

F3:
- Es el último intento del ciclo corto.
- Puede llevar un saludo breve si corresponde
  al momento del día.
- Si utilizas saludo o frase introductoria,
  deja una línea en blanco antes del contenido principal.
- Debe ser amable, breve y sin presión.
- Deja abierta la posibilidad de retomarlo posteriormente.

FORMATO:
- F1: máximo uno o dos bloques muy breves.
- F2 y F3: máximo dos párrafos breves.
- Para separar párrafos utiliza dos saltos de línea.
- No amontones saludo, introducción y solicitud
  en un solo párrafo.
"""

        response, modelo_usado = (
            generar_con_gemini_con_fallback(
                prompt,
                generation_config=(
                    genai.types.GenerationConfig(
                        temperature=0.25,
                    )
                ),
                tarea=(
                    "followup conversacional CRM"
                ),
            )
        )

        texto = (
            extraer_texto_respuesta_gemini(
                response
            )
            .strip()
        )

        if not texto:
            return fallback

        if len(texto) > 500:
            return fallback

        # ----------------------------------------------------
        # VALIDACIÓN DETERMINISTA DE F1
        # ----------------------------------------------------
        #
        # F1 ocurre pocos minutos después.
        # Aunque Gemini incumpla el estilo solicitado,
        # nunca debe volver a saludar ni presentar el mensaje
        # como un seguimiento formal.
        # ----------------------------------------------------

        if numero_followup == 1:

            inicio_f1 = (
                normalizar_texto_para_deteccion(
                    texto
                )
            )

            inicios_no_permitidos_f1 = [
                "buenos dias",
                "buenas tardes",
                "buenas noches",
                "hola",
                "solo para dar seguimiento",
                "solo para darle seguimiento",
                "retomo nuestra conversacion",
            ]

            if any(
                inicio_f1.startswith(
                    inicio_no_permitido
                )
                for inicio_no_permitido
                in inicios_no_permitidos_f1
            ):
                print(
                    "🛡️ FOLLOWUP F1 REEMPLAZADO POR FALLBACK: "
                    f"contact_id={contact.id}, "
                    "motivo=INICIO_NO_NATURAL"
                )

                return fallback

        print(
            "🧠 FOLLOWUP IA: "
            f"contact_id={contact.id}, "
            f"step={numero_followup}, "
            f"modelo={modelo_usado}"
        )

        return texto

    except Exception as e:

        print(
            "⚠️ FOLLOWUP IA FALLÓ: "
            f"contact_id={contact.id}, "
            f"error={e}"
        )

        return fallback

OBJETIVOS_SIN_FOLLOWUP_AUTOMATICO = {
    "",
    "ESPERAR_CONFIRMACION_ADMIN",
}


def finalizar_ciclo_followup_sin_respuesta(
    db: Session,
    estado_crm,
):
    """
    Termina el ciclo corto después de F3.
    """

    ahora = datetime.now(
        timezone.utc
    )

    estado_crm.followup_step = 3
    estado_crm.next_followup_at = None
    estado_crm.journey_status = "NURTURING"
    estado_crm.active_goal_status = "PAUSED"

    if estado_crm.nurturing_started_at is None:
        estado_crm.nurturing_started_at = ahora

    estado_crm.next_nurturing_at = (
        ahora
        + timedelta(
            days=CRM_NURTURING_COOLDOWN_DAYS
        )
    )

    registrar_evento_followup_crm(
        db=db,
        estado_crm=estado_crm,
        event_type=(
            "FOLLOWUP_CYCLE_COMPLETED"
        ),
        reason=(
            "Se enviaron los tres seguimientos "
            "sin nueva respuesta del prospecto."
        ),
        step_number=3,
        scheduled_for=(
            estado_crm.next_nurturing_at
        ),
    )


def programar_siguiente_followup_crm(
    db: Session,
    estado_crm,
    numero_enviado: int,
):
    """
    Programa exclusivamente el paso posterior
    al seguimiento recién enviado.
    """

    ahora = datetime.now(
        timezone.utc
    )

    expiracion = (
        obtener_expiracion_ventana_whatsapp(
            estado_crm.last_inbound_at
        )
    )

    if expiracion is None:
        estado_crm.next_followup_at = None
        return

    # --------------------------------------------------------
    # DESPUÉS DE F1 → F2
    # --------------------------------------------------------

    if numero_enviado == 1:

        candidato_f2 = (
            ahora
            + timedelta(
                hours=(
                    CRM_FOLLOWUP_SECOND_DELAY_HOURS
                )
            )
        )

        candidato_f2 = (
            ajustar_followup_a_horario_activo(
                candidato_f2
            )
        )

        candidato_f3 = (
            calcular_momento_followup_final(
                estado_crm.last_inbound_at
            )
        )

        # Si ya no existe espacio real para F2,
        # saltamos directamente a F3.
        if (
            candidato_f3 is not None
            and candidato_f2
            >= candidato_f3
        ):
            estado_crm.followup_step = 2
            estado_crm.next_followup_at = (
                candidato_f3
            )

        else:
            estado_crm.followup_step = 1
            estado_crm.next_followup_at = (
                candidato_f2
            )

        return

    # --------------------------------------------------------
    # DESPUÉS DE F2 → F3
    # --------------------------------------------------------

    if numero_enviado == 2:

        candidato_f3 = (
            calcular_momento_followup_final(
                estado_crm.last_inbound_at
            )
        )

        if (
            candidato_f3 is None
            or candidato_f3 <= ahora
            or candidato_f3 >= expiracion
        ):
            finalizar_ciclo_followup_sin_respuesta(
                db,
                estado_crm,
            )
            return

        estado_crm.followup_step = 2
        estado_crm.next_followup_at = (
            candidato_f3
        )

        return

    # --------------------------------------------------------
    # DESPUÉS DE F3
    # --------------------------------------------------------

    finalizar_ciclo_followup_sin_respuesta(
        db,
        estado_crm,
    )

def procesar_followup_estado_crm(
    db: Session,
    estado_crm,
):
    """
    Revalida TODO inmediatamente antes de enviar.

    Ninguna programación antigua es suficiente por sí sola
    para autorizar un outbound.
    """

    if estado_crm is None:
        return False

    ahora = datetime.now(
        timezone.utc
    )

    # --------------------------------------------------------
    # AUTORIZACIÓN GLOBAL
    # --------------------------------------------------------

    if not FOLLOWUP_AUTOMATION_MASTER_ENABLED:
        return False

    if not estado_crm.automation_enabled:
        return False

    if (
        estado_crm.journey_status
        != "ACTIVE_CONVERSION"
    ):
        estado_crm.next_followup_at = None
        db.commit()
        return False

    if (
        estado_crm.active_goal_status
        != "ACTIVE"
    ):
        estado_crm.next_followup_at = None
        db.commit()
        return False

    objetivo = str(
        estado_crm.current_objective
        or ""
    ).strip().upper()

    # --------------------------------------------------------
    # SNAPSHOT AUTORITATIVO DEL CONTEXTO
    # --------------------------------------------------------
    #
    # Si durante la generación IA cambia cualquiera de estos
    # datos, el follow-up que estamos construyendo queda obsoleto.
    # --------------------------------------------------------

    snapshot_objetivo = objetivo

    snapshot_last_inbound_at = (
        normalizar_datetime_utc(
            estado_crm.last_inbound_at
        )
    )

    snapshot_last_outbound_at = (
        normalizar_datetime_utc(
            estado_crm.last_outbound_at
        )
    )

    snapshot_cycle_id = str(
        estado_crm.conversation_cycle_id
        or ""
    ).strip()

    if (
        objetivo
        in OBJETIVOS_SIN_FOLLOWUP_AUTOMATICO
    ):
        estado_crm.next_followup_at = None
        db.commit()
        return False

    # --------------------------------------------------------
    # CONTACTO
    # --------------------------------------------------------

    contact = (
        db.query(Contact)
        .filter(
            Contact.id
            == estado_crm.contact_id
        )
        .first()
    )

    if contact is None:
        estado_crm.next_followup_at = None
        db.commit()
        return False

    # --------------------------------------------------------
    # MODERACIÓN
    # --------------------------------------------------------

    if contacto_esta_bloqueado(
        db,
        contact.id,
    ):
        estado_crm.next_followup_at = None
        db.commit()
        return False

    # --------------------------------------------------------
    # NUNCA HACER FOLLOW-UP SI EL ÚLTIMO MENSAJE ES INBOUND
    # --------------------------------------------------------

    ultimo_mensaje = (
        db.query(Message)
        .filter(
            Message.contact_id
            == contact.id
        )
        .order_by(
            Message.timestamp.desc(),
            Message.id.desc(),
        )
        .first()
    )

    if ultimo_mensaje is None:
        estado_crm.next_followup_at = None
        db.commit()
        return False

    if (
        str(
            ultimo_mensaje.direction
            or ""
        ).strip().lower()
        == "incoming"
    ):
        # Hay un mensaje que nosotros todavía debemos
        # procesar/responder. No es silencio del prospecto.
        estado_crm.next_followup_at = None

        registrar_evento_followup_crm(
            db=db,
            estado_crm=estado_crm,
            event_type="FOLLOWUP_CANCELLED",
            reason=(
                "El último mensaje pertenece al "
                "prospecto."
            ),
        )

        db.commit()
        return False

    # --------------------------------------------------------
    # VENTANA DE 24 HORAS
    # --------------------------------------------------------

    expiracion = (
        obtener_expiracion_ventana_whatsapp(
            estado_crm.last_inbound_at
        )
    )

    if (
        expiracion is None
        or ahora >= expiracion
    ):
        estado_crm.next_followup_at = None
        estado_crm.journey_status = "NURTURING"
        estado_crm.active_goal_status = "PAUSED"

        registrar_evento_followup_crm(
            db=db,
            estado_crm=estado_crm,
            event_type=(
                "WHATSAPP_WINDOW_EXPIRED"
            ),
            reason=(
                "Se cerró la ventana calculada desde "
                "el último inbound del prospecto."
            ),
        )

        db.commit()
        return False

    # --------------------------------------------------------
    # IDENTIFICAR F1 / F2 / F3
    # --------------------------------------------------------

    step_actual = int(
        estado_crm.followup_step
        or 0
    )

    numero_followup = (
        step_actual + 1
    )

    if numero_followup > 3:
        estado_crm.next_followup_at = None
        db.commit()
        return False

    # F2/F3 respetan horario silencioso.
    if numero_followup >= 2:

        ahora_local = ahora.astimezone(
            LOCAL_TZ
        )

        if (
            ahora_local.hour
            < CRM_FOLLOWUP_ACTIVE_START_HOUR
            or ahora_local.hour
            >= CRM_FOLLOWUP_ACTIVE_END_HOUR
        ):
            estado_crm.next_followup_at = (
                ajustar_followup_a_horario_activo(
                    ahora
                )
            )

            db.commit()
            return False

    # --------------------------------------------------------
    # GENERACIÓN
    # --------------------------------------------------------

    mensaje_followup = (
        generar_followup_contextual_crm(
            db=db,
            contact=contact,
            estado_crm=estado_crm,
            numero_followup=(
                numero_followup
            ),
        )
    )

    if not mensaje_followup:
        return False

    # --------------------------------------------------------
    # REVALIDACIÓN FINAL JUSTO ANTES DE TWILIO
    # --------------------------------------------------------

    db.refresh(
        estado_crm
    )

    # --------------------------------------------------------
    # REVALIDACIÓN DEL MISMO CONTEXTO QUE GENERÓ EL FOLLOW-UP
    # --------------------------------------------------------

    objetivo_revalidado = str(
        estado_crm.current_objective
        or ""
    ).strip().upper()

    inbound_revalidado = (
        normalizar_datetime_utc(
            estado_crm.last_inbound_at
        )
    )

    outbound_revalidado = (
        normalizar_datetime_utc(
            estado_crm.last_outbound_at
        )
    )

    cycle_revalidado = str(
        estado_crm.conversation_cycle_id
        or ""
    ).strip()

    contexto_cambio = bool(
        objetivo_revalidado
        != snapshot_objetivo

        or inbound_revalidado
        != snapshot_last_inbound_at

        or outbound_revalidado
        != snapshot_last_outbound_at

        or cycle_revalidado
        != snapshot_cycle_id
    )

    if contexto_cambio:

        print(
            "🛑 FOLLOWUP OBSOLETO CANCELADO: "
            f"contact_id={contact.id}, "
            "el contexto cambió durante la generación."
        )

        return False

    if (
        estado_crm.active_goal_status
        != "ACTIVE"
    ):
        return False

    expiracion_revalidada = (
        obtener_expiracion_ventana_whatsapp(
            estado_crm.last_inbound_at
        )
    )

    ahora_revalidado = datetime.now(
        timezone.utc
    )

    if (
        expiracion_revalidada is None
        or ahora_revalidado
        >= expiracion_revalidada
    ):
        return False

    if (
        contacto_esta_bloqueado(
            db,
            contact.id,
        )
        or not estado_crm.automation_enabled
        or estado_crm.journey_status
        != "ACTIVE_CONVERSION"
    ):
        return False

    ultimo_mensaje_revalidado = (
        db.query(Message)
        .filter(
            Message.contact_id
            == contact.id
        )
        .order_by(
            Message.timestamp.desc(),
            Message.id.desc(),
        )
        .first()
    )

    if (
        ultimo_mensaje_revalidado is None
        or str(
            ultimo_mensaje_revalidado.direction
            or ""
        ).strip().lower()
        != "outgoing"
    ):
        return False

    # --------------------------------------------------------
    # RESERVA PERSISTENTE PRE-ENVÍO
    # --------------------------------------------------------
    #
    # Preferimos at-most-once:
    # si el proceso cae exactamente después de que Twilio
    # acepta el mensaje, no debe reenviarse al reiniciar.
    # --------------------------------------------------------

    scheduled_original = (
        estado_crm.next_followup_at
    )

    estado_crm.next_followup_at = None
    estado_crm.updated_at = datetime.now(
        timezone.utc
    )

    registrar_evento_followup_crm(
        db=db,
        estado_crm=estado_crm,
        event_type="FOLLOWUP_SEND_STARTED",
        reason=(
            "Seguimiento reservado antes del envío "
            "para prevenir duplicados por reinicio."
        ),
        step_number=numero_followup,
        scheduled_for=scheduled_original,
    )

    db.commit()

    # --------------------------------------------------------
    # BARRERA FINAL DE CONCURRENCIA PRE-TWILIO
    # --------------------------------------------------------
    #
    # Un inbound puede llegar exactamente después de la última
    # revalidación y antes del envío irreversible a Twilio.
    #
    # Damos una ventana mínima para que ese inbound quede
    # persistido y revalidamos usando una SESIÓN NUEVA, evitando
    # depender del identity map o del contexto transaccional de
    # la sesión utilizada por el worker.
    # --------------------------------------------------------

    time.sleep(
        CRM_FOLLOWUP_PRE_SEND_GRACE_SECONDS
    )

    db_guard = SessionLocal()

    try:

        estado_guard = (
            obtener_estado_followup_crm(
                db_guard,
                contact.id,
            )
        )

        ultimo_mensaje_guard = (
            db_guard.query(Message)
            .filter(
                Message.contact_id
                == contact.id
            )
            .order_by(
                Message.timestamp.desc(),
                Message.id.desc(),
            )
            .first()
        )

        followup_sigue_autorizado = bool(
            estado_guard is not None
            and estado_guard.automation_enabled
            and estado_guard.journey_status
            == "ACTIVE_CONVERSION"
            and estado_guard.active_goal_status
            == "ACTIVE"
            and str(
                estado_guard.current_objective
                or ""
            ).strip().upper()
            == snapshot_objetivo
            and normalizar_datetime_utc(
                estado_guard.last_inbound_at
            )
            == snapshot_last_inbound_at
            and normalizar_datetime_utc(
                estado_guard.last_outbound_at
            )
            == snapshot_last_outbound_at
            and str(
                estado_guard.conversation_cycle_id
                or ""
            ).strip()
            == snapshot_cycle_id
            and ultimo_mensaje_guard is not None
            and str(
                ultimo_mensaje_guard.direction
                or ""
            ).strip().lower()
            == "outgoing"
        )

        if not followup_sigue_autorizado:

            if estado_guard is not None:

                estado_guard.next_followup_at = None

                registrar_evento_followup_crm(
                    db=db_guard,
                    estado_crm=estado_guard,
                    event_type=(
                        "FOLLOWUP_CANCELLED_PRE_SEND"
                    ),
                    reason=(
                        "El contexto cambió durante la "
                        "barrera final pre-Twilio."
                    ),
                    step_number=numero_followup,
                    scheduled_for=(
                        scheduled_original
                    ),
                )

                db_guard.commit()

            print(
                "🛑 FOLLOWUP CANCELADO PRE-TWILIO: "
                f"contact_id={contact.id}, "
                "se detectó actividad concurrente."
            )

            return False

    finally:
        db_guard.close()

    # --------------------------------------------------------
    # TWILIO
    # --------------------------------------------------------

    destino = str(
        estado_crm.prospect_phone
        or contact.phone_number
        or ""
    ).strip()

    if not destino.startswith(
        "whatsapp:"
    ):
        destino = (
            f"whatsapp:{destino}"
        )

    resultado_twilio = (
        enviar_respuesta_twilio(
            destino,
            mensaje_followup,
        )
    )

    if not str(
        resultado_twilio
    ).startswith(
        "✅"
    ):
        estado_crm = (
            obtener_estado_followup_crm(
                db,
                contact.id,
            )
        )

        if estado_crm is not None:

            registrar_evento_followup_crm(
                db=db,
                estado_crm=estado_crm,
                event_type="FOLLOWUP_SEND_FAILED",
                reason=str(
                    resultado_twilio
                ),
                step_number=numero_followup,
            )

            # Error controlado de Twilio:
            # reintentar en 5 minutos siempre que la
            # ventana legal siga abierta.
            expiracion_retry = (
                obtener_expiracion_ventana_whatsapp(
                    estado_crm.last_inbound_at
                )
            )

            candidato_retry = (
                datetime.now(timezone.utc)
                + timedelta(minutes=5)
            )

            if (
                expiracion_retry is not None
                and candidato_retry
                < expiracion_retry
            ):
                estado_crm.next_followup_at = (
                    candidato_retry
                )

            else:
                estado_crm.next_followup_at = None

        db.commit()
        return False
        
    # Extraer SID del helper actual.
    coincidencia_sid = re.search(
        r"SID:\s*([A-Za-z0-9]+)",
        str(resultado_twilio),
    )

    twilio_sid = (
        coincidencia_sid.group(1)
        if coincidencia_sid
        else None
    )

    # Persistir como un mensaje normal.
    mensaje_guardado = save_message(
        db=db,
        contact_id=contact.id,
        direction="outgoing",
        content=mensaje_followup,
        twilio_sid=twilio_sid,
    )

    estado_crm = (
        obtener_estado_followup_crm(
            db,
            contact.id,
        )
    )

    if estado_crm is None:
        return True

    estado_crm.last_followup_at = (
        datetime.now(
            timezone.utc
        )
    )

    estado_crm.followup_count = int(
        estado_crm.followup_count
        or 0
    ) + 1

    registrar_evento_followup_crm(
        db=db,
        estado_crm=estado_crm,
        event_type="FOLLOWUP_SENT",
        reason=(
            "Seguimiento automático enviado "
            "después de revalidar contexto."
        ),
        step_number=numero_followup,
        message_id=(
            mensaje_guardado.id
            if mensaje_guardado
            else None
        ),
        twilio_sid=twilio_sid,
    )

    programar_siguiente_followup_crm(
        db=db,
        estado_crm=estado_crm,
        numero_enviado=numero_followup,
    )

    estado_crm.updated_at = (
        datetime.now(
            timezone.utc
        )
    )

    db.commit()

    print(
        "📨 FOLLOWUP ENVIADO: "
        f"contact_id={contact.id}, "
        f"step={numero_followup}, "
        f"next={estado_crm.next_followup_at}"
    )

    return True
    

def procesar_followups_vencidos():
    """
    Procesa un lote pequeño de seguimientos vencidos.
    """

    ahora = datetime.now(
        timezone.utc
    )

    db = SessionLocal()

    try:
        estados = (
            db.query(
                ContactFollowUpState
            )
            .filter(
                ContactFollowUpState.next_followup_at
                .isnot(None),

                ContactFollowUpState.next_followup_at
                <= ahora,

                ContactFollowUpState.journey_status
                == "ACTIVE_CONVERSION",
            )
            .order_by(
                ContactFollowUpState.next_followup_at
                .asc()
            )
            .limit(20)
            .all()
        )

        for estado_crm in estados:

            try:
                procesar_followup_estado_crm(
                    db=db,
                    estado_crm=estado_crm,
                )

            except Exception as e:

                db.rollback()

                print(
                    "❌ ERROR PROCESANDO FOLLOWUP: "
                    f"contact_id="
                    f"{getattr(estado_crm, 'contact_id', None)}, "
                    f"error={e}"
                )

    finally:
        db.close()


def ejecutar_ciclo_followup_con_lock():
    """
    Sólo una instancia Railway puede ejecutar un lote
    simultáneamente.
    """

    if not FOLLOWUP_AUTOMATION_MASTER_ENABLED:
        return

    if not DATABASE_URL.startswith(
        "postgresql"
    ):
        procesar_followups_vencidos()
        return

    with engine.connect() as conn:

        adquirido = conn.execute(
            text(
                "SELECT pg_try_advisory_lock(:lock_key)"
            ),
            {
                "lock_key": (
                    CRM_FOLLOWUP_ADVISORY_LOCK_KEY
                )
            },
        ).scalar()

        if not adquirido:
            return

        try:
            procesar_followups_vencidos()

        finally:
            try:
                conn.execute(
                    text(
                        "SELECT pg_advisory_unlock(:lock_key)"
                    ),
                    {
                        "lock_key": (
                            CRM_FOLLOWUP_ADVISORY_LOCK_KEY
                        )
                    },
                )
            except Exception:
                pass


FOLLOWUP_WORKER_STOP_EVENT = (
    threading.Event()
)


def followup_worker_loop():
    """
    Worker persistente.

    No utiliza threading.Timer para esperas largas.
    El calendario vive en PostgreSQL.
    """

    print(
        "🕒 FOLLOWUP WORKER iniciado."
    )

    while not (
        FOLLOWUP_WORKER_STOP_EVENT.is_set()
    ):

        try:
            ejecutar_ciclo_followup_con_lock()

        except Exception as e:
            print(
                "❌ ERROR FOLLOWUP WORKER: "
                f"{e}"
            )

        FOLLOWUP_WORKER_STOP_EVENT.wait(
            CRM_FOLLOWUP_POLL_SECONDS
        )

    print(
        "🛑 FOLLOWUP WORKER detenido."
    )

def sincronizar_habilitacion_followup_cohorte():
    """
    Actualiza exclusivamente contactos que ya pertenecen
    a la cohorte CRM.

    Los contactos históricos siguen excluidos porque nunca
    tuvieron ContactFollowUpState.
    """

    if not FOLLOWUP_AUTOMATION_MASTER_ENABLED:
        return

    db = SessionLocal()

    try:
        estados = (
            db.query(
                ContactFollowUpState
            )
            .filter(
                ContactFollowUpState.automation_enrolled_at
                .isnot(None)
            )
            .all()
        )

        actualizados = 0

        for estado in estados:

            if not estado.automation_enabled:
                estado.automation_enabled = True
                estado.updated_at = datetime.now(
                    timezone.utc
                )
                actualizados += 1

        db.commit()

        print(
            "🤖 FOLLOWUP COHORTE HABILITADA: "
            f"actualizados={actualizados}"
        )

    except Exception as e:

        db.rollback()

        print(
            "❌ Error habilitando cohorte followup: "
            f"{e}"
        )

    finally:
        db.close()
        

def activar_crm_admisiones_si_elegible(
    db: Session,
    contact,
):
    """
    Enrola comercialmente únicamente a contactos que YA poseen
    ContactFollowUpState.

    Los contactos históricos no tienen esa fila y quedan
    excluidos de automatizaciones hasta una futura activación
    manual explícita.
    """

    if contact is None:
        return None

    estado = obtener_estado_followup_crm(
        db,
        contact.id,
    )

    if estado is None:
        print(
            "ℹ️ CRM FOLLOWUP EXCLUIDO: "
            f"contact_id={contact.id} "
            "sin estado de cohorte; "
            "se considera contacto histórico."
        )

        return None

    ahora = datetime.now(
        timezone.utc
    )

    # --------------------------------------------------------
    # PRIMER INGRESO AL EMBUDO DE ADMISIONES
    # --------------------------------------------------------

    if estado.journey_status == "NOT_ENROLLED":

        estado.automation_enrolled_at = ahora

        # La cohorte puede utilizar automatización cuando
        # el interruptor maestro de producción está activo.
        estado.automation_enabled = (
            FOLLOWUP_AUTOMATION_MASTER_ENABLED
        )

        estado.commercial_goal = (
            CRM_COMMERCIAL_GOAL_VISITA
        )

        estado.journey_status = (
            "ACTIVE_CONVERSION"
        )

        estado.conversation_cycle_id = (
            str(uuid.uuid4())
        )

        estado.conversation_mode = (
            "PRIMER_CONTACTO"
        )

        estado.active_goal = (
            "CONDUCIR_A_CITA"
        )

        estado.active_goal_status = "ACTIVE"

        estado.followup_step = 0
        estado.next_followup_at = None
        estado.next_nurturing_at = None

        registrar_evento_followup_crm(
            db=db,
            estado_crm=estado,
            event_type="CRM_ENROLLED_ADMISSIONS",
            reason=(
                "Contacto nuevo clasificado como admisiones."
            ),
        )

        registrar_evento_followup_crm(
            db=db,
            estado_crm=estado,
            event_type="CONVERSATION_CYCLE_STARTED",
            reason="PRIMER_CONTACTO",
        )

        db.commit()
        db.refresh(estado)

        print(
            "🎯 CRM ENROLADO: "
            f"contact_id={contact.id}, "
            f"cycle={estado.conversation_cycle_id}, "
            "goal=LOGRAR_VISITA_PRESENCIAL"
        )

        return estado

    # --------------------------------------------------------
    # REACTIVACIÓN FUTURA DESDE NURTURING
    # --------------------------------------------------------

    if estado.journey_status == "NURTURING":

        # ====================================================
        # RESTAURAR POSICIÓN PREVIA A UNA PAUSA EXPLÍCITA
        # ====================================================

        etapa_contacto_actual = str(
            get_note_value(
                contact,
                "ETAPA_CONVERSACIONAL",
            )
            or ""
        ).strip().upper()

        etapa_retorno = str(
            get_note_value(
                contact,
                "ETAPA_ANTES_SEGUIMIENTO",
            )
            or ""
        ).strip().upper()

        estado_retorno = str(
            get_note_value(
                contact,
                "ESTADO_ANTES_SEGUIMIENTO",
            )
            or ""
        ).strip().upper()

        objetivo_retorno = str(
            get_note_value(
                contact,
                "OBJETIVO_ANTES_SEGUIMIENTO",
            )
            or ""
        ).strip().upper()

        restauracion_aplicable = bool(
            etapa_contacto_actual
            == "SEGUIMIENTO"
            and etapa_retorno
            in ETAPAS_CONVERSACIONALES_VALIDAS
            and estado_retorno
            in ESTADOS_COMERCIALES_VALIDOS
        )

        # ----------------------------------------------------
        # COMPATIBILIDAD CON PAUSAS ANTERIORES AL SNAPSHOT
        # ----------------------------------------------------
        #
        # Algunos contactos pudieron entrar a SEGUIMIENTO antes
        # de que existieran ETAPA_ANTES_SEGUIMIENTO,
        # ESTADO_ANTES_SEGUIMIENTO y
        # OBJETIVO_ANTES_SEGUIMIENTO.
        #
        # En ese caso recuperamos el último punto comercial
        # autoritativo mediante los hitos ya persistidos.
        # ----------------------------------------------------

        if (
            etapa_contacto_actual == "SEGUIMIENTO"
            and not restauracion_aplicable
        ):
            contexto_recuperacion = (
                construir_contexto_comercial_desde_contacto(
                    contact
                )
            )

            piso_recuperacion = (
                obtener_piso_progreso_comercial(
                    contexto_recuperacion
                )
            )

            nivel_recuperacion = int(
                piso_recuperacion.get(
                    "nivel",
                    0,
                )
                or 0
            )

            etapa_recuperada = str(
                piso_recuperacion.get(
                    "etapa",
                    "",
                )
                or ""
            ).strip().upper()

            estado_recuperado = str(
                piso_recuperacion.get(
                    "estado",
                    "",
                )
                or ""
            ).strip().upper()

            objetivo_recuperado = str(
                piso_recuperacion.get(
                    "objetivo",
                    "",
                )
                or ""
            ).strip().upper()

            if (
                nivel_recuperacion > 0
                and etapa_recuperada
                in ETAPAS_CONVERSACIONALES_VALIDAS
                and etapa_recuperada != "SEGUIMIENTO"
                and estado_recuperado
                in ESTADOS_COMERCIALES_VALIDOS
            ):
                etapa_retorno = etapa_recuperada
                estado_retorno = estado_recuperado
                objetivo_retorno = objetivo_recuperado

                restauracion_aplicable = True

                print(
                    "🧭 POSICIÓN PRE-SEGUIMIENTO "
                    "RECUPERADA POR HITOS: "
                    f"contact_id={contact.id}, "
                    f"piso={piso_recuperacion.get('piso')}, "
                    f"etapa={etapa_retorno}, "
                    f"estado={estado_retorno}, "
                    f"objetivo={objetivo_retorno}"
                )

        if restauracion_aplicable:

            set_note_value(
                contact,
                "ETAPA_CONVERSACIONAL",
                etapa_retorno,
            )

            contact.status = estado_retorno

            if (
                objetivo_retorno
                in OBJETIVOS_PENDIENTES_VALIDOS
            ):
                set_note_value(
                    contact,
                    "OBJETIVO_PENDIENTE",
                    objetivo_retorno,
                )

            equivalencias_reactivacion_flow = {
                "CONTACTO_INICIAL": (
                    "SALUDO_INICIAL"
                ),
                "REFERENCIA_COLEGIO": (
                    "ESPERANDO_REFERENCIA"
                ),
                "VALIDACION_ZONA": (
                    "VALIDACION_ZONA"
                ),
                "PRESENTACION_VALOR": (
                    "PRESENTACION_VALOR"
                ),
                "EXPLICACION_METODO": (
                    "EXPLICACION_METODO"
                ),
                "IDENTIFICACION_INTERES": (
                    "ESPERANDO_AREA_INTERES"
                ),
                "PROFUNDIZACION_INTERES": (
                    "PROFUNDIZACION_INTERES"
                ),
                "INVITACION_VISITA": (
                    "INVITACION_CITA"
                ),
                "NEGOCIACION_CITA": (
                    "ESPERANDO_FECHA_CITA"
                ),
                "ESPERANDO_CONFIRMACION_ADMIN": (
                    "ESPERANDO_CONFIRMACION_ADMIN"
                ),
                "ESPERANDO_DATOS_CITA": (
                    "ESPERANDO_DATOS_CITA"
                ),
                "VISITA_CONFIRMADA": (
                    "CITA_DATOS_COMPLETOS"
                ),
            }

            flow_retorno = (
                equivalencias_reactivacion_flow.get(
                    etapa_retorno,
                    get_flow_state(
                        contact
                    ),
                )
            )

            if (
                etapa_retorno
                == "NEGOCIACION_CITA"
                and objetivo_retorno
                == "OBTENER_HORA_CITA"
            ):
                flow_retorno = (
                    "ESPERANDO_HORA_CITA"
                )

            set_flow_state(
                contact,
                flow_retorno,
            )

            estado.current_stage = (
                etapa_retorno
            )

            estado.current_commercial_status = (
                estado_retorno
            )

            if (
                objetivo_retorno
                in OBJETIVOS_PENDIENTES_VALIDOS
            ):
                estado.current_objective = (
                    objetivo_retorno
                )

            print(
                "♻️ POSICIÓN PRE-SEGUIMIENTO RESTAURADA: "
                f"contact_id={contact.id}, "
                f"etapa={etapa_retorno}, "
                f"estado={estado_retorno}, "
                f"objetivo={objetivo_retorno}"
            )

        estado.journey_status = (
            "ACTIVE_CONVERSION"
        )

        estado.conversation_cycle_id = (
            str(uuid.uuid4())
        )

        estado.conversation_mode = (
            "REACTIVACION"
        )

        objetivo_reactivado = str(
            estado.current_objective
            or ""
        ).strip().upper()

        if objetivo_reactivado in {
            "OBTENER_FECHA_CITA",
            "OBTENER_HORA_CITA",
            "CONFIRMAR_FECHA_CITA_CALENDARIO",
            "ESPERAR_CONFIRMACION_ADMIN",
        }:
            estado.active_goal = (
                "CONCRETAR_CITA"
            )

        elif objetivo_reactivado == (
            "OBTENER_DECISION_VISITA"
        ):
            estado.active_goal = (
                "LOGRAR_DECISION_VISITA"
            )

        else:
            estado.active_goal = (
                "CONDUCIR_A_CITA"
            )

        estado.active_goal_status = "ACTIVE"

        estado.followup_step = 0
        estado.next_followup_at = None
        estado.next_nurturing_at = None

        registrar_evento_followup_crm(
            db=db,
            estado_crm=estado,
            event_type=(
                "CONVERSATION_CYCLE_REACTIVATED"
            ),
            reason=(
                "Nueva manifestación del prospecto "
                "durante nurturing."
            ),
        )

        db.commit()
        db.refresh(estado)

        print(
            "♻️ CRM REACTIVADO: "
            f"contact_id={contact.id}, "
            f"cycle={estado.conversation_cycle_id}"
        )

    return estado


def actualizar_crm_por_mensaje(
    db: Session,
    contact_id: int,
    direction: str,
    timestamp,
):
    """
    Sincroniza las horas reales de mensajes con el CRM.

    No crea estados para contactos históricos.
    """

    estado = obtener_estado_followup_crm(
        db,
        contact_id,
    )

    if estado is None:
        return

    direccion = str(
        direction or ""
    ).strip().lower()

    if direccion == "incoming":

        tenia_followup_pendiente = bool(
            estado.next_followup_at
        )

        estado.last_inbound_at = timestamp

        # Toda respuesta nueva invalida una ventana anterior.
        # La nueva transición decidirá posteriormente si debe
        # abrirse una ventana distinta.
        estado.next_followup_at = None
        estado.followup_step = 0

        if tenia_followup_pendiente:
            registrar_evento_followup_crm(
                db=db,
                estado_crm=estado,
                event_type="PROSPECT_REPLIED",
                reason=(
                    "El prospecto respondió antes del "
                    "siguiente seguimiento."
                ),
            )

    elif direccion == "outgoing":

        estado.last_outbound_at = timestamp

        # Cualquier mensaje nuevo de nuestro lado invalida
        # la programación anterior.
        #
        # Si es una respuesta normal del bot, la transición
        # comercial abrirá inmediatamente después la nueva
        # ventana correcta.
        #
        # Si fue mensaje manual/admin, evitamos que quede
        # vivo un seguimiento viejo.
        estado.next_followup_at = None

    estado.updated_at = datetime.now(
        timezone.utc
    )


def sincronizar_crm_desde_transicion(
    db: Session,
    contact,
    transicion: Dict[str, Any],
):
    """
    Replica en el CRM la posición comercial resultante
    después de una respuesta exitosa.

    En esta fase sólo PERSISTE y PROGRAMA EN BD.
    NO envía seguimientos.
    """

    if contact is None:
        return None

    estado = obtener_estado_followup_crm(
        db,
        contact.id,
    )

    if estado is None:
        return None

    if not isinstance(
        transicion,
        dict,
    ):
        return estado

    # --------------------------------------------------------
    # UNA NO-TRANSICIÓN NO PUEDE MODIFICAR EL CRM
    # --------------------------------------------------------
    #
    # Si el motor comercial determinó que este turno no cambia
    # el estado, el CRM debe conservar exactamente la posición
    # anterior. En particular, nunca borrar current_objective.
    # --------------------------------------------------------

    if not bool(
        transicion.get(
            "transicion_aplicada",
            False,
        )
    ):
        print(
            "🛡️ CRM SIN CAMBIO: "
            f"contact_id={contact.id}, "
            "la transición comercial no fue aplicada; "
            "se conserva stage/status/objective actuales."
        )

        return estado

    ahora = datetime.now(
        timezone.utc
    )

    objetivo = str(
        transicion.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    etapa = str(
        transicion.get(
            "etapa_conversacional",
            "",
        )
        or ""
    ).strip().upper()

    estado_comercial = str(
        transicion.get(
            "estado_comercial",
            "",
        )
        or ""
    ).strip().upper()

    objetivo_anterior = (
        estado.current_objective
    )

    journey_anterior = (
        estado.journey_status
    )

    estado.current_objective = objetivo
    estado.current_stage = etapa
    estado.current_commercial_status = (
        estado_comercial
    )

    # --------------------------------------------------------
    # CITA YA CONFIRMADA
    # --------------------------------------------------------

    if (
        etapa == "VISITA_CONFIRMADA"
        or estado_comercial
        in {
            "VISITA_CONFIRMADA",
            "VISITA_AGENDADA",
        }
    ):
        estado.journey_status = (
            "APPOINTMENT_CONFIRMED"
        )

        estado.active_goal = (
            "COMPLETAR_CITA"
        )

        # La cita puede estar confirmada y, al mismo tiempo,
        # quedar pendiente completar datos de registro.
        #
        # No debemos marcar COMPLETAR_CITA como terminado
        # mientras Python siga esperando esos datos.
        if objetivo == "OBTENER_DATOS_CITA":
            estado.active_goal_status = "ACTIVE"
        else:
            estado.active_goal_status = "COMPLETED"

        # Una cita ya confirmada nunca genera seguimiento
        # comercial automático mientras se completa el registro.
        estado.next_followup_at = None
        estado.next_nurturing_at = None

    # --------------------------------------------------------
    # PAUSA / REGRESO A NURTURING
    # --------------------------------------------------------

    elif (
        etapa == "SEGUIMIENTO"
        or objetivo
        == "ESPERAR_REACTIVACION_PROSPECTO"
    ):
        estado.journey_status = "NURTURING"
        estado.active_goal_status = "PAUSED"
        estado.next_followup_at = None

        if estado.nurturing_started_at is None:
            estado.nurturing_started_at = ahora

        estado.next_nurturing_at = (
            ahora
            + timedelta(
                days=CRM_NURTURING_COOLDOWN_DAYS
            )
        )

        if journey_anterior != "NURTURING":
            registrar_evento_followup_crm(
                db=db,
                estado_crm=estado,
                event_type="RETURNED_TO_NURTURING",
                reason=(
                    "La conversación quedó sin conversión "
                    "activa o el prospecto pidió retomarla "
                    "posteriormente."
                ),
                scheduled_for=(
                    estado.next_nurturing_at
                ),
            )

    # --------------------------------------------------------
    # CONVERSACIÓN ACTIVA
    # --------------------------------------------------------

    else:
        estado.journey_status = (
            "ACTIVE_CONVERSION"
        )

        estado.active_goal_status = "ACTIVE"

        if objetivo == "OBTENER_DECISION_VISITA":
            estado.active_goal = (
                "LOGRAR_DECISION_VISITA"
            )

        elif objetivo in {
            "OBTENER_FECHA_CITA",
            "OBTENER_HORA_CITA",
            "CONFIRMAR_FECHA_CITA_CALENDARIO",
        }:
            estado.active_goal = (
                "CONCRETAR_CITA"
            )

        elif objetivo == "ESPERAR_CONFIRMACION_ADMIN":
            if (
                estado_comercial
                == "CITA_PENDIENTE_CONFIRMACION"
            ):
                estado.active_goal = (
                    "CONCRETAR_CITA"
                )
            else:
                estado.active_goal = (
                    "ESPERAR_DECISION_ADMIN"
                )

        elif objetivo == "OBTENER_DATOS_CITA":
            estado.active_goal = (
                "COMPLETAR_CITA"
            )

        else:
            estado.active_goal = (
                "CONDUCIR_A_CITA"
            )

        # ----------------------------------------------------
        # ¿QUIÉN DEBE RESPONDER AHORA?
        # ----------------------------------------------------
        #
        # No programamos por "etapa", sino por autoridad.
        #
        # Si el siguiente movimiento depende de administración
        # o del sistema, jamás se considera silencio comercial
        # del prospecto.
        # ----------------------------------------------------

        objetivos_sin_followup_automatico = {
            "",
            "ESPERAR_CONFIRMACION_ADMIN",
        }

        if (
            objetivo
            not in objetivos_sin_followup_automatico
        ):
            estado.followup_step = 0

            estado.next_followup_at = (
                ahora
                + timedelta(
                    minutes=(
                        CRM_FOLLOWUP_INITIAL_MINUTES
                    )
                )
            )

            estado.next_nurturing_at = None

            registrar_evento_followup_crm(
                db=db,
                estado_crm=estado,
                event_type="FOLLOWUP_WINDOW_OPENED",
                reason=(
                    "Respuesta enviada y quedó una acción "
                    "pendiente del prospecto."
                ),
                step_number=0,
                scheduled_for=(
                    estado.next_followup_at
                ),
            )

        else:
            estado.next_followup_at = None

    if (
        objetivo
        and objetivo != objetivo_anterior
    ):
        registrar_evento_followup_crm(
            db=db,
            estado_crm=estado,
            event_type="ACTIVE_OBJECTIVE_CHANGED",
            reason=(
                f"{objetivo_anterior or 'SIN_OBJETIVO'} "
                f"-> {objetivo}"
            ),
        )

    estado.updated_at = ahora

    db.commit()
    db.refresh(estado)

    print(
        "🧭 CRM FOLLOWUP SYNC: "
        f"contact_id={contact.id}, "
        f"journey={estado.journey_status}, "
        f"active_goal={estado.active_goal}, "
        f"objective={estado.current_objective}, "
        f"step={estado.followup_step}, "
        f"next_followup={estado.next_followup_at}, "
        f"automation_enabled={estado.automation_enabled}"
    )

    return estado

# ============================================================
# RECUPERACIÓN COMPLETA DEL HISTORIAL CONVERSACIONAL
# ============================================================

def obtener_historial_completo_contacto(
    db: Session,
    contact,
    max_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Recupera la conversación completa de un contacto
    en orden cronológico.

    Esta función:
    - solo consulta la base de datos;
    - no modifica contactos;
    - no modifica mensajes;
    - no realiza commits;
    - no consulta Gemini;
    - no genera respuestas;
    - no envía mensajes por Twilio.
    """

    resultado = {
        "contacto_disponible": False,
        "total_mensajes": 0,
        "conversacion": [],
        "texto_conversacion": "",
        "error": "",
    }

    if contact is None:
        resultado["error"] = "CONTACTO_NO_DISPONIBLE"
        return resultado

    try:
        query_mensajes = (
            db.query(Message)
            .filter(
                Message.contact_id == contact.id
            )
        )

        if (
            isinstance(max_message_id, int)
            and max_message_id > 0
        ):
            query_mensajes = (
                query_mensajes.filter(
                    or_(
                        Message.direction != "incoming",
                        Message.id <= max_message_id,
                    )
                )
            )

        mensajes = (
            query_mensajes
            .order_by(
                Message.timestamp.asc(),
                Message.id.asc(),
            )
            .all()
        )

    except Exception as e:
        resultado["error"] = (
            "ERROR_CONSULTANDO_HISTORIAL: "
            f"{e}"
        )
        return resultado

    conversacion = []
    lineas_texto = []

    for mensaje in mensajes:
        direccion = str(
            getattr(
                mensaje,
                "direction",
                "",
            )
            or ""
        ).strip().lower()

        if direccion == "incoming":
            emisor = "Prospecto"

        elif direccion == "outgoing":
            emisor = "Asistente"

        else:
            emisor = "Conversación"

        contenido = str(
            getattr(
                mensaje,
                "content",
                "",
            )
            or ""
        ).strip()

        timestamp = getattr(
            mensaje,
            "timestamp",
            None,
        )

        if timestamp is not None:
            try:
                fecha_iso = timestamp.isoformat()

            except AttributeError:
                fecha_iso = str(
                    timestamp
                ).strip()

        else:
            fecha_iso = ""

        registro = {
            "id": getattr(
                mensaje,
                "id",
                None,
            ),
            "direccion": direccion,
            "emisor": emisor,
            "contenido": contenido,
            "fecha": fecha_iso,
        }

        conversacion.append(
            registro
        )

        if contenido:
            if fecha_iso:
                linea = (
                    f"[{fecha_iso}] "
                    f"{emisor}: {contenido}"
                )

            else:
                linea = (
                    f"{emisor}: {contenido}"
                )

            lineas_texto.append(
                linea
            )

    resultado.update({
        "contacto_disponible": True,
        "total_mensajes": len(
            conversacion
        ),
        "conversacion": conversacion,
        "texto_conversacion": "\n".join(
            lineas_texto
        ),
        "error": "",
    })

    return resultado

# ================= APLICACIÓN FASTAPI =================
app = FastAPI(title="WhatsApp Bot CRM", version="1.0.0")

@app.on_event("startup")
def iniciar_followup_worker():
    """
    El worker sólo arranca cuando el interruptor maestro
    está explícitamente habilitado en Railway.
    """

    if not FOLLOWUP_AUTOMATION_MASTER_ENABLED:

        print(
            "⏸️ FOLLOWUP AUTOMATION deshabilitada "
            "por variable de entorno."
        )

        return

    worker = threading.Thread(
        target=followup_worker_loop,
        name="crm-followup-worker",
        daemon=True,
    )

    worker.start()

    print(
        "✅ FOLLOWUP AUTOMATION habilitada."
    )


@app.on_event("shutdown")
def detener_followup_worker():

    FOLLOWUP_WORKER_STOP_EVENT.set()


# ================= ENDPOINTS PRINCIPALES =================
@app.get("/")
async def root():
    return {
        "status": "WhatsApp Bot CRM",
        "endpoints": {
            "webhook": "/webhook/whatsapp (POST)",
            "contacts": "/contacts (GET)",
            "conversations": "/conversations/{phone} (GET)",
            "panel": "/panel (GET)",
            "health": "/health (GET)"
        }
    }

@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    """Verifica salud de la aplicación y base de datos"""
    try:
        db.execute(text("SELECT 1"))
        db_status = "✅ Conectada"
        total_contacts = db.query(Contact).count()
        total_messages = db.query(Message).count()
    except Exception as e:
        db_status = f"❌ Error: {str(e)}"
        total_contacts = 0
        total_messages = 0

    gemini_status = "✅ Configurado" if GEMINI_API_KEY else "❌ No configurado"

    return {
        "status": "healthy",
        "database": db_status,
        "gemini": gemini_status,
        "gemini_model": GEMINI_MODEL if GEMINI_API_KEY else "No configurado",
        "statistics": {
            "total_contacts": total_contacts,
            "total_messages": total_messages
        },
        "twilio_configured": bool(os.getenv("TWILIO_API_KEY"))
    }

# ============================================================
# ENDPOINT AISLADO PARA PROBAR EL FLUJO ESTRUCTURADO
# ============================================================

class GooglePlacesTestRequest(BaseModel):
    """
    Datos permitidos para probar de forma aislada
    la búsqueda de una localidad en Google Places.
    """
    localidad: str


@app.post("/debug/google-places")
def debug_google_places(
    payload: GooglePlacesTestRequest,
):
    """
    Prueba buscar_localidad_google_places() sin afectar:

    - conversaciones;
    - contactos;
    - mensajes;
    - FLOW_STATE;
    - tareas de administrador;
    - webhook productivo.
    """
    endpoint_habilitado = (
        os.getenv(
            "ENABLE_STRUCTURED_FLOW_TEST_ENDPOINT",
            "false",
        )
        .strip()
        .lower()
        in ["true", "1", "yes", "si", "sí"]
    )

    if not endpoint_habilitado:
        raise HTTPException(
            status_code=404,
            detail="Endpoint de prueba no habilitado.",
        )

    localidad = str(
        payload.localidad or ""
    ).strip()

    if not localidad:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar una localidad.",
        )

    resultado = buscar_localidad_google_places(
        localidad
    )

    return {
        "modo": "PRUEBA_GOOGLE_PLACES",
        "sin_efectos_secundarios": True,
        "localidad_recibida": localidad,
        "resultado": resultado,
    }

@app.post("/debug/google-route-by-locality")
def debug_google_route_by_locality(
    payload: GooglePlacesTestRequest,
):
    """
    Busca una localidad mediante Google Places y después
    calcula la ruta en automóvil hasta el colegio.

    Este endpoint no:

    - modifica contactos;
    - guarda mensajes;
    - cambia FLOW_STATE;
    - crea tareas de administrador;
    - envía mensajes por Twilio;
    - sustituye el webhook productivo.
    """
    endpoint_habilitado = (
        os.getenv(
            "ENABLE_STRUCTURED_FLOW_TEST_ENDPOINT",
            "false",
        )
        .strip()
        .lower()
        in ["true", "1", "yes", "si", "sí"]
    )

    if not endpoint_habilitado:
        raise HTTPException(
            status_code=404,
            detail="Endpoint de prueba no habilitado.",
        )

    localidad = str(
        payload.localidad or ""
    ).strip()

    if not localidad:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar una localidad.",
        )

    resultado_places = (
        buscar_localidad_google_places(
            localidad
        )
    )

    if not resultado_places.get("encontrado"):
        return {
            "modo": "PRUEBA_GOOGLE_ROUTE_POR_LOCALIDAD",
            "sin_efectos_secundarios": True,
            "localidad_recibida": localidad,
            "places": resultado_places,
            "ruta": None,
            "error": (
                "No fue posible localizar la localidad "
                "mediante Google Places."
            ),
        }

    latitud = resultado_places.get(
        "latitud"
    )

    longitud = resultado_places.get(
        "longitud"
    )

    resultado_ruta = calcular_ruta_google_routes(
        latitud_origen=latitud,
        longitud_origen=longitud,
    )

    return {
        "modo": "PRUEBA_GOOGLE_ROUTE_POR_LOCALIDAD",
        "sin_efectos_secundarios": True,
        "localidad_recibida": localidad,
        "places": resultado_places,
        "ruta": resultado_ruta,
        "error": "",
    }


class StructuredFlowTestRequest(BaseModel):
    """
    Datos permitidos para una prueba aislada del nuevo flujo.

    phone_number es opcional. Si se proporciona y existe el contacto,
    se utiliza su contexto real solamente para lectura.
    """
    message: str
    phone_number: Optional[str] = None
    persistir: bool = False


@app.post("/debug/structured-flow")
async def debug_structured_flow(
    payload: StructuredFlowTestRequest,
    db: Session = Depends(get_db),
):
    """
    Ejecuta el nuevo orquestador sin afectar la conversación productiva.

    No:
    - envía mensajes por Twilio;
    - guarda mensajes;
    - modifica el contacto, salvo que persistir=true;
    - cambia FLOW_STATE;
    - crea tareas de administrador;
    - sustituye el webhook actual.
    """
    endpoint_habilitado = (
        os.getenv(
            "ENABLE_STRUCTURED_FLOW_TEST_ENDPOINT",
            "false",
        )
        .strip()
        .lower()
        in ["true", "1", "yes", "si", "sí"]
    )

    if not endpoint_habilitado:
        raise HTTPException(
            status_code=404,
            detail="Endpoint de prueba no habilitado.",
        )

    mensaje = (payload.message or "").strip()

    if not mensaje:
        raise HTTPException(
            status_code=400,
            detail="El campo message no puede estar vacío.",
        )

    contact = None
    history = []

    numero_recibido = (
        payload.phone_number or ""
    ).strip()

    if numero_recibido:
        variantes_numero = {
            numero_recibido,
        }

        if numero_recibido.startswith("whatsapp:"):
            variantes_numero.add(
                numero_recibido.replace(
                    "whatsapp:",
                    "",
                    1,
                )
            )
        else:
            variantes_numero.add(
                f"whatsapp:{numero_recibido}"
            )

        contact = (
            db.query(Contact)
            .filter(
                Contact.phone_number.in_(
                    list(variantes_numero)
                )
            )
            .first()
        )

        if contact is not None:
            mensajes_recientes = (
                db.query(Message)
                .filter(
                    Message.contact_id == contact.id
                )
                .order_by(
                    Message.timestamp.desc()
                )
                .limit(8)
                .all()
            )

            history = list(
                reversed(mensajes_recientes)
            )

    resultado = procesar_mensaje_prospecto_estructurado(
        mensaje_usuario=mensaje,
        contact=contact,
        history=history,
    )

    resultado_persistencia = {
        "persistido": False,
        "campos_actualizados": [],
        "error": "",
    }

    if payload.persistir:
        if not numero_recibido:
            resultado_persistencia["error"] = (
                "PHONE_NUMBER_REQUERIDO"
            )

        elif contact is None:
            resultado_persistencia["error"] = (
                "CONTACTO_NO_ENCONTRADO"
            )

        elif not resultado.get("procesado"):
            resultado_persistencia["error"] = (
                "RESULTADO_NO_PROCESADO"
            )

        else:
            resultado_persistencia = (
                persistir_resultado_estructurado(
                    db=db,
                    contact=contact,
                    resultado=resultado,
                )
            )

    return {
        "modo": "PRUEBA_AISLADA",
        "sin_efectos_secundarios": (
            not resultado_persistencia.get(
                "persistido",
                False,
            )
        ),
        "persistencia_solicitada": payload.persistir,
        "contacto_encontrado": contact is not None,
        "mensajes_de_contexto": len(history),
        "persistencia": resultado_persistencia,
        "resultado": resultado,
    }

# ============================================================
# ENDPOINT AISLADO DE CONTEXTO COMERCIAL
# ============================================================

class HistoricalFlowSimulationRequest(BaseModel):
    """
    Datos permitidos para simular la continuación de una
    conversación existente mediante el flujo estructurado.
    """

    phone_number: str
    message: str

class CommercialContextTestRequest(BaseModel):
    """
    Datos permitidos para consultar el contexto comercial
    de un contacto existente.

    Este endpoint es exclusivamente de lectura.
    """
    phone_number: str


@app.post("/debug/commercial-context")
async def debug_commercial_context(
    payload: CommercialContextTestRequest,
    db: Session = Depends(get_db),
):
    """
    Construye y devuelve el contexto comercial de un contacto.

    No:
    - crea contactos;
    - modifica contactos;
    - guarda mensajes;
    - modifica notes;
    - cambia contact.status;
    - cambia FLOW_STATE;
    - realiza commits;
    - consulta Gemini;
    - envía mensajes por Twilio;
    - crea tareas administrativas;
    - sustituye el webhook productivo.
    """

    endpoint_habilitado = (
        os.getenv(
            "ENABLE_STRUCTURED_FLOW_TEST_ENDPOINT",
            "false",
        )
        .strip()
        .lower()
        in ["true", "1", "yes", "si", "sí"]
    )

    if not endpoint_habilitado:
        raise HTTPException(
            status_code=404,
            detail="Endpoint de prueba no habilitado.",
        )

    numero_recibido = str(
        payload.phone_number or ""
    ).strip()

    if not numero_recibido:
        raise HTTPException(
            status_code=400,
            detail=(
                "El campo phone_number "
                "no puede estar vacío."
            ),
        )

    variantes_numero = {
        numero_recibido,
    }

    if numero_recibido.startswith(
        "whatsapp:"
    ):
        variantes_numero.add(
            numero_recibido.replace(
                "whatsapp:",
                "",
                1,
            )
        )

    else:
        variantes_numero.add(
            f"whatsapp:{numero_recibido}"
        )

    contact = (
        db.query(Contact)
        .filter(
            Contact.phone_number.in_(
                list(variantes_numero)
            )
        )
        .first()
    )

    if contact is None:
        return {
            "modo": "CONSULTA_AISLADA",
            "solo_lectura": True,
            "contacto_encontrado": False,
            "phone_number_recibido": (
                numero_recibido
            ),
            "contexto_comercial": (
                crear_contexto_comercial_vacio()
            ),
            "error": "CONTACTO_NO_ENCONTRADO",
        }

    total_mensajes_db = (
        db.query(Message)
        .filter(
            Message.contact_id == contact.id
        )
        .count()
    )

    contexto_comercial = (
        construir_contexto_comercial_desde_contacto(
            contact
        )
    )

    historial_completo = (
        obtener_historial_completo_contacto(
            db=db,
            contact=contact,
        )
    )

    resultado_memoria_historica = (
        extraer_memoria_historica_con_ia(
            texto_conversacion=(
                historial_completo.get(
                    "texto_conversacion",
                    "",
                )
            )
        )
    )

    contexto_comercial_enriquecido = (
        enriquecer_contexto_comercial_con_memoria(
            contexto_comercial=(
                contexto_comercial
            ),
            resultado_memoria=(
                resultado_memoria_historica
            ),
        )
    )

    return {
        "modo": "CONSULTA_AISLADA",
        "solo_lectura": True,
        "contacto_encontrado": True,
        "phone_number_recibido": (
            numero_recibido
        ),
        "contacto": {
            "id": contact.id,
            "phone_number": (
                contact.phone_number
            ),
            "status": (
                contact.status
            ),
            "first_contact": (
                contact.first_contact.isoformat()
                if contact.first_contact
                else ""
            ),
            "last_contact": (
                contact.last_contact.isoformat()
                if contact.last_contact
                else ""
            ),
            "total_messages_registrado": (
                contact.total_messages
            ),
            "total_messages_db": (
                total_mensajes_db
            ),
            "is_competitor": (
                contact.is_competitor
            ),
        },
        "contexto_comercial": (
            contexto_comercial
        ),
        "contexto_comercial_enriquecido": (
            contexto_comercial_enriquecido
        ),
        "historial_completo": (
            historial_completo
        ),
        "memoria_historica_ia": (
            resultado_memoria_historica
        ),
        "error": "",
    }

@app.post("/debug/historical-flow-simulation")
async def debug_historical_flow_simulation(
    payload: HistoricalFlowSimulationRequest,
    db: Session = Depends(get_db),
):
    """
    Simula la continuación del flujo estructurado utilizando
    el historial completo y el contexto comercial enriquecido.

    Este endpoint:
    - no guarda mensajes;
    - no modifica contactos;
    - no modifica notes;
    - no cambia contact.status;
    - no cambia FLOW_STATE;
    - no realiza commits;
    - no envía mensajes por Twilio;
    - no crea tareas administrativas;
    - no sustituye el webhook productivo.
    """

    endpoint_habilitado = (
        os.getenv(
            "ENABLE_STRUCTURED_FLOW_TEST_ENDPOINT",
            "false",
        )
        .strip()
        .lower()
        in ["true", "1", "yes", "si", "sí"]
    )

    if not endpoint_habilitado:
        raise HTTPException(
            status_code=404,
            detail="Endpoint de prueba no habilitado.",
        )

    numero_recibido = str(
        payload.phone_number or ""
    ).strip()

    mensaje = str(
        payload.message or ""
    ).strip()

    if not numero_recibido:
        raise HTTPException(
            status_code=400,
            detail=(
                "El campo phone_number "
                "no puede estar vacío."
            ),
        )

    if not mensaje:
        raise HTTPException(
            status_code=400,
            detail=(
                "El campo message "
                "no puede estar vacío."
            ),
        )

    variantes_numero = {
        numero_recibido,
    }

    if numero_recibido.startswith(
        "whatsapp:"
    ):
        variantes_numero.add(
            numero_recibido.replace(
                "whatsapp:",
                "",
                1,
            )
        )

    else:
        variantes_numero.add(
            f"whatsapp:{numero_recibido}"
        )

    contact = (
        db.query(Contact)
        .filter(
            Contact.phone_number.in_(
                list(variantes_numero)
            )
        )
        .first()
    )

    if contact is None:
        return {
            "modo": (
                "SIMULACION_HISTORICA_AISLADA"
            ),
            "solo_lectura": True,
            "contacto_encontrado": False,
            "phone_number_recibido": (
                numero_recibido
            ),
            "mensaje_simulado": mensaje,
            "error": "CONTACTO_NO_ENCONTRADO",
        }

    # --------------------------------------------------------
    # HISTORIAL COMPLETO
    # --------------------------------------------------------

    historial_completo = (
        obtener_historial_completo_contacto(
            db=db,
            contact=contact,
        )
    )

    # --------------------------------------------------------
    # CONTEXTO GUARDADO EN EL CONTACTO
    # --------------------------------------------------------

    contexto_comercial = (
        construir_contexto_comercial_desde_contacto(
            contact
        )
    )

    # --------------------------------------------------------
    # MEMORIA HISTÓRICA
    # --------------------------------------------------------

    resultado_memoria_historica = (
        extraer_memoria_historica_con_ia(
            texto_conversacion=(
                historial_completo.get(
                    "texto_conversacion",
                    "",
                )
            )
        )
    )

    # --------------------------------------------------------
    # CONTEXTO ENRIQUECIDO
    # --------------------------------------------------------

    contexto_enriquecido = (
        enriquecer_contexto_comercial_con_memoria(
            contexto_comercial=(
                contexto_comercial
            ),
            resultado_memoria=(
                resultado_memoria_historica
            ),
        )
    )

    # --------------------------------------------------------
    # CONTACTO VIRTUAL DE SIMULACIÓN
    # --------------------------------------------------------

    class ContactoSimulacion:
        """
        Réplica temporal y no persistente del contacto.

        get_note_value() y get_flow_state() pueden leerla como
        si fuera un contacto real, pero no está asociada a la
        sesión de SQLAlchemy.
        """

        pass

    contacto_simulacion = ContactoSimulacion()

    contacto_simulacion.id = contact.id
    contacto_simulacion.phone_number = (
        contact.phone_number
    )

    contacto_simulacion.status = str(
        contexto_enriquecido.get(
            "estado_comercial",
            "PROSPECTO_NUEVO",
        )
        or "PROSPECTO_NUEVO"
    ).strip()

    contacto_simulacion.total_messages = (
        contact.total_messages
    )

    contacto_simulacion.first_contact = (
        contact.first_contact
    )

    contacto_simulacion.last_contact = (
        contact.last_contact
    )

    contacto_simulacion.is_competitor = (
        contact.is_competitor
    )

    notas_simulacion = []

    etapa_enriquecida = str(
        contexto_enriquecido.get(
            "etapa_conversacional",
            "CONTACTO_INICIAL",
        )
        or "CONTACTO_INICIAL"
    ).strip()

    notas_simulacion.append(
        "FLOW_STATE:"
        f"{etapa_enriquecida}"
    )

    notas_simulacion.append(
        "ETAPA_CONVERSACIONAL:"
        f"{etapa_enriquecida}"
    )

    zona_interes = str(
        contexto_enriquecido.get(
            "zona_interes",
            "",
        )
        or ""
    ).strip()

    if zona_interes:
        notas_simulacion.append(
            f"ZONA_INTERES:{zona_interes}"
        )

    alumnos = contexto_enriquecido.get(
        "alumnos"
    )

    if not isinstance(alumnos, list):
        alumnos = []

    primer_alumno = (
        alumnos[0]
        if alumnos
        and isinstance(
            alumnos[0],
            dict,
        )
        else {}
    )

    nivel_interes = str(
        primer_alumno.get(
            "nivel_interes",
            "",
        )
        or ""
    ).strip()

    if nivel_interes:
        notas_simulacion.append(
            f"NIVEL_INTERES:{nivel_interes}"
        )

    nombre_alumno = str(
        primer_alumno.get(
            "nombre",
            "",
        )
        or ""
    ).strip()

    if nombre_alumno:
        notas_simulacion.append(
            f"NOMBRE_ALUMNO:{nombre_alumno}"
        )

    grado_interes = str(
        primer_alumno.get(
            "grado_interes",
            "",
        )
        or ""
    ).strip()

    if grado_interes:
        notas_simulacion.append(
            f"GRADO_INTERES:{grado_interes}"
        )

    nombre_tutor = str(
        contexto_enriquecido.get(
            "nombre_tutor",
            "",
        )
        or ""
    ).strip()

    if nombre_tutor:
        notas_simulacion.append(
            f"NOMBRE_TUTOR:{nombre_tutor}"
        )

    referencia_colegio = str(
        contexto_enriquecido.get(
            "referencia_colegio",
            "",
        )
        or ""
    ).strip()

    if referencia_colegio:
        notas_simulacion.append(
            "REFERENCIA_COLEGIO:"
            f"{referencia_colegio}"
        )

    for (
        clave_nota,
        campo_contexto,
    ) in [
        (
            "HITOS_COMERCIALES",
            "hitos_comerciales",
        ),
        (
            "TEMAS_EXPLICADOS",
            "temas_explicados",
        ),
        (
            "AREAS_INTERES",
            "areas_interes",
        ),
        (
            "OBJECIONES_DETECTADAS",
            "objeciones_detectadas",
        ),
    ]:
        valores = normalizar_lista_textos(
            contexto_enriquecido.get(
                campo_contexto,
                [],
            )
        )

        if valores:
            valores_json = json.dumps(
                valores,
                ensure_ascii=False,
            )

            notas_simulacion.append(
                f"{clave_nota}:{valores_json}"
            )

    resumen_relacion = str(
        contexto_enriquecido.get(
            "resumen_relacion",
            "",
        )
        or ""
    ).strip()

    if resumen_relacion:
        notas_simulacion.append(
            "RESUMEN_RELACION:"
            f"{resumen_relacion}"
        )

    memoria = (
        resultado_memoria_historica.get(
            "memoria",
            {},
        )
        if isinstance(
            resultado_memoria_historica,
            dict,
        )
        else {}
    )

    if not isinstance(memoria, dict):
        memoria = {}

    fecha_cita_texto = str(
        memoria.get(
            "fecha_cita_texto",
            "",
        )
        or ""
    ).strip()

    if fecha_cita_texto:
        notas_simulacion.append(
            "FECHA_CITA_TEXTO:"
            f"{fecha_cita_texto}"
        )

    fecha_cita_iso = str(
        memoria.get(
            "fecha_cita_iso",
            "",
        )
        or ""
    ).strip()

    if fecha_cita_iso:
        notas_simulacion.append(
            f"FECHA_CITA_ISO:{fecha_cita_iso}"
        )

    hora_cita_texto = str(
        memoria.get(
            "hora_cita_texto",
            "",
        )
        or ""
    ).strip()

    if hora_cita_texto:
        notas_simulacion.append(
            "HORA_CITA_TEXTO:"
            f"{hora_cita_texto}"
        )

    hora_cita_24h = str(
        memoria.get(
            "hora_cita_24h",
            "",
        )
        or ""
    ).strip()

    if hora_cita_24h:
        notas_simulacion.append(
            f"HORA_CITA_24H:{hora_cita_24h}"
        )

    contacto_simulacion.notes = "\n".join(
        notas_simulacion
    )

    # --------------------------------------------------------
    # HISTORIAL PARA EL ORQUESTADOR
    # --------------------------------------------------------

    history = (
        db.query(Message)
        .filter(
            Message.contact_id == contact.id
        )
        .order_by(
            Message.timestamp.asc()
        )
        .all()
    )

    # --------------------------------------------------------
    # SIMULACIÓN DEL ORQUESTADOR
    # --------------------------------------------------------

    resultado_orquestador = (
        procesar_mensaje_prospecto_estructurado(
            mensaje_usuario=mensaje,
            contact=contacto_simulacion,
            history=history,
        )
    )

    return {
        "modo": (
            "SIMULACION_HISTORICA_AISLADA"
        ),
        "solo_lectura": True,
        "contacto_encontrado": True,
        "phone_number_recibido": (
            numero_recibido
        ),
        "mensaje_simulado": mensaje,
        "contacto_real_sin_modificar": {
            "id": contact.id,
            "phone_number": (
                contact.phone_number
            ),
            "status": contact.status,
            "notes": contact.notes,
            "total_messages": (
                contact.total_messages
            ),
        },
        "historial_completo": (
            historial_completo
        ),
        "memoria_historica_ia": (
            resultado_memoria_historica
        ),
        "contexto_comercial_original": (
            contexto_comercial
        ),
        "contexto_comercial_enriquecido": (
            contexto_enriquecido
        ),
        "contacto_virtual": {
            "status": (
                contacto_simulacion.status
            ),
            "notes": (
                contacto_simulacion.notes
            ),
        },
        "resultado_orquestador": (
            resultado_orquestador
        ),
        "error": "",
    }

class DebugStructuredAdminEscalationRequest(BaseModel):
    phone_number: str
    message: str


@app.post("/debug/structured-admin-escalation")
async def debug_structured_admin_escalation(
    payload: DebugStructuredAdminEscalationRequest,
    db: Session = Depends(get_db),
):
    """
    Evalúa el puente de escalación administrativa sin crear tareas,
    sin enviar WhatsApp y sin modificar al contacto.
    """

    numero_recibido = str(
        payload.phone_number or ""
    ).strip()

    mensaje = str(
        payload.message or ""
    ).strip()

    if not numero_recibido:
        return {
            "modo": "DIAGNOSTICO_ESCALACION_ADMIN",
            "solo_lectura": True,
            "error": "PHONE_NUMBER_REQUERIDO",
        }

    if not mensaje:
        return {
            "modo": "DIAGNOSTICO_ESCALACION_ADMIN",
            "solo_lectura": True,
            "error": "MESSAGE_REQUERIDO",
        }

    numero_limpio = normalizar_numero_whatsapp(
        numero_recibido
    )

    contact = (
        db.query(Contact)
        .filter(
            Contact.phone_number == numero_limpio
        )
        .first()
    )

    if contact is None:
        return {
            "modo": "DIAGNOSTICO_ESCALACION_ADMIN",
            "solo_lectura": True,
            "contacto_encontrado": False,
            "phone_number_recibido": numero_recibido,
            "error": "CONTACTO_NO_ENCONTRADO",
        }

    history = (
        db.query(Message)
        .filter(
            Message.contact_id == contact.id
        )
        .order_by(
            Message.timestamp.asc()
        )
        .all()
    )

    historial_completo = (
        obtener_historial_completo_contacto(
            db=db,
            contact=contact,
        )
    )

    resultado_memoria_historica = (
        extraer_memoria_historica_con_ia(
            historial_completo.get(
                "texto_conversacion",
                "",
            )
        )
    )

    resultado_orquestador = (
        procesar_mensaje_prospecto_estructurado(
            mensaje_usuario=mensaje,
            contact=contact,
            history=history,
        )
    )

    respuesta_bot = str(
        resultado_orquestador.get(
            "respuesta_generada",
            "",
        )
        or ""
    ).strip()

    resultado_escalacion = (
        procesar_escalacion_admin_estructurada(
            db=db,
            contact=contact,
            mensaje_usuario=mensaje,
            respuesta_bot=respuesta_bot,
            resultado_orquestador=resultado_orquestador,
            memoria_historica=(
                resultado_memoria_historica
            ),
            ejecutar_envio=False,
        )
    )

    return {
        "modo": "DIAGNOSTICO_ESCALACION_ADMIN",
        "solo_lectura": True,
        "contacto_encontrado": True,
        "phone_number_recibido": numero_recibido,
        "mensaje_simulado": mensaje,
        "resultado_orquestador": (
            resultado_orquestador
        ),
        "escalacion_admin": (
            resultado_escalacion
        ),
        "error": "",
    }

class DebugStructuredAdminEscalationLiveRequest(BaseModel):
    phone_number: str
    message: str
    confirmation: str


@app.post("/debug/structured-admin-escalation-live")
async def debug_structured_admin_escalation_live(
    payload: DebugStructuredAdminEscalationLiveRequest,
    db: Session = Depends(get_db),
):
    """
    Ejecuta una prueba real y controlada del puente administrativo.

    Esta ruta:
    - puede crear o reutilizar una tarea pendiente;
    - puede enviar una alerta real al WhatsApp administrador;
    - no envía la respuesta generada al prospecto;
    - no activa el flujo estructurado en el webhook.
    """

    confirmacion = str(
        payload.confirmation or ""
    ).strip()

    if confirmacion != "ENVIAR_ALERTA_ADMIN_REAL":
        return {
            "modo": "PRUEBA_REAL_ESCALACION_ADMIN",
            "ejecucion_autorizada": False,
            "error": "CONFIRMACION_INVALIDA",
        }

    numero_recibido = str(
        payload.phone_number or ""
    ).strip()

    mensaje = str(
        payload.message or ""
    ).strip()

    if not numero_recibido:
        return {
            "modo": "PRUEBA_REAL_ESCALACION_ADMIN",
            "ejecucion_autorizada": True,
            "error": "PHONE_NUMBER_REQUERIDO",
        }

    if not mensaje:
        return {
            "modo": "PRUEBA_REAL_ESCALACION_ADMIN",
            "ejecucion_autorizada": True,
            "error": "MESSAGE_REQUERIDO",
        }

    numero_limpio = normalizar_numero_whatsapp(
        numero_recibido
    )

    contact = (
        db.query(Contact)
        .filter(
            Contact.phone_number == numero_limpio
        )
        .first()
    )

    if contact is None:
        return {
            "modo": "PRUEBA_REAL_ESCALACION_ADMIN",
            "ejecucion_autorizada": True,
            "contacto_encontrado": False,
            "phone_number_recibido": numero_recibido,
            "error": "CONTACTO_NO_ENCONTRADO",
        }

    history = (
        db.query(Message)
        .filter(
            Message.contact_id == contact.id
        )
        .order_by(
            Message.timestamp.asc()
        )
        .all()
    )

    historial_completo = (
        obtener_historial_completo_contacto(
            db=db,
            contact=contact,
        )
    )

    resultado_memoria_historica = (
        extraer_memoria_historica_con_ia(
            historial_completo.get(
                "texto_conversacion",
                "",
            )
        )
    )

    resultado_orquestador = (
        procesar_mensaje_prospecto_estructurado(
            mensaje_usuario=mensaje,
            contact=contact,
            history=history,
        )
    )

    respuesta_bot = str(
        resultado_orquestador.get(
            "respuesta_generada",
            "",
        )
        or ""
    ).strip()

    resultado_escalacion = (
        procesar_escalacion_admin_estructurada(
            db=db,
            contact=contact,
            mensaje_usuario=mensaje,
            respuesta_bot=respuesta_bot,
            resultado_orquestador=resultado_orquestador,
            memoria_historica=(
                resultado_memoria_historica
            ),
            ejecutar_envio=True,
        )
    )

    return {
        "modo": "PRUEBA_REAL_ESCALACION_ADMIN",
        "ejecucion_autorizada": True,
        "contacto_encontrado": True,
        "phone_number_recibido": numero_recibido,
        "mensaje_simulado": mensaje,
        "respuesta_provisional_prospecto": (
            respuesta_bot
        ),
        "escalacion_admin": (
            resultado_escalacion
        ),
        "advertencia": (
            "No se envió ningún mensaje al prospecto. "
            "La respuesta del administrador sí podrá "
            "continuar el flujo existente."
        ),
        "error": "",
    }
    
class DebugTwilioMessageStatusRequest(BaseModel):
    message_sid: str


@app.post("/debug/twilio-message-status")
async def debug_twilio_message_status(
    payload: DebugTwilioMessageStatusRequest,
):
    """
    Consulta directamente en Twilio el estado real de un mensaje.
    No envía mensajes ni modifica la base de datos.
    """

    message_sid = str(
        payload.message_sid or ""
    ).strip()

    if not message_sid:
        return {
            "consultado": False,
            "error": "MESSAGE_SID_REQUERIDO",
        }

    try:
        account_sid = os.getenv(
            "TWILIO_ACCOUNT_SID",
            "",
        ).strip()

        api_key = os.getenv(
            "TWILIO_API_KEY",
            "",
        ).strip()

        api_secret = os.getenv(
            "TWILIO_API_SECRET",
            "",
        ).strip()

        if not account_sid:
            return {
                "consultado": False,
                "error": "TWILIO_ACCOUNT_SID_NO_CONFIGURADO",
            }

        if not api_key or not api_secret:
            return {
                "consultado": False,
                "error": "CREDENCIALES_TWILIO_INCOMPLETAS",
            }

        client = Client(
            api_key,
            api_secret,
            account_sid,
        )

        mensaje = (
            client.messages(
                message_sid
            ).fetch()
        )

        return {
            "consultado": True,
            "message_sid": mensaje.sid,
            "status": mensaje.status,
            "from": mensaje.from_,
            "to": mensaje.to,
            "direction": mensaje.direction,
            "error_code": mensaje.error_code,
            "error_message": mensaje.error_message,
            "date_created": (
                mensaje.date_created.isoformat()
                if mensaje.date_created
                else ""
            ),
            "date_sent": (
                mensaje.date_sent.isoformat()
                if mensaje.date_sent
                else ""
            ),
            "date_updated": (
                mensaje.date_updated.isoformat()
                if mensaje.date_updated
                else ""
            ),
            "error": "",
        }

    except Exception as e:
        return {
            "consultado": False,
            "message_sid": message_sid,
            "error": str(e),
        }

class DebugAdminWhatsAppTestRequest(BaseModel):
    confirmation: str


@app.post("/debug/admin-whatsapp-test")
async def debug_admin_whatsapp_test(
    payload: DebugAdminWhatsAppTestRequest,
):
    """
    Envía un mensaje simple al WhatsApp administrador.

    No crea tareas.
    No consulta prospectos.
    No envía mensajes a prospectos.
    """

    confirmacion = str(
        payload.confirmation or ""
    ).strip()

    if confirmacion != "ENVIAR_PRUEBA_ADMIN":
        return {
            "ejecutado": False,
            "error": "CONFIRMACION_INVALIDA",
        }

    admin_number = str(
        os.getenv(
            "ADMIN_WHATSAPP_NUMBER",
            "",
        )
        or ""
    ).strip()

    if not admin_number:
        return {
            "ejecutado": False,
            "error": "ADMIN_WHATSAPP_NUMBER_NO_CONFIGURADO",
        }

    mensaje_prueba = (
        "✅ Prueba de comunicación administrativa.\n\n"
        "Este mensaje confirma que el bot puede enviar "
        "alertas a su WhatsApp privado."
    )

    resultado = enviar_respuesta_twilio(
        admin_number,
        mensaje_prueba,
    )

    return {
        "ejecutado": True,
        "destino": admin_number,
        "resultado": resultado,
        "error": "",
    }

def evaluar_cortesia_estructurada(
    mensaje_usuario: str,
    contact=None,
) -> Dict[str, Any]:
    """
    Evalúa cierres sociales y agradecimientos breves.

    Devuelve una de tres acciones:

    - NO_CORTESIA:
      El mensaje contiene una intención sustantiva y debe
      continuar por el flujo estructurado normal.

    - RESPONDER:
      Es la primera cortesía del cierre social y corresponde
      responder una sola vez.

    - SILENCIO:
      Ya se respondió previamente al cierre social y el nuevo
      mensaje es únicamente otra cortesía o confirmación breve.
      No se debe enviar ningún mensaje ni modificar el estado
      comercial.
    """

    resultado = {
        "accion": "NO_CORTESIA",
        "respuesta": "",
        "es_cortesia": False,
    }

    mensaje_original = str(
        mensaje_usuario or ""
    ).strip()

    if not mensaje_original:
        return resultado

    mensaje_normalizado = unicodedata.normalize(
        "NFD",
        mensaje_original.lower(),
    )

    mensaje_normalizado = "".join(
        caracter
        for caracter in mensaje_normalizado
        if unicodedata.category(caracter) != "Mn"
    )

    mensaje_normalizado = re.sub(
        r"[^a-z0-9\s]",
        " ",
        mensaje_normalizado,
    )

    mensaje_normalizado = re.sub(
        r"\s+",
        " ",
        mensaje_normalizado,
    ).strip()

    # --------------------------------------------------------
    # DETECCIÓN DE CORTESÍA BREVE
    # --------------------------------------------------------
    #
    # Se evita depender de una lista enorme de frases exactas.
    # Una cortesía social debe ser breve y estar formada
    # únicamente por vocabulario de agradecimiento, despedida
    # o confirmación social.
    # --------------------------------------------------------

    palabras = mensaje_normalizado.split()

    vocabulario_social = {
        "gracias",
        "muchas",
        "mil",
        "muy",
        "amable",
        "perfecto",
        "bien",
        "excelente",
        "ok",
        "okay",
        "sale",
        "va",
        "listo",
        "de",
        "acuerdo",
        "esta",
        "nos",
        "vemos",
        "hasta",
        "luego",
        "manana",
        "buen",
        "buena",
        "bonito",
        "bonita",
        "dia",
        "tarde",
        "noche",
        "igualmente",
        "no",
    }

    es_cortesia = bool(
        palabras
        and len(palabras) <= 8
        and all(
            palabra in vocabulario_social
            for palabra in palabras
        )
        and any(
            palabra
            in {
                "gracias",
                "amable",
                "ok",
                "okay",
                "sale",
                "va",
                "listo",
                "perfecto",
                "vemos",
                "luego",
                "manana",
                "dia",
                "tarde",
                "noche",
                "igualmente",
            }
            for palabra in palabras
        )
    )

    if not es_cortesia:
        return resultado

    resultado["es_cortesia"] = True

    # --------------------------------------------------------
    # ¿YA RESPONDIMOS AL CIERRE SOCIAL?
    # --------------------------------------------------------

    cierre_social_activo = ""

    if contact is not None:
        cierre_social_activo = str(
            get_note_value(
                contact,
                "CIERRE_SOCIAL_ACTIVO",
            )
            or ""
        ).strip().upper()

    if cierre_social_activo == "SI":
        resultado["accion"] = "SILENCIO"
        return resultado

    # --------------------------------------------------------
    # PRIMERA CORTESÍA: RESPONDER UNA VEZ
    # --------------------------------------------------------

    nombre_tutor = ""

    if contact is not None:
        nombre_tutor = (
            get_note_value(
                contact,
                "NOMBRE_PADRES",
            )
            or get_note_value(
                contact,
                "NOMBRE_TUTOR",
            )
            or get_note_value(
                contact,
                "NOMBRE_PADRE",
            )
            or get_note_value(
                contact,
                "NOMBRE_MADRE",
            )
            or ""
        ).strip()

    primer_nombre = ""

    if nombre_tutor:
        primer_nombre = (
            nombre_tutor
            .split()[0]
        )

    estado_contacto = str(
        getattr(
            contact,
            "status",
            "",
        )
        or ""
    ).strip().upper()

    estado_flujo = ""

    if contact is not None:
        try:
            estado_flujo = str(
                get_flow_state(
                    contact
                )
                or ""
            ).strip().upper()

        except Exception:
            estado_flujo = ""

    hora_cita = ""

    if contact is not None:
        hora_cita = str(
            get_note_value(
                contact,
                "HORA_CITA",
            )
            or ""
        ).strip()

    tiene_visita_confirmada = bool(
        estado_contacto
        in {
            "VISITA_AGENDADA",
            "VISITA_CONFIRMADA",
        }
        or estado_flujo
        in {
            "CITA_DATOS_COMPLETOS",
            "VISITA_CONFIRMADA",
        }
        or hora_cita
    )

    resultado["accion"] = "RESPONDER"

    if tiene_visita_confirmada:
        if primer_nombre:
            resultado["respuesta"] = (
                f"Con gusto, {primer_nombre}. "
                "Los esperamos en su visita."
            )
        else:
            resultado["respuesta"] = (
                "Con gusto. "
                "Los esperamos en su visita."
            )

        return resultado

    if primer_nombre:
        resultado["respuesta"] = (
            f"Con gusto, {primer_nombre}. "
            "Que tenga excelente día."
        )
    else:
        resultado["respuesta"] = (
            "Con gusto. "
            "Que tenga excelente día."
        )

    return resultado
    
# ============================================================
# PUENTE PRODUCTIVO DEL NUEVO FLUJO ESTRUCTURADO
# ============================================================

def procesar_mensaje_whatsapp_estructurado_real(
    db: Session,
    contact,
    from_number: str,
    mensaje_usuario: str,
    max_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    
    """
    Ejecuta el nuevo flujo estructurado con efectos reales.

    Importante:
    - Esta función todavía no está conectada al webhook.
    - Asume que el mensaje entrante ya fue guardado.
    - Puede enviar una respuesta al prospecto.
    - Puede guardar la respuesta en la base de datos.
    - Puede crear una tarea administrativa.
    - Puede enviar una alerta al WhatsApp administrador.
    """

    resultado_final = {
        "flujo": "estructurado_real",
        "alcance_conversacion": {},
        "procesado": False,
        "mensaje_enviado": False,
        "respuesta": "",
        "twilio_resultado": "",
        "twilio_sid": None,
        "resultado_orquestador": {},
        "unidad_semantica_pendiente": {},
        "historial_completo": {},
        "memoria_historica": {},
        "contexto_comercial": {},
        "contexto_comercial_enriquecido": {},
        "escalacion_admin": {},
        "error": "",
    }

    mensaje = str(
        mensaje_usuario or ""
    ).strip()

    numero_destino = str(
        from_number or ""
    ).strip()

    if not mensaje:
        resultado_final["error"] = "MENSAJE_USUARIO_VACIO"
        return resultado_final

    if not numero_destino:
        resultado_final["error"] = "NUMERO_DESTINO_VACIO"
        return resultado_final

    if contact is None:
        resultado_final["error"] = "CONTACTO_NO_DISPONIBLE"
        return resultado_final

    # ========================================================
    # UNIDAD SEMÁNTICA PENDIENTE DESDE POSTGRESQL
    # ========================================================
    #
    # El lote recibido desde RAM nos dice qué mensajes
    # dispararon este procesamiento.
    #
    # PostgreSQL nos dice algo más importante:
    # cuáles mensajes del prospecto siguen realmente
    # sin haber recibido un outbound posterior.
    # ========================================================

    mensaje_original_lote = mensaje

    unidad_semantica = (
        obtener_unidad_semantica_pendiente_desde_bd(
            db=db,
            contact=contact,
            max_message_id=max_message_id,
            mensaje_fallback=(
                mensaje_original_lote
            ),
        )
    )

    resultado_final[
        "unidad_semantica_pendiente"
    ] = unidad_semantica

    texto_unidad_semantica = str(
        unidad_semantica.get(
            "texto",
            "",
        )
        or ""
    ).strip()

    if texto_unidad_semantica:
        mensaje = (
            texto_unidad_semantica
        )

    corte_semantico = (
        unidad_semantica.get(
            "corte_message_id"
        )
    )

    if (
        isinstance(
            corte_semantico,
            int,
        )
        and corte_semantico > 0
    ):
        max_message_id = (
            corte_semantico
        )

    print(
        "🧩 UNIDAD SEMÁNTICA PENDIENTE: "
        f"contact_id={contact.id}, "
        f"ids="
        f"{unidad_semantica.get('message_ids', [])}, "
        f"cantidad="
        f"{unidad_semantica.get('cantidad', 0)}, "
        f"ultimo_outgoing="
        f"{unidad_semantica.get('ultimo_outgoing_id')}, "
        f"corte={max_message_id}, "
        f"fallback="
        f"{unidad_semantica.get('uso_fallback', True)}"
    )

    if (
        mensaje
        != mensaje_original_lote
    ):
        print(
            "🧠 MENSAJE RECONSTRUIDO DESDE BD: "
            f"{mensaje}"
        )

    # ========================================================
    # PUERTA PREVIA DE CLASIFICACIÓN DEL ALCANCE
    # ========================================================

    query_history_alcance = (
        db.query(Message)
        .filter(
            Message.contact_id == contact.id
        )
    )

    if (
        isinstance(max_message_id, int)
        and max_message_id > 0
    ):
        query_history_alcance = (
            query_history_alcance.filter(
                or_(
                    Message.direction != "incoming",
                    Message.id <= max_message_id,
                )
            )
        )

    history_alcance = (
        query_history_alcance
        .order_by(
            Message.timestamp.asc(),
            Message.id.asc(),
        )
        .all()
    )
    
    historial_alcance = []

    for item in history_alcance[-10:]:
        direccion = str(
            getattr(
                item,
                "direction",
                "",
            )
            or ""
        ).strip().lower()

        contenido = str(
            getattr(
                item,
                "content",
                "",
            )
            or ""
        ).strip()

        if not contenido:
            continue

        if direccion == "incoming":
            emisor = "Usuario"

        elif direccion == "outgoing":
            emisor = "Asistente"

        else:
            emisor = "Conversación"

        historial_alcance.append(
            f"{emisor}: {contenido}"
        )

    admisiones_evidentes = (
        detectar_admisiones_evidentes_para_alcance(
            mensaje
        )
    )

    if admisiones_evidentes:

        alcance_determinista = (
            normalizar_alcance_conversacion({
                "alcance_conversacion": "ADMISIONES",
                "motivo_principal": (
                    "Solicitud explícita de información "
                    "para admisiones."
                ),
                "resumen_solicitud": mensaje,
                "ruta_configurada": True,
                "requiere_aclaracion": False,
                "requiere_admin": False,
                "motivo_escalacion": "",
                "confianza": 1.0,
            })
        )

        resultado_clasificacion_alcance = {
            "exitoso": True,
            "alcance": alcance_determinista,
            "modelo_usado": "DETERMINISTA",
            "intentos_realizados": 0,
            "errores": [],
        }

        print(
            "🎯 ALCANCE DETERMINISTA: "
            "solicitud explícita de admisiones detectada."
        )

    else:

        try:
            resultado_clasificacion_alcance = (
                clasificar_alcance_conversacion_con_ia(
                    mensaje_usuario=mensaje,
                    historial_lista=historial_alcance,
                )
            )

        except Exception as error_alcance:
            print(
                "⚠️ Error en puerta previa de alcance: "
                f"{error_alcance}"
            )

            resultado_clasificacion_alcance = {
                "exitoso": False,
                "alcance": (
                    crear_alcance_conversacion_vacio()
                ),
                "modelo_usado": "",
                "intentos_realizados": 0,
                "errores": [
                    str(error_alcance)
                ],
            }
            
    alcance_detectado = (
        resultado_clasificacion_alcance.get(
            "alcance",
            {},
        )
    )

    if not isinstance(
        alcance_detectado,
        dict,
    ):
        alcance_detectado = (
            crear_alcance_conversacion_vacio()
        )

    categoria_alcance = str(
        alcance_detectado.get(
            "alcance_conversacion",
            "AMBIGUO",
        )
        or "AMBIGUO"
    ).strip().upper()

    # ========================================================
    # HERENCIA SEGURA DEL ALCANCE DE ADMISIONES
    # ========================================================
    #
    # Si la conversación ya avanzó dentro del flujo comercial
    # de admisiones, un mensaje breve o incompleto no debe
    # reiniciar la clasificación general.
    #
    # Sólo corregimos AMBIGUO. Si Gemini detecta claramente
    # EMPLEO, PROVEEDORES, TRÁMITES, etc., se respeta.
    # ========================================================

    etapa_previa_alcance = str(
        get_note_value(
            contact,
            "ETAPA_CONVERSACIONAL",
        )
        or ""
    ).strip().upper()

    objetivo_previo_alcance = str(
        get_note_value(
            contact,
            "OBJETIVO_PENDIENTE",
        )
        or ""
    ).strip().upper()

    flujo_previo_alcance = str(
        get_flow_state(
            contact
        )
        or ""
    ).strip().upper()

    conversacion_admisiones_ya_iniciada = bool(
        (
            etapa_previa_alcance
            and etapa_previa_alcance
            != "CONTACTO_INICIAL"
        )
        or objetivo_previo_alcance
        or flujo_previo_alcance
        not in {
            "",
            "SALUDO_INICIAL",
        }
    )

    if (
        categoria_alcance == "AMBIGUO"
        and conversacion_admisiones_ya_iniciada
    ):
        categoria_alcance = "ADMISIONES"

        alcance_detectado.update({
            "alcance_conversacion": "ADMISIONES",
            "motivo_principal": (
                "El mensaje actual es ambiguo por sí solo, "
                "pero continúa una conversación de admisiones "
                "ya establecida."
            ),
            "ruta_configurada": True,
            "requiere_aclaracion": False,
            "requiere_admin": False,
        })

        print(
            "🧭 ALCANCE HEREDADO: mensaje ambiguo "
            "con conversación previa de admisiones."
        )

    clasificacion_exitosa = bool(
        resultado_clasificacion_alcance.get(
            "exitoso"
        )
    )

    resultado_final[
        "alcance_conversacion"
    ] = {
        "clasificacion_exitosa": (
            clasificacion_exitosa
        ),
        "categoria": categoria_alcance,
        "detalle": alcance_detectado,
        "modelo_usado": (
            resultado_clasificacion_alcance.get(
                "modelo_usado",
                "",
            )
        ),
        "errores": (
            resultado_clasificacion_alcance.get(
                "errores",
                [],
            )
        ),
    }

    print(
        "🚦 PUERTA DE ALCANCE: "
        + json.dumps(
            resultado_final[
                "alcance_conversacion"
            ],
            ensure_ascii=False,
            default=str,
        )
    )

    # --------------------------------------------------------
    # ACTIVACIÓN CRM EXCLUSIVA PARA ADMISIONES
    # --------------------------------------------------------
    #
    # La fila de cohorte ya existe únicamente si el contacto
    # nació después del rollout.
    #
    # Aquí sólo lo enrolamos comercialmente cuando la IA ya
    # determinó que la conversación pertenece a ADMISIONES.
    # --------------------------------------------------------

    estado_crm_admisiones = None

    if categoria_alcance == "ADMISIONES":
        estado_crm_admisiones = (
            activar_crm_admisiones_si_elegible(
                db=db,
                contact=contact,
            )
        )

    resultado_final[
        "crm_admisiones"
    ] = (
        {
            "enrolado": bool(
                estado_crm_admisiones
            ),
            "journey_status": (
                estado_crm_admisiones.journey_status
                if estado_crm_admisiones
                else ""
            ),
            "conversation_cycle_id": (
                estado_crm_admisiones.conversation_cycle_id
                if estado_crm_admisiones
                else ""
            ),
            "automation_enabled": (
                estado_crm_admisiones.automation_enabled
                if estado_crm_admisiones
                else False
            ),
        }
    )
    

    # --------------------------------------------------------
    # RESPUESTA PREVIA PARA ALCANCE AMBIGUO
    # --------------------------------------------------------

    requiere_aclaracion_alcance = bool(
        alcance_detectado.get(
            "requiere_aclaracion",
            True,
        )
    )

    if not clasificacion_exitosa:
        print(
            "⚠️ La puerta de alcance no pudo clasificar "
            "el mensaje. Se permite continuar al flujo "
            "conversacional existente."
        )

    if (
        clasificacion_exitosa
        and categoria_alcance == "AMBIGUO"
        and requiere_aclaracion_alcance
    ):
        respuesta_alcance = (
            "Claro. ¿Busca información para inscribir "
            "a un alumno o se comunica por otro motivo?"
        )

        if existe_mensaje_entrante_posterior_al_turno(
            db=db,
            contact=contact,
            max_message_id=max_message_id,
        ):
            print(
                "🛑 RESPUESTA OBSOLETA SUPRIMIDA: "
                "llegó un mensaje entrante posterior "
                f"al corte {max_message_id}."
            )

            resultado_final[
                "procesado"
            ] = True

            resultado_final[
                "mensaje_enviado"
            ] = False

            resultado_final[
                "respuesta_suprimida_por_turno_nuevo"
            ] = True

            resultado_final[
                "error"
            ] = ""

            return resultado_final

        resultado_twilio = (
            enviar_respuesta_twilio(
                numero_destino,
                respuesta_alcance,
            )
        )

        twilio_sid = None

        if (
            isinstance(
                resultado_twilio,
                str,
            )
            and "SID:" in resultado_twilio
        ):
            twilio_sid = (
                resultado_twilio
                .split(
                    "SID:",
                    1,
                )[1]
                .strip()
            )

        envio_exitoso = bool(
            isinstance(
                resultado_twilio,
                str,
            )
            and resultado_twilio.startswith(
                "✅"
            )
        )

        if envio_exitoso:
            save_message(
                db,
                contact.id,
                "outgoing",
                respuesta_alcance,
                twilio_sid,
            )

            db.commit()

        resultado_final.update({
            "procesado": envio_exitoso,
            "mensaje_enviado": envio_exitoso,
            "respuesta": respuesta_alcance,
            "twilio_resultado": resultado_twilio,
            "twilio_sid": twilio_sid,
            "resultado_orquestador": {
                "version": "1.0",
                "flujo": "clasificacion_alcance",
                "procesado": envio_exitoso,
                "alcance": alcance_detectado,
                "ruta": "ACLARAR_MOTIVO",
                "respuesta_generada": (
                    respuesta_alcance
                ),
                "requiere_admin": False,
                "error": "",
            },
            "error": (
                ""
                if envio_exitoso
                else "ERROR_ENVIANDO_ACLARACION_ALCANCE"
            ),
        })

        return resultado_final

    # --------------------------------------------------------
    # SALIDA RÁPIDA PARA APERTURA SOCIAL SIN SOLICITUD
    # --------------------------------------------------------

    if (
        clasificacion_exitosa
        and categoria_alcance == "AMBIGUO"
        and not requiere_aclaracion_alcance
    ):
        respuesta_social = (
            crear_respuesta_saludo_simple_estructurado(
                mensaje
            )
        )

        if respuesta_social:
            contexto_persistido = (
                construir_contexto_comercial_desde_contacto(
                    contact
                )
            )

            nombre_tutor = str(
                contexto_persistido.get(
                    "nombre_tutor",
                    "",
                )
                or ""
            ).strip()

            if nombre_tutor:
                primer_nombre = (
                    nombre_tutor.split()[0].strip()
                )

                saludo_base = (
                    detectar_saludo_simple_estructurado(
                        mensaje
                    )
                )

                if saludo_base and primer_nombre:
                    respuesta_social = (
                        f"{saludo_base}, {primer_nombre}. "
                        "¿En qué podemos ayudarle?"
                    )

        if existe_mensaje_entrante_posterior_al_turno(
            db=db,
            contact=contact,
            max_message_id=max_message_id,
        ):
            print(
                "🛑 RESPUESTA OBSOLETA SUPRIMIDA: "
                "llegó un mensaje entrante posterior "
                f"al corte {max_message_id}."
            )

            resultado_final[
                "procesado"
            ] = True

            resultado_final[
                "mensaje_enviado"
            ] = False

            resultado_final[
                "respuesta_suprimida_por_turno_nuevo"
            ] = True

            resultado_final[
                "error"
            ] = ""

            return resultado_final



            resultado_twilio = (
                enviar_respuesta_twilio(
                    numero_destino,
                    respuesta_social,
                )
            )

            twilio_sid = None

            if (
                isinstance(
                    resultado_twilio,
                    str,
                )
                and "SID:" in resultado_twilio
            ):
                twilio_sid = (
                    resultado_twilio
                    .split(
                        "SID:",
                        1,
                    )[1]
                    .strip()
                )

            envio_exitoso = bool(
                isinstance(
                    resultado_twilio,
                    str,
                )
                and resultado_twilio.startswith(
                    "✅"
                )
            )

            if envio_exitoso:
                save_message(
                    db,
                    contact.id,
                    "outgoing",
                    respuesta_social,
                    twilio_sid,
                )

                db.commit()

            resultado_final.update({
                "procesado": envio_exitoso,
                "mensaje_enviado": envio_exitoso,
                "respuesta": respuesta_social,
                "twilio_resultado": resultado_twilio,
                "twilio_sid": twilio_sid,
                "resultado_orquestador": {
                    "version": "1.0",
                    "flujo": "apertura_social_rapida",
                    "procesado": envio_exitoso,
                    "alcance": alcance_detectado,
                    "respuesta_generada": (
                        respuesta_social
                    ),
                    "gemini_adicional_utilizado": False,
                    "contexto_persistido_utilizado": bool(
                        nombre_tutor
                    ),
                    "error": "",
                },
                "memoria_historica": {
                    "omitida": True,
                    "motivo": (
                        "APERTURA_SOCIAL_RESUELTA "
                        "CON CONTEXTO_PERSISTIDO"
                    ),
                },
                "contexto_comercial": (
                    contexto_persistido
                ),
                "contexto_comercial_enriquecido": {
                    "omitido": True,
                    "motivo": (
                        "NO_REQUIERE_RECONSTRUCCION_IA"
                    ),
                },
                "error": (
                    ""
                    if envio_exitoso
                    else "ERROR_ENVIANDO_APERTURA_SOCIAL"
                ),
            })

            print(
                "⚡ Apertura social resuelta sin "
                "memoria IA ni orquestador completo: "
                f"{respuesta_social}"
            )

            return resultado_final
            

    # --------------------------------------------------------
    # DECISIÓN DE CONTINUIDAD DEL FLUJO
    # --------------------------------------------------------

    continuar_flujo_conversacional = (
        not clasificacion_exitosa
        or categoria_alcance in {
            "ADMISIONES",
            "AMBIGUO",
        }
    )

    if continuar_flujo_conversacional:
        print(
            "✅ Continúa el flujo conversacional existente: "
            f"{categoria_alcance}"
        )

    else:

        # ----------------------------------------------------
        # ARBITRAJE DE AUTORIDAD ADMINISTRATIVA
        # ----------------------------------------------------
        #
        # Si este contacto ya tiene una tarea administrativa
        # pendiente, una clasificación SIN_RUTA_CONFIGURADA
        # no puede generar otra promesa de "revisión".
        #
        # Administración ya posee la autoridad del siguiente
        # movimiento.
        # ----------------------------------------------------

        if categoria_alcance == "SIN_RUTA_CONFIGURADA":

            tarea_admin_pendiente_actual = (
                db.query(AdminPendingTask)
                .filter(
                    AdminPendingTask.contact_id
                    == contact.id,
                    AdminPendingTask.status
                    == "PENDIENTE",
                )
                .order_by(
                    AdminPendingTask.created_at.desc()
                )
                .first()
            )

            if tarea_admin_pendiente_actual:
                print(
                    "🔐 RESPUESTA DE ALCANCE SUPRIMIDA: "
                    f"contact_id={contact.id}, "
                    "administración ya tiene una tarea "
                    f"pendiente id="
                    f"{tarea_admin_pendiente_actual.id}."
                )

                resultado_final.update({
                    "procesado": True,
                    "mensaje_enviado": False,
                    "respuesta": "",
                    "twilio_resultado": "",
                    "twilio_sid": None,
                    "resultado_orquestador": {
                        "version": "1.0",
                        "flujo": (
                            "clasificacion_alcance"
                        ),
                        "procesado": True,
                        "ruta": (
                            "ADMIN_YA_POSEE_AUTORIDAD"
                        ),
                        "respuesta_generada": "",
                        "requiere_admin": True,
                        "error": "",
                    },
                    "respuesta_suprimida_por_admin_pendiente": (
                        True
                    ),
                    "error": "",
                })

                return resultado_final

        # ----------------------------------------------------
        # CONTINUIDAD DE RUTA: EMPLEO
        # ----------------------------------------------------
        #
        # La primera vez se proporciona el canal para enviar CV.
        #
        # Si la persona vuelve a escribir dentro de la misma
        # conversación de empleo, no repetimos las instrucciones.
        #
        # Después del segundo mensaje de cierre, una cortesía
        # adicional queda en silencio.
        # ----------------------------------------------------

        empleo_derivacion_atendida = bool(
            str(
                get_note_value(
                    contact,
                    "EMPLEO_DERIVACION_ATENDIDA",
                )
                or ""
            ).strip().upper()
            == "SI"
        )

        cierre_social_empleo = bool(
            str(
                get_note_value(
                    contact,
                    "CIERRE_SOCIAL_ACTIVO",
                )
                or ""
            ).strip().upper()
            == "SI"
        )

        respuesta_empleo = (
            "¡Gracias por tu interés en colaborar con nosotros! "
            "Nos puedes enviar tu currículum por WhatsApp a este "
            "número, por favor: 55 4812 3885."
        )

        empleo_segundo_contacto = False

        if (
            categoria_alcance == "EMPLEO"
            and empleo_derivacion_atendida
        ):

            evaluacion_cortesia_empleo = (
                evaluar_cortesia_estructurada(
                    mensaje_usuario=mensaje,
                    contact=contact,
                )
            )

            es_cortesia_empleo = bool(
                evaluacion_cortesia_empleo.get(
                    "es_cortesia",
                    False,
                )
            )

            # ------------------------------------------------
            # YA CERRAMOS Y SÓLO VUELVEN A AGRADECER
            # ------------------------------------------------

            if (
                cierre_social_empleo
                and es_cortesia_empleo
            ):
                print(
                    "🤫 EMPLEO CERRADO: "
                    f"contact_id={contact.id}, "
                    "cortesía adicional sin respuesta."
                )

                resultado_final.update({
                    "procesado": True,
                    "mensaje_enviado": False,
                    "respuesta": "",
                    "twilio_resultado": "",
                    "twilio_sid": None,
                    "resultado_orquestador": {
                        "version": "1.0",
                        "flujo": (
                            "clasificacion_alcance"
                        ),
                        "procesado": True,
                        "ruta": "EMPLEO_CERRADO",
                        "respuesta_generada": "",
                        "requiere_admin": False,
                        "error": "",
                    },
                    "error": "",
                })

                consumir_turno_sin_respuesta(
                    db=db,
                    contact=contact,
                    max_message_id=max_message_id,
                    motivo="EMPLEO_CERRADO_CORTESIA",
                )

                return resultado_final

            # ------------------------------------------------
            # SEGUNDO CONTACTO DE EMPLEO
            # ------------------------------------------------

            respuesta_empleo = (
                "Gracias. Ya el área correspondiente dará "
                "seguimiento directamente por ese medio."
            )

            empleo_segundo_contacto = True
    
        respuestas_por_alcance = {
            "EMPLEO": respuesta_empleo,
            "ALUMNOS_ACTUALES": (
                "Gracias por escribirnos. Identificamos que "
                "su consulta corresponde a una familia o alumno "
                "actual. Para atender correctamente su solicitud, "
                "necesitamos canalizarla con el área "
                "correspondiente. Continuaremos la atención "
                "por este medio."
            ),
            "TRAMITES_ADMINISTRATIVOS": (
                "Gracias por escribirnos. Identificamos que "
                "su solicitud corresponde a un trámite "
                "administrativo. Para proporcionarle información "
                "correcta, necesitamos revisar su caso. "
                "Continuaremos la atención por este medio."
            ),
            "PROVEEDORES": (
                "Muchas gracias por comunicarse con nosotros "
                "y por considerar al Colegio Valle de Filadelfia. "
                "Para propuestas de productos, servicios, "
                "proveeduría o colaboraciones comerciales, "
                "le pedimos por favor comunicarse directamente "
                "por WhatsApp al 55 4812 3885. "
                "Por ese medio podrán dar seguimiento a su propuesta."
            ),
            "OTRO_CONFIGURADO": (
                "Gracias por escribirnos. Identificamos el motivo "
                "de su consulta y continuaremos la atención por "
                "la ruta correspondiente."
            ),
            "SIN_RUTA_CONFIGURADA": (
                "Gracias por escribirnos. Su solicitud requiere "
                "una revisión particular para poder brindarle "
                "información correcta. Continuaremos la atención "
                "por este medio."
            ),
        }

        respuesta_alcance = (
            respuestas_por_alcance.get(
                categoria_alcance,
                (
                    "Gracias por escribirnos. Para atender "
                    "correctamente su solicitud, necesitamos "
                    "revisar el motivo de su consulta. "
                    "Continuaremos la atención por este medio."
                ),
            )
        )

        if existe_mensaje_entrante_posterior_al_turno(
            db=db,
            contact=contact,
            max_message_id=max_message_id,
        ):
            print(
                "🛑 RESPUESTA OBSOLETA SUPRIMIDA: "
                "llegó un mensaje entrante posterior "
                f"al corte {max_message_id}."
            )

            resultado_final[
                "procesado"
            ] = True

            resultado_final[
                "mensaje_enviado"
            ] = False

            resultado_final[
                "respuesta_suprimida_por_turno_nuevo"
            ] = True

            resultado_final[
                "error"
            ] = ""

            return resultado_final

        resultado_twilio = (
            enviar_respuesta_twilio(
                numero_destino,
                respuesta_alcance,
            )
        )

        twilio_sid = None

        if (
            isinstance(
                resultado_twilio,
                str,
            )
            and "SID:" in resultado_twilio
        ):
            twilio_sid = (
                resultado_twilio
                .split(
                    "SID:",
                    1,
                )[1]
                .strip()
            )

        envio_exitoso = bool(
            isinstance(
                resultado_twilio,
                str,
            )
            and resultado_twilio.startswith(
                "✅"
            )
        )

        if envio_exitoso:
            save_message(
                db,
                contact.id,
                "outgoing",
                respuesta_alcance,
                twilio_sid,
            )

            if categoria_alcance == "EMPLEO":

                set_note_value(
                    contact,
                    "EMPLEO_DERIVACION_ATENDIDA",
                    "SI",
                )

                # Después del segundo intercambio nuestra
                # intervención queda socialmente cerrada.
                #
                # Un siguiente "gracias", "perfecto", etc.
                # debe quedar en silencio.
                if empleo_segundo_contacto:
                    set_note_value(
                        contact,
                        "CIERRE_SOCIAL_ACTIVO",
                        "SI",
                    )

            db.commit()

        # ----------------------------------------------------
        # ESCALACIÓN DE ALCANCE SIN RUTA CONFIGURADA
        # ----------------------------------------------------
        #
        # Si la puerta de alcance determinó que el motivo es
        # comprensible pero requiere intervención humana,
        # creamos/reutilizamos una tarea administrativa y
        # notificamos al administrador por WhatsApp.
        # ----------------------------------------------------

        resultado_escalacion_alcance = {}

        if (
            categoria_alcance
            == "SIN_RUTA_CONFIGURADA"
        ):

            try:
                tarea_admin = (
                    crear_tarea_admin_pendiente(
                        db=db,
                        contact=contact,
                        mensaje_usuario=mensaje,
                        respuesta_bot=respuesta_alcance,
                    )
                )

                resultado_alerta_admin = (
                    enviar_alerta_admin_whatsapp(
                        db=db,
                        contact=contact,
                        mensaje_usuario=mensaje,
                        respuesta_bot=respuesta_alcance,
                        tarea_id=getattr(
                            tarea_admin,
                            "id",
                            None,
                        ),
                    )
                )

                resultado_escalacion_alcance = {
                    "requiere_escalacion": True,
                    "ejecutada": True,
                    "tarea_id": getattr(
                        tarea_admin,
                        "id",
                        None,
                    ),
                    "alerta_admin": (
                        resultado_alerta_admin
                    ),
                    "motivo": str(
                        alcance_detectado.get(
                            "motivo_escalacion",
                            "",
                        )
                        or ""
                    ).strip(),
                    "error": "",
                }

                print(
                    "📣 ESCALACIÓN DE ALCANCE EJECUTADA: "
                    f"contact_id={contact.id}, "
                    f"tarea_id="
                    f"{getattr(tarea_admin, 'id', None)}"
                )

            except Exception as e:
                db.rollback()

                resultado_escalacion_alcance = {
                    "requiere_escalacion": True,
                    "ejecutada": False,
                    "tarea_id": None,
                    "alerta_admin": "",
                    "motivo": str(
                        alcance_detectado.get(
                            "motivo_escalacion",
                            "",
                        )
                        or ""
                    ).strip(),
                    "error": str(e),
                }

                print(
                    "⚠️ ERROR ESCALANDO ALCANCE "
                    "SIN_RUTA_CONFIGURADA: "
                    f"{e}"
                )

        resultado_final.update({
            "procesado": envio_exitoso,
            "mensaje_enviado": envio_exitoso,
            "respuesta": respuesta_alcance,
            "twilio_resultado": resultado_twilio,
            "twilio_sid": twilio_sid,
            "escalacion_admin": (
                resultado_escalacion_alcance
            ),
            "resultado_orquestador": {
                "version": "1.0",
                "flujo": "clasificacion_alcance",
                "procesado": envio_exitoso,
                "alcance": alcance_detectado,
                "ruta": categoria_alcance,
                "respuesta_generada": (
                    respuesta_alcance
                ),
                "requiere_admin": (
                    categoria_alcance
                    == "SIN_RUTA_CONFIGURADA"
                ),
                "error": "",
            },
            "memoria_historica": {
                "omitida": True,
                "motivo": (
                    "ALCANCE_FUERA_DE_ADMISIONES"
                ),
            },
            "contexto_comercial": {
                "omitido": True,
                "motivo": (
                    "ALCANCE_FUERA_DE_ADMISIONES"
                ),
            },
            "contexto_comercial_enriquecido": {
                "omitido": True,
                "motivo": (
                    "ALCANCE_FUERA_DE_ADMISIONES"
                ),
            },
            "error": (
                ""
                if envio_exitoso
                else "ERROR_ENVIANDO_RESPUESTA_ALCANCE"
            ),
        })

        return resultado_final
        

    # --------------------------------------------------------
    # CONTROL DETERMINISTA DE CIERRE SOCIAL
    # --------------------------------------------------------

    evaluacion_cortesia = (
        evaluar_cortesia_estructurada(
            mensaje_usuario=mensaje,
            contact=contact,
        )
    )

    accion_cortesia = str(
        evaluacion_cortesia.get(
            "accion",
            "NO_CORTESIA",
        )
        or "NO_CORTESIA"
    ).strip().upper()

    # --------------------------------------------------------
    # CORTESÍA REPETIDA: SILENCIO
    # --------------------------------------------------------

    if accion_cortesia == "SILENCIO":
        print(
            "🤫 Cierre social ya atendido. "
            "No se enviará otra respuesta."
        )

        resultado_final.update({
            "procesado": True,
            "mensaje_enviado": False,
            "respuesta": "",
            "twilio_resultado": "",
            "twilio_sid": None,
            "resultado_orquestador": {
                "version": "1.0",
                "flujo": "estructurado",
                "procesado": True,
                "tipo_respuesta": (
                    "CIERRE_SOCIAL_SILENCIOSO"
                ),
                "respuesta_generada": "",
                "gemini_utilizado": False,
                "error": "",
            },
            "error": "",
        })

        consumir_turno_sin_respuesta(
            db=db,
            contact=contact,
            max_message_id=max_message_id,
            motivo="CIERRE_SOCIAL_SILENCIOSO",
        )

        return resultado_final

    # --------------------------------------------------------
    # PRIMERA CORTESÍA: RESPONDER UNA SOLA VEZ
    # --------------------------------------------------------

    if accion_cortesia == "RESPONDER":

        respuesta_cortesia = str(
            evaluacion_cortesia.get(
                "respuesta",
                "",
            )
            or ""
        ).strip()

        print(
            "👋 Primera cortesía de cierre detectada. "
            "Se omite Gemini."
        )

        if existe_mensaje_entrante_posterior_al_turno(
            db=db,
            contact=contact,
            max_message_id=max_message_id,
        ):
            print(
                "🛑 RESPUESTA OBSOLETA SUPRIMIDA: "
                "llegó un mensaje entrante posterior "
                f"al corte {max_message_id}."
            )

            resultado_final[
                "procesado"
            ] = True

            resultado_final[
                "mensaje_enviado"
            ] = False

            resultado_final[
                "respuesta_suprimida_por_turno_nuevo"
            ] = True

            resultado_final[
                "error"
            ] = ""

            return resultado_final

        resultado_twilio = (
            enviar_respuesta_twilio(
                numero_destino,
                respuesta_cortesia,
            )
        )

        twilio_sid = None

        if (
            isinstance(
                resultado_twilio,
                str,
            )
            and "SID:" in resultado_twilio
        ):
            twilio_sid = (
                resultado_twilio
                .split(
                    "SID:",
                    1,
                )[1]
                .strip()
            )

        envio_exitoso = bool(
            isinstance(
                resultado_twilio,
                str,
            )
            and resultado_twilio.startswith(
                "✅"
            )
        )

        if envio_exitoso:
            save_message(
                db,
                contact.id,
                "outgoing",
                respuesta_cortesia,
                twilio_sid,
            )

            set_note_value(
                contact,
                "CIERRE_SOCIAL_ACTIVO",
                "SI",
            )

            db.commit()

        resultado_final.update({
            "procesado": envio_exitoso,
            "mensaje_enviado": envio_exitoso,
            "respuesta": respuesta_cortesia,
            "twilio_resultado": resultado_twilio,
            "twilio_sid": twilio_sid,
            "resultado_orquestador": {
                "version": "1.0",
                "flujo": "estructurado",
                "procesado": envio_exitoso,
                "tipo_respuesta": (
                    "CORTESIA_DETERMINISTA"
                ),
                "respuesta_generada": (
                    respuesta_cortesia
                ),
                "gemini_utilizado": False,
                "error": "",
            },
            "error": (
                ""
                if envio_exitoso
                else "ERROR_ENVIANDO_CORTESIA_TWILIO"
            ),
        })

        return resultado_final

    # --------------------------------------------------------
    # MENSAJE SUSTANTIVO: REABRIR CONVERSACIÓN SOCIAL
    # --------------------------------------------------------

    cierre_social_previo = str(
        get_note_value(
            contact,
            "CIERRE_SOCIAL_ACTIVO",
        )
        or ""
    ).strip().upper()

    if cierre_social_previo == "SI":
        set_note_value(
            contact,
            "CIERRE_SOCIAL_ACTIVO",
            "",
        )

        db.commit()

        print(
            "🔄 Nuevo mensaje sustantivo. "
            "Se reabre la conversación después "
            "del cierre social."
        )
        
    try:
        # ----------------------------------------------------
        # 1. HISTORIAL COMPLETO PARA EL ORQUESTADOR
        # ----------------------------------------------------

        query_history = (
            db.query(Message)
            .filter(
                Message.contact_id == contact.id
            )
        )

        if (
            isinstance(max_message_id, int)
            and max_message_id > 0
        ):
            query_history = (
                query_history.filter(
                    or_(
                        Message.direction != "incoming",
                        Message.id <= max_message_id,
                    )
                )
            )

        history = (
            query_history
            .order_by(
                Message.timestamp.asc(),
                Message.id.asc(),
            )
            .all()
        )

        historial_completo = (
            obtener_historial_completo_contacto(
                db=db,
                contact=contact,
                max_message_id=max_message_id,
            )
        )

        resultado_final[
            "historial_completo"
        ] = historial_completo

        # ----------------------------------------------------
        # 2. MEMORIA HISTÓRICA ADAPTATIVA
        # ----------------------------------------------------

        texto_conversacion = str(
            historial_completo.get(
                "texto_conversacion",
                "",
            )
            or ""
        ).strip()

        total_mensajes_historial = len(
            history or []
        )

        requiere_memoria_historica_ia = bool(
            texto_conversacion
            and total_mensajes_historial > 8
        )

        if requiere_memoria_historica_ia:
            resultado_memoria_historica = (
                extraer_memoria_historica_con_ia(
                    texto_conversacion=(
                        texto_conversacion
                    )
                )
            )

            print(
                "🧠 Memoria histórica IA utilizada: "
                f"{total_mensajes_historial} mensajes "
                "superan la ventana reciente de 8."
            )

        else:
            motivo_omision_memoria = (
                "HISTORIAL_COMPLETO_VACIO"
                if not texto_conversacion
                else (
                    "HISTORIAL_RECIENTE_COMPLETO "
                    "YA_DISPONIBLE_EN_ANALIZADOR"
                )
            )

            resultado_memoria_historica = {
                "exitoso": False,
                "memoria": (
                    crear_memoria_historica_vacia()
                ),
                "modelo_usado": "",
                "intentos_realizados": 0,
                "errores": [],
                "omitida": True,
                "motivo": motivo_omision_memoria,
                "total_mensajes": (
                    total_mensajes_historial
                ),
            }

            print(
                "⚡ Memoria histórica IA omitida: "
                f"{motivo_omision_memoria}; "
                f"mensajes={total_mensajes_historial}"
            )
        resultado_final[
            "memoria_historica"
        ] = resultado_memoria_historica

        # ----------------------------------------------------
        # 3. CONTEXTO COMERCIAL ENRIQUECIDO
        # ----------------------------------------------------

        contexto_comercial = (
            construir_contexto_comercial_desde_contacto(
                contact
            )
        )

        contexto_comercial_enriquecido = (
            enriquecer_contexto_comercial_con_memoria(
                contexto_comercial=(
                    contexto_comercial
                ),
                resultado_memoria=(
                    resultado_memoria_historica
                ),
            )
        )

        resultado_final[
            "contexto_comercial"
        ] = contexto_comercial

        resultado_final[
            "contexto_comercial_enriquecido"
        ] = contexto_comercial_enriquecido

        # ----------------------------------------------------
        # GUARD: CORTESÍA MIENTRAS LA CITA ESPERA ADMIN
        # ----------------------------------------------------
        #
        # Cuando una visita ya quedó pendiente de confirmación
        # administrativa, una respuesta breve de cortesía no debe
        # reactivar el embudo comercial ni generar otra respuesta.
        # ----------------------------------------------------

        objetivo_pendiente_actual = str(
            contexto_comercial_enriquecido.get(
                "objetivo_pendiente",
                "",
            )
            or ""
        ).strip().upper()

        etapa_actual = str(
            contexto_comercial_enriquecido.get(
                "etapa_conversacional",
                "",
            )
            or ""
        ).strip().upper()

        estado_actual = str(
            contexto_comercial_enriquecido.get(
                "estado_comercial",
                "",
            )
            or ""
        ).strip().upper()

        mensaje_normalizado_espera = (
            normalizar_texto_para_deteccion(
                mensaje
            )
        )

        mensajes_cortesia_espera_admin = {
            "si",
            "sí",
            "ok",
            "okay",
            "claro",
            "claro que si",
            "claro que sí",
            "gracias",
            "muchas gracias",
            "de acuerdo",
            "esta bien",
            "está bien",
            "perfecto",
            "vale",
            "entendido",
            "muy bien",
            "sale",
        }
        cita_esperando_admin = (
            objetivo_pendiente_actual
            == "ESPERAR_CONFIRMACION_ADMIN"
            or etapa_actual
            == "ESPERANDO_CONFIRMACION_ADMIN"
            or estado_actual
            == "CITA_PENDIENTE_CONFIRMACION"
        )

        if (
            cita_esperando_admin
            and mensaje_normalizado_espera
            in mensajes_cortesia_espera_admin
        ):
            print(
                "⏳ CORTESÍA SUPRIMIDA DURANTE ESPERA ADMIN: "
                f"{mensaje!r}"
            )

            resultado_final[
                "procesado"
            ] = True

            resultado_final[
                "mensaje_enviado"
            ] = False

            resultado_final[
                "respuesta"
            ] = ""

            resultado_final[
                "cortesia_suprimida_espera_admin"
            ] = True

            resultado_final[
                "error"
            ] = ""

            consumir_turno_sin_respuesta(
                db=db,
                contact=contact,
                max_message_id=max_message_id,
                motivo="CORTESIA_DURANTE_ESPERA_ADMIN",
            )

            return resultado_final

        # ----------------------------------------------------
        # 4. ORQUESTADOR ESTRUCTURADO
        # ----------------------------------------------------

        resultado_orquestador = (
            procesar_mensaje_prospecto_estructurado(
                mensaje_usuario=mensaje,
                contact=contact,
                history=history,
                contexto_comercial=(
                    contexto_comercial_enriquecido
                ),
            )
        )
        
        resultado_final[
            "resultado_orquestador"
        ] = resultado_orquestador

        if not isinstance(
            resultado_orquestador,
            dict,
        ):
            resultado_final[
                "error"
            ] = "RESULTADO_ORQUESTADOR_INVALIDO"

            return resultado_final

        # ----------------------------------------------------
        # PERSISTENCIA DEL CONTEXTO ESTRUCTURADO
        # ----------------------------------------------------

        resultado_persistencia = (
            persistir_resultado_estructurado(
                db=db,
                contact=contact,
                resultado=resultado_orquestador,
            )
        )

        resultado_final[
            "persistencia_contexto"
        ] = resultado_persistencia

        print(
            "💾 Contexto estructurado persistido: "
            + json.dumps(
                resultado_persistencia,
                ensure_ascii=False,
                default=str,
            )
        )

        respuesta_bot = str(
            resultado_orquestador.get(
                "respuesta_generada",
                "",
            )
            or ""
        ).strip()

        if not respuesta_bot:
            respuesta_bot = (
                "Creo que no comprendí bien su mensaje.\n\n"
                "¿Podría aclararme su respuesta, por favor?"
            )

            resultado_orquestador[
                "respuesta_generada"
            ] = respuesta_bot

            resultado_orquestador[
                "respuesta_fallback_productiva"
            ] = True

        resultado_final[
            "respuesta"
        ] = respuesta_bot

        # ====================================================
        # AUTORIDAD PRE-OUTBOUND PARA ESCALACIÓN ADMINISTRATIVA
        # ====================================================
        #
        # Si Python ya determinó CONSULTAR_ADMIN, esa transición
        # y su tarea administrativa son hechos operativos.
        #
        # No deben depender de que el mensaje al prospecto siga
        # vigente ni de que Twilio llegue a enviarlo.
        # ====================================================

        decision_actual = (
            resultado_orquestador.get(
                "decision",
                {},
            )
            if isinstance(
                resultado_orquestador,
                dict,
            )
            else {}
        )

        if not isinstance(
            decision_actual,
            dict,
        ):
            decision_actual = {}

        accion_actual = str(
            decision_actual.get(
                "accion",
                "",
            )
            or ""
        ).strip().upper()

        transicion_critica_pre_outbound = None
        crm_critico_pre_outbound = None
        escalacion_critica_pre_outbound = None

        if accion_actual == "CONSULTAR_ADMIN":

            # ------------------------------------------------
            # 1. PERSISTIR ESTADO AUTORITATIVO
            # ------------------------------------------------

            transicion_critica_pre_outbound = (
                persistir_transicion_comercial_post_envio(
                    db=db,
                    contact=contact,
                    resultado=resultado_orquestador,
                    contexto_actual=(
                        contexto_comercial_enriquecido
                    ),
                )
            )

            resultado_final[
                "transicion_comercial_pre_outbound"
            ] = transicion_critica_pre_outbound

            # ------------------------------------------------
            # 2. SINCRONIZAR CRM EN MODO OBSERVACIÓN
            # ------------------------------------------------

            crm_critico_pre_outbound = (
                sincronizar_crm_desde_transicion(
                    db=db,
                    contact=contact,
                    transicion=(
                        transicion_critica_pre_outbound
                    ),
                )
            )

            # ------------------------------------------------
            # 3. CREAR TAREA / AVISAR ADMIN
            # ------------------------------------------------

            escalacion_critica_pre_outbound = (
                procesar_escalacion_admin_estructurada(
                    db=db,
                    contact=contact,
                    mensaje_usuario=mensaje,
                    respuesta_bot=respuesta_bot,
                    resultado_orquestador=(
                        resultado_orquestador
                    ),
                    memoria_historica=(
                        resultado_memoria_historica
                    ),
                    ejecutar_envio=True,
                )
            )

            resultado_final[
                "escalacion_admin"
            ] = escalacion_critica_pre_outbound

            resultado_final[
                "autoridad_admin_pre_outbound"
            ] = True

            print(
                "🔐 AUTORIDAD ADMIN PRE-OUTBOUND: "
                f"contact_id={contact.id}, "
                "estado persistido y escalación ejecutada."
            )
            

        # ----------------------------------------------------
        # GUARD FINAL: LA IA NO PUEDE CONFIRMAR DISPONIBILIDAD
        # MIENTRAS ADMINISTRACIÓN SIGUE SIENDO LA AUTORIDAD
        # ----------------------------------------------------

        if cita_esperando_admin:

            respuesta_normalizada_admin = (
                normalizar_texto_para_deteccion(
                    respuesta_bot
                )
            )

            expresiones_confirmacion_no_autorizada = [
                "le confirmo que tenemos disponible",
                "le confirmo que hay disponibilidad",
                "tenemos disponible el espacio",
                "su visita ha quedado programada",
                "su cita ha quedado programada",
                "su visita esta confirmada",
                "su cita esta confirmada",
                "podemos recibirle",
                "podemos recibirlo",
                "podemos recibirla",
            ]

            confirma_sin_admin = any(
                expresion
                in respuesta_normalizada_admin
                for expresion
                in expresiones_confirmacion_no_autorizada
            )

            if confirma_sin_admin:
                print(
                    "🛡️ CONFIRMACIÓN DE CITA BLOQUEADA: "
                    "la conversación sigue esperando "
                    "confirmación administrativa."
                )

                respuesta_bot = (
                    "Su solicitud de visita sigue pendiente "
                    "de confirmación.\n\n"
                    "En cuanto tengamos respuesta se la "
                    "compartiremos por este medio."
                )

                resultado_orquestador[
                    "respuesta_generada"
                ] = respuesta_bot

                resultado_orquestador[
                    "guard_confirmacion_admin_aplicado"
                ] = True

                resultado_final[
                    "respuesta"
                ] = respuesta_bot

        # ----------------------------------------------------
        # 5. ENVÍO REAL AL PROSPECTO
        # ----------------------------------------------------
        
        # Releer el estado actual antes de decidir si este turno
        # conserva todavía autoridad para responder.
        db.refresh(contact)
        
        existe_inbound_posterior = (
            existe_mensaje_entrante_posterior_al_turno(
                db=db,
                contact=contact,
                max_message_id=max_message_id,
            )
        )
        
        existe_outbound_posterior = (
            existe_mensaje_saliente_posterior_al_turno(
                db=db,
                contact=contact,
                max_message_id=max_message_id,
            )
        )
        
        turno_superado = bool(
            existe_inbound_posterior
            or existe_outbound_posterior
        )
        
        if turno_superado:
        
            motivo_supresion = (
                "INBOUND_POSTERIOR"
                if existe_inbound_posterior
                else "OUTBOUND_AUTORITATIVO_POSTERIOR"
            )
        
            print(
                "🛑 RESPUESTA OBSOLETA SUPRIMIDA: "
                f"contact_id={contact.id}, "
                f"motivo={motivo_supresion}, "
                f"corte={max_message_id}."
            )
        
            resultado_final[
                "procesado"
            ] = True
        
            resultado_final[
                "mensaje_enviado"
            ] = False
        
            resultado_final[
                "respuesta_suprimida_por_turno_nuevo"
            ] = True
        
            resultado_final[
                "motivo_supresion_turno"
            ] = motivo_supresion
        
            resultado_final[
                "error"
            ] = ""
        
            return resultado_final
            
        
        resultado_twilio = (
            enviar_respuesta_twilio(
                numero_destino,
                respuesta_bot,
            )
        )

        resultado_final[
            "twilio_resultado"
        ] = resultado_twilio

        twilio_sid = None

        if (
            isinstance(resultado_twilio, str)
            and "SID:" in resultado_twilio
        ):
            twilio_sid = (
                resultado_twilio
                .split("SID:", 1)[1]
                .strip()
            )

        resultado_final[
            "twilio_sid"
        ] = twilio_sid

        envio_exitoso = bool(
            isinstance(resultado_twilio, str)
            and resultado_twilio.startswith(
                "✅"
            )
        )

        resultado_final[
            "mensaje_enviado"
        ] = envio_exitoso

        if not envio_exitoso:
            resultado_final[
                "error"
            ] = "ERROR_ENVIANDO_RESPUESTA_TWILIO"

            return resultado_final

        # ----------------------------------------------------
        # 6. GUARDADO DE LA RESPUESTA
        # ----------------------------------------------------

        save_message(
            db,
            contact.id,
            "outgoing",
            respuesta_bot,
            twilio_sid,
        )

        if (
            isinstance(
                max_message_id,
                int,
            )
            and max_message_id > 0
        ):
            marcar_inbound_procesado_hasta(
                contact,
                max_message_id,
            )

        db.commit()

        # ----------------------------------------------------
        # 7. PERSISTENCIA COMERCIAL
        # ----------------------------------------------------

        if (
            accion_actual == "CONSULTAR_ADMIN"
            and transicion_critica_pre_outbound
            is not None
        ):
            resultado_transicion_post_envio = (
                transicion_critica_pre_outbound
            )

            print(
                "🔐 Transición comercial ya persistida "
                "pre-outbound; no se duplica."
            )

        else:
            resultado_transicion_post_envio = (
                persistir_transicion_comercial_post_envio(
                    db=db,
                    contact=contact,
                    resultado=resultado_orquestador,
                    contexto_actual=(
                        contexto_comercial_enriquecido
                    ),
                )
            )
            
        resultado_final[
            "transicion_comercial_post_envio"
        ] = resultado_transicion_post_envio

        print(
            "🧭 Transición comercial post-envío: "
            + json.dumps(
                resultado_transicion_post_envio,
                ensure_ascii=False,
                default=str,
            )
        )

        # ----------------------------------------------------
        # 7-B. SINCRONIZACIÓN CRM EN MODO OBSERVACIÓN
        # ----------------------------------------------------
        #
        # Replica la posición comercial resultante en las
        # nuevas tablas persistentes.
        #
        # IMPORTANTE:
        # - no envía mensajes;
        # - automation_enabled continúa False;
        # - next_followup_at es únicamente una marca observable;
        # - contactos históricos no poseen estado CRM y por
        #   tanto esta llamada no les afecta.
        # ----------------------------------------------------

        if (
            accion_actual == "CONSULTAR_ADMIN"
            and crm_critico_pre_outbound
            is not None
        ):
            estado_crm_followup = (
                crm_critico_pre_outbound
            )

        else:
            estado_crm_followup = (
                sincronizar_crm_desde_transicion(
                    db=db,
                    contact=contact,
                    transicion=(
                        resultado_transicion_post_envio
                    ),
                )
            )
            

        resultado_final[
            "crm_followup_state"
        ] = (
            {
                "journey_status": (
                    estado_crm_followup.journey_status
                ),
                "commercial_goal": (
                    estado_crm_followup.commercial_goal
                ),
                "active_goal": (
                    estado_crm_followup.active_goal
                ),
                "active_goal_status": (
                    estado_crm_followup.active_goal_status
                ),
                "current_objective": (
                    estado_crm_followup.current_objective
                ),
                "current_stage": (
                    estado_crm_followup.current_stage
                ),
                "current_commercial_status": (
                    estado_crm_followup.current_commercial_status
                ),
                "conversation_cycle_id": (
                    estado_crm_followup.conversation_cycle_id
                ),
                "followup_step": (
                    estado_crm_followup.followup_step
                ),
                "next_followup_at": (
                    estado_crm_followup.next_followup_at
                ),
                "next_nurturing_at": (
                    estado_crm_followup.next_nurturing_at
                ),
                "automation_enabled": (
                    estado_crm_followup.automation_enabled
                ),
            }
            if estado_crm_followup
            else None
        )

        # ----------------------------------------------------
        # 8. ESCALACIÓN ADMINISTRATIVA
        # ----------------------------------------------------
        
        if (
            accion_actual == "CONSULTAR_ADMIN"
            and escalacion_critica_pre_outbound
            is not None
        ):
            resultado_escalacion = (
                escalacion_critica_pre_outbound
            )

            print(
                "🔐 Escalación administrativa ya ejecutada "
                "pre-outbound; no se duplica."
            )

        else:
            resultado_escalacion = (
                procesar_escalacion_admin_estructurada(
                    db=db,
                    contact=contact,
                    mensaje_usuario=mensaje,
                    respuesta_bot=respuesta_bot,
                    resultado_orquestador=(
                        resultado_orquestador
                    ),
                    memoria_historica=(
                        resultado_memoria_historica
                    ),
                    ejecutar_envio=True,
                )
            )
            
        resultado_final[
            "escalacion_admin"
        ] = resultado_escalacion

        resultado_final[
            "procesado"
        ] = True

        print(
            "✅ Flujo estructurado real procesado: "
            f"contact_id={contact.id}, "
            f"mensaje_enviado={envio_exitoso}, "
            f"requiere_admin="
            f"{resultado_escalacion.get('requiere_escalacion', False)}"
        )

        return resultado_final

    except Exception as e:
        db.rollback()

        resultado_final[
            "error"
        ] = str(e)

        print(
            "❌ Error en flujo estructurado real: "
            f"{e}"
        )

        return resultado_final

# ============================================================
# PRUEBA INTEGRAL CONTROLADA DEL FLUJO ESTRUCTURADO REAL
# ============================================================

class DebugStructuredRealFlowRequest(BaseModel):
    phone_number: str
    message: str
    confirmation: str


@app.post("/debug/structured-real-flow-live")
async def debug_structured_real_flow_live(
    payload: DebugStructuredRealFlowRequest,
    db: Session = Depends(get_db),
):
    """
    Ejecuta una prueba integral y real del nuevo flujo estructurado.

    Esta ruta:
    - guarda un mensaje entrante;
    - ejecuta el nuevo orquestador;
    - envía una respuesta real al número indicado;
    - guarda la respuesta saliente;
    - puede crear una tarea administrativa;
    - puede enviar una alerta al administrador;
    - no modifica el feature flag;
    - no conecta todavía el nuevo flujo al webhook.
    """

    confirmacion = str(
        payload.confirmation or ""
    ).strip()

    if (
        confirmacion
        != "EJECUTAR_FLUJO_ESTRUCTURADO_REAL"
    ):
        return {
            "modo": "PRUEBA_INTEGRAL_ESTRUCTURADA",
            "ejecucion_autorizada": False,
            "mensaje_enviado": False,
            "error": "CONFIRMACION_INVALIDA",
        }

    numero_recibido = str(
        payload.phone_number or ""
    ).strip()

    mensaje = str(
        payload.message or ""
    ).strip()

    if not numero_recibido:
        return {
            "modo": "PRUEBA_INTEGRAL_ESTRUCTURADA",
            "ejecucion_autorizada": True,
            "mensaje_enviado": False,
            "error": "PHONE_NUMBER_REQUERIDO",
        }

    if not mensaje:
        return {
            "modo": "PRUEBA_INTEGRAL_ESTRUCTURADA",
            "ejecucion_autorizada": True,
            "mensaje_enviado": False,
            "error": "MESSAGE_REQUERIDO",
        }

    numero_whatsapp = numero_recibido

    if not numero_whatsapp.startswith(
        "whatsapp:"
    ):
        numero_whatsapp = (
            f"whatsapp:{numero_whatsapp}"
        )

    if es_numero_admin(numero_whatsapp):
        return {
            "modo": "PRUEBA_INTEGRAL_ESTRUCTURADA",
            "ejecucion_autorizada": True,
            "mensaje_enviado": False,
            "error": (
                "NO_SE_PUEDE_USAR_EL_NUMERO_ADMIN_COMO_PROSPECTO"
            ),
        }

    try:
        contact = get_or_create_contact(
            db,
            numero_whatsapp,
        )

        # El puente productivo asume que el mensaje entrante
        # ya fue guardado, tal como sucede en el webhook.
        save_message(
            db,
            contact.id,
            "incoming",
            mensaje,
        )

        resultado_flujo = (
            procesar_mensaje_whatsapp_estructurado_real(
                db=db,
                contact=contact,
                from_number=numero_whatsapp,
                mensaje_usuario=mensaje,
            )
        )

        return {
            "modo": "PRUEBA_INTEGRAL_ESTRUCTURADA",
            "ejecucion_autorizada": True,
            "feature_flag_activo": (
                USE_STRUCTURED_AI_FLOW
            ),
            "phone_number_recibido": (
                numero_recibido
            ),
            "mensaje_simulado": mensaje,
            "contact_id": contact.id,
            "resultado_flujo": resultado_flujo,
            "advertencia": (
                "Esta prueba sí guardó el mensaje y sí pudo "
                "enviar una respuesta real por WhatsApp."
            ),
            "error": "",
        }

    except Exception as e:
        db.rollback()

        return {
            "modo": "PRUEBA_INTEGRAL_ESTRUCTURADA",
            "ejecucion_autorizada": True,
            "mensaje_enviado": False,
            "error": str(e),
        }

# ============================================================
# PROCESAMIENTO DIFERIDO DEL BUFFER DE WHATSAPP
# ============================================================

def procesar_buffer_whatsapp_estructurado(
    from_number: str,
    identificador_buffer: str,
) -> None:
    """
    Procesa conjuntamente los mensajes recibidos después de que
    transcurre el periodo de inactividad configurado.

    Un mismo contacto nunca puede tener dos ejecuciones del flujo
    estructurado procesándose al mismo tiempo.

    El lock por contacto se adquiere ANTES de retirar el buffer,
    para evitar que lotes posteriores del mismo contacto sean
    extraídos mientras todavía se procesa un lote anterior.
    """

    numero_normalizado = (
        normalizar_numero_whatsapp(
            from_number
        )
    )

    clave_buffer = (
        numero_normalizado
        or str(from_number or "").strip()
    )

    lock_contacto = (
        obtener_lock_procesamiento_estructurado(
            clave_buffer
        )
    )

    # --------------------------------------------------------
    # SERIALIZACIÓN COMPLETA POR CONTACTO
    # --------------------------------------------------------

    with lock_contacto:

        mensajes_buffer = []
        message_ids_buffer = []

        # ----------------------------------------------------
        # Sólo ahora retiramos el lote del buffer.
        #
        # Si mientras esperábamos el lock llegó otro mensaje,
        # habrá cambiado el identificador y este timer viejo
        # simplemente dejará de ser válido.
        # ----------------------------------------------------

        with MESSAGE_BUFFER_LOCK:

            buffer_actual = MESSAGE_BUFFERS.get(
                clave_buffer
            )

            if not buffer_actual:
                return

            if (
                buffer_actual.get("identificador")
                != identificador_buffer
            ):
                return

            programado_para_texto = str(
                buffer_actual.get(
                    "programado_para",
                    "",
                )
                or ""
            ).strip()

            if programado_para_texto:
                try:
                    programado_para = (
                        datetime.fromisoformat(
                            programado_para_texto
                        )
                    )

                    ahora_real = datetime.now(
                        timezone.utc
                    )

                    retraso_timer = (
                        ahora_real
                        - programado_para
                    ).total_seconds()

                    print(
                        "⏱️ BUFFER TIMER: "
                        f"programado={programado_para_texto}, "
                        f"ejecutado={ahora_real.isoformat()}, "
                        f"retraso={retraso_timer:.2f}s"
                    )

                except Exception as e:
                    print(
                        "⚠️ No fue posible medir retraso "
                        f"del buffer: {e}"
                    )

            mensajes_buffer = list(
                buffer_actual.get(
                    "mensajes",
                    [],
                )
            )

            message_ids_buffer = list(
                buffer_actual.get(
                    "message_ids",
                    [],
                )
            )

            MESSAGE_BUFFERS.pop(
                clave_buffer,
                None,
            )

        mensaje_conjunto = "\n".join(
            str(mensaje or "").strip()
            for mensaje in mensajes_buffer
            if str(mensaje or "").strip()
        ).strip()

        if not mensaje_conjunto:
            return

        message_ids_validos = [
            message_id
            for message_id in message_ids_buffer
            if (
                isinstance(message_id, int)
                and message_id > 0
            )
        ]

        max_message_id_lote = (
            max(message_ids_validos)
            if message_ids_validos
            else None
        )

        db_buffer = SessionLocal()

        try:
            contact = get_or_create_contact(
                db_buffer,
                from_number,
            )

            db_buffer.refresh(
                contact
            )

            print(
                "\n🔒 PROCESAMIENTO ESTRUCTURADO "
                "EXCLUSIVO POR CONTACTO"
            )

            print(
                f"📱 Número: {from_number}"
            )

            print(
                "\n📦 PROCESANDO BUFFER DE WHATSAPP"
            )

            print(
                f"📨 Mensajes agrupados: "
                f"{len(mensajes_buffer)}"
            )

            print(
                f"🆔 Corte de historial: "
                f"{max_message_id_lote}"
            )

            print(
                f"📝 Mensaje conjunto: "
                f"{mensaje_conjunto}"
            )

            resultado_estructurado = (
                procesar_mensaje_whatsapp_estructurado_real(
                    db=db_buffer,
                    contact=contact,
                    from_number=from_number,
                    mensaje_usuario=mensaje_conjunto,
                    max_message_id=max_message_id_lote,
                )
            )

            resultado_json = json.dumps(
                resultado_estructurado,
                ensure_ascii=False,
                default=str,
            )

            print(
                "✅ Buffer estructurado procesado: "
                + resultado_json
            )

        except Exception as e:
            db_buffer.rollback()

            print(
                "❌ Error procesando buffer "
                f"de WhatsApp: {e}"
            )

        finally:
            db_buffer.close()
            
def agregar_mensaje_al_buffer_whatsapp(
    from_number: str,
    mensaje: str,
    message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Agrega un mensaje al buffer del contacto.

    Cada mensaje nuevo cancela el temporizador anterior y vuelve
    a iniciar el periodo de espera.

    También conserva los IDs de los mensajes persistidos para
    poder delimitar exactamente el historial correspondiente
    a cada lote.
    """

    numero_normalizado = (
        normalizar_numero_whatsapp(
            from_number
        )
    )

    clave_buffer = (
        numero_normalizado
        or str(from_number or "").strip()
    )

    identificador_buffer = (
        f"{clave_buffer}-"
        f"{datetime.now(timezone.utc).timestamp()}"
    )

    with MESSAGE_BUFFER_LOCK:

        buffer_anterior = MESSAGE_BUFFERS.get(
            clave_buffer
        )

        mensajes_acumulados = []
        message_ids_acumulados = []

        if buffer_anterior:

            temporizador_anterior = (
                buffer_anterior.get(
                    "temporizador"
                )
            )

            if temporizador_anterior:
                try:
                    temporizador_anterior.cancel()
                except Exception:
                    pass

            mensajes_acumulados = list(
                buffer_anterior.get(
                    "mensajes",
                    [],
                )
            )

            message_ids_acumulados = list(
                buffer_anterior.get(
                    "message_ids",
                    [],
                )
            )

        mensaje_limpio = str(
            mensaje or ""
        ).strip()

        if mensaje_limpio:
            mensajes_acumulados.append(
                mensaje_limpio
            )

        if (
            isinstance(message_id, int)
            and message_id > 0
        ):
            message_ids_acumulados.append(
                message_id
            )

        temporizador = threading.Timer(
            MESSAGE_BUFFER_SECONDS,
            procesar_buffer_whatsapp_estructurado,
            args=(
                from_number,
                identificador_buffer,
            ),
        )

        temporizador.daemon = True

        ahora_buffer = datetime.now(
            timezone.utc
        )

        MESSAGE_BUFFERS[clave_buffer] = {
            "identificador": identificador_buffer,
            "mensajes": mensajes_acumulados,
            "message_ids": message_ids_acumulados,
            "temporizador": temporizador,
            "ultima_actualizacion": (
                ahora_buffer.isoformat()
            ),
            "programado_para": (
                ahora_buffer
                + timedelta(
                    seconds=MESSAGE_BUFFER_SECONDS
                )
            ).isoformat(),
        }
        temporizador.start()

    print(
        "📥 Mensaje agregado al buffer: "
        f"numero={from_number}, "
        f"mensajes={len(mensajes_acumulados)}, "
        f"message_ids={message_ids_acumulados}, "
        f"espera={MESSAGE_BUFFER_SECONDS}s"
    )

    return {
        "buffer_activo": True,
        "mensajes_acumulados": (
            len(mensajes_acumulados)
        ),
        "message_ids": (
            message_ids_acumulados
        ),
        "segundos_espera": (
            MESSAGE_BUFFER_SECONDS
        ),
    }
    
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(""),
    ButtonText: str = Form(""),
    ButtonPayload: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: str = Form(""),
    MediaContentType0: str = Form(""),
    db: Session = Depends(get_db)
):
    try:
        mensaje_entrada = (Body or "").strip()

        # Si llegó audio por WhatsApp, lo transcribimos
        if es_audio_whatsapp(NumMedia, MediaContentType0):
            print("🎙️ Se detectó audio entrante por WhatsApp")

            try:
                audio_bytes = descargar_media_twilio(MediaUrl0)
                transcripcion = transcribir_audio_gemini(audio_bytes, mime_type=MediaContentType0 or "audio/ogg")

                if transcripcion:
                    mensaje_entrada = transcripcion
                    print(f"📝 Transcripción audio: {mensaje_entrada}")
                else:
                    mensaje_entrada = "[Audio recibido sin transcripción]"
                    print("⚠️ Audio recibido, pero no se obtuvo texto")

            except Exception as e:
                print(f"❌ Error transcribiendo audio: {e}")
                mensaje_entrada = "[Audio recibido pero no se pudo transcribir]"

        # ===== RESPUESTA DE ADMINISTRADOR / WHATSAPP MAESTRO =====
        # El número administrador nunca entra al flujo normal del bot.
        # ===== RESPUESTA DE ADMINISTRADOR / WHATSAPP MAESTRO =====
        # El número administrador nunca entra al flujo normal del bot.
        if es_numero_admin(From):
            print(
                "👑 Mensaje recibido desde WhatsApp maestro/admin"
            )
            registrar_inbound_admin_whatsapp(
                db,
                From,
            )

            button_payload_limpio = str(
                ButtonPayload or ""
            ).strip().upper()

            button_text_limpio = str(
                ButtonText or ""
            ).strip()

            if button_payload_limpio:
                print(
                    "🔘 Quick Reply admin recibido: "
                    f"payload={button_payload_limpio}, "
                    f"text={button_text_limpio!r}"
                )

            if button_payload_limpio == "VER_MENSAJE":
                ADMIN_SELECTED_TASKS.pop(
                    normalizar_numero_whatsapp(From),
                    None,
                )

                tareas_pendientes = (
                    obtener_tareas_admin_pendientes(db)
                )

                if not tareas_pendientes:
                    respuesta_admin = (
                        "No hay conversaciones pendientes "
                        "de confirmación en este momento."
                    )

                    resultado = enviar_respuesta_twilio(
                        From,
                        respuesta_admin,
                    )

                    print(
                        "📣 Botón VER_MENSAJE sin pendientes: "
                        f"{resultado}"
                    )

                    return {
                        "status": (
                            "admin_quick_reply_no_pending"
                        )
                    }

                if len(tareas_pendientes) == 1:
                    tarea = tareas_pendientes[0]

                    ADMIN_SELECTED_TASKS[
                        normalizar_numero_whatsapp(From)
                    ] = tarea.id

                    contacto_tarea = (
                        db.query(Contact)
                        .filter(
                            Contact.id == tarea.contact_id
                        )
                        .first()
                    )

                    fecha_cita_admin = ""
                    hora_cita_admin = ""

                    if contacto_tarea is not None:
                        fecha_cita_raw = str(
                            get_note_value(
                                contacto_tarea,
                                "FECHA_CITA",
                            )
                            or get_note_value(
                                contacto_tarea,
                                "FECHA_CITA_ISO",
                            )
                            or ""
                        ).strip()

                        hora_cita_admin = str(
                            get_note_value(
                                contacto_tarea,
                                "HORA_CITA",
                            )
                            or ""
                        ).strip()

                        fecha_cita_admin = (
                            formatear_fecha_cita_calendario(
                                fecha_cita_raw
                            )
                            or fecha_cita_raw
                        )

                    if (
                        fecha_cita_admin
                        and hora_cita_admin
                    ):
                        detalle_solicitud = (
                            "Solicitud de visita:\n"
                            f"{fecha_cita_admin}, "
                            f"{hora_cita_admin}"
                        )

                    else:
                        detalle_solicitud = (
                            "Solicitud pendiente:\n"
                            f"{tarea.trigger_message or 'Sin detalle'}"
                        )

                    respuesta_admin = (
                        "🔔 Confirmación de cita pendiente\n\n"
                        f"Prospecto: "
                        f"{tarea.prospect_phone or 'No disponible'}\n\n"
                        f"{detalle_solicitud}\n\n"
                        "¿Qué deseas que le responda?"
                    )
                    
                    resultado = enviar_respuesta_twilio(
                        From,
                        respuesta_admin,
                    )

                    print(
                        "📋 Quick Reply abrió tarea admin "
                        f"{tarea.id}: {resultado}"
                    )

                    return {
                        "status": (
                            "admin_quick_reply_task_opened"
                        ),
                        "task_id": tarea.id,
                    }

                menu = construir_menu_tareas_pendientes(
                    tareas_pendientes
                )

                resultado = enviar_respuesta_twilio(
                    From,
                    menu,
                )

                print(
                    "📋 Quick Reply abrió menú de pendientes: "
                    f"{resultado}"
                )

                return {
                    "status": (
                        "admin_quick_reply_menu_sent"
                    )
                }

            return procesar_respuesta_admin(
                db,
                From,
                mensaje_entrada,
            )

        # ===== FALLBACK SI FALLÓ LA TRANSCRIPCIÓN =====
        if mensaje_entrada in [
            "[Audio recibido pero no se pudo transcribir]",
            "[Audio recibido sin transcripción]"
        ]:
            
            respuesta = (
                "Con gusto le apoyamos.\n\n"
                "Recibimos su mensaje de voz, pero en este momento no pudimos procesarlo correctamente.\n\n"
                "¿Nos lo podría compartir por texto para darle seguimiento adecuado por este medio?"
            )

            contact = get_or_create_contact(
                db,
                From,
            )

            # El inbound se conserva siempre para auditoría.
            mensaje_guardado = save_message(
                db,
                contact.id,
                "incoming",
                mensaje_entrada,
            )

            # Un contacto bloqueado no recibe tampoco
            # respuestas de fallback.
            if contacto_esta_bloqueado(
                db,
                contact.id,
            ):
                print(
                    "🚫 FALLBACK AUDIO SUPRIMIDO: "
                    f"contact_id={contact.id} bloqueado."
                )

                return {
                    "status": "blocked_contact_audio",
                    "contact_id": contact.id,
                    "message_id": (
                        mensaje_guardado.id
                        if mensaje_guardado
                        else None
                    ),
                }

            resultado = enviar_respuesta_twilio(
                From,
                respuesta,
            )

            twilio_sid = None

            if "SID:" in resultado:
                twilio_sid = (
                    resultado
                    .split("SID: ")[1]
                    .strip()
                )

            save_message(
                db,
                contact.id,
                "outgoing",
                respuesta,
                twilio_sid,
            )

            print(
                f"🤖 BOT (fallback audio): {respuesta}"
            )
            print(
                f"📤 Estado: {resultado}"
            )

            return {
                "status": "processed_audio_fallback",
                "contact_id": contact.id,
            }
        
        
        print(f"\n{'='*60}")
        print(f"💬 WHATSAPP CHAT - {datetime.now().strftime('%H:%M:%S')}")
        print(f"📱 De: {From}")
        print(f"👤 USUARIO: {mensaje_entrada}")
        print(f"{'-'*40}")

        contact = get_or_create_contact(
            db,
            From,
        )

        # El mensaje entrante se guarda una sola vez antes
        # de decidir cuál flujo debe procesarlo.
        mensaje_guardado = save_message(
            db,
            contact.id,
            "incoming",
            mensaje_entrada,
        )

        # ====================================================
        # PUERTA DE MODERACIÓN
        # ====================================================
        #
        # El inbound ya quedó persistido para auditoría.
        #
        # A partir de aquí un contacto bloqueado:
        # - no entra a Gemini;
        # - no entra al flujo comercial;
        # - no modifica una cita;
        # - no genera respuesta automática.
        # ====================================================

        if contacto_esta_bloqueado(
            db,
            contact.id,
        ):
            print(
                "🚫 INBOUND IGNORADO: "
                f"contact_id={contact.id} "
                "ya se encuentra bloqueado."
            )

            return {
                "status": "blocked_contact",
                "contact_id": contact.id,
                "message_id": (
                    mensaje_guardado.id
                    if mensaje_guardado
                    else None
                ),
            }

        evaluacion_moderacion = (
            evaluar_riesgo_mensaje_entrante(
                mensaje_entrada
            )
        )

        if (
            evaluacion_moderacion.get(
                "accion"
            )
            == "BLOCK"
        ):
            bloquear_contacto(
                db=db,
                contact=contact,
                reason=(
                    evaluacion_moderacion.get(
                        "motivo",
                        "",
                    )
                ),
                risk_category=(
                    evaluacion_moderacion.get(
                        "categoria",
                        "SPAM",
                    )
                ),
                source="AUTO",
                message_id=(
                    mensaje_guardado.id
                    if mensaje_guardado
                    else None
                ),
            )

            # Bloqueo silencioso:
            # no respondemos al remitente.
            return {
                "status": (
                    "blocked_by_moderation"
                ),
                "contact_id": contact.id,
                "message_id": (
                    mensaje_guardado.id
                    if mensaje_guardado
                    else None
                ),
            }

        # ====================================================
        # PRIORIDAD ABSOLUTA: COMPLETAR DATOS DE CITA CONFIRMADA
        # ====================================================
        #
        # Si administración ya confirmó la cita y estamos
        # esperando datos del tutor/alumno, este mensaje NO debe
        # regresar al flujo comercial estructurado.
        #
        # El único objetivo en este estado es completar el
        # registro de la cita.
        # ====================================================

        estado_flujo_actual = get_flow_state(
            contact
        )

        objetivo_pendiente_actual = str(
            get_note_value(
                contact,
                "OBJETIVO_PENDIENTE",
            )
            or ""
        ).strip().upper()

        estado_comercial_actual = str(
            getattr(
                contact,
                "status",
                "",
            )
            or ""
        ).strip().upper()

        # ====================================================
        # INVARIANTE: CITA CONFIRMADA CON DATOS PENDIENTES
        # ====================================================
        #
        # No dependemos de una sola marca de estado.
        #
        # Si cualquiera de las fuentes persistentes indica que
        # todavía estamos completando una cita confirmada,
        # preservamos ese objetivo y evitamos regresar al embudo.
        #
        # También recuperamos el estado si por alguna razón
        # FLOW_STATE y OBJETIVO_PENDIENTE quedaron desalineados.
        # ====================================================

        datos_cita_realmente_pendientes = False

        if (
            estado_comercial_actual
            == "VISITA_CONFIRMADA"
        ):
            try:
                datos_cita_realmente_pendientes = bool(
                    construir_solicitud_datos_cita(
                        contact
                    )
                )
            except Exception as e:
                print(
                    "⚠️ No fue posible verificar datos "
                    f"pendientes de cita: {e}"
                )

        cita_confirmada_con_datos_pendientes = bool(
            estado_flujo_actual
            == "ESPERANDO_DATOS_CITA"
            or objetivo_pendiente_actual
            == "OBTENER_DATOS_CITA"
            or (
                estado_comercial_actual
                == "VISITA_CONFIRMADA"
                and datos_cita_realmente_pendientes
            )
        )

        if cita_confirmada_con_datos_pendientes:

            # Reparación defensiva de cualquier divergencia.
            if (
                estado_flujo_actual
                != "ESPERANDO_DATOS_CITA"
            ):
                set_flow_state(
                    contact,
                    "ESPERANDO_DATOS_CITA",
                )

            if (
                objetivo_pendiente_actual
                != "OBTENER_DATOS_CITA"
            ):
                set_note_value(
                    contact,
                    "OBJETIVO_PENDIENTE",
                    "OBTENER_DATOS_CITA",
                )

            db.commit()

            print(
                "📌 Cita confirmada con datos pendientes: "
                "se preserva OBTENER_DATOS_CITA y se procesa "
                "el mensaje únicamente dentro del registro."
            )

            return procesar_datos_registro_cita(
                db,
                contact,
                From,
                mensaje_entrada,
            )
            
        usar_flujo_estructurado = bool(
            USE_STRUCTURED_AI_FLOW
            or es_numero_prueba_flujo_estructurado(
                From
            )
        )

        if usar_flujo_estructurado:
            origen_activacion = (
                "FEATURE_FLAG_GENERAL"
                if USE_STRUCTURED_AI_FLOW
                else "NUMERO_DE_PRUEBA"
            )

            print(
                "🧪 Flujo estructurado enviado al buffer: "
                f"origen={origen_activacion}, "
                f"numero={From}"
            )

            resultado_buffer = (
                agregar_mensaje_al_buffer_whatsapp(
                    from_number=From,
                    mensaje=mensaje_entrada,
                    message_id=(
                        mensaje_guardado.id
                        if mensaje_guardado
                        else None
                    ),
                )
            )

            return {
                "status": "buffered_structured_flow",
                "contact_id": contact.id,
                "activation_source": (
                    origen_activacion
                ),
                "buffer_result": resultado_buffer,
            }

            
        if es_saludo_repetido_temprano(estado_flujo_actual, mensaje_entrada):
            return responder_saludo_repetido_temprano(db, contact, From, mensaje_entrada)
        
        nivel_detectado = detectar_nivel_interes(mensaje_entrada)

        if nivel_detectado:
            set_note_value(contact, "NIVEL_INTERES", nivel_detectado)
            db.commit()
            print(f"🎓 Nivel de interés guardado: {nivel_detectado}")

        history = get_conversation_history(db, From, limit=5)

        print(f"🧠 Usando Gemini: {bool(GEMINI_API_KEY)}")
        print(f"📊 Historial disponible: {len(history)} mensajes")

        
        respuesta, estado_actual, estado_siguiente = generar_respuesta_inteligente(
            mensaje_entrada,
            contact,
            history,
        )
        
        print(
            f"🧭 Estado flujo usado para responder: "
            f"{estado_actual}"
        )
        print(
            f"➡️ Estado flujo siguiente: "
            f"{estado_siguiente}"
        )

        # ====================================================
        # BARRERA FINAL DE SALIDA COMERCIAL AL PROSPECTO
        # ====================================================

        validacion_salida = (
            validar_salida_comercial_prospecto(
                respuesta
            )
        )

        if validacion_salida.get(
            "bloqueada",
            False,
        ):
            print(
                "🛡️ Respuesta original sustituida "
                "antes de enviar al prospecto."
            )

            respuesta = str(
                validacion_salida.get(
                    "mensaje_seguro",
                    "",
                )
                or ""
            ).strip()

        resultado = enviar_respuesta_twilio(
            From,
            respuesta,
        )
        

        twilio_sid = None
        if "SID:" in resultado:
            twilio_sid = resultado.split("SID: ")[1].strip()

        save_message(db, contact.id, 'outgoing', respuesta, twilio_sid)
        
        set_flow_state(contact, estado_siguiente)
        db.commit()
        
        if detecta_condicion_consulta_admin(respuesta):
            tarea_admin = crear_tarea_admin_pendiente(db, contact, mensaje_entrada, respuesta)
            enviar_alerta_admin_whatsapp(
                db,
                contact,
                mensaje_entrada,
                respuesta,
                tarea_admin.id,
            )
        
        nuevo_estado = actualizar_estado_segun_intencion(mensaje_entrada, respuesta, contact, db)
        print(f"🎯 Análisis de intención: {nuevo_estado}")

        print(f"🤖 BOT: {respuesta}")
        print(f"🤖 Motor disponible: {'Gemini' if GEMINI_API_KEY else 'Predeterminado'}")
        print(f"📤 Estado: {resultado}")
        print(f"👤 Estado contacto: {contact.status}")
        print(f"📊 Total mensajes: {contact.total_messages}")
        print(f"{'='*60}\n")

        return {"status": "processed", "contact_id": contact.id}

    except Exception as e:
        print(f"❌ Error en webhook: {e}")
        return {"status": "error", "detail": str(e)}



# Configuración de Gemini
GEMINI_API_KEY = os.getenv("GOOGLE_AI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

GEMINI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-1.5-flash"
    ).split(",")
    if model.strip()
]

# Configurar la API de Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ================= FUNCIONES DE BASE DE DATOS =================
def get_or_create_contact(db: Session, phone_number: str):
    """Obtiene o crea un contacto en la base de datos"""
    # Limpiar número: quitar prefijo "whatsapp:"
    if phone_number.startswith("whatsapp:"):
        clean_number = phone_number.replace("whatsapp:", "")
    else:
        clean_number = phone_number
    
    contact = db.query(Contact).filter(Contact.phone_number == clean_number).first()
    
    if not contact:
        # Es un nuevo contacto
        contact = Contact(
            phone_number=clean_number,  # Usar número limpio
            status="PROSPECTO_NUEVO",
            first_contact=datetime.now(timezone.utc),
            last_contact=datetime.now(timezone.utc),
            total_messages=0
        )
        db.add(contact)
        db.commit()
        db.refresh(contact)
        
        crear_estado_crm_contacto_nuevo(
            db=db,
            contact=contact,
        )
    
    return contact

def save_message(db: Session, contact_id: int, direction: str, content: str, twilio_sid: str = None):
    """Guarda un mensaje en la base de datos"""
    # Usar datetime estándar (la BD guardará en UTC)
    timestamp = datetime.now(timezone.utc)
    
    message = Message(
        contact_id=contact_id,
        direction=direction,
        content=content,
        timestamp=timestamp,
        twilio_sid=twilio_sid
    )
    db.add(message)
    
    # Actualizar contador de mensajes del contacto
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if contact:
        contact.total_messages += 1
        contact.last_contact = datetime.now(timezone.utc)

    actualizar_crm_por_mensaje(
        db=db,
        contact_id=contact_id,
        direction=direction,
        timestamp=timestamp,
    )
    
    db.commit()
    return message

def registrar_inbound_admin_whatsapp(
    db: Session,
    from_number: str,
):
    """
    Registra de forma persistente la última vez que el
    administrador escribió al número WhatsApp del bot.

    Este registro se mantiene fuera del CRM de prospectos.
    """

    admin_number_normalizado = (
        normalizar_numero_whatsapp(
            from_number
        )
    )

    if not admin_number_normalizado:
        print(
            "⚠️ No se pudo registrar inbound admin: "
            "número vacío o inválido"
        )
        return None

    ahora_utc = datetime.now(
        timezone.utc
    )

    try:
        estado_admin = (
            db.query(AdminWhatsappState)
            .filter(
                AdminWhatsappState.admin_number
                == admin_number_normalizado
            )
            .first()
        )

        if estado_admin is None:
            estado_admin = AdminWhatsappState(
                admin_number=(
                    admin_number_normalizado
                ),
                last_inbound_at=ahora_utc,
                created_at=ahora_utc,
                updated_at=ahora_utc,
            )

            db.add(
                estado_admin
            )

        else:
            estado_admin.last_inbound_at = (
                ahora_utc
            )

            estado_admin.updated_at = (
                ahora_utc
            )

        db.commit()
        db.refresh(
            estado_admin
        )

        print(
            "🕒 Inbound admin registrado: "
            f"numero={admin_number_normalizado}, "
            f"last_inbound_at="
            f"{estado_admin.last_inbound_at}"
        )

        return estado_admin

    except Exception as e:
        db.rollback()

        print(
            "❌ Error registrando inbound "
            f"del administrador: {e}"
        )

        return None

def admin_whatsapp_tiene_ventana_abierta(
    db: Session,
    admin_number: str,
) -> bool:
    """
    Determina si el administrador escribió al bot
    durante las últimas 24 horas.

    Si no existe registro, no existe fecha de inbound
    o ocurre cualquier error, se considera la ventana
    cerrada para utilizar la plantilla aprobada.
    """

    admin_number_normalizado = (
        normalizar_numero_whatsapp(
            admin_number
        )
    )

    if not admin_number_normalizado:
        print(
            "⚠️ Ventana admin: número inválido. "
            "Se considera cerrada."
        )
        return False

    try:
        estado_admin = (
            db.query(AdminWhatsappState)
            .filter(
                AdminWhatsappState.admin_number
                == admin_number_normalizado
            )
            .first()
        )

        if estado_admin is None:
            print(
                "🕒 Ventana admin cerrada: "
                "no existe registro previo."
            )
            return False

        ultimo_inbound = (
            estado_admin.last_inbound_at
        )

        if ultimo_inbound is None:
            print(
                "🕒 Ventana admin cerrada: "
                "no existe last_inbound_at."
            )
            return False

        # Protección para bases de datos o drivers que
        # puedan devolver el timestamp sin tzinfo.
        if ultimo_inbound.tzinfo is None:
            ultimo_inbound = (
                ultimo_inbound.replace(
                    tzinfo=timezone.utc
                )
            )

        ahora_utc = datetime.now(
            timezone.utc
        )

        tiempo_transcurrido = (
            ahora_utc - ultimo_inbound
        )

        ventana_abierta = (
            timedelta(0)
            <= tiempo_transcurrido
            < timedelta(hours=24)
        )

        horas_transcurridas = (
            tiempo_transcurrido.total_seconds()
            / 3600
        )

        print(
            "🕒 Estado ventana admin: "
            f"abierta={ventana_abierta}, "
            f"ultimo_inbound={ultimo_inbound}, "
            f"horas_transcurridas="
            f"{horas_transcurridas:.2f}"
        )

        return ventana_abierta

    except Exception as e:
        print(
            "❌ Error consultando ventana "
            f"WhatsApp admin: {e}"
        )

        return False
    
def get_conversation_history(db: Session, phone_number: str, limit: int = 10):
    """Obtiene el historial de conversación de un contacto"""
    if phone_number.startswith("whatsapp:"):
        clean_number = phone_number.replace("whatsapp:", "")
    else:
        clean_number = phone_number

    contact = db.query(Contact).filter(Contact.phone_number == clean_number).first()
    if not contact:
        return []

    messages = db.query(Message).filter(Message.contact_id == contact.id)\
        .order_by(Message.timestamp.desc())\
        .limit(limit)\
        .all()

    return messages[::-1]  # Invertir para orden cronológico

def generar_respuesta_gemini(mensaje_usuario: str, contact, history):
    """Genera respuesta usando Gemini API y devuelve respuesta + estado usado + estado siguiente"""

    estado_actual = get_flow_state(contact)
    estado_respuesta = determinar_estado_respuesta(estado_actual, mensaje_usuario, history)
    estado_siguiente = determinar_estado_siguiente(estado_respuesta, mensaje_usuario)
    
    # El saludo inicial se responde de forma controlada por código.
    # La IA empieza a participar a partir del segundo mensaje.
    if estado_respuesta == "SALUDO_INICIAL":
        respuesta = generar_respuesta_predeterminada(mensaje_usuario, contact, estado_respuesta)
        return respuesta, estado_respuesta, estado_siguiente
    
    if estado_respuesta == "CAMPUS_EXTERNO_NO_ATENDIBLE":
        respuesta = generar_respuesta_predeterminada(mensaje_usuario, contact, estado_respuesta)
        return respuesta, estado_respuesta, estado_siguiente

    if estado_respuesta == "CITA_DIA_NO_LABORAL":
        respuesta = generar_respuesta_predeterminada(mensaje_usuario, contact, estado_respuesta)
        return respuesta, estado_respuesta, estado_siguiente

    if estado_respuesta == "VALIDACION_ZONA_OBLIGATORIA":
        respuesta = generar_respuesta_predeterminada(mensaje_usuario, contact, estado_respuesta)
        return respuesta, estado_respuesta, estado_siguiente

    if not GEMINI_API_KEY:
        print("⚠️  Gemini API Key no configurada, usando respuestas predeterminadas")
        respuesta = generar_respuesta_predeterminada(mensaje_usuario, contact, estado_respuesta)
        return respuesta, estado_respuesta, estado_siguiente

    historial_lista = []
    if history:
        for msg in history:
            prefijo = "Usuario" if msg.direction == "incoming" else "Asistente"
            historial_lista.append(f"{prefijo}: {msg.content}")

    nivel_interes = get_note_value(contact, "NIVEL_INTERES")

    if nivel_interes:
        historial_lista.append(
            f"DATO CONOCIDO DEL PROSPECTO: El nivel de interés es {nivel_interes}. No vuelva a preguntar el nivel educativo."
        )

    prompt = prompt_manager.build_prompt(
        mensaje_usuario=mensaje_usuario,
        historial_lista=historial_lista,
        estado=estado_respuesta
    )

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7
            ),
            tarea="respuesta principal"
        )
        
        respuesta = extraer_texto_respuesta_gemini(response)
        print(f"🤖 Gemini modelo usado: {modelo_usado}")
        print(f"🤖 Gemini respuesta COMPLETA: {repr(respuesta)}")
        return respuesta, estado_respuesta, estado_siguiente

    except Exception as e:
        print(f"❌ Excepción en Gemini: {e}")
        respuesta = generar_respuesta_predeterminada(mensaje_usuario, contact, estado_respuesta)
        return respuesta, estado_respuesta, estado_siguiente
        
def generar_respuesta_predeterminada(mensaje_usuario: str, contact, estado_actual: str) -> str:
    """Fallback alineado al embudo inicial por estado"""

    if estado_actual == "SALUDO_INICIAL":
        return generar_saludo_inicial_contextual(mensaje_usuario)

    if estado_actual == "CITA_DIA_NO_LABORAL":
        return """Le ofrezco una disculpa, los sábados y domingos no recibimos visitas.

Con gusto podemos agendarle de lunes a viernes, en un horario de 8:00 a.m. a 1:00 p.m.

Si por cuestiones laborales requiere otro horario, podemos revisar una alternativa, máximo hasta las 4:00 p.m.

¿Qué día de lunes a viernes le funcionaría mejor?"""

    if estado_actual == "ESPERANDO_INTENCION":
        return """Con gusto le orientamos,

¿ya tiene alguna referencia de Colegio Valle de Filadelfia Campus Santa Cruz?"""

    if estado_actual == "ESPERANDO_REFERENCIA":
        return """Muy bien.

Con fines de confirmar, nuestro campus está en Santa Cruz Atizapán, a unos 15 min de Santiago Tianguistenco.

¿En qué zona vive usted?"""

    if estado_actual == "VALIDACION_ZONA":
        return """Para continuar y orientarle correctamente, necesito confirmar si se encuentra dentro de nuestra zona de atención.

¿En qué zona vive usted?"""

    if estado_actual == "VALIDACION_ZONA_OBLIGATORIA":
        return """Con gusto le compartimos la información de colegiaturas.

Antes de avanzar con costos, necesito confirmar si se encuentra dentro de nuestra zona de atención, ya que este canal corresponde únicamente al Campus Santa Cruz Atizapán.

¿En qué zona vive usted?"""

    if estado_actual == "ZONA_INVALIDA_POTENCIAL_METEPEC":
        return """Le ofrecemos una disculpa.
    
    Por la zona que nos menciona, es posible que esté buscando otro campus o una ubicación distinta al Campus Santa Cruz Atizapán.
    
    Este canal corresponde al Colegio Valle de Filadelfia Campus Santa Cruz Atizapán.
    
    ¿Desea continuar con información de este campus?"""

    if estado_actual == "RESPUESTA_SOBRE_METODO":
        return """Nuestro *Método Filadelfia* es un modelo pedagógico que:

Se centra en cada niño(a), adaptando contenidos y retos a sus necesidades.

Se basa en 3 pilares:
1. Desarrollo *lógico matemático*
2. Estimulación *artístico musical*
3. Fortalecimiento físico de *ligamentos y articulaciones*

También incluye desarrollo emocional, emprendimiento y salud física.

Integra el método Suzuki y apoyo neuromotor para potenciar el aprendizaje.

Esta propuesta hace que aprender sea una experiencia práctica y significativa.

¿Qué área le interesa más fortalecer en su hijo(a)?"""

    if estado_actual == "RESPUESTA_DE_INTERES":
        return """¿Qué área le interesa más fortalecer en su hijo(a)?"""

    if estado_actual == "DESPUES_DEL_TEMA":
        return """Para nosotros es muy importante que las familias conozcan nuestro modelo educativo en persona.

Una conversación por WhatsApp se queda limitada para transmitir todo lo que ofrecemos.

¿Le gustaría agendar una visita para conocer las instalaciones y platicar con la directora del nivel?"""

    if estado_actual == "INVITACION_CITA":
        return """Para nosotros es muy importante que las familias conozcan nuestro modelo educativo en persona.

Una conversación por WhatsApp se queda limitada para transmitir todo lo que ofrecemos.

¿Le gustaría agendar una visita para conocer las instalaciones y platicar con la directora del nivel?"""

    if estado_actual == "COSTOS_EN_ETAPA_AVANZADA":
        return """Para poderle dar el resto de la información, es importante que las familias conozcan nuestro modelo educativo y nuestras instalaciones.

También es importante conocerlos a ustedes para poder orientarlos mejor.

¿Le gustaría agendar una cita presencial para compartirle todos los detalles, incluyendo costos y opciones?"""

    if estado_actual == "INSISTE_COSTOS_ANTES_DE_AGENDAR":
        return """Le podemos recibir de lunes a viernes en un horario de 8am a 1pm, pero si requiere algún horario en especial por cuestiones de sus actividades laborales, con gusto evaluamos la alternativa, siendo el horario máximo hasta las 4pm.

¿En qué día y hora le funciona mejor para agendar su cita?"""

    if estado_actual == "ESPERANDO_PROPUESTA_CITA":
        return """Le podemos recibir de lunes a viernes en un horario de 8am a 1pm, pero si requiere algún horario en especial por cuestiones de sus actividades laborales, con gusto evaluamos la alternativa, siendo el horario máximo hasta las 4pm.

¿En qué día y hora le funciona mejor para agendar su cita?"""

    if estado_actual == "SEGUIMIENTO_ACORDADO":
        return """Claro, entendemos que es una decisión importante.

Quedamos pendientes por este medio cuando guste retomarlo."""

    if estado_actual == "CAMPUS_EXTERNO_NO_ATENDIBLE":
        msg = (mensaje_usuario or "").lower().strip()
    
        if any(x in msg for x in [
            "número", "numero", "teléfono", "telefono", "contacto",
            "whatsapp", "celular", "llamar", "comunicar"
        ]):
            return """Le ofrecemos una disculpa.
    
    No contamos con el número telefónico, WhatsApp ni datos de contacto del Campus Metepec.
    
    Este canal corresponde únicamente al Colegio Valle de Filadelfia Campus Santa Cruz Atizapán, y cada campus se administra de forma independiente.
    
    Le sugerimos buscar directamente los canales oficiales del Campus Metepec."""
    
        if any(x in msg for x in [
            "costo", "costos", "precio", "precios", "colegiatura",
            "inscripción", "inscripcion", "mensualidad"
        ]):
            return """Le ofrecemos una disculpa.
    
    No contamos con costos, colegiaturas ni información administrativa del Campus Metepec.
    
    Este canal corresponde únicamente al Colegio Valle de Filadelfia Campus Santa Cruz Atizapán.
    
    Le sugerimos consultar directamente los canales oficiales del Campus Metepec."""
    
        return """Entiendo, usted busca información del Campus Metepec.
    
    Este canal corresponde únicamente al Colegio Valle de Filadelfia Campus Santa Cruz Atizapán.
    
    No contamos con información operativa, costos, horarios ni teléfonos de otros campus, ya que cada campus se administra de forma independiente.
    
    Le sugerimos contactar directamente al Campus Metepec por sus canales oficiales."""
    
    return """Con gusto le apoyamos.

¿Podría indicarme un poco más sobre lo que le interesa conocer?"""

 

def actualizar_estado_segun_intencion(mensaje_usuario: str, respuesta_gemini: str, contact, db: Session):
    """
    Analiza la intención y actualiza el estado comercial del contacto.

    IMPORTANTE:
    contact.status = estado comercial del CRM
    contact.notes = estado conversacional del flujo, por ejemplo FLOW_STATE:SEGUIMIENTO_ACORDADO
    """

    mensaje_lower = (mensaje_usuario or "").lower().strip()
    respuesta_lower = (respuesta_gemini or "").lower().strip()
    flow_state = get_flow_state(contact)

    if detecta_campus_externo(mensaje_usuario) or flow_state == "CAMPUS_EXTERNO_NO_ATENDIBLE":
        if contact.status != "COMPETENCIA":
            contact.status = "COMPETENCIA"
            contact.is_competitor = True
            print("🎯 Estado comercial actualizado: COMPETENCIA (campus externo)")
        db.commit()
        return contact.status

    # =========================
    # 1. SEÑALES DE COMPETENCIA
    # =========================
    señales_competencia = [
        "otro colegio", "competencia", "comparar precios", "vs ",
        "versus", "más barato", "mas barato", "mejor precio",
        "diferencia con", "qué tal ", "que tal ",
        "me recomiendan", "estoy viendo", "otras opciones"
    ]

    es_competencia = any(señal in mensaje_lower for señal in señales_competencia)

    # =========================
    # 2. SEÑALES DE INTERÉS REAL
    # =========================
    señales_interes = [
        "inscribir", "matricular", "proceso", "requisitos",
        "documentos", "vacantes", "agendar visita", "quiero conocer",
        "cuándo empiezan", "cuando empiezan", "horarios de",
        "puedo visitar", "me interesa", "informes", "información",
        "informacion", "costos", "precio", "colegiatura",
        "colegiaturas", "inscripción", "inscripcion",
        "primaria", "preescolar", "secundaria"
    ]

    es_interes = any(señal in mensaje_lower for señal in señales_interes)

    # =========================
    # 3. SEÑALES DE QUE YA FUE INFORMADO
    # =========================
    señales_respuesta_informativa = [
        "método filadelfia", "metodo filadelfia",
        "colegiatura mensual",
        "aproximadamente de $",
        "becas",
        "descuentos",
        "planes de apoyo",
        "plataformas digitales",
        "knotion",
        "agendar una visita",
        "cita presencial"
    ]

    bot_ya_informo = any(señal in respuesta_lower for señal in señales_respuesta_informativa)

    # =========================
    # 4. SEÑALES DE PAUSA / SEGUIMIENTO
    # =========================
    señales_pausa = [
        "no por el momento",
        "por el momento no",
        "lo reviso",
        "lo checo",
        "lo consulto",
        "lo platico",
        "lo veo con mi esposo",
        "lo veo con mi esposa",
        "lo reviso con mi esposo",
        "lo reviso con mi esposa",
        "lo consulto con mi esposo",
        "lo consulto con mi esposa",
        "después les aviso",
        "despues les aviso",
        "luego les aviso",
        "yo les aviso",
        "más adelante",
        "mas adelante"
    ]

    prospecto_pausa = any(señal in mensaje_lower for señal in señales_pausa)

    # =========================
    # 5. SEÑALES DE CITA
    # =========================
    señales_cita = [
        "quiero agendar",
        "quiero visitar",
        "quiero conocer",
        "agendamos",
        "agendar cita",
        "agendar una cita",
        "visita",
        "cita"
    ]

    quiere_cita = any(señal in mensaje_lower for señal in señales_cita)

    # =========================
    # 6. ACTUALIZACIÓN DE ESTADO COMERCIAL
    # =========================

    # Competencia solo si no hay interés real claro
    if es_competencia and not es_interes:
        if contact.status != "COMPETENCIA":
            contact.status = "COMPETENCIA"
            contact.is_competitor = True
            print("🎯 Estado comercial actualizado: COMPETENCIA")
        db.commit()
        return contact.status

    # Si ya está en estados más avanzados, no degradar
    estados_no_degradar = [
        "VISITA_AGENDADA",
        "INSCRIPCION_PENDIENTE",
        "ALUMNO_ACTIVO",
        "ALUMNO_INACTIVO",
        "EX_ALUMNO"
    ]

    if contact.status in estados_no_degradar:
        db.commit()
        return contact.status

    # Si pide cita explícitamente, por ahora se marca como informado.
    # No usamos VISITA_AGENDADA hasta que exista confirmación interna real.
    if quiere_cita:
        if contact.status == "PROSPECTO_NUEVO":
            contact.status = "PROSPECTO_INFORMADO"
            print("🎯 Estado comercial actualizado: PROSPECTO_INFORMADO (interés en cita)")
        db.commit()
        return contact.status

    # Si ya recibió valor, precio, explicación o pausó después de recibir información,
    # ya no debe permanecer como prospecto nuevo.
    if es_interes or bot_ya_informo or prospecto_pausa or flow_state == "SEGUIMIENTO_ACORDADO":
        if contact.status == "PROSPECTO_NUEVO":
            contact.status = "PROSPECTO_INFORMADO"
            print("🎯 Estado comercial actualizado: PROSPECTO_INFORMADO")
        db.commit()
        return contact.status

    db.commit()
    return contact.status

def generar_respuesta_inteligente(mensaje: str, contact, history):
    """Función principal que decide qué motor de respuesta usar"""
    return generar_respuesta_gemini(mensaje, contact, history)

def validar_salida_comercial_prospecto(
    mensaje: str,
) -> Dict[str, Any]:
    """
    Valida la respuesta final destinada a un prospecto antes
    de enviarla por WhatsApp.

    Política comercial:
    - No se deben entregar montos de colegiaturas,
      inscripciones, descuentos, becas o planes de pago
      directamente por WhatsApp.
    - La conversación sí puede hablar conceptualmente de
      costos, becas, descuentos u opciones.
    - Números no económicos como horarios, fechas, grados,
      distancias o teléfonos no deben bloquearse.

    Esta función todavía no envía ni modifica mensajes.
    """

    texto = str(
        mensaje or ""
    ).strip()

    resultado = {
        "valida": True,
        "bloqueada": False,
        "motivos": [],
        "mensaje_original": texto,
        "mensaje_seguro": texto,
    }

    if not texto:
        resultado["valida"] = False
        resultado["bloqueada"] = True
        resultado["motivos"].append(
            "RESPUESTA_VACIA"
        )

        resultado["mensaje_seguro"] = (
            "Con gusto le orientamos. "
            "¿En qué podemos apoyarle?"
        )

        return resultado

    # ========================================================
    # 1. DETECCIÓN DE MONTOS ECONÓMICOS EXPLÍCITOS
    # ========================================================

    patrones_monto = [
        # $5,600 / $ 5,600.00 / $3400
        r"\$\s*\d[\d\s,\.]*",

        # 5,600 MXN / 3400 pesos / 5 mil pesos
        (
            r"\b\d[\d\s,\.]*\s*"
            r"(?:mxn|pesos?|peso\s+mexicano(?:s)?)\b"
        ),

        # 5 mil / 5 mil pesos
        (
            r"\b\d+(?:[\.,]\d+)?\s*mil"
            r"(?:\s+(?:mxn|pesos?))?\b"
        ),
    ]

    montos_detectados = []

    for patron in patrones_monto:
        coincidencias = re.findall(
            patron,
            texto,
            flags=re.IGNORECASE,
        )

        for coincidencia in coincidencias:
            coincidencia_limpia = str(
                coincidencia
            ).strip()

            if (
                coincidencia_limpia
                and coincidencia_limpia
                not in montos_detectados
            ):
                montos_detectados.append(
                    coincidencia_limpia
                )

    if montos_detectados:
        resultado["motivos"].append(
            "CONTIENE_MONTO_ECONOMICO"
        )

    # ========================================================
    # 2. DETECCIÓN DE PORCENTAJES ECONÓMICOS
    # ========================================================

    texto_normalizado = (
        normalizar_texto_geografico(
            texto
        )
    )

    contiene_porcentaje = bool(
        re.search(
            r"\b\d+(?:[\.,]\d+)?\s*%",
            texto,
            flags=re.IGNORECASE,
        )
    )

    contexto_economico = any(
        termino in texto_normalizado
        for termino in [
            "beca",
            "becas",
            "descuento",
            "descuentos",
            "promocion",
            "promociones",
            "colegiatura",
            "colegiaturas",
            "inscripcion",
            "mensualidad",
            "mensualidades",
            "pago",
            "pagos",
        ]
    )

    if (
        contiene_porcentaje
        and contexto_economico
    ):
        resultado["motivos"].append(
            "CONTIENE_PORCENTAJE_ECONOMICO"
        )

    # ========================================================
    # 3. DETECCIÓN DE CONDICIONES DE PAGO NO AUTORIZADAS
    # ========================================================

    patrones_condiciones_pago = [
        r"\bpagos?\s+semanales?\b",
        r"\bpagos?\s+quincenales?\b",
        r"\bmensualidades?\s+semanales?\b",
        r"\bplan(?:es)?\s+de\s+pago\s+semanal(?:es)?\b",
        r"\bplan(?:es)?\s+de\s+pago\s+quincenal(?:es)?\b",
    ]

    condicion_pago_detectada = any(
        re.search(
            patron,
            texto_normalizado,
            flags=re.IGNORECASE,
        )
        for patron in patrones_condiciones_pago
    )

    if condicion_pago_detectada:
        resultado["motivos"].append(
            "CONDICION_PAGO_NO_AUTORIZADA"
        )

    # ========================================================
    # 4. DECISIÓN FINAL
    # ========================================================

    if resultado["motivos"]:
        resultado["valida"] = False
        resultado["bloqueada"] = True

        resultado["mensaje_seguro"] = (
            "Con gusto le orientamos sobre colegiaturas "
            "y las opciones disponibles.\n\n"
            "Esta información la revisamos de manera "
            "personalizada, ya que puede variar según el "
            "nivel y las condiciones aplicables.\n\n"
            "Lo ideal es explicarle todos los detalles "
            "durante una visita al campus. "
            "Si gusta, puedo ayudarle a coordinarla."
        )

        print(
            "🛡️ Salida comercial bloqueada: "
            f"motivos={resultado['motivos']}"
        )

        if montos_detectados:
            print(
                "🛡️ Montos detectados en respuesta: "
                f"{montos_detectados}"
            )

        return resultado

    print(
        "✅ Salida comercial prospecto validada"
    )

    return resultado
    
def enviar_respuesta_twilio(to_number: str, mensaje: str) -> str:
    """Envía mensaje de vuelta via Twilio API usando API Key"""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    if not all([account_sid, api_key, api_secret, twilio_number]):
        return "❌ Faltan credenciales Twilio"
    
    try:
        client = Client(api_key, api_secret, account_sid)
        message = client.messages.create(
            body=mensaje,
            from_=twilio_number,
            to=to_number
        )
        return f"✅ Mensaje enviado. SID: {message.sid}"
    except Exception as e:
        return f"❌ Error Twilio: {str(e)}"

def enviar_template_alerta_admin_whatsapp(
    to_number: str,
) -> str:
    """
    Envía al administrador la plantilla WhatsApp aprobada
    para reabrir la ventana de conversación cuando no sea
    posible enviar texto libre.

    La plantilla no contiene información del prospecto.
    Su única función es avisar al administrador y permitirle
    pulsar el Quick Reply VER_MENSAJE.
    """

    account_sid = os.getenv(
        "TWILIO_ACCOUNT_SID"
    )

    api_key = os.getenv(
        "TWILIO_API_KEY"
    )

    api_secret = os.getenv(
        "TWILIO_API_SECRET"
    )

    twilio_number = os.getenv(
        "TWILIO_WHATSAPP_NUMBER"
    )

    template_sid = os.getenv(
        "ADMIN_WHATSAPP_TEMPLATE_SID"
    )

    if not all([
        account_sid,
        api_key,
        api_secret,
        twilio_number,
        template_sid,
    ]):
        return (
            "❌ Faltan credenciales Twilio o "
            "ADMIN_WHATSAPP_TEMPLATE_SID"
        )

    try:
        client = Client(
            api_key,
            api_secret,
            account_sid,
        )

        message = client.messages.create(
            from_=twilio_number,
            to=to_number,
            content_sid=template_sid,
        )

        return (
            "✅ Template admin enviado. "
            f"SID: {message.sid}"
        )

    except Exception as e:
        return (
            "❌ Error Twilio enviando template admin: "
            f"{str(e)}"
        )

def enviar_notificacion_admin_whatsapp(
    db: Session,
    to_number: str,
    mensaje_libre: str,
) -> str:
    """
    Envía una notificación al WhatsApp administrador.

    Estrategia:
    - Si el administrador escribió durante las últimas
      24 horas, envía el detalle completo como texto libre.
    - Si la ventana está cerrada o no puede determinarse,
      envía directamente la plantilla aprobada.
    """

    ventana_abierta = (
        admin_whatsapp_tiene_ventana_abierta(
            db,
            to_number,
        )
    )

    if ventana_abierta:
        print(
            "📣 Ventana admin abierta. "
            "Se enviará texto libre."
        )

        resultado_libre = (
            enviar_respuesta_twilio(
                to_number,
                mensaje_libre,
            )
        )

        print(
            "📣 Resultado texto libre admin: "
            f"{resultado_libre}"
        )

        return resultado_libre

    print(
        "📨 Ventana admin cerrada o desconocida. "
        "Se enviará template aprobado."
    )

    resultado_template = (
        enviar_template_alerta_admin_whatsapp(
            to_number
        )
    )

    print(
        "📨 Resultado template admin: "
        f"{resultado_template}"
    )

    return resultado_template
    
def procesar_escalacion_admin_estructurada(
    db: Session,
    contact,
    mensaje_usuario: str,
    respuesta_bot: str,
    resultado_orquestador: Dict[str, Any],
    memoria_historica: Optional[Dict[str, Any]] = None,
    ejecutar_envio: bool = False,
) -> Dict[str, Any]:
    """
    Prepara o ejecuta una escalación administrativa originada
    por el nuevo flujo estructurado.

    Cuando ejecutar_envio=False:
    - no crea tareas;
    - no envía WhatsApp;
    - no modifica el contacto;
    - solamente devuelve el diagnóstico.

    Cuando ejecutar_envio=True:
    - crea o reutiliza una tarea pendiente;
    - envía la alerta al WhatsApp administrador.

    Esta función no debe utilizarse desde endpoints de simulación
    con ejecutar_envio=True.
    """

    resultado = {
        "requiere_escalacion": False,
        "ejecutada": False,
        "tarea_id": None,
        "alerta_admin": "",
        "motivo": "",
        "accion": "",
        "fecha_cita": "",
        "hora_cita": "",
        "error": "",
    }

    if not isinstance(resultado_orquestador, dict):
        resultado["error"] = "RESULTADO_ORQUESTADOR_INVALIDO"
        return resultado

    decision = resultado_orquestador.get(
        "decision",
        {},
    )

    analisis = resultado_orquestador.get(
        "analisis",
        {},
    )

    if not isinstance(decision, dict):
        resultado["error"] = "DECISION_ESTRUCTURADA_INVALIDA"
        return resultado

    if not isinstance(analisis, dict):
        analisis = {}

    requiere_admin = bool(
        decision.get(
            "requiere_admin",
            False,
        )
    )

    if not isinstance(memoria_historica, dict):
        memoria_historica = {}

    if isinstance(
        memoria_historica.get("memoria"),
        dict,
    ):
        memoria_cita = memoria_historica.get(
            "memoria",
            {},
        )
    else:
        memoria_cita = memoria_historica

    accion = str(
        decision.get(
            "accion",
            "",
        )
        or ""
    ).strip().upper()

    motivo = str(
        decision.get(
            "motivo",
            "",
        )
        or ""
    ).strip()

    fecha_cita_analisis = str(
        analisis.get(
            "fecha_cita_iso",
            "",
        )
        or analisis.get(
            "fecha_cita_texto",
            "",
        )
        or ""
    ).strip()

    hora_cita_analisis = str(
        analisis.get(
            "hora_cita_24h",
            "",
        )
        or analisis.get(
            "hora_cita_texto",
            "",
        )
        or ""
    ).strip()

    fecha_cita_memoria = str(
        memoria_cita.get(
            "fecha_cita_iso",
            "",
        )
        or memoria_cita.get(
            "fecha_cita_texto",
            "",
        )
        or ""
    ).strip()

    hora_cita_memoria = str(
        memoria_cita.get(
            "hora_cita_24h",
            "",
        )
        or memoria_cita.get(
            "hora_cita_texto",
            "",
        )
        or ""
    ).strip()

    fecha_cita_contacto = ""
    hora_cita_contacto = ""

    if contact is not None:
        try:
            fecha_cita_contacto = str(
                get_note_value(
                    contact,
                    "FECHA_CITA",
                )
                or get_note_value(
                    contact,
                    "FECHA_CITA_TEXTO",
                )
                or ""
            ).strip()

            hora_cita_contacto = str(
                get_note_value(
                    contact,
                    "HORA_CITA",
                )
                or get_note_value(
                    contact,
                    "HORA_CITA_24H",
                )
                or get_note_value(
                    contact,
                    "HORA_CITA_TEXTO",
                )
                or ""
            ).strip()

        except Exception as e:
            print(
                "⚠️ No fue posible recuperar fecha/hora "
                f"de cita desde el contacto: {e}"
            )

    resultado.update({
        "requiere_escalacion": requiere_admin,
        "accion": accion,
        "motivo": motivo,
        "fecha_cita": (
            fecha_cita_analisis
            or fecha_cita_memoria
            or fecha_cita_contacto
        ),
        "hora_cita": (
            hora_cita_analisis
            or hora_cita_memoria
            or hora_cita_contacto
        ),
    })
    
    if not requiere_admin:
        return resultado

    if contact is None:
        resultado["error"] = "CONTACTO_NO_DISPONIBLE"
        return resultado

    if not ejecutar_envio:
        return resultado

    try:
        tarea_admin = crear_tarea_admin_pendiente(
            db=db,
            contact=contact,
            mensaje_usuario=mensaje_usuario,
            respuesta_bot=respuesta_bot,
        )

        resultado["tarea_id"] = getattr(
            tarea_admin,
            "id",
            None,
        )

        resultado_alerta = enviar_alerta_admin_whatsapp(
            db=db,
            contact=contact,
            mensaje_usuario=mensaje_usuario,
            respuesta_bot=respuesta_bot,
            tarea_id=resultado["tarea_id"],
        )

        resultado.update({
            "ejecutada": True,
            "alerta_admin": resultado_alerta,
            "error": "",
        })

        return resultado

    except Exception as e:
        db.rollback()

        print(
            "⚠️ Error procesando escalación "
            f"administrativa estructurada: {e}"
        )

        resultado["error"] = str(e)
        return resultado
        

def crear_tarea_admin_pendiente(db: Session, contact, mensaje_usuario: str, respuesta_bot: str):
    """
    Crea una tarea pendiente para que el administrador pueda responder
    y esa respuesta se envíe al prospecto.
    """
    tarea_existente = db.query(AdminPendingTask).filter(
        AdminPendingTask.contact_id == contact.id,
        AdminPendingTask.status == "PENDIENTE"
    ).order_by(AdminPendingTask.created_at.desc()).first()

    if tarea_existente:
        return tarea_existente

    tarea = AdminPendingTask(
        contact_id=contact.id,
        prospect_phone=contact.phone_number,
        status="PENDIENTE",
        trigger_message=mensaje_usuario,
        bot_response=respuesta_bot
    )

    db.add(tarea)
    db.commit()
    db.refresh(tarea)

    return tarea


def obtener_ultima_tarea_admin_pendiente(db: Session):
    """
    Obtiene la última tarea pendiente de atención humana.
    """
    return db.query(AdminPendingTask).filter(
        AdminPendingTask.status == "PENDIENTE"
    ).order_by(AdminPendingTask.created_at.desc()).first()

def obtener_tareas_admin_pendientes(db: Session):
    """
    Obtiene todas las tareas pendientes de atención humana.
    """
    return db.query(AdminPendingTask).filter(
        AdminPendingTask.status == "PENDIENTE"
    ).order_by(AdminPendingTask.created_at.asc()).all()

def construir_menu_tareas_pendientes(tareas):
    """
    Construye un menú de tareas pendientes para el administrador.
    """
    if not tareas:
        return "No hay conversaciones pendientes de confirmación en este momento."

    lineas = []

    total = len(tareas)

    if total == 1:
        lineas.append("Tienes 1 conversación pendiente de confirmación:\n")
    else:
        lineas.append(f"Tienes {total} conversaciones pendientes de confirmación:\n")

    for idx, tarea in enumerate(tareas, start=1):
        phone = tarea.prospect_phone or "Teléfono no disponible"
        ultimo_mensaje = (tarea.trigger_message or "").strip()
        if len(ultimo_mensaje) > 120:
            ultimo_mensaje = ultimo_mensaje[:120] + "..."

        lineas.append(f"{idx}) {phone}")
        lineas.append(f"Último mensaje: {ultimo_mensaje}")
        lineas.append("")

    lineas.append("Responde con el número de la conversación que deseas atender.")
    lineas.append("Ejemplo: 1")

    return "\n".join(lineas)
    

def enviar_alerta_admin_whatsapp(
    db: Session,
    contact,
    mensaje_usuario: str,
    respuesta_bot: str,
    tarea_id: int = None,
) -> str:
    """
    Envía al administrador únicamente la información
    operativa necesaria para atender una solicitud.
    """

    admin_number = os.getenv(
        "ADMIN_WHATSAPP_NUMBER"
    )

    if not admin_number:
        print(
            "⚠️ ADMIN_WHATSAPP_NUMBER no configurado; "
            "no se envió alerta interna"
        )
        return (
            "ADMIN_WHATSAPP_NUMBER no configurado"
        )

    phone = (
        contact.phone_number
        if contact
        else "Teléfono no disponible"
    )

    fecha_cita = ""
    hora_cita = ""

    if contact is not None:
        try:
            fecha_cita_raw = str(
                get_note_value(
                    contact,
                    "FECHA_CITA",
                )
                or get_note_value(
                    contact,
                    "FECHA_CITA_ISO",
                )
                or ""
            ).strip()

            hora_cita = str(
                get_note_value(
                    contact,
                    "HORA_CITA",
                )
                or get_note_value(
                    contact,
                    "HORA_CITA_24H",
                )
                or ""
            ).strip()

            fecha_cita = (
                formatear_fecha_cita_calendario(
                    fecha_cita_raw
                )
                or fecha_cita_raw
            )

        except Exception as e:
            print(
                "⚠️ No fue posible construir "
                "el resumen de cita para admin: "
                f"{e}"
            )

    if fecha_cita and hora_cita:
        mensaje_alerta = (
            "🔔 Confirmación de cita pendiente\n\n"
            f"Prospecto: {phone}\n\n"
            "Desea visitar el colegio:\n"
            f"{fecha_cita}, {hora_cita}\n\n"
            "¿Confirmamos esta cita?"
        )

    else:
        mensaje_alerta = (
            "🔔 Atención requerida\n\n"
            f"Prospecto: {phone}\n\n"
            "Solicitud:\n"
            f"{mensaje_usuario}\n\n"
            "¿Qué deseas que le responda?"
        )

    resultado = (
        enviar_notificacion_admin_whatsapp(
            db,
            admin_number,
            mensaje_alerta,
        )
    )

    print(
        f"📣 Alerta interna enviada a: "
        f"{admin_number}"
    )
    print(
        f"📣 Resultado alerta interna: "
        f"{resultado}"
    )

    return resultado

def limpiar_instruccion_admin(texto_admin: str) -> str:
    """
    Limpia frases internas del administrador para usarlas como respaldo seguro
    si la IA genera una respuesta incompleta.
    """
    texto = (texto_admin or "").strip()

    prefijos = [
        "dile que ",
        "diles que ",
        "contéstale que ",
        "contestale que ",
        "confírmale que ",
        "confirmale que ",
        "respóndele que ",
        "respondele que ",
        "avísale que ",
        "avisale que ",
        "dile ",
        "diles ",
        "contéstale ",
        "contestale ",
        "confírmale ",
        "confirmale "
    ]

    texto_lower = texto.lower()

    for prefijo in prefijos:
        if texto_lower.startswith(prefijo):
            texto = texto[len(prefijo):].strip()
            break

    if texto:
        texto = texto[0].upper() + texto[1:]

    return texto


def respuesta_admin_parece_incompleta(texto: str) -> bool:
    """
    Detecta si la respuesta generada por IA parece vacía, cortada o incompleta.
    No evalúa estilo; sólo evita enviar mensajes técnicamente incompletos.
    """
    respuesta = (texto or "").strip()

    if not respuesta:
        return True

    if len(respuesta) < 25:
        return True

    respuesta_lower = respuesta.lower().strip()

    finales_incompletos = [
        " se encontrará",
        " se encontrara",
        " se encuentra",
        " se encontrarán",
        " se encontraran",
        " estará",
        " estara",
        " estarán",
        " estaran",
        " podremos",
        " podemos",
        " podría",
        " podria",
        " podrían",
        " podrian",
        " quedaría",
        " quedaria",
        " sería",
        " seria",
        " a las",
        " el día",
        " el dia",
        " para",
        " con",
        " que",
        " de",
        " en"
    ]

    if any(respuesta_lower.endswith(final) for final in finales_incompletos):
        return True

    if respuesta_lower.endswith((",", ":", ";")):
        return True
    
    if respuesta_lower.endswith(("a.m", "p.m")):
        return True

    return False

def normalizar_texto_para_deteccion(texto: str) -> str:
    """
    Normaliza texto para detectar intención sin depender de acentos,
    mayúsculas o puntuación.
    """
    msg = (texto or "").lower().strip()

    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u"
    }

    for origen, destino in reemplazos.items():
        msg = msg.replace(origen, destino)

    for caracter in [",", ".", "!", "¡", "?", "¿", ":", ";"]:
        msg = msg.replace(caracter, " ")

    return " ".join(msg.split())
    

def clasificar_respuesta_admin_cita_con_ia(texto_admin: str, tarea: AdminPendingTask = None) -> str:
    """
    Usa IA para interpretar la intención real del administrador respecto a una cita.
    Devuelve:
    - CONFIRMA_CITA
    - PROPONE_ALTERNATIVA
    - RECHAZA_DISPONIBILIDAD
    - AMBIGUO
    """
    texto_admin = (texto_admin or "").strip()

    if not texto_admin:
        return "AMBIGUO"

    if not GEMINI_API_KEY:
        return "AMBIGUO"

    mensaje_prospecto = ""
    respuesta_bot = ""

    if tarea:
        mensaje_prospecto = tarea.trigger_message or ""
        respuesta_bot = tarea.bot_response or ""

    prompt = f"""
Eres un clasificador estricto para un flujo de citas escolares por WhatsApp.

CONTEXTO:
El prospecto pidió una cita o propuso un horario.
El bot consultó al administrador la disponibilidad.
Ahora el administrador respondió con una instrucción interna.

MENSAJE ORIGINAL DEL PROSPECTO:
{mensaje_prospecto}

RESPUESTA DEL BOT AL PROSPECTO ANTES DE CONSULTAR:
{respuesta_bot}

RESPUESTA INTERNA DEL ADMINISTRADOR:
{texto_admin}

TAREA:
Clasifica la respuesta interna del administrador en UNA sola etiqueta.

ETIQUETAS VÁLIDAS:

CONFIRMA_CITA
Usa esta etiqueta cuando el administrador acepta o confirma que sí se puede recibir al prospecto en el día y horario mencionado, aunque no use la palabra "confirmar".
Ejemplos:
- sí, está bien
- sí, el día de mañana está bien a las 3pm
- claro, mañana a las 3
- sí podemos recibirla
- adelante
- perfecto
- ok, está bien
- sí hay disponibilidad
- le puedes confirmar

PROPONE_ALTERNATIVA
Usa esta etiqueta cuando el administrador NO confirma el horario solicitado, pero propone otro día, otra hora o una opción tentativa.
Ejemplos:
- hoy no, podría ser mañana
- mejor el viernes
- puede ser la siguiente semana
- a esa hora no, pero a las 3:30 sí
- podríamos recibirla mañana

RECHAZA_DISPONIBILIDAD
Usa esta etiqueta cuando el administrador dice que no se puede atender y no da una alternativa clara.
Ejemplos:
- hoy no podemos
- no hay disponibilidad
- no se puede
- no nos da tiempo

AMBIGUO
Usa esta etiqueta si no queda claro si está confirmando, proponiendo alternativa o rechazando.

REGLAS:
- Responde únicamente con una etiqueta.
- No expliques nada.
- No agregues puntuación.
- Si el administrador dice "sí" y menciona día/hora, normalmente es CONFIRMA_CITA.
- Si usa "podría", "podríamos", "tal vez", "mejor", normalmente es PROPONE_ALTERNATIVA, salvo que también confirme explícitamente.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.0
            ),
            tarea="clasificación respuesta admin cita"
        )

        etiqueta = extraer_texto_respuesta_gemini(response).strip().upper()

        etiquetas_validas = [
            "CONFIRMA_CITA",
            "PROPONE_ALTERNATIVA",
            "RECHAZA_DISPONIBILIDAD",
            "AMBIGUO"
        ]

        if etiqueta in etiquetas_validas:
            print(f"👑 Clasificación admin cita IA: {etiqueta} usando {modelo_usado}")
            return etiqueta

        print(f"⚠️ Clasificación admin cita no válida: {repr(etiqueta)}")
        return "AMBIGUO"

    except Exception as e:
        print(f"⚠️ Error clasificando respuesta admin cita con IA: {e}")
        return "AMBIGUO"
        

def admin_confirma_cita_final(texto_admin: str, tarea: AdminPendingTask = None) -> bool:
    """
    Detecta si la respuesta del admin confirma definitivamente la cita.
    Primero usa IA. Si la IA falla, usa un fallback mínimo por seguridad.
    """
    clasificacion = clasificar_respuesta_admin_cita_con_ia(texto_admin, tarea)

    if clasificacion == "CONFIRMA_CITA":
        return True

    if clasificacion in ["PROPONE_ALTERNATIVA", "RECHAZA_DISPONIBILIDAD"]:
        return False

    # Fallback mínimo, sólo por si Gemini falla.
    msg = normalizar_texto_para_deteccion(texto_admin)

    if msg in ["si", "ok", "confirmado", "confirmada", "adelante"]:
        return True

    if "confirm" in msg and not any(x in msg for x in ["no confirm", "sin confirm"]):
        return True

    return False

def clasificar_resolucion_admin_zona(
    texto_admin: str,
    tarea=None,
) -> str:
    """
    Clasifica una resolución administrativa relacionada
    específicamente con la viabilidad de una zona.

    Devuelve:
    - APRUEBA_ZONA
    - RECHAZA_ZONA
    - NO_DETERMINADO
    """

    texto = str(
        texto_admin or ""
    ).strip()

    if not texto:
        return "NO_DETERMINADO"

    contexto_tarea = ""

    if tarea is not None:
        contexto_tarea = str(
            getattr(
                tarea,
                "trigger_message",
                "",
            )
            or ""
        ).strip()

    if GEMINI_API_KEY:

        prompt = f"""
Eres un clasificador estricto para decisiones internas
de admisiones de un colegio.

La consulta administrativa se refiere a determinar si
una localidad o zona puede ser atendida por el colegio.

MENSAJE DEL PROSPECTO O CONTEXTO DE LA CONSULTA:
{contexto_tarea}

RESPUESTA INTERNA DEL ADMINISTRADOR:
{texto}

Clasifica la respuesta del administrador en UNA sola etiqueta.

ETIQUETAS VÁLIDAS:

APRUEBA_ZONA
El administrador confirma que sí se puede atender a la familia
desde esa localidad, autoriza continuar, dice que la zona es
viable o proporciona una respuesta positiva equivalente.

Ejemplos:
- sí, esa zona está bien
- sí podemos atenderlos
- adelante
- Atlapulco sí es una zona que les permite llegar
- tenemos alumnos de esa localidad
- puedes continuar con la familia

RECHAZA_ZONA
El administrador indica expresamente que la zona no debe
atenderse o que no es viable continuar desde esa localidad.

Ejemplos:
- esa zona no la atendemos
- no es viable
- está demasiado lejos, no continuar
- no podemos atenderlos desde ahí

NO_DETERMINADO
La respuesta no permite concluir claramente si la localidad
fue autorizada o rechazada.

REGLAS:
- Responde únicamente con una etiqueta.
- No expliques nada.
- No agregues puntuación.
"""

        try:
            response, modelo_usado = (
                generar_con_gemini_con_fallback(
                    prompt,
                    generation_config=(
                        genai.types.GenerationConfig(
                            temperature=0.0
                        )
                    ),
                    tarea=(
                        "clasificación resolución admin zona"
                    ),
                )
            )

            etiqueta = (
                extraer_texto_respuesta_gemini(
                    response
                )
                .strip()
                .upper()
            )

            if etiqueta in {
                "APRUEBA_ZONA",
                "RECHAZA_ZONA",
                "NO_DETERMINADO",
            }:
                print(
                    "👑 Clasificación admin zona IA: "
                    f"{etiqueta} usando {modelo_usado}"
                )
                return etiqueta

            print(
                "⚠️ Clasificación admin zona no válida: "
                f"{repr(etiqueta)}"
            )

        except Exception as e:
            print(
                "⚠️ Error clasificando resolución "
                f"administrativa de zona: {e}"
            )

    # --------------------------------------------------------
    # FALLBACK CONSERVADOR
    # --------------------------------------------------------

    normalizado = (
        normalizar_texto_para_deteccion(
            texto
        )
    )

    expresiones_rechazo = [
        "no se puede",
        "no podemos atender",
        "no atender",
        "zona no autorizada",
        "zona no viable",
        "no es viable",
    ]

    expresiones_aprobacion = [
        "si puede",
        "si se puede",
        "puedes continuar",
        "puede continuar",
        "zona autorizada",
        "zona aprobada",
        "si atendemos",
        "si podemos atender",
        "tenemos alumnos",
        "si es una zona",
    ]

    if any(
        expresion in normalizado
        for expresion in expresiones_rechazo
    ):
        return "RECHAZA_ZONA"

    if any(
        expresion in normalizado
        for expresion in expresiones_aprobacion
    ):
        return "APRUEBA_ZONA"

    return "NO_DETERMINADO"
    

def formatear_fecha_larga_es(fecha):
    """
    Convierte una fecha a formato: 20 de julio
    """
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
    ]

    return f"{fecha.day} de {meses[fecha.month - 1]}"


def calcular_fecha_proximo_dia(dia_nombre: str):
    """
    Calcula la fecha del próximo día de la semana mencionado.
    Ejemplo: si hoy es jueves y dice lunes, devuelve el lunes siguiente.
    """
    dias = {
        "lunes": 0,
        "martes": 1,
        "miércoles": 2,
        "miercoles": 2,
        "jueves": 3,
        "viernes": 4,
        "sábado": 5,
        "sabado": 5,
        "domingo": 6
    }

    dia_nombre = (dia_nombre or "").lower().strip()

    if dia_nombre not in dias:
        return None

    hoy = datetime.now(LOCAL_TZ).date()
    objetivo = dias[dia_nombre]

    dias_a_sumar = (objetivo - hoy.weekday()) % 7

    # Si dice el mismo día, asumimos hoy.
    # Ejemplo: si hoy es lunes y agenda "lunes a las 8", se entiende hoy lunes.
    fecha_objetivo = hoy + timedelta(days=dias_a_sumar)

    return fecha_objetivo


def enriquecer_fecha_cita_en_mensaje(mensaje: str) -> str:
    """
    Agrega la fecha exacta cuando el mensaje contiene frases como:
    - este lunes a las 8:00 a.m.
    - el lunes a las 8:00 a.m.
    - lunes a las 8:00 a.m.

    Resultado:
    - este lunes 20 de julio a las 8:00 a.m.
    """
    texto = mensaje or ""

    dias_regex = r"(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)"

    patrones = [
        rf"\b(este|esta)\s+{dias_regex}\s+a\s+las\b",
        rf"\b(el|la)\s+{dias_regex}\s+a\s+las\b",
        rf"\b{dias_regex}\s+a\s+las\b"
    ]

    for patron in patrones:
        matches = list(re.finditer(patron, texto, flags=re.IGNORECASE))

        for match in reversed(matches):
            grupos = match.groups()

            if len(grupos) >= 2 and grupos[0].lower() in ["este", "esta", "el", "la"]:
                articulo = grupos[0]
                dia = grupos[1]
                inicio_dia = match.start(1)
                fin_dia = match.end(1)
            else:
                articulo = ""
                dia = grupos[0]
                inicio_dia = match.start(1)
                fin_dia = match.end(1)

            fecha = calcular_fecha_proximo_dia(dia)

            if not fecha:
                continue

            fecha_texto = formatear_fecha_larga_es(fecha)

            # Evita duplicar fecha si ya dice algo como "lunes 20 de julio"
            texto_despues_dia = texto[fin_dia:fin_dia + 20].lower()
            if " de " in texto_despues_dia:
                continue

            texto = texto[:fin_dia] + f" {fecha_texto}" + texto[fin_dia:]

    return texto

def construir_solicitud_datos_cita(
    contact,
) -> str:
    """
    Construye una sola solicitud con únicamente los datos
    faltantes después de que administración confirma la cita.

    Soporta uno o varios alumnos asociados a la misma visita.
    """

    nombre_tutor = str(
        (
            get_note_value(
                contact,
                "NOMBRE_PADRES",
            )
            or get_note_value(
                contact,
                "NOMBRE_TUTOR",
            )
            or ""
        )
    ).strip()

    alumnos_cita = (
        obtener_alumnos_cita_persistidos(
            contact
        )
    )

    faltantes = []

    if not nombre_tutor:
        faltantes.append(
            "su nombre completo"
        )

    if not alumnos_cita:

        faltantes.append(
            "el nombre completo de su hijo(a)"
        )

        nivel_interes = str(
            get_note_value(
                contact,
                "NIVEL_INTERES",
            )
            or ""
        ).strip()

        grado_interes = str(
            (
                get_note_value(
                    contact,
                    "GRADO_INTERES",
                )
                or get_note_value(
                    contact,
                    "GRADO_SOLICITADO",
                )
                or ""
            )
        ).strip()

        if not nivel_interes:
            faltantes.append(
                "el nivel educativo de interés"
            )

        if (
            nivel_interes
            and not grado_interes
        ):
            faltantes.append(
                "el grado específico al que ingresaría"
            )

    else:

        total_alumnos = len(
            alumnos_cita
        )

        for indice, alumno in enumerate(
            alumnos_cita,
            start=1,
        ):

            nombre = str(
                alumno.get(
                    "nombre",
                    "",
                )
                or ""
            ).strip()

            nivel = str(
                alumno.get(
                    "nivel",
                    "",
                )
                or ""
            ).strip()

            grado = str(
                alumno.get(
                    "grado",
                    "",
                )
                or ""
            ).strip()

            if total_alumnos == 1:

                if not nombre:
                    faltantes.append(
                        "el nombre completo de su hijo(a)"
                    )

                if not nivel:
                    faltantes.append(
                        "el nivel educativo de interés"
                    )

                if (
                    nivel
                    and not grado
                ):
                    faltantes.append(
                        "el grado específico al que ingresaría"
                    )

            else:

                referencia_alumno = (
                    f"del alumno {indice}"
                )

                if not nombre:
                    faltantes.append(
                        "el nombre completo "
                        f"{referencia_alumno}"
                    )

                if not nivel:
                    faltantes.append(
                        "el nivel educativo "
                        f"{referencia_alumno}"
                    )

                if (
                    nivel
                    and not grado
                ):
                    faltantes.append(
                        "el grado específico "
                        f"{referencia_alumno}"
                    )

    if not faltantes:
        return ""

    etiquetas = []

    for faltante in faltantes:

        faltante_normalizado = str(
            faltante or ""
        ).strip().lower()

        if faltante_normalizado == "su nombre completo":
            etiqueta = "Nombre de usted"

        elif (
            "nombre completo de su hijo"
            in faltante_normalizado
        ):
            etiqueta = "Nombre completo de su hijo(a)"

        elif (
            "grado específico al que ingresaría"
            in faltante_normalizado
        ):
            etiqueta = "Grado al que ingresaría"

        elif (
            "nivel educativo de interés"
            in faltante_normalizado
        ):
            etiqueta = "Nivel educativo de interés"

        else:
            etiqueta = faltante.strip().capitalize()

        if etiqueta not in etiquetas:
            etiquetas.append(
                etiqueta
            )

    lineas = "\n".join(
        f"- {etiqueta}"
        for etiqueta in etiquetas
    )

    return (
        "Para completar su registro de cita, "
        "por favor ayúdenos con lo siguiente:\n\n"
        f"{lineas}"
    )
    

def extraer_hora_cita_confirmada(
    mensaje_confirmacion: str,
    respaldo: str = "",
) -> str:
    """
    Extrae una frase breve y completa con el día y la hora
    confirmados para una cita.

    Prioridad:
    1. Mensaje final enviado al prospecto.
    2. Mensaje original del prospecto.
    3. Gemini como último respaldo.

    Nunca acepta horarios incompletos como "11:".
    """

    texto = str(
        mensaje_confirmacion or ""
    ).strip()

    respaldo_limpio = str(
        respaldo or ""
    ).strip()

    def extraer_fecha_hora_determinista(
        contenido: str,
    ) -> str:
        contenido = str(
            contenido or ""
        ).strip()

        if not contenido:
            return ""

        patrones = [
            # mañana a las 11:30 am
            (
                r"\b("
                r"hoy|mañana|pasado\s+mañana|"
                r"este\s+lunes|este\s+martes|"
                r"este\s+miércoles|este\s+miercoles|"
                r"este\s+jueves|este\s+viernes|"
                r"lunes|martes|miércoles|miercoles|"
                r"jueves|viernes"
                r")"
                r"(?:\s+\d{1,2}\s+de\s+[a-záéíóúñ]+)?"
                r"\s+(?:a\s+las?|a\s+la)\s+"
                r"(\d{1,2}:\d{2})"
                r"\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)?\b"
            ),

            # mañana a las 11 am
            (
                r"\b("
                r"hoy|mañana|pasado\s+mañana|"
                r"este\s+lunes|este\s+martes|"
                r"este\s+miércoles|este\s+miercoles|"
                r"este\s+jueves|este\s+viernes|"
                r"lunes|martes|miércoles|miercoles|"
                r"jueves|viernes"
                r")"
                r"(?:\s+\d{1,2}\s+de\s+[a-záéíóúñ]+)?"
                r"\s+(?:a\s+las?|a\s+la)\s+"
                r"(\d{1,2})"
                r"\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)\b"
            ),
        ]

        for patron in patrones:
            coincidencia = re.search(
                patron,
                contenido,
                flags=re.IGNORECASE,
            )

            if not coincidencia:
                continue

            hora = str(
                coincidencia.group(2)
                or ""
            ).strip()

            periodo = str(
                coincidencia.group(3)
                or ""
            ).strip()

            if not hora:
                continue

            if hora.endswith(":"):
                continue

            hora_completa = (
                f"{hora} {periodo}".strip()
            )

            return hora_completa

        return ""

    # ========================================================
    # 1. INTENTO DETERMINISTA CON EL MENSAJE CONFIRMADO
    # ========================================================

    resultado = extraer_fecha_hora_determinista(
        texto
    )

    if resultado:
        print(
            "✅ Hora de cita extraída del mensaje confirmado: "
            f"{resultado!r}"
        )
        return resultado

    # ========================================================
    # 2. INTENTO DETERMINISTA CON EL MENSAJE ORIGINAL
    # ========================================================

    resultado = extraer_fecha_hora_determinista(
        respaldo_limpio
    )

    if resultado:
        print(
            "✅ Hora de cita recuperada del mensaje original: "
            f"{resultado!r}"
        )
        return resultado

    # ========================================================
    # 3. GEMINI COMO ÚLTIMO RESPALDO
    # ========================================================

    if not GEMINI_API_KEY:
        return respaldo_limpio or texto[:120]

    prompt = f"""
Extrae únicamente el día y la hora completos de la cita.

MENSAJE CONFIRMADO:
{texto}

MENSAJE ORIGINAL DEL PROSPECTO:
{respaldo_limpio}

REGLAS:
- Responde únicamente con una frase breve.
- Ejemplo: mañana a las 11:30 am
- La hora debe estar completa.
- Nunca devuelvas una hora terminada en dos puntos.
- Si el mensaje confirmado está incompleto, usa el mensaje original.
- No agregues explicaciones.
"""

    try:
        response, modelo_usado = (
            generar_con_gemini_con_fallback(
                prompt,
                generation_config=(
                    genai.types.GenerationConfig(
                        temperature=0.0,
                    )
                ),
                tarea="extracción hora cita",
            )
        )

        respuesta_ia = (
            extraer_texto_respuesta_gemini(
                response
            ).strip()
        )

        resultado_ia = (
            extraer_fecha_hora_determinista(
                respuesta_ia
            )
        )

        if resultado_ia:
            print(
                "✅ Hora de cita extraída con Gemini: "
                f"{resultado_ia!r}, "
                f"modelo={modelo_usado}"
            )
            return resultado_ia

        print(
            "⚠️ Gemini devolvió una hora incompleta "
            f"o inválida: {respuesta_ia!r}"
        )

        return ""

    except Exception as e:
        print(
            "⚠️ Error extrayendo hora de cita: "
            f"{e}"
        )

        return ""

def extraer_datos_registro_cita(
    mensaje_usuario: str,
    contact,
    ultimo_mensaje_asistente: str = "",
) -> dict:
    """
    Extrae los datos necesarios para completar una cita confirmada.

    Soporta:
    - nombre del padre, madre o tutor;
    - uno o varios alumnos;
    - nivel y grado cuando aparecen en el mensaje.

    Mantiene compatibilidad con el flujo anterior mediante
    la clave singular "alumno", que representa al primer alumno
    detectado.
    """

    texto = str(
        mensaje_usuario or ""
    ).strip()

    nivel_conocido = str(
        get_note_value(
            contact,
            "NIVEL_INTERES",
        )
        or ""
    ).strip()

    grado_conocido = str(
        get_note_value(
            contact,
            "GRADO_INTERES",
        )
        or get_note_value(
            contact,
            "GRADO_SOLICITADO",
        )
        or ""
    ).strip()

    # ========================================================
    # ALUMNOS YA ASOCIADOS A LA CITA
    # ========================================================
    #
    # Si la cita ya contiene uno o varios alumnos, esta
    # estructura es la referencia principal para asociar los
    # nuevos nombres sin perder nivel, grado ni orden.
    # ========================================================

    alumnos_cita_previos = (
        obtener_alumnos_cita_persistidos(
            contact
        )
    )

    if not isinstance(
        alumnos_cita_previos,
        list,
    ):
        alumnos_cita_previos = []

    alumnos_cita_previos_json = json.dumps(
        alumnos_cita_previos,
        ensure_ascii=False,
        indent=2,
    )

    datos = {
        "padres": "",
        "alumno": "",
        "alumnos": [],
        "nivel": nivel_conocido,
        "grado": grado_conocido,
    }

    if not texto:
        return datos

    # ========================================================
    # LIMPIEZA DE NOMBRES
    # ========================================================

    def limpiar_nombre_extraido(
        valor: str,
    ) -> str:

        nombre = str(
            valor or ""
        ).strip()

        nombre = re.sub(
            r"\s+",
            " ",
            nombre,
        ).strip()

        nombre = nombre.strip(
            " ,.;:-"
        )

        return nombre

    # ========================================================
    # 1. EXTRACCIÓN DETERMINISTA DEL TUTOR
    # ========================================================

    patrones_tutor = [
        (
            r"(?:yo\s+)?"
            r"(?:me\s+llamo|mi\s+nombre\s+es|soy)"
            r"\s+(.+?)"
            r"(?=\s+(?:y|,)\s+"
            r"(?:el\s+de\s+mi|los\s+de\s+mis|mi|mis)\s+"
            r"(?:hijo|hija|hijos|hijas|alumno|alumna|"
            r"alumnos|alumnas)\b|$)"
        ),
        (
            r"(?:nombre\s+del\s+padre|"
            r"nombre\s+de\s+la\s+madre|"
            r"nombre\s+del\s+tutor)"
            r"\s*(?:es|:)\s*(.+?)"
            r"(?=\s+(?:y|,)\s+|$)"
        ),
        (
            r"(?:el\s+m[ií]o\s+es)\s+(.+?)"
            r"(?=\s+y\s+el\s+de\s+mi\s+"
            r"(?:hijo|hija)\b|$)"
        ),
    ]

    for patron in patrones_tutor:

        coincidencia = re.search(
            patron,
            texto,
            flags=re.IGNORECASE,
        )

        if coincidencia:

            datos["padres"] = (
                limpiar_nombre_extraido(
                    coincidencia.group(1)
                )
            )

            break

    # ========================================================
    # 2. EXTRACCIÓN DETERMINISTA DE UN ALUMNO
    # ========================================================
    #
    # Se conserva porque resuelve de forma muy confiable
    # conversaciones simples sin depender de Gemini.
    # Para múltiples alumnos, Gemini complementará después.
    # ========================================================

    patrones_alumno = [
        (
            r"(?:mi\s+)?"
            r"(?:hijo|hija|alumno|alumna)"
            r"\s+(?:es|se\s+llama)\s+(.+?)$"
        ),
        (
            r"el\s+de\s+mi\s+"
            r"(?:hijo|hija|alumno|alumna)"
            r"\s+(?:es|se\s+llama)\s+(.+?)$"
        ),
        (
            r"nombre\s+(?:del|de\s+la)\s+"
            r"(?:hijo|hija|alumno|alumna)"
            r"\s*(?:es|:)\s*(.+?)$"
        ),
    ]

    for patron in patrones_alumno:

        coincidencia = re.search(
            patron,
            texto,
            flags=re.IGNORECASE,
        )

        if coincidencia:

            nombre_alumno = (
                limpiar_nombre_extraido(
                    coincidencia.group(1)
                )
            )

            if nombre_alumno:

                datos["alumno"] = nombre_alumno

                datos["alumnos"].append({
                    "nombre": nombre_alumno,
                    "nivel": nivel_conocido,
                    "grado": grado_conocido,
                })

            break

    # ========================================================
    # 3. GEMINI PARA INTERPRETAR UNO O VARIOS ALUMNOS
    # ========================================================

    if not GEMINI_API_KEY:
        return datos

    prompt = f"""
Extrae los datos necesarios para completar el registro de una
cita escolar ya confirmada.

IMPORTANTE:
El mensaje actual forma parte de una conversación existente.
Nunca lo interpretes de manera aislada.

ÚLTIMA SOLICITUD DEL ASISTENTE AL PROSPECTO:

{ultimo_mensaje_asistente or "No disponible"}

MENSAJE ACTUAL DEL PROSPECTO:

{texto}

DATOS YA EXTRAÍDOS:

Tutor:
{datos["padres"] or "No identificado"}

Alumno detectado inicialmente:
{datos["alumno"] or "No identificado"}

NIVEL YA CONOCIDO EN EL CONTACTO:
{nivel_conocido or "No especificado"}

GRADO YA CONOCIDO EN EL CONTACTO:
{grado_conocido or "No especificado"}

ALUMNOS YA ASOCIADOS A ESTA CITA:

{alumnos_cita_previos_json if alumnos_cita_previos else "No hay alumnos estructurados previamente."}

IMPORTANTE:

La lista anterior representa los alumnos que ya fueron identificados
durante la conversación antes de confirmar la cita.

Cuando exista esa lista:

- Consérvala como estructura base.
- Conserva el mismo número de alumnos.
- Conserva el mismo orden.
- Conserva cualquier nivel o grado que ya exista.
- Completa únicamente la información nueva que aporte el prospecto.
- No elimines un alumno porque el mensaje actual no repita su nivel.
- No cambies de lugar los alumnos.
- No asignes un nombre a un alumno si la asociación no puede
  determinarse razonablemente.
- Si el prospecto identifica claramente a qué alumno corresponde
  cada nombre, completa el nombre en ese elemento.
- Si responde los nombres en el mismo orden en que fueron
  solicitados, conserva ese mismo orden.
- No mezcles dos alumnos en un mismo elemento.

TAREA:

Devuelve únicamente un objeto JSON válido con esta estructura:

{{
  "padres": "",
  "alumnos": [
    {{
      "nombre": "",
      "nivel": "",
      "grado": ""
    }}
  ]
}}

REGLAS OBLIGATORIAS:

- Interpreta el MENSAJE ACTUAL como respuesta a la ÚLTIMA
  SOLICITUD DEL ASISTENTE.

- Si el prospecto responde de forma breve, con uno o varios
  nombres, utiliza la pregunta previa y los datos que todavía
  faltan para comprender razonablemente a quién corresponde
  cada nombre.

- Si el asistente solicitó varios datos en un orden determinado
  y el prospecto responde varios valores claramente en ese mismo
  orden, conserva esa correspondencia cuando sea razonable.

- No exijas que el prospecto escriba frases como "yo me llamo",
  "mi hijo se llama", "nombre del tutor" o expresiones similares
  si el contexto conversacional permite comprender la respuesta.

- Al mismo tiempo, no inventes una asociación cuando realmente
  sea ambigua. Si no es posible determinar razonablemente a
  quién corresponde un nombre, deja ese campo vacío.

- La información ya persistida tiene prioridad. Completa lo
  faltante; no reinicies ni reconstruyas el registro desde cero.

- "padres" debe contener el nombre completo de la mamá,
  papá o tutor que agenda.

- "alumnos" debe contener TODOS los niños o jóvenes
  mencionados en el mensaje.

- Si aparece un solo alumno, devuelve una lista con
  un solo elemento.

- Si aparecen dos o más hijos, hijas, alumnos o alumnas,
  crea un elemento independiente para cada uno.

- No combines dos nombres en un solo campo.

- "nombre" debe contener únicamente el nombre del alumno.

- "nivel" debe representar únicamente:
  Kínder, Primaria o Secundaria.

- "grado" debe representar exclusivamente el grado específico,
  por ejemplo:
  1ro, 2do, 3ro, primero, segundo, tercero.

- Nunca utilices Primaria, Secundaria, Kínder o Preescolar
  como valor de "grado".

- Si el mensaje no indica nivel o grado para un alumno,
  déjalo vacío.

- Si existe un único alumno y el nivel o grado ya conocido
  claramente corresponde a ese alumno, puedes conservarlo.

- Si existen varios alumnos y no está claro qué nivel o grado
  corresponde a cada uno, NO repartas ni inventes información.

- No inventes nombres.

- No inventes parentescos.

- No inventes niveles.

- No inventes grados.

- Conserva los datos ya identificados cuando sean correctos.

- Devuelve exclusivamente JSON válido.

- No uses Markdown.

- No agregues explicaciones.
"""

    try:

        response, modelo_usado = (
            generar_con_gemini_con_fallback(
                prompt,
                generation_config=(
                    genai.types.GenerationConfig(
                        temperature=0.0,
                    )
                ),
                tarea="extracción datos cita",
            )
        )

        texto_respuesta = (
            extraer_texto_respuesta_gemini(
                response
            ).strip()
        )

        datos_ia = extraer_json_de_texto(
            texto_respuesta
        )

        if not isinstance(
            datos_ia,
            dict,
        ):
            print(
                "⚠️ Gemini no devolvió un JSON válido "
                "para los datos de cita."
            )

            return datos

        # ----------------------------------------------------
        # TUTOR
        # ----------------------------------------------------

        padres_ia = limpiar_nombre_extraido(
            datos_ia.get(
                "padres",
                "",
            )
        )

        if (
            not datos["padres"]
            and padres_ia
        ):
            datos["padres"] = padres_ia

        # ----------------------------------------------------
        # ALUMNOS
        # ----------------------------------------------------

        alumnos_ia = datos_ia.get(
            "alumnos",
            [],
        )

        alumnos_normalizados = []

        if isinstance(
            alumnos_ia,
            list,
        ):

            for alumno_ia in alumnos_ia:

                if not isinstance(
                    alumno_ia,
                    dict,
                ):
                    continue

                nombre = limpiar_nombre_extraido(
                    alumno_ia.get(
                        "nombre",
                        "",
                    )
                )

                nivel = str(
                    alumno_ia.get(
                        "nivel",
                        "",
                    )
                    or ""
                ).strip()

                grado = str(
                    alumno_ia.get(
                        "grado",
                        "",
                    )
                    or ""
                ).strip()

                if not any(
                    [
                        nombre,
                        nivel,
                        grado,
                    ]
                ):
                    continue

                alumnos_normalizados.append({
                    "nombre": nombre,
                    "nivel": nivel,
                    "grado": grado,
                })
                
        if alumnos_normalizados:

            datos["alumnos"] = (
                alumnos_normalizados
            )

            # Compatibilidad temporal con el flujo anterior.
            datos["alumno"] = (
                alumnos_normalizados[0][
                    "nombre"
                ]
            )

            # Sólo trasladamos nivel/grado a los campos
            # singulares cuando existe exactamente un alumno.
            if len(alumnos_normalizados) == 1:

                alumno_unico = (
                    alumnos_normalizados[0]
                )

                nivel_alumno = str(
                    alumno_unico.get(
                        "nivel",
                        "",
                    )
                    or ""
                ).strip()

                grado_alumno = str(
                    alumno_unico.get(
                        "grado",
                        "",
                    )
                    or ""
                ).strip()

                if nivel_alumno:
                    datos["nivel"] = nivel_alumno

                if grado_alumno:
                    datos["grado"] = grado_alumno

        print(
            "✅ Datos de cita extraídos: "
            f"padres={datos['padres']!r}, "
            f"alumnos={datos['alumnos']!r}, "
            f"modelo={modelo_usado}"
        )

        return datos

    except Exception as e:

        print(
            "⚠️ Error extrayendo datos de cita: "
            f"{e}"
        )

        return datos

def obtener_alumnos_cita_persistidos(
    contact,
) -> List[Dict[str, str]]:
    """
    Recupera la lista estructurada de alumnos asociados
    a una cita.

    Prioridad:
    1. ALUMNOS_CITA.
    2. Campos singulares anteriores como compatibilidad.
    """

    alumnos = []

    alumnos_json = str(
        get_note_value(
            contact,
            "ALUMNOS_CITA",
        )
        or ""
    ).strip()

    if alumnos_json:
        try:
            alumnos_crudos = json.loads(
                alumnos_json
            )

            if isinstance(
                alumnos_crudos,
                list,
            ):
                for alumno in alumnos_crudos:

                    if not isinstance(
                        alumno,
                        dict,
                    ):
                        continue

                    nombre = str(
                        alumno.get(
                            "nombre",
                            "",
                        )
                        or ""
                    ).strip()

                    nivel = str(
                        alumno.get(
                            "nivel",
                            "",
                        )
                        or ""
                    ).strip()

                    grado = str(
                        alumno.get(
                            "grado",
                            "",
                        )
                        or ""
                    ).strip()

                    if not any(
                        [
                            nombre,
                            nivel,
                            grado,
                        ]
                    ):
                        continue

                    alumnos.append({
                        "nombre": nombre,
                        "nivel": nivel,
                        "grado": grado,
                    })

        except Exception as e:
            print(
                "⚠️ No se pudo leer ALUMNOS_CITA: "
                f"{e}"
            )

    if alumnos:
        return alumnos

    # --------------------------------------------------------
    # COMPATIBILIDAD CON REGISTROS ANTERIORES
    # --------------------------------------------------------

    nombre = str(
        get_note_value(
            contact,
            "NOMBRE_ALUMNO",
        )
        or ""
    ).strip()

    nivel = str(
        get_note_value(
            contact,
            "NIVEL_INTERES",
        )
        or ""
    ).strip()

    grado = str(
        get_note_value(
            contact,
            "GRADO_INTERES",
        )
        or get_note_value(
            contact,
            "GRADO_SOLICITADO",
        )
        or ""
    ).strip()

    if any(
        [
            nombre,
            nivel,
            grado,
        ]
    ):
        alumnos.append({
            "nombre": nombre,
            "nivel": nivel,
            "grado": grado,
        })

    return alumnos
    
def construir_resumen_cita_admin(contact) -> str:
    """
    Construye el resumen final que se envía al WhatsApp maestro.

    Soporta uno o varios alumnos asociados a la misma cita.
    """

    padres = str(
        (
            get_note_value(
                contact,
                "NOMBRE_PADRES",
            )
            or get_note_value(
                contact,
                "NOMBRE_TUTOR",
            )
            or ""
        )
    ).strip()

    fecha_cita = str(
        (
            get_note_value(
                contact,
                "FECHA_CITA",
            )
            or get_note_value(
                contact,
                "FECHA_CITA_TEXTO",
            )
            or get_note_value(
                contact,
                "FECHA_CITA_ISO",
            )
            or ""
        )
    ).strip()

    hora_cita = str(
        get_note_value(
            contact,
            "HORA_CITA",
        )
        or ""
    ).strip()

    fecha_cita_mostrable = (
        formatear_fecha_cita_calendario(
            fecha_cita
        )
        or fecha_cita
        or "Pendiente"
    )

    hora_cita_mostrable = (
        hora_cita
        or "Pendiente"
    )

    alumnos_cita = (
        obtener_alumnos_cita_persistidos(
            contact
        )
    )

    lineas_alumnos = []

    if alumnos_cita:

        total_alumnos = len(
            alumnos_cita
        )

        for indice, alumno in enumerate(
            alumnos_cita,
            start=1,
        ):

            nombre = str(
                alumno.get(
                    "nombre",
                    "",
                )
                or ""
            ).strip()

            nivel = str(
                alumno.get(
                    "nivel",
                    "",
                )
                or ""
            ).strip()

            grado = str(
                alumno.get(
                    "grado",
                    "",
                )
                or ""
            ).strip()

            if total_alumnos == 1:

                lineas_alumnos.extend([
                    (
                        "Alumno: "
                        f"{nombre or 'Pendiente'}"
                    ),
                    (
                        "Nivel: "
                        f"{nivel or 'Pendiente'}"
                    ),
                    (
                        "Grado: "
                        f"{grado or 'Pendiente'}"
                    ),
                ])

            else:

                lineas_alumnos.extend([
                    (
                        f"Alumno {indice}: "
                        f"{nombre or 'Pendiente'}"
                    ),
                    (
                        "Nivel: "
                        f"{nivel or 'Pendiente'}"
                    ),
                    (
                        "Grado: "
                        f"{grado or 'Pendiente'}"
                    ),
                ])

                if indice < total_alumnos:
                    lineas_alumnos.append("")

    else:

        lineas_alumnos.extend([
            "Alumno: Pendiente",
            "Nivel: Pendiente",
            "Grado: Pendiente",
        ])

    bloque_alumnos = "\n".join(
        lineas_alumnos
    )

    return (
        "📌 Cita registrada\n\n"
        f"Tutor: {padres or 'Pendiente'}\n"
        f"Cel: {contact.phone_number}\n\n"
        f"{bloque_alumnos}\n\n"
        f"Cita: {fecha_cita_mostrable}, "
        f"{hora_cita_mostrable}"
    ) 

def enviar_resumen_cita_admin_whatsapp(contact):
    """
    Envía al WhatsApp maestro el resumen de la cita registrada.
    """
    admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "whatsapp:+5215546080064")

    resumen = construir_resumen_cita_admin(contact)

    resultado = enviar_respuesta_twilio(admin_number, resumen)

    print(f"📌 Resumen de cita enviado al admin: {resultado}")
    print(f"📌 Resumen cita: {repr(resumen)}")

    return resultado


def procesar_datos_registro_cita(
    db: Session,
    contact,
    from_number: str,
    mensaje_usuario: str,
):
    """
    Procesa los datos faltantes para completar una cita ya confirmada.

    Mantiene separados:
    - NIVEL_INTERES
    - GRADO_INTERES

    El objetivo pendiente sólo se limpia cuando todos los
    datos requeridos para la cita están completos.
    """

    # --------------------------------------------------------
    # CONTEXTO INMEDIATO DE LA RESPUESTA
    # --------------------------------------------------------
    #
    # El prospecto puede contestar únicamente con nombres,
    # grados u otros datos breves. Recuperamos la última
    # solicitud que recibió para que Gemini interprete la
    # respuesta dentro de su contexto real.
    # --------------------------------------------------------

    ultimo_mensaje_asistente_obj = (
        db.query(Message)
        .filter(
            Message.contact_id == contact.id,
            Message.direction == "outgoing",
        )
        .order_by(
            Message.id.desc()
        )
        .first()
    )

    ultimo_mensaje_asistente = str(
        getattr(
            ultimo_mensaje_asistente_obj,
            "content",
            "",
        )
        or ""
    ).strip()

    datos = extraer_datos_registro_cita(
        mensaje_usuario,
        contact,
        ultimo_mensaje_asistente=(
            ultimo_mensaje_asistente
        ),
    )

    # --------------------------------------------------------
    # PERSISTIR DATOS DETECTADOS
    # --------------------------------------------------------

    if datos.get("padres"):
        set_note_value(
            contact,
            "NOMBRE_PADRES",
            datos["padres"],
        )

        set_note_value(
            contact,
            "NOMBRE_TUTOR",
            datos["padres"],
        )

    # --------------------------------------------------------
    # PERSISTIR UNO O VARIOS ALUMNOS DE LA CITA
    # --------------------------------------------------------

    alumnos_detectados = datos.get(
        "alumnos",
        [],
    )

    if not isinstance(
        alumnos_detectados,
        list,
    ):
        alumnos_detectados = []

    alumnos_normalizados = []

    for alumno_detectado in alumnos_detectados:

        if not isinstance(
            alumno_detectado,
            dict,
        ):
            continue

        nombre_alumno = str(
            alumno_detectado.get(
                "nombre",
                "",
            )
            or ""
        ).strip()

        nivel_alumno = str(
            alumno_detectado.get(
                "nivel",
                "",
            )
            or ""
        ).strip()

        grado_alumno = str(
            alumno_detectado.get(
                "grado",
                "",
            )
            or ""
        ).strip()

        if not any(
            [
                nombre_alumno,
                nivel_alumno,
                grado_alumno,
            ]
        ):
            continue

        alumnos_normalizados.append({
            "nombre": nombre_alumno,
            "nivel": nivel_alumno,
            "grado": grado_alumno,
        })

    if alumnos_normalizados:

        # ----------------------------------------------------
        # FUSIONAR CON ALUMNOS YA ASOCIADOS A LA CITA
        # ----------------------------------------------------

        alumnos_previos = (
            obtener_alumnos_cita_persistidos(
                contact
            )
        )

        if not isinstance(
            alumnos_previos,
            list,
        ):
            alumnos_previos = []

        alumnos_fusionados = []

        # ----------------------------------------------------
        # CASO PRINCIPAL:
        # YA EXISTE ESTRUCTURA DE ALUMNOS EN LA CITA
        # ----------------------------------------------------

        if alumnos_previos:

            for indice, alumno_previo in enumerate(
                alumnos_previos
            ):

                if not isinstance(
                    alumno_previo,
                    dict,
                ):
                    alumno_previo = {}

                nombre_previo = str(
                    alumno_previo.get(
                        "nombre",
                        "",
                    )
                    or ""
                ).strip()

                nivel_previo = str(
                    alumno_previo.get(
                        "nivel",
                        "",
                    )
                    or ""
                ).strip()

                grado_previo = str(
                    alumno_previo.get(
                        "grado",
                        "",
                    )
                    or ""
                ).strip()

                alumno_nuevo = (
                    alumnos_normalizados[indice]
                    if indice < len(
                        alumnos_normalizados
                    )
                    else {}
                )

                nombre_nuevo = str(
                    alumno_nuevo.get(
                        "nombre",
                        "",
                    )
                    or ""
                ).strip()

                nivel_nuevo = str(
                    alumno_nuevo.get(
                        "nivel",
                        "",
                    )
                    or ""
                ).strip()

                grado_nuevo = str(
                    alumno_nuevo.get(
                        "grado",
                        "",
                    )
                    or ""
                ).strip()

                alumnos_fusionados.append({
                    "nombre": (
                        nombre_nuevo
                        or nombre_previo
                    ),
                    "nivel": (
                        nivel_previo
                        or nivel_nuevo
                    ),
                    "grado": (
                        grado_previo
                        or grado_nuevo
                    ),
                })

            # Si Gemini detectó alumnos adicionales que realmente
            # no existían en la estructura previa, no los perdemos.
            if (
                len(alumnos_normalizados)
                > len(alumnos_previos)
            ):

                for alumno_extra in (
                    alumnos_normalizados[
                        len(alumnos_previos):
                    ]
                ):

                    alumnos_fusionados.append(
                        alumno_extra
                    )

        # ----------------------------------------------------
        # SI NO HABÍA ESTRUCTURA PREVIA, USAMOS LO DETECTADO
        # ----------------------------------------------------

        else:
            alumnos_fusionados = (
                alumnos_normalizados
            )

        set_note_value(
            contact,
            "ALUMNOS_CITA",
            json.dumps(
                alumnos_fusionados,
                ensure_ascii=False,
            ),
        )

        # Compatibilidad con funciones anteriores.
        primer_alumno_con_nombre = next(
            (
                alumno
                for alumno in alumnos_fusionados
                if str(
                    alumno.get(
                        "nombre",
                        "",
                    )
                    or ""
                ).strip()
            ),
            None,
        )

        if primer_alumno_con_nombre:

            set_note_value(
                contact,
                "NOMBRE_ALUMNO",
                str(
                    primer_alumno_con_nombre.get(
                        "nombre",
                        "",
                    )
                    or ""
                ).strip(),
            )
            
    elif datos.get("alumno"):

        # Compatibilidad defensiva por si el extractor anterior
        # entrega únicamente el campo singular.
        nombre_alumno_singular = str(
            datos.get(
                "alumno",
                "",
            )
            or ""
        ).strip()

        if nombre_alumno_singular:

            set_note_value(
                contact,
                "NOMBRE_ALUMNO",
                nombre_alumno_singular,
            )

            set_note_value(
                contact,
                "ALUMNOS_CITA",
                json.dumps(
                    [
                        {
                            "nombre": (
                                nombre_alumno_singular
                            ),
                            "nivel": str(
                                datos.get(
                                    "nivel",
                                    "",
                                )
                                or ""
                            ).strip(),
                            "grado": str(
                                datos.get(
                                    "grado",
                                    "",
                                )
                                or ""
                            ).strip(),
                        }
                    ],
                    ensure_ascii=False,
                ),
            )

    if datos.get("nivel"):
        set_note_value(
            contact,
            "NIVEL_INTERES",
            datos["nivel"],
        )

    if datos.get("grado"):
        set_note_value(
            contact,
            "GRADO_INTERES",
            datos["grado"],
        )

    db.commit()

    # --------------------------------------------------------
    # RECONSTRUIR DATOS PERSISTIDOS
    # --------------------------------------------------------

    padres = str(
        (
            get_note_value(
                contact,
                "NOMBRE_PADRES",
            )
            or get_note_value(
                contact,
                "NOMBRE_TUTOR",
            )
            or ""
        )
    ).strip()

    alumnos_cita = (
        obtener_alumnos_cita_persistidos(
            contact
        )
    )

    # --------------------------------------------------------
    # DETERMINAR ÚNICAMENTE DATOS FALTANTES
    # --------------------------------------------------------

    faltantes = []

    if not padres:
        faltantes.append(
            "su nombre completo"
        )

    if not alumnos_cita:
        faltantes.append(
            "el nombre completo de su hijo(a)"
        )

    else:
        total_alumnos = len(
            alumnos_cita
        )

        for indice, alumno_cita in enumerate(
            alumnos_cita,
            start=1,
        ):
            nombre = str(
                alumno_cita.get(
                    "nombre",
                    "",
                )
                or ""
            ).strip()

            nivel = str(
                alumno_cita.get(
                    "nivel",
                    "",
                )
                or ""
            ).strip()

            grado = str(
                alumno_cita.get(
                    "grado",
                    "",
                )
                or ""
            ).strip()

            if total_alumnos == 1:

                if not nombre:
                    faltantes.append(
                        "el nombre completo de su hijo(a)"
                    )

                if not nivel:
                    faltantes.append(
                        "el nivel educativo de interés"
                    )

                if nivel and not grado:
                    faltantes.append(
                        "el grado específico al que ingresaría"
                    )

            else:

                etiqueta = (
                    f"del alumno {indice}"
                )

                if not nombre:
                    faltantes.append(
                        "el nombre completo "
                        f"{etiqueta}"
                    )

                if not nivel:
                    faltantes.append(
                        "el nivel educativo "
                        f"{etiqueta}"
                    )

                if nivel and not grado:
                    faltantes.append(
                        "el grado específico "
                        f"{etiqueta}"
                    )
                    
    # --------------------------------------------------------
    # TODAVÍA FALTAN DATOS
    # --------------------------------------------------------

    if faltantes:
        set_note_value(
            contact,
            "OBJETIVO_PENDIENTE",
            "OBTENER_DATOS_CITA",
        )

        set_flow_state(
            contact,
            "ESPERANDO_DATOS_CITA",
        )

        db.commit()

        if len(faltantes) == 1:
            faltantes_texto = faltantes[0]

        elif len(faltantes) == 2:
            faltantes_texto = (
                f"{faltantes[0]} y "
                f"{faltantes[1]}"
            )

        else:
            faltantes_texto = (
                ", ".join(faltantes[:-1])
                + " y "
                + faltantes[-1]
            )

        respuesta = (
            "Muchas gracias. "
            "Para completar el registro de su cita, "
            "¿me podría apoyar también con "
            f"{faltantes_texto}?"
        )

        resultado = enviar_respuesta_twilio(
            from_number,
            respuesta,
        )

        twilio_sid = None

        if "SID:" in resultado:
            twilio_sid = (
                resultado
                .split("SID: ")[1]
                .strip()
            )

        save_message(
            db,
            contact.id,
            "outgoing",
            respuesta,
            twilio_sid,
        )

        db.commit()

        print(
            "📌 Datos de cita incompletos. "
            f"Faltan: {faltantes}"
        )

        return {
            "status": "datos_cita_incompletos",
            "faltantes": faltantes,
        }

    # --------------------------------------------------------
    # DATOS COMPLETOS
    # --------------------------------------------------------

    fecha_cita_confirmada = str(
        (
            get_note_value(
                contact,
                "FECHA_CITA",
            )
            or get_note_value(
                contact,
                "FECHA_CITA_TEXTO",
            )
            or get_note_value(
                contact,
                "FECHA_CITA_ISO",
            )
            or ""
        )
    ).strip()

    hora_cita_confirmada = str(
        get_note_value(
            contact,
            "HORA_CITA",
        )
        or ""
    ).strip()

    if (
        fecha_cita_confirmada
        and hora_cita_confirmada
    ):
        respuesta = (
            "Perfecto, su cita ha quedado registrada "
            f"para el {fecha_cita_confirmada} "
            f"a las {hora_cita_confirmada}. "
            "Los esperamos."
        )

    elif fecha_cita_confirmada:
        respuesta = (
            "Perfecto, su cita ha quedado registrada "
            f"para el {fecha_cita_confirmada}. "
            "Los esperamos."
        )

    elif hora_cita_confirmada:
        respuesta = (
            "Perfecto, su cita ha quedado registrada "
            f"a las {hora_cita_confirmada}. "
            "Los esperamos."
        )

    else:
        respuesta = (
            "Perfecto, su cita ha quedado registrada. "
            "Los esperamos."
        )
    resultado = enviar_respuesta_twilio(
        from_number,
        respuesta,
    )

    envio_exitoso = str(
        resultado or ""
    ).strip().startswith("✅")

    if not envio_exitoso:
        db.rollback()

        print(
            "❌ No se cerrará el registro de cita "
            "porque falló el envío final al prospecto: "
            f"{resultado}"
        )

        return {
            "status": "datos_cita_final_send_failed",
            "error": str(resultado),
        }

    twilio_sid = None

    if "SID:" in resultado:
        twilio_sid = (
            resultado
            .split("SID: ")[1]
            .strip()
        )

    save_message(
        db,
        contact.id,
        "outgoing",
        respuesta,
        twilio_sid,
    )

    # --------------------------------------------------------
    # CIERRE PERSISTENTE DE LA CITA
    # --------------------------------------------------------

    set_note_value(
        contact,
        "OBJETIVO_PENDIENTE",
        "",
    )

    set_note_value(
        contact,
        "ETAPA_CONVERSACIONAL",
        "VISITA_CONFIRMADA",
    )

    set_flow_state(
        contact,
        "CITA_DATOS_COMPLETOS",
    )

    contact.status = "VISITA_CONFIRMADA"

    db.commit()

    # --------------------------------------------------------
    # SINCRONIZAR CRM AL CERRAR EL REGISTRO DE LA CITA
    # --------------------------------------------------------

    sincronizar_crm_desde_transicion(
        db,
        contact,
        {
            "transicion_aplicada": True,
            "etapa_conversacional": (
                "VISITA_CONFIRMADA"
            ),
            "estado_comercial": (
                "VISITA_CONFIRMADA"
            ),
            "objetivo_pendiente": "",
        },
    )

    enviar_resumen_cita_admin_whatsapp(
        contact
    )

    print(
        "📌 Datos de cita completos. "
        "Objetivo pendiente cerrado y resumen "
        "enviado al administrador."
    )

    return {
        "status": "datos_cita_completos",
        "alumnos": alumnos_cita,
    }
    

def redactar_respuesta_admin_para_prospecto(texto_admin: str, tarea: AdminPendingTask) -> str:
    """
    Convierte la respuesta del administrador en un mensaje listo para el prospecto.
    Siempre intenta usar IA para adaptar el tono.
    """
    texto_admin = (texto_admin or "").strip()

    if not texto_admin:
        return """Gracias por esperar.

En breve le confirmamos la disponibilidad por este medio."""

    if not GEMINI_API_KEY:
        return f"""Gracias por esperar.

Le compartimos la confirmación:

{texto_admin}"""

    prompt = f"""
Eres el asistente de WhatsApp del Colegio Valle de Filadelfia Campus Santa Cruz Atizapán.

CONTEXTO:
Un prospecto estaba esperando confirmación de disponibilidad para una visita.

ÚLTIMO MENSAJE DEL PROSPECTO:
{tarea.trigger_message or ""}

RESPUESTA PREVIA DEL BOT AL PROSPECTO:
{tarea.bot_response or ""}

RESPUESTA INTERNA DEL ADMINISTRADOR:
{texto_admin}

TAREA:
Convierte la respuesta interna del administrador en un mensaje final para el prospecto.

REGLAS:
- Redacta en tono amable, claro e institucional.
- No menciones al administrador.
- No digas "mi jefe", "el director", "el administrador" ni "me autorizaron".
- No menciones que eres IA.
- No uses lenguaje interno.
- Si el administrador confirma disponibilidad, confirma la cita con día y hora.
- Cuando el administrador confirme definitivamente una cita, no inicies con saludos como "Hola", "¡Hola!", "Buenos días", "Buenas tardes" o similares.
- En una confirmación definitiva de cita, inicia directamente con: "Le confirmo que su visita ha quedado programada".
- Si el administrador propone otro horario disponible, explica que ese horario está disponible y pide confirmación.
- Si el administrador propone alternativas sin confirmar disponibilidad definitiva, preséntalas como opciones posibles y pide al prospecto cuál le acomoda mejor.
- Si el administrador rechaza la disponibilidad, pide una alternativa de día u hora.
- Mantén formato WhatsApp con bloques cortos.
- No inventes datos que no estén en la respuesta del administrador.
- Respeta exactamente el día, hora, condición o alternativa indicada por el administrador.
- Si el administrador dice que un día u horario no está disponible, explícalo claramente.
- Si el administrador propone alternativas, inclúyelas de forma clara.
- No omitas información importante de la respuesta interna del administrador.
- Cuando la cita quede confirmada y todavía deban solicitarse datos para completar el registro, no cierres la conversación.
- MUY IMPORTANTE: no solicites nombres, nivel, grado ni datos de registro en esta respuesta.
- Tu única función aquí es comunicar la decisión del administrador.
- Si faltan datos para registrar la cita, Python los solicitará después de esta respuesta.
- No preguntes el nombre del tutor, del alumno, el nivel ni el grado.
- No utilices frases de despedida como "Que tenga un excelente día", "Estamos a sus órdenes", "Cualquier duda hágamelo saber", "Si requiere indicaciones para llegar" o expresiones equivalentes.
- Evita cualquier despedida antes de que se hayan solicitado y recibido los datos faltantes del tutor y del alumno.
- Si después de esta respuesta se solicitarán datos para completar el registro de la cita, termina la confirmación de forma natural y abierta, sin despedirte del prospecto.
- No dejes frases incompletas.
- No termines el mensaje con frases como "se encontrará", "a las", "para", "con", "que", "de" o "en".
- Antes de responder, verifica que el mensaje final tenga sentido completo.
- Responde sólo con el mensaje final para el prospecto.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2
            ),
            tarea="respuesta admin para prospecto"
        )
        
        print(f"👑 Modelo usado para respuesta admin: {modelo_usado}")

        mensaje_final = extraer_texto_respuesta_gemini(response).strip()
        
        print(f"👑 Respuesta admin generada por IA: {repr(mensaje_final)}")
        
        if respuesta_admin_parece_incompleta(mensaje_final):
            print(f"⚠️ Respuesta admin generada parece incompleta: {repr(mensaje_final)}")
        
            texto_limpio = limpiar_instruccion_admin(texto_admin)
        
            if texto_limpio:
                return f"""Gracias por su amable espera.
        
        Le compartimos la información:
        
        {texto_limpio}"""
        
            return """Gracias por su amable espera.
        
        En breve le confirmamos la disponibilidad por este medio."""
        
        return mensaje_final

    except Exception as e:
        print(f"⚠️ Error redactando respuesta admin con IA: {e}")
        return f"""Gracias por esperar.

Le compartimos la confirmación:

{texto_admin}"""

def procesar_respuesta_admin(db: Session, from_number: str, mensaje_admin: str):
    """
    Procesa mensajes del WhatsApp maestro/admin.

    Flujo:
    1. Si no hay tarea seleccionada, muestra menú de pendientes.
    2. Si el admin responde con un número, selecciona esa tarea.
    3. Si ya hay tarea seleccionada, procesa el siguiente mensaje como respuesta final.
    """
    admin_key = normalizar_numero_whatsapp(from_number)
    mensaje_limpio = (mensaje_admin or "").strip()

    tareas = obtener_tareas_admin_pendientes(db)

    if not tareas:
        ADMIN_SELECTED_TASKS.pop(admin_key, None)

        respuesta_admin = "No hay conversaciones pendientes de confirmación en este momento."
        resultado = enviar_respuesta_twilio(from_number, respuesta_admin)

        print(f"📣 Admin sin pendientes: {resultado}")
        return {"status": "admin_no_pending"}

    # Si el admin escribe "cancelar", salimos de la selección actual.
    if mensaje_limpio.lower() in ["cancelar", "salir", "menú", "menu"]:
        ADMIN_SELECTED_TASKS.pop(admin_key, None)

        menu = construir_menu_tareas_pendientes(tareas)
        resultado = enviar_respuesta_twilio(from_number, menu)

        print(f"📋 Menú de pendientes enviado al admin: {resultado}")
        return {"status": "admin_menu_sent"}

    # Si no hay tarea seleccionada todavía, interpretamos el mensaje como selección
    # o, si sólo hay una tarea pendiente, como respuesta directa.
    tarea_id_seleccionada = ADMIN_SELECTED_TASKS.get(admin_key)

    if not tarea_id_seleccionada:
        # Si el admin responde con número, selecciona la tarea como antes.
        if mensaje_limpio.isdigit():
            indice = int(mensaje_limpio)

            if 1 <= indice <= len(tareas):
                tarea = tareas[indice - 1]
                ADMIN_SELECTED_TASKS[admin_key] = tarea.id

                respuesta_admin = f"""Seleccionaste al prospecto {tarea.prospect_phone}.

Último mensaje del prospecto:
{tarea.trigger_message or ""}

Ahora escribe la respuesta que deseas enviar.
La IA la adaptará antes de mandarla.

Para cancelar, escribe:
cancelar"""

                resultado = enviar_respuesta_twilio(from_number, respuesta_admin)

                print(f"✅ Admin seleccionó tarea {tarea.id}: {resultado}")
                return {
                    "status": "admin_task_selected",
                    "task_id": tarea.id
                }

            respuesta_admin = f"""La opción {indice} no existe.

{construir_menu_tareas_pendientes(tareas)}"""

            resultado = enviar_respuesta_twilio(from_number, respuesta_admin)
            return {"status": "admin_invalid_option"}

        # Si solamente existe una tarea pendiente, permitimos respuesta
        # directa únicamente cuando el mensaje contiene una instrucción
        # administrativa clara. Saludos o textos ambiguos no se envían
        # al prospecto.

        if len(tareas) == 1:
            mensaje_admin_normalizado = normalizar_texto_para_deteccion(
                mensaje_limpio
            )

            mensajes_no_accionables = {
                "",
                "hola",
                "holi",
                "hello",
                "buen dia",
                "buenos dias",
                "buena tarde",
                "buenas tardes",
                "buena noche",
                "buenas noches",
                "oye",
                "ok",
                "okay",
                "gracias",
                "si",
                "no",
            }

            if mensaje_admin_normalizado in mensajes_no_accionables:
                tarea = tareas[0]

                respuesta_admin = f"""Tiene una conversación pendiente.

Prospecto:
{tarea.prospect_phone}

Último mensaje:
{tarea.trigger_message or ""}

Escriba una instrucción clara para responderle.

Ejemplos:
- Confirmar lunes a las 10:30
- Proponer martes a las 11:00
- Indicar que seguimos revisando disponibilidad
- Rechazar ese horario y ofrecer otra opción

Para ver el menú, escriba:
menu"""

                resultado = enviar_respuesta_twilio(
                    from_number,
                    respuesta_admin,
                )

                print(
                    "🛡️ Mensaje administrativo no accionable; "
                    f"no se respondió al prospecto: {resultado}"
                )

                return {
                    "status": "admin_instruction_required",
                    "task_id": tarea.id,
                }

            tarea = tareas[0]
            ADMIN_SELECTED_TASKS[admin_key] = tarea.id
            tarea_id_seleccionada = tarea.id

            print(
                "✅ Admin respondió con una instrucción directa; "
                f"se usará la única tarea pendiente {tarea.id}"
            )


        else:
            # Si hay varias conversaciones pendientes, por seguridad se exige selección.
            menu = construir_menu_tareas_pendientes(tareas)
            resultado = enviar_respuesta_twilio(from_number, menu)

            print(f"📋 Admin escribió sin selección; se envió menú: {resultado}")
            return {"status": "admin_menu_sent"}

    # Si ya había tarea seleccionada, ahora sí procesamos el mensaje como respuesta.
    tarea = db.query(AdminPendingTask).filter(
        AdminPendingTask.id == tarea_id_seleccionada,
        AdminPendingTask.status == "PENDIENTE"
    ).first()

    if not tarea:
        ADMIN_SELECTED_TASKS.pop(admin_key, None)

        respuesta_admin = """La conversación que habías seleccionado ya no está pendiente.

Te muestro nuevamente el menú actualizado:

""" + construir_menu_tareas_pendientes(tareas)

        resultado = enviar_respuesta_twilio(from_number, respuesta_admin)

        print(f"⚠️ Tarea seleccionada ya no disponible: {resultado}")
        return {"status": "admin_selected_task_not_available"}

    contact = db.query(Contact).filter(Contact.id == tarea.contact_id).first()

    if not contact:
        ADMIN_SELECTED_TASKS.pop(admin_key, None)

        respuesta_admin = "No encontré el contacto del prospecto pendiente."
        resultado = enviar_respuesta_twilio(from_number, respuesta_admin)

        print(f"⚠️ Contacto pendiente no encontrado: {resultado}")
        return {"status": "admin_contact_not_found"}

    # --------------------------------------------------------
    # MODERACIÓN TAMBIÉN AUTORIZA EL OUTBOUND ADMINISTRATIVO
    # --------------------------------------------------------

    if contacto_esta_bloqueado(
        db,
        contact.id,
    ):
        ADMIN_SELECTED_TASKS.pop(
            admin_key,
            None,
        )

        respuesta_admin = (
            "⚠️ Este contacto se encuentra bloqueado "
            "en el CRM. No se envió ningún mensaje "
            "al prospecto."
        )

        resultado = enviar_respuesta_twilio(
            from_number,
            respuesta_admin,
        )

        print(
            "🚫 RESPUESTA ADMIN SUPRIMIDA POR MODERACIÓN: "
            f"contact_id={contact.id}"
        )

        return {
            "status": "admin_response_blocked_by_moderation",
            "contact_id": contact.id,
        }

    contexto_admin_actual = (
        construir_contexto_comercial_desde_contacto(
            contact
        )
    )

    etapa_admin_actual = str(
        contexto_admin_actual.get(
            "etapa_conversacional",
            "",
        )
        or ""
    ).strip().upper()

    estado_admin_actual = str(
        contexto_admin_actual.get(
            "estado_comercial",
            "",
        )
        or ""
    ).strip().upper()

    objetivo_admin_actual = str(
        contexto_admin_actual.get(
            "objetivo_pendiente",
            "",
        )
        or ""
    ).strip().upper()

    revision_admin_no_cita = bool(
        objetivo_admin_actual
        == "ESPERAR_CONFIRMACION_ADMIN"
        and etapa_admin_actual
        != "ESPERANDO_CONFIRMACION_ADMIN"
        and estado_admin_actual
        != "CITA_PENDIENTE_CONFIRMACION"
    )

    mensaje_para_prospecto = redactar_respuesta_admin_para_prospecto(mensaje_limpio, tarea)

    # Si el admin está confirmando definitivamente la cita,
    # primero enriquecemos el mensaje antes de enviarlo al prospecto.
    if (
        not revision_admin_no_cita
        and admin_confirma_cita_final(
            mensaje_limpio,
            tarea,
        )
    ):
        # ----------------------------------------------------
        # CITA CONFIRMADA POR ADMINISTRACIÓN
        # ----------------------------------------------------
        #
        # A partir de este punto ya no estamos esperando
        # confirmación administrativa.
        #
        # El siguiente objetivo real es completar únicamente
        # los datos faltantes necesarios para registrar la cita.
        # ----------------------------------------------------

        contact.status = "VISITA_CONFIRMADA"

        set_note_value(
            contact,
            "ETAPA_CONVERSACIONAL",
            "VISITA_CONFIRMADA",
        )

        set_note_value(
            contact,
            "OBJETIVO_PENDIENTE",
            "OBTENER_DATOS_CITA",
        )

        set_flow_state(
            contact,
            "ESPERANDO_DATOS_CITA",
        )

        # ----------------------------------------------------
        # HITO CITA_CONFIRMADA
        # ----------------------------------------------------

        hitos_actuales_raw = get_note_value(
            contact,
            "HITOS_COMERCIALES",
        )

        hitos_actuales = []

        if hitos_actuales_raw:
            try:
                hitos_decodificados = json.loads(
                    hitos_actuales_raw
                )

                if isinstance(
                    hitos_decodificados,
                    list,
                ):
                    hitos_actuales = [
                        str(hito).strip().upper()
                        for hito in hitos_decodificados
                        if str(hito).strip()
                    ]

            except Exception:
                hitos_actuales = []

        if (
            "CITA_CONFIRMADA"
            not in hitos_actuales
        ):
            hitos_actuales.append(
                "CITA_CONFIRMADA"
            )

        set_note_value(
            contact,
            "HITOS_COMERCIALES",
            json.dumps(
                hitos_actuales,
                ensure_ascii=False,
            ),
        )

        # ----------------------------------------------------
        # ENRIQUECIMIENTO DE LA CONFIRMACIÓN
        # ----------------------------------------------------

        mensaje_para_prospecto = (
            enriquecer_fecha_cita_en_mensaje(
                mensaje_para_prospecto
            )
        )

        hora_cita = str(
            get_note_value(
                contact,
                "HORA_CITA",
            )
            or get_note_value(
                contact,
                "HORA_CITA_24H",
            )
            or ""
        ).strip()

        # La hora ya confirmada durante el flujo comercial
        # es la fuente prioritaria. Sólo intentamos extraerla
        # nuevamente si realmente no existe.
        if not hora_cita:
            hora_cita = (
                extraer_hora_cita_confirmada(
                    mensaje_para_prospecto,
                    respaldo=(
                        tarea.trigger_message
                        or ""
                    ),
                )
            )

            if hora_cita:
                set_note_value(
                    contact,
                    "HORA_CITA",
                    hora_cita,
                )

        # ----------------------------------------------------
        # INICIALIZAR ALUMNOS ASOCIADOS A LA CITA
        # ----------------------------------------------------
        #
        # Al confirmarse definitivamente la visita, convertimos
        # los alumnos ya identificados durante la conversación
        # comercial en la estructura específica de la cita.
        #
        # No sustituimos ALUMNOS_CITA si ya existe, para evitar
        # perder nombres o datos completados previamente.
        # ----------------------------------------------------

        alumnos_cita_existentes_raw = str(
            get_note_value(
                contact,
                "ALUMNOS_CITA",
            )
            or ""
        ).strip()

        if not alumnos_cita_existentes_raw:

            contexto_cita = (
                construir_contexto_comercial_desde_contacto(
                    contact
                )
            )

            alumnos_comerciales = (
                contexto_cita.get(
                    "alumnos",
                    [],
                )
                if isinstance(
                    contexto_cita,
                    dict,
                )
                else []
            )

            if not isinstance(
                alumnos_comerciales,
                list,
            ):
                alumnos_comerciales = []

            alumnos_para_cita = []

            for alumno_comercial in alumnos_comerciales:

                if not isinstance(
                    alumno_comercial,
                    dict,
                ):
                    continue

                nombre = str(
                    alumno_comercial.get(
                        "nombre",
                        "",
                    )
                    or ""
                ).strip()

                nivel = str(
                    alumno_comercial.get(
                        "nivel_interes",
                        alumno_comercial.get(
                            "nivel",
                            "",
                        ),
                    )
                    or ""
                ).strip()

                grado = str(
                    alumno_comercial.get(
                        "grado_interes",
                        alumno_comercial.get(
                            "grado",
                            "",
                        ),
                    )
                    or ""
                ).strip()

                if not any(
                    [
                        nombre,
                        nivel,
                        grado,
                    ]
                ):
                    continue

                alumnos_para_cita.append({
                    "nombre": nombre,
                    "nivel": nivel,
                    "grado": grado,
                })

            if alumnos_para_cita:

                set_note_value(
                    contact,
                    "ALUMNOS_CITA",
                    json.dumps(
                        alumnos_para_cita,
                        ensure_ascii=False,
                    ),
                )

                # Compatibilidad con funciones antiguas que
                # todavía consultan campos singulares.
                primer_alumno_cita = (
                    alumnos_para_cita[0]
                )

                if primer_alumno_cita.get(
                    "nombre"
                ):
                    set_note_value(
                        contact,
                        "NOMBRE_ALUMNO",
                        primer_alumno_cita[
                            "nombre"
                        ],
                    )

                if primer_alumno_cita.get(
                    "nivel"
                ):
                    set_note_value(
                        contact,
                        "NIVEL_INTERES",
                        primer_alumno_cita[
                            "nivel"
                        ],
                    )

                if primer_alumno_cita.get(
                    "grado"
                ):
                    set_note_value(
                        contact,
                        "GRADO_INTERES",
                        primer_alumno_cita[
                            "grado"
                        ],
                    )

        # ----------------------------------------------------
        # PERSISTIR AUTORIDAD DE LA CITA ANTES DEL OUTBOUND
        # ----------------------------------------------------
        #
        # La cita ya fue confirmada por administración.
        # Esa verdad operativa no debe depender de que el
        # prospecto tarde o no en responder al mensaje.
        #
        # Persistimos antes del envío para que cualquier
        # inbound concurrente encuentre inmediatamente:
        #
        # VISITA_CONFIRMADA
        # OBTENER_DATOS_CITA
        # ESPERANDO_DATOS_CITA
        # ----------------------------------------------------

        db.commit()

        # ----------------------------------------------------
        # SOLICITAR ÚNICAMENTE DATOS FALTANTES
        # ----------------------------------------------------

        solicitud_datos = (
            construir_solicitud_datos_cita(
                contact
            )
        )

        if solicitud_datos:

            # La cita ya está confirmada, pero el registro
            # todavía necesita datos del prospecto.
            set_note_value(
                contact,
                "OBJETIVO_PENDIENTE",
                "OBTENER_DATOS_CITA",
            )

            set_flow_state(
                contact,
                "ESPERANDO_DATOS_CITA",
            )

            db.commit()

            sincronizar_crm_desde_transicion(
                db,
                contact,
                {
                    "transicion_aplicada": True,
                    "etapa_conversacional": (
                        "VISITA_CONFIRMADA"
                    ),
                    "estado_comercial": (
                        "VISITA_CONFIRMADA"
                    ),
                    "objetivo_pendiente": (
                        "OBTENER_DATOS_CITA"
                    ),
                },
            )

            mensaje_para_prospecto = (
                f"{mensaje_para_prospecto.rstrip()}\n\n"
                f"{solicitud_datos}"
            )

        else:

            # Todos los datos necesarios ya estaban disponibles.
            # La cita queda completamente cerrada desde ahora.
            set_note_value(
                contact,
                "OBJETIVO_PENDIENTE",
                "",
            )

            set_flow_state(
                contact,
                "CITA_DATOS_COMPLETOS",
            )

            db.commit()

            sincronizar_crm_desde_transicion(
                db,
                contact,
                {
                    "transicion_aplicada": True,
                    "etapa_conversacional": (
                        "VISITA_CONFIRMADA"
                    ),
                    "estado_comercial": (
                        "VISITA_CONFIRMADA"
                    ),
                    "objetivo_pendiente": "",
                },
            )

    print(f"👑 Texto admin original: {repr(mensaje_limpio)}")
    print(f"👑 Mensaje final para prospecto: {repr(mensaje_para_prospecto)}")

    prospecto_to = f"whatsapp:{contact.phone_number}"

    resultado_envio = enviar_respuesta_twilio(
        prospecto_to,
        mensaje_para_prospecto,
    )

    envio_exitoso = str(
        resultado_envio or ""
    ).strip().startswith("✅")

    if not envio_exitoso:
        db.rollback()

        print(
            "❌ No se persistirá la respuesta del admin "
            "porque falló el envío al prospecto: "
            f"{resultado_envio}"
        )

        return {
            "status": "admin_response_send_failed",
            "prospect_phone": contact.phone_number,
            "error": str(resultado_envio),
        }

    twilio_sid = None

    if "SID:" in resultado_envio:
        twilio_sid = (
            resultado_envio
            .split("SID: ")[1]
            .strip()
        )

    save_message(
        db,
        contact.id,
        "outgoing",
        mensaje_para_prospecto,
        twilio_sid,
    )

    if revision_admin_no_cita:

        # ----------------------------------------------------
        # RESOLUCIÓN ADMINISTRATIVA NO RELACIONADA CON CITA
        # ----------------------------------------------------

        clasificacion_admin_zona = (
            clasificar_resolucion_admin_zona(
                mensaje_limpio,
                tarea,
            )
        )

        if (
            clasificacion_admin_zona
            == "APRUEBA_ZONA"
        ):

            zona_aprobada = str(
                get_note_value(
                    contact,
                    "ZONA_INTERES",
                )
                or ""
            ).strip()

            if zona_aprobada:

                set_note_value(
                    contact,
                    "ZONA_VALIDADA_AUTORITATIVA",
                    zona_aprobada,
                )

                set_note_value(
                    contact,
                    "ZONA_VALIDADA",
                    "true",
                )

                agregar_hito_comercial_contacto(
                    contact,
                    "ZONA_VALIDADA",
                )

                print(
                    "✅ ZONA APROBADA POR ADMIN Y PERSISTIDA: "
                    f"contact_id={contact.id}, "
                    f"zona={zona_aprobada!r}"
                )

        elif (
            clasificacion_admin_zona
            == "RECHAZA_ZONA"
        ):
            print(
                "🚫 ZONA RECHAZADA POR ADMIN: "
                f"contact_id={contact.id}, "
                f"zona="
                f"{get_note_value(contact, 'ZONA_INTERES')!r}"
            )

        # La decisión humana ya fue comunicada.
        # La espera administrativa termina aquí.
        set_note_value(
            contact,
            "OBJETIVO_PENDIENTE",
            "",
        )

        estado_crm_admin = (
            obtener_estado_followup_crm(
                db,
                contact.id,
            )
        )

        if estado_crm_admin is not None:
            estado_crm_admin.current_objective = ""
            estado_crm_admin.next_followup_at = None
            estado_crm_admin.updated_at = (
                datetime.now(
                    timezone.utc
                )
            )

        print(
            "🔓 AUTORIDAD ADMIN RESUELTA: "
            f"contact_id={contact.id}, "
            "se cerró ESPERAR_CONFIRMACION_ADMIN."
        )
        
    tarea.status = "RESUELTA"
    tarea.admin_response = mensaje_limpio
    tarea.final_response = mensaje_para_prospecto
    tarea.resolved_at = datetime.now(timezone.utc)

    db.commit()
    
    ADMIN_SELECTED_TASKS.pop(admin_key, None)

    confirmacion_admin = f"""✅ Mensaje enviado al prospecto {contact.phone_number}.

Mensaje enviado:
{mensaje_para_prospecto}"""

    resultado_admin = enviar_respuesta_twilio(from_number, confirmacion_admin)

    print(f"📤 Respuesta admin enviada al prospecto: {resultado_envio}")
    print(f"📣 Confirmación enviada al admin: {resultado_admin}")

    return {
        "status": "admin_response_processed",
        "prospect_phone": contact.phone_number
    }

    
# ================= ENDPOINTS CRM =================
@app.get("/contacts")
async def list_contacts(
    db: Session = Depends(get_db),
    status: str = None,
    limit: int = 50
):
    """Lista todos los contactos con filtros"""
    query = db.query(Contact)
    
    if status:
        query = query.filter(Contact.status == status)
    
    contacts = query.order_by(Contact.last_contact.desc()).limit(limit).all()
    
    return {
        "total": len(contacts),
        "contacts": [
            {
                "id": c.id,
                "phone_number": c.phone_number,
                "status": c.status,
                "first_contact": c.first_contact,
                "last_contact": c.last_contact,
                "total_messages": c.total_messages,
                "is_competitor": c.is_competitor
            }
            for c in contacts
        ]
    }

def agregar_hito_comercial_contacto(
    contact,
    hito: str,
) -> bool:

    if contact is None:
        return False

    hito_normalizado = str(
        hito or ""
    ).strip().upper()

    if (
        not hito_normalizado
        or hito_normalizado
        not in HITOS_COMERCIALES_VALIDOS
    ):
        return False

    hitos_raw = get_note_value(
        contact,
        "HITOS_COMERCIALES",
    )

    hitos = []

    if hitos_raw:
        try:
            decodificados = json.loads(
                hitos_raw
            )

            if isinstance(
                decodificados,
                list,
            ):
                hitos = [
                    str(valor or "")
                    .strip()
                    .upper()
                    for valor in decodificados
                    if str(valor or "").strip()
                ]

        except Exception:
            hitos = []

    if hito_normalizado not in hitos:
        hitos.append(
            hito_normalizado
        )

    set_note_value(
        contact,
        "HITOS_COMERCIALES",
        json.dumps(
            hitos,
            ensure_ascii=False,
        ),
    )

    return True

@app.get("/conversations/{phone_number}")
async def get_conversations_by_phone(
    phone_number: str,
    db: Session = Depends(get_db),
    limit: int = 50
):
    """Obtiene todas las conversaciones de un contacto específico - VERSIÓN SIMPLIFICADA PARA PANEL"""
    # Limpiar número si viene con prefijo
    if phone_number.startswith("whatsapp:"):
        clean_number = phone_number.replace("whatsapp:", "")
    else:
        clean_number = phone_number
    
    # Buscar contacto
    contact = db.query(Contact).filter(Contact.phone_number == clean_number).first()
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    
    # Obtener mensajes ordenados cronológicamente
    messages = db.query(Message).filter(Message.contact_id == contact.id)\
        .order_by(Message.timestamp.asc())\
        .limit(limit)\
        .all()
    
    # Formatear mensajes de manera SIMPLE para el panel
    conversation_simple = []
    for msg in messages:
        # Determinar tipo de mensaje (usuario/bot)
        if msg.direction == "incoming":
            message_type = "usuario"
        else:
            message_type = "bot"
    
        dt_local = convertir_a_hora_local(msg.timestamp)
    
        conversation_simple.append({
            "tipo": message_type,
            "texto": msg.content,
            "hora": dt_local.strftime("%H:%M"),
            "fecha": dt_local.strftime("%d/%m/%Y")
        })
    
    return {
        "contacto": {
            "telefono": contact.phone_number,
            "estado": contact.status,
            "total_mensajes": contact.total_messages,
            "ultimo_contacto": convertir_a_hora_local(contact.last_contact).strftime("%d/%m/%Y %H:%M")
        },
        "conversacion": conversation_simple
    }

@app.get("/panel")
async def crm_panel(db: Session = Depends(get_db), page: int = 1, limit: int = 10):
    """Panel web de CRM con vista de conversaciones integrada y paginación"""
    
    # Obtener estadísticas
    total_contacts = db.query(Contact).count()
    by_status = db.query(Contact.status, func.count(Contact.id)).group_by(Contact.status).all()
    
    # Calcular offset para paginación
    offset = (page - 1) * limit
    
    # Últimos contactos con paginación
    recent_contacts = db.query(Contact)\
        .order_by(Contact.last_contact.desc())\
        .offset(offset)\
        .limit(limit)\
        .all()
    
    # Para cada contacto, obtener los últimos 5 mensajes
    contacts_with_messages = []
    for contact in recent_contacts:
        # Obtener últimos mensajes (solo 5 para vista previa)
        recent_messages = db.query(Message).filter(Message.contact_id == contact.id)\
            .order_by(Message.timestamp.desc())\
            .limit(10)\
            .all()
        
        # Invertir para orden cronológico
        recent_messages = recent_messages[::-1]
        
        # Formatear mensajes simplificados
        mensajes_simples = []
        for msg in recent_messages:
            mensajes_simples.append({
                "tipo": "usuario" if msg.direction == "incoming" else "bot",
                "texto": msg.content[:100] + "..." if len(msg.content) > 100 else msg.content,
                "hora": formatear_fecha_para_mensaje(msg.timestamp),
                "completo": msg.content
            })
        
        contacts_with_messages.append({
            "contacto": {
                "id": contact.id,
                "phone_number": contact.phone_number,
                "status": contact.status,
                "last_contact": convertir_a_hora_local(contact.last_contact).strftime('%d/%m/%Y %H:%M'),
                "total_messages": contact.total_messages
            },
            "mensajes_recientes": mensajes_simples
        })
    
    # Calcular si hay más páginas
    has_next = (offset + limit) < total_contacts
    has_prev = page > 1
    
    # Construir HTML de manera segura - SIN F-STRINGS COMPLEJAS
    html_parts = []
    
    # Header
    html_parts.append('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>CRM WhatsApp Bot - Colegio</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
            .header { background: #25D366; color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .stats { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 20px; }
            .stat-card { background: white; padding: 15px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); flex: 1; min-width: 200px; }
            .contact-list { background: white; padding: 20px; border-radius: 8px; }
            .contact-item { border: 1px solid #ddd; margin-bottom: 15px; padding: 15px; border-radius: 8px; }
            .page-btn { padding: 10px 20px; margin: 0 10px; background: #25D366; color: white; border: none; border-radius: 5px; text-decoration: none; display: inline-block; }
            .page-btn:hover { background: #128C7E; }
            .page-btn.disabled { background: #ccc; cursor: not-allowed; }
            .message-preview { background: #f9f9f9; padding: 10px; margin-top: 10px; border-radius: 5px; border-left: 3px solid #25D366; }
            .user-message { color: #666; font-style: italic; }
            .bot-message { color: #25D366; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📱 CRM WhatsApp Cole - Colegio</h1>
            <p>Gestión de prospectos, alumnos y competencia</p>
            <div style="margin-top: 10px;">
                <a href="/panel?page=1" style="color: white; margin-right: 15px;">🏠 Panel</a>
                <a href="/contacts" style="color: white; margin-right: 15px;">📋 Contactos</a>
                <a href="/health" style="color: white;">🩺 Health</a>
            </div>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <h3>👥 Total Contactos</h3>
                <p style="font-size: 24px; font-weight: bold;">''')
    
    html_parts.append(str(total_contacts))
    
    html_parts.append('''</p>
            </div>''')
    
    # Stats por estado
    for status, count in by_status:
        status_display = status.replace("_", " ").title()
        html_parts.append(f'''
            <div class="stat-card">
                <h3>📊 {status_display}</h3>
                <p style="font-size: 20px; font-weight: bold;">{count}</p>
            </div>
        ''')
    
    html_parts.append('''
        </div>
        
        <div class="contact-list">
            <h2>🕐 Contactos Recientes</h2>
            <p style="color: #666; margin-bottom: 20px;">Página ''')
    
    html_parts.append(str(page))
    html_parts.append(' de ')
    html_parts.append(str((total_contacts + limit - 1) // limit))
    html_parts.append('''</p>''')
    
    if contacts_with_messages:
        for item in contacts_with_messages:
            contacto = item["contacto"]
            mensajes = item["mensajes_recientes"]
            
            # Pre-procesar el número para URL (sin backslash en f-string)
            telefono_url = contacto['phone_number'].replace('+', '%2B')
            
            html_parts.append(f'''
            <div class="contact-item">
                <div style="font-weight: bold; font-size: 1.2em;">📞 {contacto['phone_number']}</div>
                <div style="color: #666; margin: 10px 0;">
                    <span>Estado: {contacto['status']}</span> • 
                    <span>Último: {contacto['last_contact']}</span> • 
                    <span>Mensajes: {contacto['total_messages']}</span>
                </div>
                
                <div class="message-preview">
                    <strong>Últimos mensajes:</strong>
            ''')
            
            for msg in mensajes[-3:]:  # Mostrar solo los últimos 3 mensajes
                tipo_clase = "user-message" if msg["tipo"] == "usuario" else "bot-message"
                icono = "👤" if msg["tipo"] == "usuario" else "🤖"
                # Pre-procesar el texto para evitar backslashes en f-string
                texto_seguro = msg["texto"]
                html_parts.append(f'''
                    <div class="{tipo_clase}">
                        {icono} {msg["hora"]}: {texto_seguro}
                    </div>
                ''')
            
            html_parts.append(f'''
                </div>
                
                <div style="margin-top: 10px;">
                    <a href="/panel/conversations/{telefono_url}" style="color: #25D366; text-decoration: none; font-weight: bold;">
                        📋 Ver conversación completa
                    </a>
                </div>
            </div>
            ''')
    else:
        html_parts.append('''
            <div style="text-align: center; padding: 40px; color: #999;">
                <h3>📭 No hay contactos registrados aún</h3>
                <p>Los contactos aparecerán aquí cuando interactúen con el bot de WhatsApp.</p>
            </div>
        ''')
    
    # PAGINACIÓN
    html_parts.append('''
        </div>
        
        <div style="text-align: center; margin: 30px 0;">
    ''')
    
    if has_prev:
        html_parts.append(f'<a href="/panel?page={page-1}&limit={limit}" class="page-btn">← Anterior</a> ')
    else:
        html_parts.append('<span class="page-btn disabled">← Anterior</span> ')
    
    html_parts.append(f'<span style="padding: 10px 20px; background: white; border-radius: 5px; margin: 0 10px;">Página {page}</span>')
    
    if has_next:
        html_parts.append(f' <a href="/panel?page={page+1}&limit={limit}" class="page-btn">Siguiente →</a>')
    else:
        html_parts.append(' <span class="page-btn disabled">Siguiente →</span>')
    
    # Footer
    html_parts.append(f'''
        </div>
        
        <footer style="text-align: center; margin-top: 40px; color: #888; padding: 20px; border-top: 1px solid #ddd;">
            <p>CRM WhatsApp Cole • Colegio • {datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")}</p>
            <p style="font-size: 0.9em; margin-top: 10px;">Total contactos: {total_contacts} | Total páginas: {(total_contacts + limit - 1) // limit}</p>
        </footer>
    </body>
    </html>
    ''')
    
    return HTMLResponse(content=''.join(html_parts))

@app.get("/panel/conversations/{phone_number}")
async def view_full_conversation(
    phone_number: str,
    db: Session = Depends(get_db)
):
    """Vista completa de conversación con diseño tipo WhatsApp"""
    from fastapi.responses import HTMLResponse
    
    # Limpiar número
    if phone_number.startswith("whatsapp:"):
        clean_number = phone_number.replace("whatsapp:", "")
    else:
        clean_number = phone_number
    
    # Buscar contacto
    contact = db.query(Contact).filter(Contact.phone_number == clean_number).first()
    
    if not contact:
        return HTMLResponse(f"""
            <html>
                <body style="font-family: Arial; padding: 20px;">
                    <h2>Contacto no encontrado</h2>
                    <a href="/panel">← Volver al panel</a>
                </body>
            </html>
        """, status_code=404)

    # --------------------------------------------------------
    # ESTADO DE MODERACIÓN DEL CONTACTO
    # --------------------------------------------------------

    estado_moderacion = obtener_estado_moderacion(
        db,
        contact.id,
    )

    contacto_bloqueado = bool(
        estado_moderacion
        and estado_moderacion.moderation_status
        == MODERATION_STATUS_BLOCKED
    )

    categoria_moderacion = (
        str(
            estado_moderacion.risk_category
            or ""
        ).strip()
        if estado_moderacion
        else ""
    )

    motivo_moderacion = (
        str(
            estado_moderacion.block_reason
            or ""
        ).strip()
        if estado_moderacion
        else ""
    )

    telefono_url = clean_number.replace(
        "+",
        "%2B",
    )
    
    # Obtener TODOS los mensajes ordenados
    messages = db.query(Message).filter(Message.contact_id == contact.id)\
        .order_by(Message.timestamp.asc())\
        .all()
    
    # Construir HTML en partes para evitar problemas de f-string
    html_parts = []
    
    # Header del HTML
    html_parts.append("""<!DOCTYPE html>
    <html>
    <head>
        <title>Conversación con """)
    
    html_parts.append(contact.phone_number)
    
    html_parts.append("""</title>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #f0f2f5;
                height: 100vh;
                display: flex;
                flex-direction: column;
            }
            
            /* HEADER SIMPLE */
            .header {
                background: #25D366;
                color: white;
                padding: 15px 20px;
                display: flex;
                align-items: center;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            }
            
            .back-btn {
                background: rgba(255,255,255,0.2);
                border: none;
                color: white;
                width: 40px;
                height: 40px;
                border-radius: 50%;
                font-size: 20px;
                margin-right: 15px;
                cursor: pointer;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            
            .back-btn:hover {
                background: rgba(255,255,255,0.3);
            }
            
            .contact-info {
                flex: 1;
            }
            
            .contact-name {
                font-weight: 600;
                font-size: 1.2em;
            }
            
            .contact-meta {
                font-size: 0.9em;
                opacity: 0.9;
                margin-top: 3px;
            }

            /* MODERACIÓN */
            .moderation-bar {
                padding: 12px 20px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 15px;
                border-bottom: 1px solid #ddd;
                background: white;
            }

            .moderation-status {
                font-size: 0.9em;
                line-height: 1.4;
            }

            .moderation-status.blocked {
                color: #b42318;
                font-weight: 600;
            }

            .moderation-status.clear {
                color: #166534;
                font-weight: 600;
            }

            .moderation-button {
                border: none;
                border-radius: 8px;
                padding: 9px 14px;
                font-weight: 600;
                cursor: pointer;
            }

            .moderation-button.block {
                background: #fee2e2;
                color: #991b1b;
            }

            .moderation-button.unblock {
                background: #dcfce7;
                color: #166534;
            }
            
            /* CONTENEDOR DE MENSAJES */
            .messages-container {
                flex: 1;
                overflow-y: auto;
                padding: 20px;
                background: #efeae2;
                background-image: url("data:image/svg+xml,%3Csvg width='100' height='100' viewBox='0 0 100 100' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M11 18c3.866 0 7-3.134 7-7s-3.134-7-7-7-7 3.134-7 7 3.134 7 7 7zm48 25c3.866 0 7-3.134 7-7s-3.134-7-7-7-7 3.134-7 7 3.134 7 7 7zm-43-7c1.657 0 3-1.343 3-3s-1.343-3-3-3-3 1.343-3 3 1.343 3 3 3zm63 31c1.657 0 3-1.343 3-3s-1.343-3-3-3-3 1.343-3 3 1.343 3 3 3zM34 90c1.657 0 3-1.343 3-3s-1.343-3-3-3-3 1.343-3 3 1.343 3 3 3zm56-76c1.657 0 3-1.343 3-3s-1.343-3-3-3-3 1.343-3 3 1.343 3 3 3zM12 86c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm28-65c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm23-11c2.76 0 5-2.24 5-5s-2.24-5-5-5-5 2.24-5 5 2.24 5 5 5zm-6 60c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm29 22c2.76 0 5-2.24 5-5s-2.24-5-5-5-5 2.24-5 5 2.24 5 5 5zM32 63c2.76 0 5-2.24 5-5s-2.24-5-5-5-5 2.24-5 5 2.24 5 5 5zm57-13c2.76 0 5-2.24 5-5s-2.24-5-5-5-5 2.24-5 5 2.24 5 5 5zm-9-21c1.105 0 2-.895 2-2s-.895-2-2-2-2 .895-2 2 .895 2 2 2zM60 91c1.105 0 2-.895 2-2s-.895-2-2-2-2 .895-2 2 .895 2 2 2zM35 41c1.105 0 2-.895 2-2s-.895-2-2-2-2 .895-2 2 .895 2 2 2zM12 60c1.105 0 2-.895 2-2s-.895-2-2-2-2 .895-2 2 .895 2 2 2z' fill='%239C9286' fill-opacity='0.1' fill-rule='evenodd'/%3E%3C/svg%3E");
            }
            
            /* MENSAJES */
            .message {
                margin: 10px 0;
                display: flex;
                flex-direction: column;
                max-width: 70%;
            }
            
            .message.usuario {
                align-items: flex-start;
            }
            
            .message.bot {
                align-items: flex-end;
                margin-left: auto;
            }
            
            .message-content {
                padding: 10px 15px;
                border-radius: 18px;
                position: relative;
                word-wrap: break-word;
                line-height: 1.4;
                font-size: 0.95em;
                box-shadow: 0 1px 2px rgba(0,0,0,0.1);
            }
            
            .message.usuario .message-content {
                background: white;
                color: #333;
                border-bottom-left-radius: 5px;
            }
            
            .message.bot .message-content {
                background: #DCF8C6;
                color: #333;
                border-bottom-right-radius: 5px;
            }
            
            .message-time {
                font-size: 0.75em;
                color: #666;
                margin-top: 5px;
                padding: 0 5px;
            }
            
            .message-sender {
                font-size: 0.8em;
                font-weight: 600;
                margin-bottom: 4px;
                padding: 0 5px;
            }
            
            .message.usuario .message-sender {
                color: #25D366;
            }
            
            .message.bot .message-sender {
                color: #128C7E;
            }
            
            /* DÍA SEPARADOR */
            .day-separator {
                text-align: center;
                margin: 20px 0;
            }
            
            .day-label {
                background: rgba(0,0,0,0.1);
                color: #666;
                display: inline-block;
                padding: 5px 15px;
                border-radius: 15px;
                font-size: 0.8em;
            }

            /* ENVÍO MANUAL DE MENSAJES */
            .message-composer {
                background: #f0f2f5;
                padding: 12px 18px;
                border-top: 1px solid #ddd;
            }
            
            .message-form {
                display: flex;
                align-items: center;
                gap: 10px;
                max-width: 1200px;
                margin: 0 auto;
            }
            
            .message-input {
                flex: 1;
                resize: none;
                border: 1px solid #ddd;
                border-radius: 20px;
                padding: 12px 16px;
                font-family: inherit;
                font-size: 0.95em;
                outline: none;
                background: white;
            }
            
            .message-input:focus {
                border-color: #25D366;
            }
            
            .send-button {
                background: #25D366;
                color: white;
                border: none;
                border-radius: 20px;
                padding: 12px 20px;
                font-weight: 600;
                cursor: pointer;
                white-space: nowrap;
            }
            
            .send-button:hover {
                background: #1ebe5d;
            }
            
            /* FOOTER */
            .footer {
                background: white;
                padding: 15px 20px;
                text-align: center;
                border-top: 1px solid #ddd;
                box-shadow: 0 -2px 5px rgba(0,0,0,0.05);
            }
            
            .footer-link {
                color: #25D366;
                text-decoration: none;
                font-weight: 500;
                margin: 0 10px;
            }
            
            .footer-link:hover {
                text-decoration: underline;
            }
            
            /* SCROLLBAR */
            ::-webkit-scrollbar {
                width: 8px;
            }
            
            ::-webkit-scrollbar-track {
                background: transparent;
            }
            
            ::-webkit-scrollbar-thumb {
                background: #ccc;
                border-radius: 4px;
            }
            
            ::-webkit-scrollbar-thumb:hover {
                background: #aaa;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <button class="back-btn" onclick="window.location.href='/panel'">←</button>
            <div class="contact-info">
                <div class="contact-name">📱 """)
    
    html_parts.append(contact.phone_number)
    
    html_parts.append("""</div>
                <div class="contact-meta">
                    """)
    
    html_parts.append(str(contact.total_messages))
    
    html_parts.append(""" mensajes • Último contacto: """)
    
    html_parts.append(convertir_a_hora_local(contact.last_contact).strftime('%d/%m/%Y %H:%M'))
    
    html_parts.append("""
                    <span style="background: #FFEAA7; color: #E17055; padding: 2px 10px; border-radius: 10px; font-size: 0.8em; margin-left: 10px;">""")
    
    html_parts.append(contact.status)
    
    html_parts.append("""</span>
                </div>
            </div>
        </div>
    """)

    # --------------------------------------------------------
    # BARRA DE MODERACIÓN
    # --------------------------------------------------------

    if contacto_bloqueado:

        detalle_bloqueo = (
            motivo_moderacion
            or "Contacto bloqueado manual o automáticamente."
        )

        html_parts.append(f"""
        <div class="moderation-bar">
            <div class="moderation-status blocked">
                🚫 CONTACTO BLOQUEADO
                <div style="font-weight: normal; margin-top: 3px;">
                    {categoria_moderacion or "SIN_CATEGORÍA"} —
                    {detalle_bloqueo}
                </div>
            </div>

            <form
                action="/panel/conversations/{telefono_url}/moderation"
                method="post"
            >
                <input
                    type="hidden"
                    name="action"
                    value="unblock"
                >

                <button
                    type="submit"
                    class="moderation-button unblock"
                >
                    Desbloquear contacto
                </button>
            </form>
        </div>
        """)

    else:

        html_parts.append(f"""
        <div class="moderation-bar">
            <div class="moderation-status clear">
                ✅ Contacto habilitado
            </div>

            <form
                action="/panel/conversations/{telefono_url}/moderation"
                method="post"
            >
                <input
                    type="hidden"
                    name="action"
                    value="block"
                >

                <button
                    type="submit"
                    class="moderation-button block"
                    onclick="return confirm(
                        '¿Deseas bloquear este contacto?'
                    );"
                >
                    Bloquear contacto
                </button>
            </form>
        </div>
        """)

    html_parts.append("""
        <div class="messages-container" id="messagesContainer">
    """)
    
    # Agrupar mensajes por fecha
    current_date = None
    for msg in messages:
        msg_date = msg.timestamp.strftime("%d/%m/%Y")
        msg_time = formatear_fecha_para_mensaje(msg.timestamp)
        msg_type = "usuario" if msg.direction == "incoming" else "bot"
        sender = "Usuario" if msg.direction == "incoming" else "Colegio Bot"
        
        # Agregar separador por día
        if msg_date != current_date:
            current_date = msg_date
            today = datetime.now().strftime("%d/%m/%Y")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%d/%m/%Y")
            
            if msg_date == today:
                day_label = "HOY"
            elif msg_date == yesterday:
                day_label = "AYER"
            else:
                # Formato: "Viernes 8 de diciembre"
                dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
                meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
                         "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    
                dt = msg.timestamp
                day_label = f"{dias_semana[dt.weekday()]} {dt.day} de {meses[dt.month-1]}"
            
            html_parts.append(f"""
                <div class="day-separator">
                    <span class="day-label">{day_label}</span>
                </div>
            """)
        
        # Pre-procesar el contenido para evitar backslashes en f-string
        # CORRECCIÓN CRÍTICA: Usar replace con caracteres, no strings con backslash
        contenido_procesado = msg.content.replace(chr(10), '<br>')
        
        # Mostrar mensaje
        html_parts.append(f"""
            <div class="message {msg_type}">
                <div class="message-sender">{sender}</div>
                <div class="message-content">
                    {contenido_procesado}
                </div>
                <div class="message-time">{msg_time}</div>
            </div>
        """)


    html_parts.append("""
        </div>
    """)

    if contacto_bloqueado:

        html_parts.append("""
        <div class="message-composer">
            <div
                style="
                    text-align: center;
                    color: #991b1b;
                    font-weight: 600;
                    padding: 10px;
                "
            >
                🚫 Este contacto está bloqueado.
                Desbloquéalo para enviar mensajes.
            </div>
        </div>
        """)

    else:

        html_parts.append(f"""
        <div class="message-composer">
            <form
                action="/panel/conversations/{telefono_url}/send"
                method="post"
                class="message-form"
            >
                <textarea
                    name="message"
                    class="message-input"
                    placeholder="Escribe un mensaje al prospecto..."
                    rows="2"
                    maxlength="1500"
                    required
                ></textarea>

                <button
                    type="submit"
                    class="send-button"
                >
                    Enviar ➤
                </button>
            </form>
        </div>
        """)

    html_parts.append("""
    
        <div class="footer">
            <a href="/panel" class="footer-link">← Volver al Panel</a>
            <span style="color: #ccc;">•</span>
            <a href="/contacts" class="footer-link">Ver Todos los Contactos</a>
            <span style="color: #ccc;">•</span>
            <a href="/" class="footer-link">Inicio</a>
        </div>
        
        <script>
            // Auto-scroll al final
            window.onload = function() {{
                const container = document.getElementById('messagesContainer');
                if (container) {{
                    container.scrollTop = container.scrollHeight;
                }}
            }};
            
            // Hotkey ESC para volver
            document.onkeydown = function(e) {{
                if (e.key === 'Escape') {{
                    window.location.href = '/panel';
                }}
            }};
        </script>
    </body>
    </html>
    """)
    
    return HTMLResponse(content=''.join(html_parts))

@app.post(
    "/panel/conversations/{phone_number}/moderation"
)
async def update_contact_moderation_from_panel(
    phone_number: str,
    action: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Bloquea o desbloquea un contacto desde el panel CRM.
    """

    if phone_number.startswith("whatsapp:"):
        clean_number = phone_number.replace(
            "whatsapp:",
            "",
        )
    else:
        clean_number = phone_number

    contact = (
        db.query(Contact)
        .filter(
            Contact.phone_number == clean_number
        )
        .first()
    )

    if not contact:
        raise HTTPException(
            status_code=404,
            detail="Contacto no encontrado",
        )

    accion = str(
        action or ""
    ).strip().lower()

    if accion == "block":

        bloquear_contacto(
            db=db,
            contact=contact,
            reason="Bloqueo manual desde panel CRM.",
            risk_category="SPAM",
            source="MANUAL_PANEL",
            message_id=None,
        )

    elif accion == "unblock":

        desbloquear_contacto(
            db=db,
            contact=contact,
        )

    else:
        raise HTTPException(
            status_code=400,
            detail="Acción de moderación no válida.",
        )

    telefono_url = clean_number.replace(
        "+",
        "%2B",
    )

    return HTMLResponse(
        content=f"""
        <html>
        <head>
            <meta
                http-equiv="refresh"
                content="0;url=/panel/conversations/{telefono_url}"
            >
        </head>
        <body>
            Estado de moderación actualizado.
        </body>
        </html>
        """
    )

@app.post("/panel/conversations/{phone_number}/send")
async def send_manual_message_from_panel(
    phone_number: str,
    message: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Envía manualmente un mensaje de WhatsApp al prospecto
    desde el panel CRM utilizando el mismo número de Twilio.
    """

    # Limpiar número
    if phone_number.startswith("whatsapp:"):
        clean_number = phone_number.replace("whatsapp:", "")
    else:
        clean_number = phone_number

    # Buscar contacto existente
    contact = db.query(Contact).filter(
        Contact.phone_number == clean_number
    ).first()

    if not contact:
        raise HTTPException(
            status_code=404,
            detail="Contacto no encontrado"
        )

    # --------------------------------------------------------
    # AUTORIDAD DE MODERACIÓN
    # --------------------------------------------------------

    if contacto_esta_bloqueado(
        db,
        contact.id,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "El contacto está bloqueado. "
                "Debe desbloquearse antes de enviar "
                "un mensaje manual."
            ),
        )
    
    # Limpiar mensaje
    mensaje_limpio = str(message or "").strip()

    if not mensaje_limpio:
        raise HTTPException(
            status_code=400,
            detail="El mensaje no puede estar vacío"
        )

    # Preparar destino WhatsApp
    prospecto_to = f"whatsapp:{clean_number}"

    # Enviar utilizando la función Twilio existente
    resultado_envio = enviar_respuesta_twilio(
        prospecto_to,
        mensaje_limpio
    )

    # Validar resultado de Twilio
    if not resultado_envio.startswith("✅"):
        raise HTTPException(
            status_code=500,
            detail=resultado_envio
        )

    # Recuperar SID de Twilio
    twilio_sid = None

    if "SID:" in resultado_envio:
        twilio_sid = resultado_envio.split(
            "SID: ",
            1
        )[1].strip()

    # Guardar mensaje saliente en el CRM
    save_message(
        db,
        contact.id,
        "outgoing",
        mensaje_limpio,
        twilio_sid
    )

    # ============================================================
    # CERRAR TAREAS ADMIN PENDIENTES ATENDIDAS DESDE EL PANEL
    # ============================================================
    #
    # Si el administrador respondió manualmente desde el CRM,
    # cualquier tarea pendiente de ese contacto relacionada con
    # confirmación/atención humana deja de estar pendiente.
    #
    # Esto evita que conversaciones ya atendidas vuelvan a aparecer
    # posteriormente en el menú del WhatsApp maestro.
    #

    tareas_pendientes_contacto = (
        db.query(AdminPendingTask)
        .filter(
            AdminPendingTask.contact_id == contact.id,
            AdminPendingTask.status == "PENDIENTE",
        )
        .all()
    )

    if tareas_pendientes_contacto:
        ahora_utc = datetime.now(timezone.utc)

        for tarea_pendiente in tareas_pendientes_contacto:
            tarea_pendiente.status = "RESUELTA"
            tarea_pendiente.admin_response = (
                "RESPUESTA_MANUAL_DESDE_PANEL"
            )
            tarea_pendiente.final_response = mensaje_limpio
            tarea_pendiente.resolved_at = ahora_utc

        db.commit()

        ids_tareas_resueltas = [
            tarea.id
            for tarea in tareas_pendientes_contacto
        ]

        print(
            "✅ Tareas admin cerradas desde panel: "
            f"contact_id={contact.id}, "
            f"tareas={ids_tareas_resueltas}"
        )

        # Una selección temporal del administrador podría apuntar
        # a alguna de estas tareas. Se elimina para evitar referencias
        # obsoletas.
        for admin_key_guardado, tarea_id_guardada in list(
            ADMIN_SELECTED_TASKS.items()
        ):
            if tarea_id_guardada in ids_tareas_resueltas:
                ADMIN_SELECTED_TASKS.pop(
                    admin_key_guardado,
                    None,
                )

    # Regresar automáticamente a la conversación

    telefono_url = clean_number.replace("+", "%2B")

    return HTMLResponse(
        content=f"""
        <html>
        <head>
            <meta http-equiv="refresh"
                  content="0;url=/panel/conversations/{telefono_url}">
        </head>
        <body>
            Mensaje enviado correctamente.
        </body>
        </html>
        """
    )
    

# ================= ENDPOINTS ADICIONALES =================
@app.get("/panel/search")
async def search_contacts(
    query: str,
    db: Session = Depends(get_db),
    limit: int = 20
):
    """Buscar contactos por número telefónico"""
    contacts = db.query(Contact).filter(
        Contact.phone_number.contains(query)
    ).order_by(Contact.last_contact.desc()).limit(limit).all()
    
    return {"results": [
        {
            "id": c.id,
            "phone_number": c.phone_number,
            "status": c.status,
            "last_contact": c.last_contact.strftime('%d/%m/%Y %H:%M'),
            "total_messages": c.total_messages
        }
        for c in contacts
    ]}

@app.get("/debug/time")
async def debug_time():
    """Endpoint para depurar problemas de zona horaria"""
    now_utc = datetime.utcnow()
    now_local = datetime.now()
    
    # Ejemplo con una hora específica (01:00 UTC)
    ejemplo_utc = datetime(2025, 12, 9, 1, 0, 0)  # 01:00 UTC
    ejemplo_local = ejemplo_utc
    
    # Aplicar offset manual para México
    es_horario_verano = 4 <= now_local.month <= 10
    offset_horas = -5 if es_horario_verano else -6
    ejemplo_mexico = ejemplo_utc + timedelta(hours=offset_horas)
    
    return {
        "utc_now": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "local_now": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "ejemplo_01_utc": ejemplo_utc.strftime("%H:%M"),
        "ejemplo_01_mexico": ejemplo_mexico.strftime("%H:%M %p"),
        "offset_actual_horas": offset_horas,
        "es_horario_verano": es_horario_verano,
        "nota": "Hora México: UTC-6 (invierno), UTC-5 (verano)"
    }

@app.get("/test-admin-template")
async def test_admin_template():
    """
    Endpoint temporal para probar exclusivamente el envío
    de la plantilla WhatsApp aprobada al administrador.

    No crea tareas.
    No modifica contactos.
    No modifica conversaciones.
    No envía mensajes a prospectos.
    """

    endpoint_habilitado = (
        os.getenv(
            "ENABLE_STRUCTURED_FLOW_TEST_ENDPOINT",
            "false",
        )
        .strip()
        .lower()
        in ["true", "1", "yes", "si", "sí"]
    )

    if not endpoint_habilitado:
        raise HTTPException(
            status_code=404,
            detail="Endpoint de prueba deshabilitado",
        )

    admin_number = os.getenv(
        "ADMIN_WHATSAPP_NUMBER"
    )

    if not admin_number:
        raise HTTPException(
            status_code=500,
            detail=(
                "ADMIN_WHATSAPP_NUMBER no configurado"
            ),
        )

    resultado = (
        enviar_template_alerta_admin_whatsapp(
            admin_number
        )
    )

    print(
        "🧪 TEST TEMPLATE ADMIN: "
        f"{resultado}"
    )

    return {
        "status": "test_admin_template",
        "resultado": resultado,
    }

@app.get("/test-gemini")
async def test_gemini(message: str = "Hola, ¿cuáles son los horarios?"):
    """Endpoint para probar Gemini sin usar WhatsApp"""
    if not GEMINI_API_KEY:
        return {"error": "Gemini API Key no configurada"}
    
    # Crear un contacto de prueba
    class ContactoPrueba:
        def __init__(self):
            self.status = "PROSPECTO_NUEVO"
            self.total_messages = 1
    
    contacto_prueba = ContactoPrueba()
    historial_prueba = []
    
    respuesta, estado_actual, estado_siguiente = generar_respuesta_gemini(message, contacto_prueba, historial_prueba)
    
    return {
        "mensaje_usuario": message,
        "respuesta_gemini": respuesta,
        "estado_actual": estado_actual,
        "estado_siguiente": estado_siguiente,
        "modelo": GEMINI_MODEL,
        "api_key_configurada": bool(GEMINI_API_KEY)
    }


# ================= INICIALIZACIÓN =================
if __name__ == "__main__":
    import uvicorn
    import os
    
    # Obtener puerto de variable de entorno o usar 8080 por defecto
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

@app.get("/reset-contact")
def reset_contact(
    phone: str = "+5215548123885",
    db: Session = Depends(get_db),
):
    """
    Borra de forma segura un contacto de prueba y todas
    las entidades dependientes asociadas.

    IMPORTANTE:
    Este endpoint es exclusivamente para pruebas.
    """

    numero = str(
        phone or ""
    ).strip()

    if numero.startswith("whatsapp:"):
        numero = numero.replace(
            "whatsapp:",
            "",
            1,
        )

    contact = (
        db.query(Contact)
        .filter(
            Contact.phone_number == numero
        )
        .first()
    )

    if not contact:
        return {
            "status": "not_found",
            "phone": numero,
        }

    contact_id = contact.id

    try:

        # ----------------------------------------------------
        # 1. EVENTOS CRM
        # ----------------------------------------------------
        #
        # FollowUpEvent puede depender tanto del contacto
        # como de mensajes, por lo que debe eliminarse antes.
        # ----------------------------------------------------

        followup_events_borrados = (
            db.query(FollowUpEvent)
            .filter(
                FollowUpEvent.contact_id
                == contact_id
            )
            .delete(
                synchronize_session=False
            )
        )

        # ----------------------------------------------------
        # 2. ESTADO CRM PERSISTENTE
        # ----------------------------------------------------

        followup_state_borrado = (
            db.query(ContactFollowUpState)
            .filter(
                ContactFollowUpState.contact_id
                == contact_id
            )
            .delete(
                synchronize_session=False
            )
        )

        # ----------------------------------------------------
        # 3. ESTADO DE MODERACIÓN
        # ----------------------------------------------------
        #
        # También puede contener referencia a un mensaje
        # mediante last_flagged_message_id.
        # ----------------------------------------------------

        moderation_state_borrado = (
            db.query(ContactModerationState)
            .filter(
                ContactModerationState.contact_id
                == contact_id
            )
            .delete(
                synchronize_session=False
            )
        )

        # ----------------------------------------------------
        # 4. TAREAS ADMINISTRATIVAS
        # ----------------------------------------------------

        tareas_borradas = (
            db.query(AdminPendingTask)
            .filter(
                AdminPendingTask.contact_id
                == contact_id
            )
            .delete(
                synchronize_session=False
            )
        )

        # ----------------------------------------------------
        # 5. MENSAJES
        # ----------------------------------------------------

        mensajes_borrados = (
            db.query(Message)
            .filter(
                Message.contact_id
                == contact_id
            )
            .delete(
                synchronize_session=False
            )
        )

        # ----------------------------------------------------
        # 6. ESTADO TEMPORAL EN MEMORIA
        # ----------------------------------------------------

        ADMIN_SELECTED_TASKS.clear()

        # Limpiar buffer pendiente de ese teléfono si existiera.
        with MESSAGE_BUFFER_LOCK:
            MESSAGE_BUFFERS.pop(
                numero,
                None,
            )

            MESSAGE_BUFFERS.pop(
                f"whatsapp:{numero}",
                None,
            )

        # ----------------------------------------------------
        # 7. CONTACTO
        # ----------------------------------------------------

        db.delete(contact)

        db.commit()

        return {
            "status": "contact_deleted",
            "phone": numero,
            "contact_id": contact_id,
            "messages_deleted": mensajes_borrados,
            "admin_tasks_deleted": tareas_borradas,
            "followup_events_deleted": (
                followup_events_borrados
            ),
            "followup_state_deleted": (
                followup_state_borrado
            ),
            "moderation_state_deleted": (
                moderation_state_borrado
            ),
        }

    except Exception as e:

        db.rollback()

        print(
            "❌ Error eliminando contacto de prueba: "
            f"contact_id={contact_id}, error={e}"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "No fue posible eliminar completamente "
                "el contacto de prueba."
            ),
        )
