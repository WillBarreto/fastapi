from fastapi import FastAPI, Request, Form, Depends, HTTPException
from pydantic import BaseModel
import os
import time
from twilio.rest import Client
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Enum, Boolean, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.sql import func
import enum

# ================= CONFIGURACIÓN DE BASE DE DATOS =================
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./whatsapp_bot.db")

print(f"🔧 DATABASE_URL configurada: {DATABASE_URL[:50]}...")  # Log parcial por seguridad

# Si la URL es de PostgreSQL, reemplazamos el esquema para usar psycopg2
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    print("✅ Usando PostgreSQL con psycopg2")

# Configurar engine con parámetros de reconexión
engine_params = {
    "pool_pre_ping": True,      # Verifica conexión antes de usar
    "pool_recycle": 300,        # Recicla conexiones cada 5 minutos
    "echo": True,               # Muestra queries SQL en logs (útil para debug)
}

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL, 
        connect_args={"check_same_thread": False},
        **engine_params
    )
    print("✅ Usando SQLite local")
else:
    # Para PostgreSQL, agregar timeout de conexión
    engine = create_engine(
        DATABASE_URL,
        connect_args={
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        },
        **engine_params
    )
    print("✅ Configurando PostgreSQL con timeouts")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# ================= MODELOS DE BASE DE DATOS =================

class ContactStatus(enum.Enum):
    PROSPECTO_NUEVO = "prospecto_nuevo"
    PROSPECTO_INFORMADO = "prospecto_informado"
    VISITA_AGENDADA = "visita_agendada"
    INSCRIPCION_PENDIENTE = "inscripcion_pendiente"
    ALUMNO_ACTIVO = "alumno_activo"
    ALUMNO_INACTIVO = "alumno_inactivo"
    COMPETENCIA = "competencia"
    EX_ALUMNO = "ex_alumno"

class Contact(Base):
    __tablename__ = "contacts"
    
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(20), unique=True, index=True, nullable=False)
    status = Column(Enum(ContactStatus), default=ContactStatus.PROSPECTO_NUEVO)
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
    direction = Column(Enum('incoming', 'outgoing'), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=func.now())
    twilio_sid = Column(String(50), nullable=True)
    
    # Relación con contacto
    contact = relationship("Contact", back_populates="messages")

# ================= FUNCIÓN PARA CREAR TABLAS CON REINTENTOS =================
def create_tables_with_retry(max_retries=10, initial_delay=2):
    """Intenta crear tablas con reintentos exponenciales"""
    print(f"🔄 Iniciando creación de tablas (máximo {max_retries} reintentos)...")
    
    for attempt in range(max_retries):
        try:
            print(f"📝 Intento {attempt + 1}/{max_retries}: Creando tablas...")
            
            # Intentar una conexión simple primero
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                print("   ✅ Conexión a BD exitosa")
            
            # Crear tablas
            Base.metadata.create_all(bind=engine)
            print("   ✅ Tablas creadas exitosamente")
            return True
            
        except Exception as e:
            error_msg = str(e)
            print(f"   ❌ Error en intento {attempt + 1}: {error_msg}")
            
            # Si es error de conexión, reintentar
            if any(keyword in error_msg.lower() for keyword in [
                "could not translate host", 
                "connection refused", 
                "timeout", 
                "name or service not known"
            ]):
                if attempt < max_retries - 1:
                    # Retraso exponencial: 2, 4, 8, 16, 32 segundos...
                    delay = initial_delay * (2 ** attempt)
                    print(f"   ⏳ Esperando {delay} segundos antes de reintentar...")
                    time.sleep(delay)
                    continue
                else:
                    print("   ❌ Máximo de reintentos alcanzado. Continuando sin tablas...")
                    return False
            else:
                # Otro tipo de error (permisos, sintaxis, etc.)
                print(f"   ❌ Error crítico: {error_msg}")
                raise
    
    return False

# ================= CREAR TABLAS AL INICIAR =================
print("🚀 Iniciando aplicación FastAPI...")

