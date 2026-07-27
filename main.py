from fastapi import FastAPI, Request, Form, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import os
import google.generativeai as genai
from twilio.rest import Client
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.sql import func
from fastapi.responses import HTMLResponse
import requests
import json
import base64
import re
import unicodedata

from sqlalchemy.dialects.postgresql import ENUM
from prompt_manager import PromptManager


LOCAL_TZ = ZoneInfo("America/Mexico_City")
prompt_manager = PromptManager()

FLOW_STATE_PREFIX = "FLOW_STATE:"
ADMIN_SELECTED_TASKS = {}

USE_STRUCTURED_AI_FLOW = (
    os.getenv("USE_STRUCTURED_AI_FLOW", "false")
    .strip()
    .lower()
    in ["true", "1", "yes", "si", "sí"]
)

# ============================================================
# CONTRATO DE ANÁLISIS ESTRUCTURADO DEL MENSAJE DEL PROSPECTO
# ============================================================

INTENCIONES_PRINCIPALES_VALIDAS = {
    "SALUDO",
    "PEDIR_INFORMES",
    "PEDIR_COSTOS",
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
    "PEDIR_ZONA",
    "CONTINUAR_INFORMES",
    "RESPONDER_TEMA",
    "RESPONDER_COSTOS",
    "INVITAR_CITA",
    "PEDIR_FECHA_CITA",
    "PEDIR_HORA_CITA",
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

    pausa_conversacion: bool = False

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

        "pausa_conversacion": normalizar_booleano(
            datos_crudos.get("pausa_conversacion")
        ),

        "datos_detectados": normalizar_lista_textos(
            datos_crudos.get("datos_detectados")
        ),
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
            "gemini-1.5-flash"
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


def generar_con_gemini_con_fallback(
    contenido,
    generation_config=None,
    tarea: str = "gemini"
):
    """
    Intenta generar contenido usando el modelo principal.
    Si falla, prueba modelos de respaldo.
    """
    ultimo_error = None

    for model_name in obtener_modelos_gemini():
        try:
            print(f"🧠 Probando Gemini para {tarea}: {model_name}")

            model = genai.GenerativeModel(model_name)

            if generation_config:
                response = model.generate_content(
                    contenido,
                    generation_config=generation_config
                )
            else:
                response = model.generate_content(contenido)

            print(f"✅ Gemini usado para {tarea}: {model_name}")
            return response, model_name

        except Exception as e:
            ultimo_error = e
            print(f"⚠️ Falló Gemini para {tarea} con {model_name}: {e}")

    raise RuntimeError(f"Todos los modelos Gemini fallaron para {tarea}. Último error: {ultimo_error}")

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
    Determina si la memoria histórica contiene datos útiles.
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

    if any(
        str(
            memoria.get(
                campo,
                "",
            )
            or ""
        ).strip()
        for campo in campos_texto
    ):
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

    if any(
        memoria.get(
            campo
        )
        for campo in campos_lista
    ):
        return True

    campos_booleanos = [
        "solicito_costos",
        "costos_presentados",
        "acepto_visita",
        "cita_solicitada",
        "cita_confirmada",
    ]

    return any(
        bool(
            memoria.get(
                campo
            )
        )
        for campo in campos_booleanos
    )


def extraer_memoria_historica_con_ia(
    texto_conversacion: str,
) -> Dict[str, Any]:
    """
    Analiza el historial completo con Gemini y devuelve
    una memoria histórica validada.

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

    modelos = obtener_modelos_gemini()

    if not modelos:
        resultado_fallo["errores"].append(
            "NO_HAY_MODELOS_CONFIGURADOS"
        )
        return resultado_fallo

    instrucciones_reintento = """

REINTENTO OBLIGATORIO:

La respuesta anterior no pudo validarse.

Devuelve exclusivamente JSON válido.
Respeta exactamente el contrato.
No uses Markdown.
No agregues explicaciones.
"""

    for indice, model_name in enumerate(
        modelos
    ):
        intentos_permitidos = (
            2
            if indice == 0
            else 1
        )

        for numero_intento in range(
            1,
            intentos_permitidos + 1,
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
                    f"modelo={model_name}, "
                    f"intento={numero_intento}"
                )

                model = genai.GenerativeModel(
                    model_name
                )

                response = model.generate_content(
                    prompt_intento,
                    generation_config=(
                        genai.types.GenerationConfig(
                            max_output_tokens=12000,
                            temperature=0.0,
                        )
                    ),
                )
                
                texto_respuesta = (
                    extraer_texto_respuesta_gemini(
                        response
                    )
                )

                if not texto_respuesta:
                    error = (
                        f"{model_name}: "
                        f"intento {numero_intento}: "
                        "RESPUESTA_VACIA"
                    )

                    resultado_fallo[
                        "errores"
                    ].append(
                        error
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
                        .replace("\n", "\\n")
                    )

                    muestra_final = (
                        texto_respuesta[-500:]
                        .replace("\n", "\\n")
                    )

                    razon_terminacion = ""

                    try:
                        razon_terminacion = str(
                            response.candidates[
                                0
                            ].finish_reason
                        )
                    except Exception:
                        razon_terminacion = (
                            "NO_DISPONIBLE"
                        )

                    print(
                        "⚠️ JSON histórico no válido | "
                        f"caracteres={len(texto_respuesta)} | "
                        f"finish_reason={razon_terminacion}"
                    )

                    print(
                        "⚠️ Inicio respuesta: "
                        f"{muestra_inicio}"
                    )

                    print(
                        "⚠️ Final respuesta: "
                        f"{muestra_final}"
                    )

                    error = (
                        f"{model_name}: "
                        f"intento {numero_intento}: "
                        "JSON_INVALIDO"
                    )
                    
                    resultado_fallo[
                        "errores"
                    ].append(
                        error
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
                        f"{model_name}: "
                        f"intento {numero_intento}: "
                        "MEMORIA_VACIA"
                    )

                    resultado_fallo[
                        "errores"
                    ].append(
                        error
                    )

                    continue

                return {
                    "exitoso": True,
                    "memoria": memoria,
                    "modelo_usado": model_name,
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
                    f"{model_name}: "
                    f"intento {numero_intento}: "
                    f"{e}"
                )

                resultado_fallo[
                    "errores"
                ].append(
                    error
                )

                print(
                    "⚠️ Error memoria histórica IA: "
                    f"{error}"
                )

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

    Estrategia:
    1. Intenta dos veces con el modelo principal.
    2. Intenta una vez con cada modelo de respaldo.
    3. Rechaza respuestas vacías, texto no JSON y contratos vacíos.
    4. Devuelve información de auditoría sin lanzar el error
       hacia el flujo conversacional.
    """
    modelos = obtener_modelos_gemini()

    resultado_fallo = {
        "exitoso": False,
        "analisis": crear_analisis_mensaje_vacio(),
        "modelo_usado": "",
        "intentos_realizados": 0,
        "errores": [],
    }

    if not modelos:
        resultado_fallo["errores"].append(
            "NO_HAY_MODELOS_CONFIGURADOS"
        )
        return resultado_fallo

    instrucciones_reintento = """

REINTENTO OBLIGATORIO:
La respuesta anterior no pudo validarse.

Devuelve exclusivamente un objeto JSON válido que cumpla
exactamente el contrato solicitado.

No uses Markdown.
No agregues explicaciones.
No dejes la respuesta vacía.
"""

    intentos_por_modelo = {}

    for indice, model_name in enumerate(modelos):
        intentos_permitidos = 2 if indice == 0 else 1
        intentos_por_modelo[model_name] = intentos_permitidos

    for model_name, intentos_permitidos in intentos_por_modelo.items():
        for numero_intento in range(
            1,
            intentos_permitidos + 1,
        ):
            resultado_fallo["intentos_realizados"] += 1

            prompt_intento = prompt_analisis

            if numero_intento > 1:
                prompt_intento += instrucciones_reintento

            try:
                print(
                    "🧠 Análisis estructurado: "
                    f"modelo={model_name}, "
                    f"intento={numero_intento}"
                )

                model = genai.GenerativeModel(
                    model_name
                )

                response = model.generate_content(
                    prompt_intento,
                    generation_config=(
                        genai.types.GenerationConfig(
                            max_output_tokens=3000,
                            temperature=0.0,
                        )
                    ),
                )

                texto_respuesta = (
                    extraer_texto_respuesta_gemini(
                        response
                    )
                )

                if not texto_respuesta:
                    error = (
                        f"{model_name}: intento "
                        f"{numero_intento}: "
                        "RESPUESTA_VACIA"
                    )
                    resultado_fallo["errores"].append(
                        error
                    )
                    print(f"⚠️ {error}")
                    continue

                datos_crudos = extraer_json_de_texto(
                    texto_respuesta
                )

                if datos_crudos is None:
                    error = (
                        f"{model_name}: intento "
                        f"{numero_intento}: "
                        "JSON_INVALIDO"
                    )
                    resultado_fallo["errores"].append(
                        error
                    )
                    print(f"⚠️ {error}")
                    continue

                analisis = normalizar_analisis_mensaje_ia(
                    datos_crudos
                )

                if not (
                    analisis_estructurado_contiene_informacion(
                        analisis
                    )
                ):
                    error = (
                        f"{model_name}: intento "
                        f"{numero_intento}: "
                        "CONTRATO_VACIO"
                    )
                    resultado_fallo["errores"].append(
                        error
                    )
                    print(f"⚠️ {error}")
                    continue

                print(
                    "✅ Análisis estructurado válido: "
                    f"modelo={model_name}, "
                    f"intento={numero_intento}"
                )

                return {
                    "exitoso": True,
                    "analisis": analisis,
                    "modelo_usado": model_name,
                    "intentos_realizados": (
                        resultado_fallo[
                            "intentos_realizados"
                        ]
                    ),
                    "errores": (
                        resultado_fallo["errores"]
                    ),
                }

            except Exception as e:
                error = (
                    f"{model_name}: intento "
                    f"{numero_intento}: {e}"
                )

                resultado_fallo["errores"].append(
                    error
                )

                print(
                    "⚠️ Error en análisis estructurado: "
                    f"{error}"
                )

    return resultado_fallo
    

def analizar_mensaje_prospecto_con_ia(
    mensaje_usuario: str,
    contact=None,
    history=None,
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

HISTORIAL RECIENTE:
{historial_texto}

MENSAJE ACTUAL DEL PROSPECTO:
{mensaje}

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

8. Debes interpretar el mensaje actual como continuación de la
conversación, no como un mensaje aislado.

Usa conjuntamente:

- el estado conversacional actual;
- el estatus comercial;
- los datos previos guardados;
- el historial reciente;
- la última promesa realizada por el asistente;
- la fecha y hora de cita recuperadas del contexto;
- el mensaje actual.

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
cuando pregunta costos, idiomas, dirección u otro tema mientras
existe una cita pendiente, pero su mensaje no es un seguimiento de
la confirmación.

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
- conserva la frase en "hora_cita_texto"
- conviértela a HH:MM en "hora_cita_24h" cuando sea inequívoca

10. Las visitas sólo se realizan de lunes a viernes.
Marca "dia_no_laboral": true cuando la fecha propuesta sea sábado
o domingo.

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
    Busca una localidad general mediante Google Places API (New).

    Esta función:
    - No solicita ni utiliza la ubicación exacta del prospecto.
    - No calcula todavía la distancia al colegio.
    - No modifica la base de datos.
    - No altera el flujo conversacional.
    - Devuelve una estructura segura incluso cuando la API falla.
    """
    resultado = {
        "encontrado": False,
        "consulta": "",
        "nombre": "",
        "direccion_formateada": "",
        "place_id": "",
        "latitud": None,
        "longitud": None,
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
        ) or ""
    ).strip()

    if not api_key:
        resultado["error"] = "GOOGLE_MAPS_API_KEY_NO_CONFIGURADA"
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
        "pageSize": 1,
    }

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

        except (ValueError, TypeError, AttributeError):
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

    if not isinstance(lugares, list) or not lugares:
        resultado["error"] = (
            "LOCALIDAD_NO_ENCONTRADA"
        )

        return resultado

    lugar = lugares[0]

    if not isinstance(lugar, dict):
        resultado["error"] = (
            "FORMATO_LUGAR_INVALIDO"
        )

        return resultado

    display_name = lugar.get(
        "displayName",
        {},
    )

    if isinstance(display_name, dict):
        nombre = str(
            display_name.get(
                "text",
                "",
            )
            or ""
        ).strip()
    else:
        nombre = ""

    location = lugar.get(
        "location",
        {},
    )

    if not isinstance(location, dict):
        location = {}

    latitud = location.get(
        "latitude"
    )

    longitud = location.get(
        "longitude"
    )

    try:
        latitud = (
            float(latitud)
            if latitud is not None
            else None
        )

        longitud = (
            float(longitud)
            if longitud is not None
            else None
        )

    except (TypeError, ValueError):
        latitud = None
        longitud = None

    resultado.update({
        "encontrado": bool(
            lugar.get("id")
            and latitud is not None
            and longitud is not None
        ),
        "nombre": nombre,
        "direccion_formateada": str(
            lugar.get(
                "formattedAddress",
                "",
            )
            or ""
        ).strip(),
        "place_id": str(
            lugar.get(
                "id",
                "",
            )
            or ""
        ).strip(),
        "latitud": latitud,
        "longitud": longitud,
    })

    if not resultado["encontrado"]:
        resultado["error"] = (
            "RESULTADO_SIN_COORDENADAS_COMPLETAS"
        )

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
        
def zona_previamente_validada_en_flujo(contact=None) -> bool:
    """
    Determina si la conversación ya superó la validación de zona.

    Por ahora se apoya en el estado conversacional existente.
    No modifica las notas ni la base de datos.
    """
    if contact is None:
        return False

    try:
        estado_actual = get_flow_state(contact)
    except Exception:
        return False

    estados_antes_de_validar_zona = {
        "",
        "SALUDO_INICIAL",
        "ESPERANDO_INTENCION",
        "ESPERANDO_REFERENCIA",
        "VALIDACION_ZONA",
        "VALIDACION_ZONA_OBLIGATORIA",
        "ZONA_INVALIDA_POTENCIAL_METEPEC",
        "CAMPUS_EXTERNO_NO_ATENDIBLE",
    }

    return estado_actual not in estados_antes_de_validar_zona


def construir_datos_detectados_para_decision(
    analisis: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Reúne solamente datos útiles que posteriormente podrían guardarse.

    Esta función no escribe todavía en contact.notes.
    """
    datos = {}

    if analisis.get("zona_mencionada"):
        datos["zona_mencionada"] = analisis["zona_mencionada"]

    if analisis.get("nivel"):
        datos["nivel"] = analisis["nivel"]

    if analisis.get("grado"):
        datos["grado"] = analisis["grado"]

    if analisis.get("edad_alumno") is not None:
        datos["edad_alumno"] = analisis["edad_alumno"]

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
        datos["nombre_tutor"] = analisis["nombre_tutor"]

    if analisis.get("nombre_alumno"):
        datos["nombre_alumno"] = analisis["nombre_alumno"]

    if analisis.get("fecha_cita_iso"):
        datos["fecha_cita_iso"] = analisis["fecha_cita_iso"]

    if analisis.get("hora_cita_24h"):
        datos["hora_cita_24h"] = analisis["hora_cita_24h"]

    return datos

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

def aplicar_reglas_negocio_estructuradas(
    analisis: Dict[str, Any],
    contact=None,
    mensaje_usuario: str = "",
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

    campus_mencionado = str(
        analisis_seguro.get(
            "campus_mencionado",
            "",
        ) or ""
    ).strip()

    clasificacion_zona_determinista = (
        clasificar_zona_determinista(
            mensaje_usuario=mensaje_usuario,
            zona_mencionada=zona_mencionada,
            campus_mencionado=campus_mencionado,
        )
    )

    decision["datos_detectados"][
        "clasificacion_zona_determinista"
    ] = clasificacion_zona_determinista

    validacion_geografica = None

    if clasificacion_zona_determinista.get(
        "requiere_validacion_geografica",
        False,
    ):
        localidad_para_validar = (
            zona_mencionada
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

    zona_valida_previamente = (
        zona_previamente_validada_en_flujo(
            contact
        )
    )

    zona_validada = (
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

    contexto_cita_pendiente = (
        contexto_cita_pendiente_determinista
        or contexto_cita_pendiente_ia
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
    # 5. PAUSA O CIERRE TEMPORAL
    # ========================================================

 
    if (
    analisis_seguro.get("pausa_conversacion")
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

    if (
        analisis_seguro.get("saludo_simple")
        and analisis_seguro.get("intencion_principal")
        == "SALUDO"
        and not analisis_seguro.get(
            "intenciones_secundarias"
        )
    ):
        decision.update({
            "accion": "RESPONDER_SALUDO",
            "motivo": (
                "El mensaje contiene únicamente un saludo."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": False,
        })

        return decision

    # ========================================================
    # 7. COSTOS: SIEMPRE PROTEGIDOS POR ZONA
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

            return decision

        decision.update({
            "accion": "RESPONDER_COSTOS",
            "motivo": (
                "El prospecto pidió costos y la zona ya fue "
                "validada."
            ),
            "requiere_admin": False,
            "puede_compartir_costos": True,
        })

        return decision

    # ========================================================
    # 8. FLUJO DE CITA
    # ========================================================

    intenciones_cita = {
        "PEDIR_CITA",
        "PROPONER_FECHA_CITA",
        "PROPONER_HORA_CITA",
    }

    tiene_intencion_cita = (
        analisis_seguro.get("pide_cita")
        or analisis_seguro.get("intencion_principal")
        in intenciones_cita
        or any(
            intencion in intenciones_cita
            for intencion in analisis_seguro.get(
                "intenciones_secundarias",
                [],
            )
        )
    )

    if tiene_intencion_cita:
        fecha_cita = analisis_seguro.get(
            "fecha_cita_iso",
            "",
        )

        hora_cita = analisis_seguro.get(
            "hora_cita_24h",
            "",
        )

        if not fecha_cita:
            decision.update({
                "accion": "PEDIR_FECHA_CITA",
                "motivo": (
                    "El prospecto quiere una cita, pero no "
                    "proporcionó una fecha."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
            })

            return decision

        if not hora_cita:
            decision.update({
                "accion": "PEDIR_HORA_CITA",
                "motivo": (
                    "El prospecto proporcionó fecha, pero "
                    "todavía falta el horario."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
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
            })

            return decision

        if clasificacion_horario == "FUERA":
            decision.update({
                "accion": "CITA_FUERA_HORARIO",
                "motivo": (
                    "El horario solicitado está fuera del "
                    "rango disponible para visitas."
                ),
                "requiere_admin": False,
                "puede_compartir_costos": zona_validada,
            })

            return decision

        if clasificacion_horario == "EVALUAR":
            decision.update({
                "accion": "CONSULTAR_ADMIN",
                "motivo": (
                    "El horario solicitado es posterior a "
                    "las 13:00 y debe ser evaluado por "
                    "administración."
                ),
                "requiere_admin": True,
                "puede_compartir_costos": zona_validada,
            })

            return decision

        decision.update({
            "accion": "CONSULTAR_ADMIN",
            "motivo": (
                "El prospecto proporcionó una fecha y un "
                "horario regular; la disponibilidad debe "
                "confirmarla una persona administradora."
            ),
            "requiere_admin": True,
            "puede_compartir_costos": zona_validada,
        })

        return decision

    # ========================================================
    # 8. DATOS POSTERIORES A LA CONFIRMACIÓN DE CITA
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
    # 9. RESPUESTA DE ZONA VÁLIDA
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
    # 10. TEMA EDUCATIVO
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
    # 11. INFORMES GENERALES
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

    if accion == "PEDIR_FECHA_CITA":
        plan.update({
            "objetivo": (
                "Solicitar el día de lunes a viernes en que "
                "la familia desea visitar el colegio."
            ),
            "debe_incluir": [
                "Una pregunta concreta para conocer la fecha deseada.",
            ],
        })

        return plan

    if accion == "PEDIR_HORA_CITA":
        plan.update({
            "objetivo": (
                "Solicitar el horario en que la familia desea "
                "realizar la visita."
            ),
            "debe_incluir": [
                "Una pregunta concreta para conocer el horario deseado.",
            ],
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

    if accion in [
        "CONTINUAR_INFORMES",
        "RESPONDER_TEMA",
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
            return "¡Hola! Con gusto le atendemos."

        if accion == "PEDIR_FECHA_NACIMIENTO":
            return (
                "¿Me comparte, por favor, la fecha de nacimiento "
                "completa del alumno, incluyendo día, mes y año?"
            )

        if accion == "PEDIR_FECHA_CITA":
            return (
                "¿Qué día de lunes a viernes le gustaría visitar "
                "el colegio?"
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

        palabras_respuesta = re.findall(
            r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+",
            texto_sin_signos_finales,
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

INSTRUCCIONES:

- Respeta la decisión de Python.
- Cumple el objetivo y las restricciones del plan.
- Escribe una sola respuesta breve, cordial y natural.
- No muestres análisis, pasos, listas ni razonamientos.
- No uses encabezados.
- No uses Markdown.
- No uses numeraciones.
- No menciones acciones o clasificaciones internas.
- No menciones Google, rutas, coordenadas ni distancias.
- No inventes costos, fechas, disponibilidad ni datos.
- Devuelve exclusivamente el texto para WhatsApp.
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
                            max_output_tokens=1300,
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
        )

        analisis_fallo = (
            analisis.get("intencion_principal") == "OTRO"
            and analisis.get("confianza", 0.0) == 0.0
            and not analisis.get("datos_detectados")
            and not analisis.get("zona_mencionada")
            and not analisis.get("nivel")
        )
        
        if analisis_fallo:
            decision_fallback = (
                crear_decision_negocio_vacia()
            )

            decision_fallback.update({
                "accion": "FALLBACK_CONVERSACIONAL",
                "motivo": (
                    "Gemini no devolvió un análisis válido "
                    "después de los reintentos automáticos."
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
                    "ANALISIS_IA_INVALIDO_RECUPERADO"
                ),
            }
            
        decision = aplicar_reglas_negocio_estructuradas(
            analisis=analisis,
            contact=contact,
            mensaje_usuario=mensaje,
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
    # DATOS DEL ALUMNO
    # --------------------------------------------------------

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
        alumno = {
            "nombre": nombre_alumno,
            "nivel_interes": nivel_interes,
            "grado_interes": grado_interes,
            "edad": edad_alumno,
            "fecha_nacimiento": fecha_nacimiento,
        }

        contexto["alumnos"] = [
            alumno
        ]

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

    etapa_sugerida = str(
        memoria.get(
            "etapa_conversacional_sugerida",
            "",
        )
        or ""
    ).strip().upper()

    if (
        etapa_sugerida
        in ETAPAS_CONVERSACIONALES_VALIDAS
    ):
        contexto_base[
            "etapa_conversacional"
        ] = etapa_sugerida

    estado_sugerido = str(
        memoria.get(
            "estado_comercial_sugerido",
            "",
        )
        or ""
    ).strip().upper()

    if (
        estado_sugerido
        in ESTADOS_COMERCIALES_VALIDOS
    ):
        contexto_base[
            "estado_comercial"
        ] = estado_sugerido

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
        "hitos_comerciales": (
            "hitos_comerciales"
        ),
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

        guardar_valor(
            "NOMBRE_ALUMNO",
            analisis.get("nombre_alumno"),
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
                max_output_tokens=300,
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

def normalizar_numero_whatsapp(numero: str) -> str:
    """
    Normaliza números de WhatsApp para comparar.
    """
    numero = (numero or "").strip()

    if numero.startswith("whatsapp:"):
        numero = numero.replace("whatsapp:", "", 1)

    return numero.strip()


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
                max_output_tokens=300,
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
        "PROSPECTO_INFORMADO", 
        "VISITA_AGENDADA", 
        "INSCRIPCION_PENDIENTE", 
        "ALUMNO_ACTIVO", 
        "ALUMNO_INACTIVO", 
        "COMPETENCIA", 
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
# RECUPERACIÓN COMPLETA DEL HISTORIAL CONVERSACIONAL
# ============================================================

def obtener_historial_completo_contacto(
    db: Session,
    contact,
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
        mensajes = (
            db.query(Message)
            .filter(
                Message.contact_id == contact.id
            )
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
    

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(""),
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
        if es_numero_admin(From):
            print("👑 Mensaje recibido desde WhatsApp maestro/admin")
            return procesar_respuesta_admin(db, From, mensaje_entrada)

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
        
            contact = get_or_create_contact(db, From)
        
            resultado = enviar_respuesta_twilio(From, respuesta)
        
            twilio_sid = None
            if "SID:" in resultado:
                twilio_sid = resultado.split("SID: ")[1].strip()
        
            save_message(db, contact.id, 'incoming', mensaje_entrada)
            save_message(db, contact.id, 'outgoing', respuesta, twilio_sid)
        
            print(f"🤖 BOT (fallback audio): {respuesta}")
            print(f"📤 Estado: {resultado}")
        
            return {"status": "processed_audio_fallback", "contact_id": contact.id}
        
        print(f"\n{'='*60}")
        print(f"💬 WHATSAPP CHAT - {datetime.now().strftime('%H:%M:%S')}")
        print(f"📱 De: {From}")
        print(f"👤 USUARIO: {mensaje_entrada}")
        print(f"{'-'*40}")

        contact = get_or_create_contact(db, From)
        save_message(db, contact.id, 'incoming', mensaje_entrada)
        
        estado_flujo_actual = get_flow_state(contact)
        
        if estado_flujo_actual == "ESPERANDO_DATOS_CITA":
            return procesar_datos_registro_cita(db, contact, From, mensaje_entrada)
        
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

        respuesta, estado_actual, estado_siguiente = generar_respuesta_inteligente(mensaje_entrada, contact, history)
        
        print(f"🧭 Estado flujo usado para responder: {estado_actual}")
        print(f"➡️ Estado flujo siguiente: {estado_siguiente}")

        resultado = enviar_respuesta_twilio(From, respuesta)

        twilio_sid = None
        if "SID:" in resultado:
            twilio_sid = resultado.split("SID: ")[1].strip()

        save_message(db, contact.id, 'outgoing', respuesta, twilio_sid)
        
        set_flow_state(contact, estado_siguiente)
        db.commit()
        
        if detecta_condicion_consulta_admin(respuesta):
            tarea_admin = crear_tarea_admin_pendiente(db, contact, mensaje_entrada, respuesta)
            enviar_alerta_admin_whatsapp(contact, mensaje_entrada, respuesta, tarea_admin.id)
        
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


# Configuración del negocio
NEGOCIO_INFO = """
Eres el asistente virtual del Colegio. 
Información clave:
- Horarios: Lunes a Viernes 7am-3pm
- Ubicación: [TU DIRECCIÓN AQUÍ]
- Servicios: Primaria, Secundaria
- Costo inscripción: $5,000 MXN
- Agendar visita: https://calendly.com/tu-colegio
Responde solo con esta información. Si no sabes algo, di: 'Te ayudo a agendar una cita.'
"""

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
    
    db.commit()
    return message

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
                max_output_tokens=4000,
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

def procesar_escalacion_admin_estructurada(
    db: Session,
    contact,
    mensaje_usuario: str,
    respuesta_bot: str,
    resultado_orquestador: Dict[str, Any],
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
            or fecha_cita_contacto
        ),
        "hora_cita": (
            hora_cita_analisis
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
    

def enviar_alerta_admin_whatsapp(contact, mensaje_usuario: str, respuesta_bot: str, tarea_id: int = None) -> str:
    """
    Envía una alerta interna al administrador cuando una conversación
    requiere atención humana.
    """
    admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER")

    if not admin_number:
        print("⚠️ ADMIN_WHATSAPP_NUMBER no configurado; no se envió alerta interna")
        return "ADMIN_WHATSAPP_NUMBER no configurado"

    phone = contact.phone_number if contact else "Teléfono no disponible"
    tarea_txt = f"\nID pendiente: {tarea_id}\n" if tarea_id else ""

    mensaje_alerta = f"""🔔 Atención requerida

Un prospecto está esperando confirmación de disponibilidad para visita.
{tarea_txt}
Teléfono: {phone}

Último mensaje del prospecto:
{mensaje_usuario}

Respuesta del bot:
{respuesta_bot}

Si sólo hay una conversación pendiente, puede responder directamente con la indicación que desea enviar al prospecto.

Si hay varias conversaciones pendientes, primero se le pedirá elegir a cuál responder.

Ejemplo:
Confirmar lunes 11am

La IA adaptará su respuesta antes de enviarla al prospecto.

Revisar conversación:
https://fastapi-production-efb5.up.railway.app/panel"""

    resultado = enviar_respuesta_twilio(admin_number, mensaje_alerta)
    
    print(f"📣 Alerta interna enviada a: {admin_number}")
    print(f"📣 Resultado alerta interna: {resultado}")
    
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
                max_output_tokens=300,
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
    

def construir_solicitud_datos_cita(contact) -> str:
    """
    Construye el mensaje para pedir datos después de confirmar la cita.
    """
    nivel_interes = get_note_value(contact, "NIVEL_INTERES")

    if nivel_interes:
        complemento_grado = ""
    else:
        complemento_grado = "\n\nTambién, ¿me podría confirmar para qué grado o nivel educativo está interesado?"

    return f"""Para registrar su cita, ¿me podría ayudar por favor con su nombre completo y el nombre completo de su hijo(a)?

De esta manera podremos tenerlos registrados y dedicarles el tiempo que requieren.{complemento_grado}"""


def extraer_hora_cita_confirmada(mensaje_confirmacion: str, respaldo: str = "") -> str:
    """
    Extrae una frase breve con día y hora de la cita a partir del mensaje confirmado.
    """
    texto = (mensaje_confirmacion or "").strip()
    respaldo = (respaldo or "").strip()

    if not GEMINI_API_KEY:
        return respaldo or texto[:120]

    prompt = f"""
Extrae únicamente el día y hora de la cita del siguiente mensaje.

MENSAJE:
{texto}

RESPALDO:
{respaldo}

REGLAS:
- Responde sólo con una frase breve.
- Ejemplo: lunes a las 8:00 a.m.
- Si no encuentras día y hora completos, usa el respaldo.
- No expliques nada.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=300,
                temperature=0.0
            ),
            tarea="extracción hora cita"
        )

        resultado = extraer_texto_respuesta_gemini(response).strip()
        return resultado or respaldo or texto[:120]

    except Exception as e:
        print(f"⚠️ Error extrayendo hora de cita: {e}")
        return respaldo or texto[:120]


def extraer_datos_registro_cita(mensaje_usuario: str, contact) -> dict:
    """
    Extrae nombre del padre/madre/tutor, nombre del alumno y grado/nivel.
    """
    texto = (mensaje_usuario or "").strip()
    nivel_conocido = get_note_value(contact, "NIVEL_INTERES")

    datos = {
        "padres": "",
        "alumno": "",
        "grado": nivel_conocido or ""
    }

    if not texto:
        return datos

    if not GEMINI_API_KEY:
        return datos

    prompt = f"""
Extrae datos de registro de cita escolar desde el siguiente mensaje de WhatsApp.

MENSAJE DEL PROSPECTO:
{texto}

GRADO O NIVEL YA CONOCIDO:
{nivel_conocido or "No especificado"}

TAREA:
Devuelve únicamente un JSON válido con estas claves:
{{
  "padres": "",
  "alumno": "",
  "grado": ""
}}

REGLAS:
- "padres" debe ser el nombre de la mamá, papá o tutor que agenda.
- "alumno" debe ser el nombre del niño, niña o alumno.
- "grado" debe ser el grado o nivel de interés.
- Si el grado ya está conocido, úsalo.
- Si algún dato no aparece, déjalo como cadena vacía.
- No inventes apellidos.
- No agregues explicaciones fuera del JSON.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=300,
                temperature=0.0
            ),
            tarea="extracción datos cita"
        )

        texto_respuesta = extraer_texto_respuesta_gemini(response).strip()

        texto_respuesta = texto_respuesta.replace("```json", "").replace("```", "").strip()

        datos_ia = json.loads(texto_respuesta)

        datos["padres"] = (datos_ia.get("padres") or "").strip()
        datos["alumno"] = (datos_ia.get("alumno") or "").strip()
        datos["grado"] = (datos_ia.get("grado") or nivel_conocido or "").strip()

        return datos

    except Exception as e:
        print(f"⚠️ Error extrayendo datos de cita: {e}")
        return datos


def construir_resumen_cita_admin(contact) -> str:
    """
    Construye el resumen final que se envía al WhatsApp maestro.
    """
    padres = get_note_value(contact, "NOMBRE_PADRES")
    alumno = get_note_value(contact, "NOMBRE_ALUMNO")
    grado = get_note_value(contact, "GRADO_INTERES") or get_note_value(contact, "NIVEL_INTERES")
    hora_cita = get_note_value(contact, "HORA_CITA")

    return f"""📌 Cita registrada

Padres: {padres or "Pendiente"}
Cel: {contact.phone_number}
Alumno:
{alumno or "Pendiente"}
{grado or "Pendiente"}
Hora cita: {hora_cita or "Pendiente"}"""


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


def procesar_datos_registro_cita(db: Session, contact, from_number: str, mensaje_usuario: str):
    """
    Procesa la respuesta del prospecto cuando ya se le pidieron datos para registrar cita.
    """
    datos = extraer_datos_registro_cita(mensaje_usuario, contact)

    if datos.get("padres"):
        set_note_value(contact, "NOMBRE_PADRES", datos["padres"])

    if datos.get("alumno"):
        set_note_value(contact, "NOMBRE_ALUMNO", datos["alumno"])

    if datos.get("grado"):
        set_note_value(contact, "GRADO_INTERES", datos["grado"])

    db.commit()

    padres = get_note_value(contact, "NOMBRE_PADRES")
    alumno = get_note_value(contact, "NOMBRE_ALUMNO")
    grado = get_note_value(contact, "GRADO_INTERES") or get_note_value(contact, "NIVEL_INTERES")

    faltantes = []

    if not padres:
        faltantes.append("su nombre completo")

    if not alumno:
        faltantes.append("el nombre completo de su hijo(a)")

    if not grado:
        faltantes.append("el grado o nivel de interés")

    if faltantes:
        if len(faltantes) == 1:
            faltantes_texto = faltantes[0]
        else:
            faltantes_texto = ", ".join(faltantes[:-1]) + " y " + faltantes[-1]

        respuesta = f"""Muchas gracias.

Para completar el registro de su cita, ¿me podría apoyar también con {faltantes_texto}?"""

        resultado = enviar_respuesta_twilio(from_number, respuesta)

        twilio_sid = None
        if "SID:" in resultado:
            twilio_sid = resultado.split("SID: ")[1].strip()

        save_message(db, contact.id, "outgoing", respuesta, twilio_sid)

        print(f"📌 Datos de cita incompletos. Faltan: {faltantes}")
        return {"status": "datos_cita_incompletos"}

    respuesta = """Muchas gracias.

Su cita queda registrada. Le esperamos con mucho gusto."""

    resultado = enviar_respuesta_twilio(from_number, respuesta)

    twilio_sid = None
    if "SID:" in resultado:
        twilio_sid = resultado.split("SID: ")[1].strip()

    save_message(db, contact.id, "outgoing", respuesta, twilio_sid)

    set_flow_state(contact, "CITA_DATOS_COMPLETOS")
    contact.status = "VISITA_AGENDADA"
    db.commit()

    enviar_resumen_cita_admin_whatsapp(contact)

    print("📌 Datos de cita completos y resumen enviado al admin")

    return {"status": "datos_cita_completos"}
    

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
- Si el administrador propone otro horario disponible, explica que ese horario está disponible y pide confirmación.
- Si el administrador propone alternativas sin confirmar disponibilidad definitiva, preséntalas como opciones posibles y pide al prospecto cuál le acomoda mejor.
- Si el administrador rechaza la disponibilidad, pide una alternativa de día u hora.
- Mantén formato WhatsApp con bloques cortos.
- No inventes datos que no estén en la respuesta del administrador.
- Respeta exactamente el día, hora, condición o alternativa indicada por el administrador.
- Si el administrador dice que un día u horario no está disponible, explícalo claramente.
- Si el administrador propone alternativas, inclúyelas de forma clara.
- No omitas información importante de la respuesta interna del administrador.
- No dejes frases incompletas.
- No termines el mensaje con frases como "se encontrará", "a las", "para", "con", "que", "de" o "en".
- Antes de responder, verifica que el mensaje final tenga sentido completo.
- Responde sólo con el mensaje final para el prospecto.
"""

    try:
        response, modelo_usado = generar_con_gemini_con_fallback(
            prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=1200,
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

        # NUEVO:
        # Si sólo hay una conversación pendiente y el admin escribe texto,
        # tomamos ese texto como respuesta directa para ese único prospecto.
        if len(tareas) == 1:
            tarea = tareas[0]
            ADMIN_SELECTED_TASKS[admin_key] = tarea.id
            tarea_id_seleccionada = tarea.id

            print(f"✅ Admin respondió directo; se usará la única tarea pendiente {tarea.id}")

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

    mensaje_para_prospecto = redactar_respuesta_admin_para_prospecto(mensaje_limpio, tarea)

    # Si el admin está confirmando definitivamente la cita,
    # primero enriquecemos el mensaje antes de enviarlo al prospecto.
    if admin_confirma_cita_final(mensaje_limpio, tarea):
        contact.status = "VISITA_AGENDADA"

        # Agrega fecha exacta si el mensaje dice algo como:
        # "este lunes a las 8:00 a.m."
        # para convertirlo en:
        # "este lunes 20 de julio a las 8:00 a.m."
        mensaje_para_prospecto = enriquecer_fecha_cita_en_mensaje(mensaje_para_prospecto)

        hora_cita = extraer_hora_cita_confirmada(
            mensaje_para_prospecto,
            respaldo=tarea.trigger_message or ""
        )

        if hora_cita:
            set_note_value(contact, "HORA_CITA", hora_cita)

        solicitud_datos = construir_solicitud_datos_cita(contact)

        if "nombre completo" not in mensaje_para_prospecto.lower():
            mensaje_para_prospecto = f"""{mensaje_para_prospecto}

{solicitud_datos}"""

        set_flow_state(contact, "ESPERANDO_DATOS_CITA")

    print(f"👑 Texto admin original: {repr(mensaje_limpio)}")
    print(f"👑 Mensaje final para prospecto: {repr(mensaje_para_prospecto)}")

    prospecto_to = f"whatsapp:{contact.phone_number}"

    resultado_envio = enviar_respuesta_twilio(prospecto_to, mensaje_para_prospecto)

    twilio_sid = None
    if "SID:" in resultado_envio:
        twilio_sid = resultado_envio.split("SID: ")[1].strip()

    save_message(db, contact.id, "outgoing", mensaje_para_prospecto, twilio_sid)

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
        
        <div class="messages-container" id="messagesContainer">""")
    
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
        
        <div class="footer">
            <a href="/panel" class="footer-link">← Volver al Panel</a>
            <span style="color: #ccc;">•</span>
            <a href="/contacts" class="footer-link">Ver Todos los Contactos</a>
            <span style="color: #ccc;">•</span>
            <a href="/" class="footer-link">Inicio</a>
        </div>
        
        <script>
            // Auto-scroll al final
            window.onload = function() {
                const container = document.getElementById('messagesContainer');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            };
            
            // Hotkey ESC para volver
            document.onkeydown = function(e) {
                if (e.key === 'Escape') {
                    window.location.href = '/panel';
                }
            };
        </script>
    </body>
    </html>
    """)
    
    return HTMLResponse(content=''.join(html_parts))

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
def reset_contact(phone: str = "+5215548123885", db: Session = Depends(get_db)):
    """
    Borra de forma segura un contacto de prueba, incluyendo:
    - mensajes
    - tareas pendientes de administrador
    - selección temporal de admin si aplica
    - contacto
    """
    numero = (phone or "").strip()

    if numero.startswith("whatsapp:"):
        numero = numero.replace("whatsapp:", "", 1)

    contact = db.query(Contact).filter(Contact.phone_number == numero).first()

    if not contact:
        return {
            "status": "not_found",
            "phone": numero
        }

    contact_id = contact.id

    # Primero borrar tareas admin relacionadas, porque dependen del contacto.
    tareas_borradas = db.query(AdminPendingTask).filter(
        AdminPendingTask.contact_id == contact_id
    ).delete(synchronize_session=False)

    # Luego borrar mensajes.
    mensajes_borrados = db.query(Message).filter(
        Message.contact_id == contact_id
    ).delete(synchronize_session=False)

    # Limpiar selección temporal del admin si alguna apuntaba a tareas borradas.
    ADMIN_SELECTED_TASKS.clear()

    # Finalmente borrar contacto.
    db.delete(contact)
    db.commit()

    return {
        "status": "contact_deleted",
        "phone": numero,
        "contact_id": contact_id,
        "messages_deleted": mensajes_borrados,
        "admin_tasks_deleted": tareas_borradas
    }

