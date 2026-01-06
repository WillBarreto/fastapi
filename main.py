from fastapi import FastAPI, Request, Form, Depends, HTTPException
from pydantic import BaseModel
import os
import google.generativeai as genai
from twilio.rest import Client
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.sql import func
from fastapi.responses import HTMLResponse 
import requests
import json
from sqlalchemy.dialects.postgresql import ENUM

def formatear_fecha_para_mensaje(dt: datetime) -> str:
    """Formatea fecha para mostrar en mensajes - USANDO ZONA HORARIA MÉXICO"""
    # Definir offset para México (UTC-6)
    # IMPORTANTE: Esto no considera horario de verano automáticamente
    # Para CDMX: UTC-6 en invierno, UTC-5 en verano
    
    # Determinar si estamos en horario de verano (aproximado)
    # En México: primer domingo de abril a último domingo de octubre
    hoy = datetime.now()
    es_horario_verano = False
    
    # Simple aproximación (no es 100% exacta pero funciona para la mayoría de los casos)
    if 4 <= hoy.month <= 10:
        es_horario_verano = True
    elif hoy.month == 4 and hoy.day >= 7:  # Después del primer domingo de abril
        es_horario_verano = True
    elif hoy.month == 10 and hoy.day <= 28:  # Antes del último domingo de octubre
        es_horario_verano = True
    
    # Ajustar offset
    offset_horas = -5 if es_horario_verano else -6
    
    # Aplicar offset
    dt_local = dt + timedelta(hours=offset_horas)
    
    # Fechas de referencia
    hoy_local = datetime.now() + timedelta(hours=offset_horas)
    fecha_hoy = hoy_local.date()
    fecha_ayer = fecha_hoy - timedelta(days=1)
    fecha_msg = dt_local.date()
    
    # Formatear hora en formato 12h (7:00 PM)
    hora = dt_local.hour
    minutos = dt_local.minute
    
    # Determinar AM/PM
    if hora < 12:
        periodo = "a.m."
    else:
        periodo = "p.m."
    
    # Convertir a formato 12h
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
        return f"{dt_local.day} {meses[dt_local.month-1]} {hora_str}"

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

# ================= APLICACIÓN FASTAPI =================
app = FastAPI(title="WhatsApp Bot CRM", version="1.0.0")

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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
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
            first_contact=datetime.now(),
            last_contact=datetime.now(),
            total_messages=0
        )
        db.add(contact)
        db.commit()
        db.refresh(contact)
    
    return contact

def save_message(db: Session, contact_id: int, direction: str, content: str, twilio_sid: str = None):
    """Guarda un mensaje en la base de datos"""
    # Usar datetime estándar (la BD guardará en UTC)
    timestamp = datetime.now()
    
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
        contact.last_contact = datetime.now()
    
    db.commit()
    return message

