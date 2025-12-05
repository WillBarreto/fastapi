from fastapi import FastAPI, Request, Form
import os
from twilio.rest import Client
from datetime import datetime

app = FastAPI()

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

@app.get("/")
async def root():
    return {
        "status": "WhatsApp bot activo", 
        "endpoint": "/webhook/whatsapp",
        "test": "/test",
        "health": "/health"
    }

@app.get("/health")
async def health_check():
    """Endpoint para verificar que el servidor está funcionando"""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    return {
        "status": "healthy",
        "twilio_credentials_loaded": bool(account_sid and api_key and api_secret and twilio_number),
        "twilio_number": twilio_number or "No configurado",
        "variables_loaded": {
            "TWILIO_ACCOUNT_SID": "✅" if account_sid else "❌",
            "TWILIO_API_KEY": "✅" if api_key else "❌",
            "TWILIO_API_SECRET": "✅" if api_secret else "❌",
            "TWILIO_WHATSAPP_NUMBER": "✅" if twilio_number else "❌"
        }
    }

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(
    From: str = Form(...),
    Body: str = Form(...),
    To: str = Form(None)
):
    try:
        print(f"\n{'='*60}")
        print(f"💬 WHATSAPP CHAT - {datetime.now().strftime('%H:%M:%S')}")
        print(f"📱 De: {From}")
        print(f"👤 USUARIO: {Body}")
        print(f"{'-'*40}")
        # ========================================================
        
        # Generar respuesta inteligente
        respuesta = generar_respuesta_inteligente(Body)
        
        # Enviar respuesta via Twilio
        resultado = enviar_respuesta_twilio(From, respuesta)
        
        # ================= NUEVO: RESPUESTA DEL BOT =================
        print(f"🤖 BOT: {respuesta}")
        print(f"📤 Estado: {resultado}")
        print(f"{'='*60}\n")
        # ========================================================
        
        return {"status": "processed", "message": respuesta[:50] + "..."}
    
    except Exception as e:
        print(f"❌ Error en webhook: {e}")
        return {"status": "error", "detail": str(e)}

def generar_respuesta_inteligente(mensaje: str) -> str:
    """Genera una respuesta basada en el mensaje recibido"""
    mensaje = mensaje.lower().strip()
    
    # Palabras clave y respuestas
    if any(palabra in mensaje for palabra in ["hola", "buenos días", "buenas tardes"]):
        return "¡Hola! Soy el asistente virtual del Colegio. ¿En qué puedo ayudarte? Puedo informarte sobre horarios, ubicación, costos o agendar una visita."
    
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
    return "¡Hola! Soy el asistente del Colegio. Puedo ayudarte con:\n• Horarios\n• Ubicación\n• Costos\n• Agendar visitas\n\n¿En qué necesitas información? O si prefieres: https://calendly.com/tu-colegio"

def enviar_respuesta_twilio(to_number: str, mensaje: str) -> str:
    """Envía mensaje de vuelta via Twilio API usando API Key"""
    # Obtener variables de entorno
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    # Debug en logs
    print(f"🔍 Debug - Account SID: {'✅' if account_sid else '❌'}")
    print(f"🔍 Debug - API Key: {'✅' if api_key else '❌'}")
    print(f"🔍 Debug - API Secret: {'✅' if api_secret else '❌'}")
    print(f"🔍 Debug - Twilio Number: {twilio_number if twilio_number else '❌ No configurado'}")
    
    # Validar credenciales
    if not account_sid:
        return "❌ Faltan credenciales Twilio: TWILIO_ACCOUNT_SID"
    if not api_key:
        return "❌ Faltan credenciales Twilio: TWILIO_API_KEY"
    if not api_secret:
        return "❌ Faltan credenciales Twilio: TWILIO_API_SECRET"
    if not twilio_number:
        return "❌ Faltan credenciales Twilio: TWILIO_WHATSAPP_NUMBER"
    
    try:
        # Crear cliente Twilio con API Key
        client = Client(api_key, api_secret, account_sid)
        
        # Enviar mensaje
        message = client.messages.create(
            body=mensaje,
            from_=twilio_number,
            to=to_number
        )
        
        return f"✅ Mensaje enviado exitosamente. SID: {message.sid}"
        
    except Exception as e:
        error_msg = f"❌ Error Twilio: {str(e)}"
        print(error_msg)
        return error_msg

@app.get("/test")
async def test_endpoint():
    """Endpoint de prueba"""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY")
    api_secret = os.getenv("TWILIO_API_SECRET")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    return {
        "status": "ok",
        "message": "Bot funcionando",
        "webhook_url": "https://fastapi-production-efb5.up.railway.app/webhook/whatsapp",
        "credentials_status": {
            "TWILIO_ACCOUNT_SID": "✅ Cargada" if account_sid else "❌ Faltante",
            "TWILIO_API_KEY": "✅ Cargada" if api_key else "❌ Faltante",
            "TWILIO_API_SECRET": "✅ Cargada" if api_secret else "❌ Faltante",
            "TWILIO_WHATSAPP_NUMBER": "✅ Cargada" if twilio_number else "❌ Faltante"
        },
        "twilio_number": twilio_number or "No configurado",
        "endpoints": {
            "root": "/",
            "webhook": "/webhook/whatsapp (POST)",
            "test": "/test",
            "health": "/health"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
