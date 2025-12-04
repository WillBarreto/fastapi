from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import os
import requests
import json
from typing import Dict, Optional

app = FastAPI()

# Configuración por negocio (la expandiremos después)
NEGOCIOS = {
    "default": {
        "nombre": "Colegio",
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "info_negocio": """
        Eres el asistente virtual del Colegio [NOMBRE]. 
        Información clave:
        - Horarios: L-V 7am-3pm
        - Ubicación: [DIRECCIÓN]
        - Servicios: Primaria, Secundaria
        - Costo inscripción: $5,000 MXN
        - Agendar visita: https://calendly.com/tu-colegio
        """
    }
}

# Modelo para mensajes de WhatsApp (Twilio webhook)
class WhatsAppMessage(BaseModel):
    From: str
    Body: str
    To: str

@app.get("/")
async def root():
    return {"status": "Bot WhatsApp activo", "negocios": list(NEGOCIOS.keys())}

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    try:
        form_data = await request.form()
        mensaje = WhatsAppMessage(
            From=form_data.get("From", ""),
            Body=form_data.get("Body", ""),
            To=form_data.get("To", "")
        )
        
        print(f"📨 Mensaje de {mensaje.From}: {mensaje.Body}")
        
        # Detectar negocio por número destino
        numero_destino = mensaje.To.replace("whatsapp:", "")
        negocio = "default"  # Por ahora, después detectamos por número
        
        # Responder con DeepSeek
        respuesta = await generar_respuesta_ia(
            mensaje.Body, 
            NEGOCIOS[negocio]["info_negocio"],
            NEGOCIOS[negocio]["deepseek_api_key"]
        )
        
        # Aquí después conectaremos con Twilio para responder
        print(f"🤖 Respuesta: {respuesta}")
        
        return {"status": "processed", "respuesta": respuesta}
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"status": "error", "detail": str(e)}

async def generar_respuesta_ia(pregunta: str, contexto: str, api_key: str) -> str:
    """Consulta a DeepSeek API"""
    if not api_key:
        return "Hola, soy el asistente del Colegio. ¿En qué puedo ayudarte?"
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": f"Eres un asistente profesional. Responde solo con esta información: {contexto}. Si no sabes algo, di 'Te ayudo a agendar una cita para resolver tus dudas.'"},
                {"role": "user", "content": pregunta}
            ],
            "max_tokens": 150
        }
        
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return "Gracias por tu mensaje. ¿Te ayudo a agendar una cita?"
            
    except Exception as e:
        print(f"Error DeepSeek: {e}")
        return "Estoy aquí para ayudarte. ¿Te gustaría agendar una visita?"

@app.post("/test")
async def test_bot():
    """Endpoint para probar el bot sin WhatsApp"""
    test_pregunta = "¿Cuál es el horario del colegio?"
    respuesta = await generar_respuesta_ia(
        test_pregunta,
        NEGOCIOS["default"]["info_negocio"],
        NEGOCIOS["default"]["deepseek_api_key"]
    )
    return {"pregunta": test_pregunta, "respuesta": respuesta}