def get_conversation_history(db: Session, phone_number: str, limit: int = 10):
    """Obtiene el historial de conversación de un contacto"""
    contact = db.query(Contact).filter(Contact.phone_number == phone_number).first()
    if not contact:
        return []
    
    messages = db.query(Message).filter(Message.contact_id == contact.id)\
        .order_by(Message.timestamp.desc())\
        .limit(limit)\
        .all()
    
    return messages[::-1]  # Invertir para orden cronológico

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
        # Verificar conexión a BD
        db.execute(text("SELECT 1"))
        db_status = "✅ Conectada"
        
        # Estadísticas
        total_contacts = db.query(Contact).count()
        total_messages = db.query(Message).count()
        
    except Exception as e:
        db_status = f"❌ Error: {str(e)}"
        total_contacts = 0
        total_messages = 0
    
    # Verificar Gemini
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
    
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        # ================= LOG EN CONSOLA =================
        print(f"\n{'='*60}")
        print(f"💬 WHATSAPP CHAT - {datetime.now().strftime('%H:%M:%S')}")
        print(f"📱 De: {From}")
        print(f"👤 USUARIO: {Body}")
        print(f"{'-'*40}")
        
        # ================= GESTIÓN DE CONTACTO =================
        # Obtener o crear contacto
        contact = get_or_create_contact(db, From)
        
        # Guardar mensaje entrante
        save_message(db, contact.id, 'incoming', Body)
        
        # ================= OBTENER HISTORIAL =================
        history = get_conversation_history(db, From, limit=5)
        
        # ================= GENERAR RESPUESTA CON GEMINI =================
        print(f"🧠 Usando Gemini: {bool(GEMINI_API_KEY)}")
        print(f"📊 Historial disponible: {len(history)} mensajes")
        respuesta = generar_respuesta_inteligente(Body, contact, history)
        
        # ================= ENVIAR RESPUESTA =================
        resultado = enviar_respuesta_twilio(From, respuesta)
        
        # Extraer SID si está disponible
        twilio_sid = None
        if "SID:" in resultado:
            twilio_sid = resultado.split("SID: ")[1].strip()
        
        # ================= GUARDAR RESPUESTA =================
        save_message(db, contact.id, 'outgoing', respuesta, twilio_sid)
        
        # ================= LOG DE RESPUESTA =================
        print(f"🤖 BOT: {respuesta}")
        print(f"🤖 Motor: {'Gemini' if GEMINI_API_KEY else 'Predeterminado'}")
        print(f"📤 Estado: {resultado}")
        print(f"👤 Estado contacto: {contact.status}")
        print(f"📊 Total mensajes: {contact.total_messages}")
        print(f"{'='*60}\n")
        
        return {"status": "processed", "contact_id": contact.id}
    
    except Exception as e:
        print(f"❌ Error en webhook: {e}")
        return {"status": "error", "detail": str(e)}


def generar_respuesta_gemini(mensaje_usuario: str, contact, history) -> str:
    """Genera respuesta usando Gemini API"""
    
    if not GEMINI_API_KEY:
        print("⚠️  Gemini API Key no configurada, usando respuestas predeterminadas")
        return generar_respuesta_predeterminada(mensaje_usuario, contact)
    
    # Construir el contexto del historial
    historial_contexto = ""
    if history:
        historial_contexto = "Historial reciente:\n"
        # Usar slicing en lugar de copiar la lista
        start_idx = max(0, len(history) - 5)
        for msg in history[start_idx:]:
            prefix = "Usuario" if msg.direction == "incoming" else "Asistente"
            # CORREGIDO: Usar slicing seguro
            contenido_truncado = msg.content[:200] + "..." if len(msg.content) > 200 else msg.content
            historial_contexto += f"{prefix}: {contenido_truncado}\n"
    
    # Construir el prompt (mismo que antes, solo cambia el motor)
    prompt = f"""
Eres el asistente virtual del Colegio. Tu nombre es "Colegio Bot".

INFORMACIÓN DEL COLEGIO (NO INVENTES NADA MÁS):
- Horarios: Lunes a Viernes 7:00 am a 3:00 pm
- Ubicación: [DIRECCIÓN COMPLETA AQUÍ]
- Servicios: Primaria y Secundaria
- Costo inscripción: $5,000 MXN
- Agendar visita: https://calendly.com/tu-colegio

CONTEXTO DEL CONTACTO:
- Estado: {contact.status}
- Total mensajes previos: {contact.total_messages}

{historial_contexto}

Mensaje actual del usuario: "{mensaje_usuario}"

INSTRUCCIONES:
1. Responde solo con la información del colegio proporcionada
2. Mantén un tono amable y profesional
3. Sé conciso (máximo 2 oraciones)
4. Si el usuario pregunta algo fuera de la información proporcionada, invítale a agendar una cita
5. NO inventes información sobre horarios, precios o servicios no mencionados
6. Si es un saludo inicial, preséntate brevemente

Respuesta:
"""
    
    try:
        # Inicializar el modelo de Gemini
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        # Generar respuesta
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=150,
                temperature=0.7
            )
        )
        
        respuesta = response.text.strip()
        print(f"🤖 Gemini respuesta: {respuesta[:100]}...")
        return respuesta
        
    except Exception as e:
        print(f"❌ Excepción en Gemini: {e}")
        return generar_respuesta_predeterminada(mensaje_usuario, contact)

