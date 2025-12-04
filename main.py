from fastapi import FastAPI, Request, Form
from pydantic import BaseModel
import os
from twilio.rest import Client  # Importamos al inicio para mejor manejo de errores

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

class WhatsAppMessage(BaseModel):
    From: str
    Body: str
    To: str

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
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    return {
        "status": "healthy",
        "twilio_credentials_loaded": bool(twilio_account_sid and twilio_auth_token and twilio_number),
        "twilio_number": twilio_number if twilio_number else "No configurado",
        "variables_loaded": {
            "TWILIO_ACCOUNT_SID": "✅" if twilio_account_sid else "❌",
            "TWILIO_AUTH_TOKEN": "✅" if twilio_auth_token else "❌",
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
        # Log del mensaje recibido
        print(f"📨 Mensaje de {From}: {Body}")
        
        # Generar respuesta inteligente
        respuesta = generar_respuesta_inteligente(Body)
        
        # Enviar respuesta via Twilio
        resultado = enviar_respuesta_twilio(From, respuesta)
        print(f"📤 {resultado}")
        
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
    """Envía mensaje de vuelta via Twilio API"""
    # Obtener variables de entorno
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    # Debug en logs
    print(f"🔍 Debug - Account SID: {'✅' if account_sid else '❌'}")
    print(f"🔍 Debug - Auth Token: {'✅' if auth_token else '❌'}")
    print(f"🔍 Debug - Twilio Number: {twilio_number if twilio_number else '❌ No configurado'}")
    
    # Validar credenciales
    if not account_sid:
        return "❌ Faltan credenciales Twilio: TWILIO_ACCOUNT_SID"
    if not auth_token:
        return "❌ Faltan credenciales Twilio: TWILIO_AUTH_TOKEN"
    if not twilio_number:
        return "❌ Faltan credenciales Twilio: TWILIO_WHATSAPP_NUMBER"
    
    try:
        # Crear cliente Twilio
        client = Client(account_sid, auth_token)
        
        # Enviar mensaje
        message = client.messages.create(
            body=mensaje,
            from_=twilio_number,  # Usa la variable de entorno
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
    # Verificar si las variables están cargadas
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
    
    return {
        "status": "ok",
        "message": "Bot funcionando",
        "webhook_url": "https://fastapi-production-efb5.up.railway.app/webhook/whatsapp",
        "credentials_status": {
            "TWILIO_ACCOUNT_SID": "✅ Cargada" if account_sid else "❌ Faltante",
            "TWILIO_AUTH_TOKEN": "✅ Cargada" if auth_token else "❌ Faltante",
            "TWILIO_WHATSAPP_NUMBER": "✅ Cargada" if twilio_number else "❌ Faltante"
        },
        "twilio_number_example": twilio_number or "No configurado",
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