# Crear tablas con reintentos (pero no bloquear si falla)
try:
    tables_created = create_tables_with_retry()
    if not tables_created:
        print("⚠️  ADVERTENCIA: Las tablas no se pudieron crear. Algunas funciones pueden no trabajar.")
        print("⚠️  Esto puede ser normal si la BD no está disponible aún. Se reintentará en uso.")
except Exception as e:
    print(f"⚠️  Error durante creación de tablas: {e}")
    print("⚠️  Continuando sin tablas (puede causar errores más adelante)")

# ================= DEPENDENCIA DE BASE DE DATOS =================
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        print(f"❌ Error en sesión BD: {e}")
        db.rollback()
        raise
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

# ================= FUNCIONES DE BASE DE DATOS =================
def get_or_create_contact(db: Session, phone_number: str):
    """Obtiene o crea un contacto en la base de datos"""
    try:
        contact = db.query(Contact).filter(Contact.phone_number == phone_number).first()
        
        if not contact:
            # Es un nuevo contacto
            contact = Contact(
                phone_number=phone_number,
                status=ContactStatus.PROSPECTO_NUEVO,
                first_contact=datetime.now(),
                last_contact=datetime.now(),
                total_messages=0
            )
            db.add(contact)
            db.commit()
            db.refresh(contact)
        
        return contact
    except Exception as e:
        print(f"❌ Error en get_or_create_contact: {e}")
        db.rollback()
        # Crear contacto temporal en memoria si BD falla
        class TempContact:
            def __init__(self):
                self.id = 0
                self.phone_number = phone_number
                self.status = ContactStatus.PROSPECTO_NUEVO
                self.total_messages = 0
        return TempContact()