def generar_respuesta_predeterminada(mensaje: str, contact) -> str:
    """Respuestas predeterminadas como fallback"""
    mensaje = mensaje.lower().strip()
    
    # Si es un contacto con historial, personalizar respuesta
    if contact.total_messages > 1:
        if contact.status == "COMPETENCIA":
            return "Gracias por tu interés nuevamente. Te invito a agendar una visita para conocer nuestras instalaciones personalmente: https://calendly.com/tu-colegio"
        
        if contact.status == "PROSPECTO_INFORMADO":
            return "Ya te hemos proporcionado la información básica. ¿Te gustaría agendar una visita para conocer nuestras instalaciones?"
    
    # Respuestas basadas en palabras clave
    if any(palabra in mensaje for palabra in ["hola", "buenos días", "buenas tardes"]):
        if contact.total_messages == 1:
            return "¡Hola! Soy el asistente virtual del Colegio. ¿Es tu primera vez en contacto con nosotros?"
        else:
            return f"¡Hola de nuevo! Veo que ya hemos conversado antes ({contact.total_messages} mensajes). ¿En qué más puedo ayudarte?"
    
    elif any(palabra in mensaje for palabra in ["horario", "horarios", "abierto", "cierran"]):
        return "Horarios: Lunes a Viernes de 7:00 am a 3:00 pm"
    
    elif any(palabra in mensaje for palabra in ["ubicación", "dirección", "donde están", "dónde"]):
        return "📍 Estamos ubicados en: [TU DIRECCIÓN COMPLETA AQUÍ]"
    
    elif any(palabra in mensaje for palabra in ["costo", "precio", "inscripción", "cuota"]):
        return "💰 Costo de inscripción: $5,000 MXN. ¿Te gustaría agendar una cita para más detalles?"
    
    elif any(palabra in mensaje for palabra in ["cita", "visita", "agendar", "calendario"]):
        return "📅 Puedes agendar una visita en: https://calendly.com/tu-colegio"
    
    elif any(palabra in mensaje for palabra in ["servicios", "niveles", "grados", "primaria", "secundaria"]):
        return "🏫 Ofrecemos: Primaria y Secundaria. Educación de calidad con enfoque integral."
    
    # Respuesta por defecto
    return "¡Hola! Soy el asistente del Colegio. Puedo ayudarte con:\n• Horarios\n• Ubicación\n• Costos\n• Agendar visitas\n\n¿En qué necesitas información?"


def generar_respuesta_inteligente(mensaje: str, contact, history):
    """Función principal que decide qué motor de respuesta usar"""
    # Usar Gemini si está configurado, sino usar respuestas predeterminadas
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
        
        conversation_simple.append({
            "tipo": message_type,
            "texto": msg.content,
            "hora": msg.timestamp.strftime("%H:%M"),  # Solo hora:minutos
            "fecha": msg.timestamp.strftime("%d/%m/%Y")  # Para agrupación visual
        })
    
    return {
        "contacto": {
            "telefono": contact.phone_number,
            "estado": contact.status,
            "total_mensajes": contact.total_messages,
            "ultimo_contacto": contact.last_contact.strftime("%d/%m/%Y %H:%M")
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
                "last_contact": contact.last_contact.strftime('%d/%m/%Y %H:%M'),
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
            <p>CRM WhatsApp Cole • Colegio • {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
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
    
    html_parts.append(contact.last_contact.strftime('%d/%m/%Y %H:%M'))
    
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
    
    respuesta = generar_respuesta_gemini(message, contacto_prueba, historial_prueba)
    
    return {
        "mensaje_usuario": message,
        "respuesta_gemini": respuesta,
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
