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
        
        # Aquí después conectaremos DeepSeek
        respuesta = "¡Hola! Soy el asistente del Colegio. Próximamente responderé automáticamente."
        
        return {"status": "received", "user": from_user, "bot_response": respuesta}
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"status": "error", "detail": str(e)}

@app.get("/test")
async def test_endpoint():
    """Endpoint de prueba"""
    return {
        "status": "ok",
        "message": "Bot funcionando",
        "webhook_url": "https://TU-URL.railway.app/webhook/whatsapp"
    }