def save_message(db: Session, contact_id: int, direction: str, content: str, twilio_sid: str = None):
    """Guarda un mensaje en la base de datos"""
    try:
        message = Message(
            contact_id=contact_id,
            direction=direction,
            content=content,
            timestamp=datetime.now(),
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
    except Exception as e:
        print(f"❌ Error en save_message: {e}")
        db.rollback()
        return None

def get_conversation_history(db: Session, phone_number: str, limit: int = 10):
    """Obtiene el historial de conversación de un contacto"""
    try:
        contact = db.query(Contact).filter(Contact.phone_number == phone_number).first()
        if not contact:
            return []
        
        messages = db.query(Message).filter(Message.contact_id == contact.id)\
            .order_by(Message.timestamp.desc())\
            .limit(limit)\
            .all()
        
        return messages[::-1]  # Invertir para orden cronológico
    except Exception as e:
        print(f"❌ Error en get_conversation_history: {e}")
        return []

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
        
        # Estadísticas (con manejo de errores)
        try:
            total_contacts = db.query(Contact).count()
            total_messages = db.query(Message).count()
        except:
            total_contacts = 0
            total_messages = 0
        
    except Exception as e:
        db_status = f"❌ Error: {str(e)}"
        total_contacts = 0
        total_messages = 0
    
    # Verificar variables de Twilio (corregir typo)
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("TWILTO_ACCOUNT_SID")
    twilio_api_key = os.getenv("TWILIO_API_KEY") or os.getenv("TWILTO_API_KEY")
    twilio_api_secret = os.getenv("TWILIO_API_SECRET") or os.getenv("TWILTO_API_SECRET")
    
    return {
        "status": "healthy",
        "database": db_status,
        "statistics": {
            "total_contacts": total_contacts,
            "total_messages": total_messages
        },
        "twilio_configured": bool(twilio_account_sid and twilio_api_key and twilio_api_secret),
        "twilio_variables_found": {
            "account_sid": bool(twilio_account_sid),
            "api_key": bool(twilio_api_key),
            "api_secret": bool(twilio_api_secret),
            "whatsapp_number": bool(os.getenv("TWILIO_WHATSAPP_NUMBER") or os.getenv("TWILTO_WHATSAPP_NUMBER"))
        }
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
        
        # ================= GENERAR RESPUESTA =================
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
        print(f"📤 Estado: {resultado}")
        print(f"👤 Estado contacto: {contact.status.value}")
        print(f"📊 Total mensajes: {contact.total_messages}")
        print(f"{'='*60}\n")
        
        return {"status": "processed", "contact_id": contact.id}
    
    except Exception as e:
        print(f"❌ Error en webhook: {e}")
        return {"status": "error", "detail": str(e)}

def generar_respuesta_inteligente(mensaje: str, contact, history):
    """Genera respuesta basada en mensaje, contacto e historial"""
    mensaje = mensaje.lower().strip()
    
    # Si es un contacto con historial, personalizar respuesta
    if contact.total_messages > 1:
        if contact.status == ContactStatus.COMPETENCIA:
            return "Gracias por tu interés nuevamente. Te invito a agendar una visita para conocer nuestras instalaciones personalmente: https://calendly.com/tu-colegio"
        
        if contact.status == ContactStatus.PROSPECTO_INFORMADO:
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
        return "📅 Puedes agendar una visita en: https://calendray.com/tu-colegio"
    
    elif any(palabra in mensaje for palabra in ["servicios", "niveles", "grados", "primaria", "secundaria"]):
        return "🏫 Ofrecemos: Primaria y Secundaria. Educación de calidad con enfoque integral."
    
    # Respuesta por defecto
    return "¡Hola! Soy el asistente del Colegio. Puedo ayudarte con:\n• Horarios\n• Ubicación\n• Costos\n• Agendar visitas\n\n¿En qué necesitas información?"

def enviar_respuesta_twilio(to_number: str, mensaje: str) -> str:
    """Envía mensaje de vuelta via Twilio API usando API Key"""
    # Corregir posibles typos en nombres de variables
    account_sid = os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("TWILTO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY") or os.getenv("TWILTO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET") or os.getenv("TWILTO_API_SECRET")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER") or os.getenv("TWILTO_WHATSAPP_NUMBER")
    
    print(f"🔑 Twilio Config - SID: {bool(account_sid)}, Key: {bool(api_key)}, Secret: {bool(api_secret)}, Number: {twilio_number}")
    
    if not all([account_sid, api_key, api_secret, twilio_number]):
        return "❌ Faltan credenciales Twilio. Verifica variables TWILIO_*"
    
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
    try:
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
                    "status": c.status.value,
                    "first_contact": c.first_contact,
                    "last_contact": c.last_contact,
                    "total_messages": c.total_messages,
                    "is_competitor": c.is_competitor
                }
                for c in contacts
            ]
        }
    except Exception as e:
        print(f"❌ Error en /contacts: {e}")
        return {"total": 0, "contacts": [], "error": str(e)}

@app.get("/conversations/{phone_number}")
async def get_conversations_by_phone(
    phone_number: str,
    db: Session = Depends(get_db),
    limit: int = 50
):
    """Obtiene todas las conversaciones de un contacto específico"""
    try:
        contact = db.query(Contact).filter(Contact.phone_number == phone_number).first()
        
        if not contact:
            raise HTTPException(status_code=404, detail="Contacto no encontrado")
        
        messages = db.query(Message).filter(Message.contact_id == contact.id)\
            .order_by(Message.timestamp.asc())\
            .limit(limit)\
            .all()
        
        return {
            "contact": {
                "id": contact.id,
                "phone_number": contact.phone_number,
                "status": contact.status.value,
                "first_contact": contact.first_contact,
                "last_contact": contact.last_contact,
                "total_messages": contact.total_messages,
                "notes": contact.notes
            },
            "conversation": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "content": m.content,
                    "timestamp": m.timestamp,
                    "twilio_sid": m.twilio_sid
                }
                for m in messages
            ]
        }
    except Exception as e:
        print(f"❌ Error en /conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/panel")
