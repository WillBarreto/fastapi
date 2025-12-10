import os
import requests
import json


import sys

# Forzar output inmediato
print("=" * 60, file=sys.stderr)
print("🎯 TEST OPENROUTER - INICIANDO", file=sys.stderr)
print("=" * 60, file=sys.stderr, flush=True)

# ======= CONFIGURACIÓN ========
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

def test_openrouter():
    """Prueba simple de conexión con OpenRouter"""
    
    print("🧪 Probando conexión con OpenRouter...")
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "model": "google/gemini-2.0-flash-exp:free",
        "messages": [
            {
                "role": "system",
                "content": "Eres un asistente útil. Responde en español."
            },
            {
                "role": "user", 
                "content": "Hola, ¿puedes saludarme?"
            }
        ],
        "max_tokens": 100
    }
    
    try:
        response = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 200:
            resultado = response.json()
            respuesta = resultado["choices"][0]["message"]["content"]
            print(f"✅ Conexión exitosa!")
            print(f"🤖 Respuesta: {respuesta}")
            return True
        else:
            print(f"❌ Error: Código {response.status_code}")
            print(f"Detalles: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error de conexión: {e}")
        return False

if __name__ == "__main__":
    # Reemplaza con tu API Key COMPLETA
    if "..." in OPENROUTER_API_KEY:
        print("⚠️  REEMPLAZA 'sk-or-v1-a56...if7' con tu API Key completa en la línea 7")
    else:
        test_openrouter()
