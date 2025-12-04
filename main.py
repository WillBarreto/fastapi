from fastapi import FastAPI, Request
from pydantic import BaseModel
import os
import requests

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
    return {"status": "WhatsApp bot activo", "endpoint": "/webhook/whatsapp"}

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    try:
        # Recibir datos de Twilio
        form_data = await request.form()
        from_user = form_data.get("From", "")
        message_body = form_data.get("Body", "")
        
        print(f"📨 Mensaje de {from_user}: {message_body}")
        
        # Respuesta temporal
        respuesta = "¡Hola! Soy el asistente del Colegio. Estoy en desarrollo, pronto responderé automáticamente."
        
        # ENVIAR RESPUESTA VÍA TWILIO
        resultado = enviar_respuesta_twilio(from_user, respuesta)
        print(f"📤 {resultado}")
        
        return {"status": "processed"}
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"status": "error", "detail": str(e)}

def enviar_respuesta_twilio(to_number: str, mensaje: str):
    """Envía mensaje de vuelta via Twilio API"""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    if not account_sid or not auth_token:
        return "Faltan credenciales Twilio"
    
    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        
        message = client.messages.create(
            body=mensaje,
            from_='whatsapp:+14155238886',  # Número sandbox
            to=to_number
        )
        return f"Mensaje enviado: {message.sid}"
    except Exception as e:
        return f"Error Twilio: {e}"

@app.get("/test")
async def test_endpoint():
    """Endpoint de prueba"""
    return {
        "status": "ok",
        "message": "Bot funcionando",
        "webhook_url": "https://fastapi-production-efb5.up.railway.app/webhook/whatsapp"
    }