async def crm_panel(db: Session = Depends(get_db)):
    """Panel web de CRM"""
    try:
        # Obtener estadísticas
        total_contacts = db.query(Contact).count()
        by_status = db.query(Contact.status, func.count(Contact.id)).group_by(Contact.status).all()
        
        # Últimos contactos
        recent_contacts = db.query(Contact).order_by(Contact.last_contact.desc()).limit(10).all()
        
        # Generar HTML
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>CRM WhatsApp Bot - Colegio</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
                .header {{ background: linear-gradient(135deg, #25D366, #128C7E); color: white; padding: 25px; border-radius: 15px; margin-bottom: 20px; }}
                .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }}
                .stat-card {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1); }}
                .contact-list {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 3px 10px rgba(0,0,0,0.1); }}
                .contact-item {{ padding: 12px; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; }}
                .status-badge {{ padding: 4px 12px; border-radius: 20px; font-size: 0.8em; font-weight: bold; }}
                .status-prospecto {{ background: #FFEAA7; color: #E17055; }}
                .status-alumno {{ background: #55EFC4; color: #00B894; }}
                .status-competencia {{ background: #FD79A8; color: #E84393; }}
                a {{ color: #128C7E; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>📱 CRM WhatsApp Bot - Colegio</h1>
                <p>Gestión de prospectos, alumnos y competencia vía WhatsApp</p>
                <p>
                    <a href="/contacts" style="color: white; margin-right: 15px;">📋 Todos los contactos</a>
                    <a href="/health" style="color: white; margin-right: 15px;">🩺 Health Check</a>
                    <a href="/" style="color: white;">🏠 Inicio</a>
                </p>
            </div>
            
            <div class="stats">
                <div class="stat-card">
                    <h3>👥 Total Contactos</h3>
                    <p style="font-size: 2em; font-weight: bold;">{total_contacts}</p>
                </div>
        """
        
        # Agregar estadísticas por estado
        for status, count in by_status:
            color_class = "status-prospecto" if "prospecto" in status.value else "status-alumno" if "alumno" in status.value else "status-competencia"
            html += f"""
                <div class="stat-card">
                    <h3>📊 {status.value.replace('_', ' ').title()}</h3>
                    <p style="font-size: 2em; font-weight: bold;">{count}</p>
                    <span class="status-badge {color_class}">{status.value}</span>
                </div>
            """
        
        html += """
            </div>
            
            <div class="contact-list">
                <h2>🕐 Contactos Recientes</h2>
        """
        
        for contact in recent_contacts:
            status_class = "status-prospecto" if "prospecto" in contact.status.value else "status-alumno" if "alumno" in contact.status.value else "status-competencia"
            html += f"""
                <div class="contact-item">
                    <div>
                        <strong>📞 {contact.phone_number}</strong><br>
                        <small>Último contacto: {contact.last_contact.strftime('%d/%m/%Y %H:%M')}</small>
                    </div>
                    <div>
                        <span class="status-badge {status_class}">{contact.status.value}</span><br>
                        <small>📨 {contact.total_messages} mensajes</small>
                    </div>
                    <div>
                        <a href="/conversations/{contact.phone_number}">Ver conversación</a>
                    </div>
                </div>
            """
        
        html += """
            </div>
            
            <div style="margin-top: 30px; text-align: center; color: #666; padding: 20px;">
                <p>Sistema WhatsApp CRM desarrollado por Will Barreto FastAPI + Twilio + SQLite</p>
                <p>📧 Contacto técnico: contacto@willbarreto.com | 📅 {fecha_actual}</p>
            </div>
        </body>
        </html>
        """.replace("{fecha_actual}", datetime.now().strftime("%d/%m/%Y"))
        
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html)
    except Exception as e:
        print(f"❌ Error en /panel: {e}")
        return HTMLResponse(content=f"<h1>Error cargando panel: {str(e)}</h1>")

# ================= INICIALIZACIÓN =================
@app.on_event("startup")
async def startup_event():
    """Evento que se ejecuta al iniciar la aplicación"""
    print("\n" + "="*60)
    print("🚀 FASTAPI WhatsApp CRM INICIANDO")
    print("="*60)
    print(f"📅 Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🌐 Host: 0.0.0.0:8080")
    print("="*60 + "\n")

if __name__ == "__main__":
    import uvicorn
    print("🎯 Iniciando servidor Uvicorn...")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
