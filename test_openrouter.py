import os
import sys
import requests
import json

# Forzar output inmediato
print("=" * 60, file=sys.stderr)
print("🎯 TEST OPENROUTER - INICIANDO", file=sys.stderr)
print("=" * 60, file=sys.stderr, flush=True)

# ======= CONFIGURACIÓN ========
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

print(f"🔍 DEBUG: OPENROUTER_API_KEY existe? {'SÍ' if OPENROUTER_API_KEY else 'NO'}", file=sys.stderr)
print(f"🔍 DEBUG: Primeros 5 chars: {OPENROUTER_API_KEY[:5] if OPENROUTER_API_KEY else 'VACÍA'}...", file=sys.stderr)

def test_openrouter():
    """Prueba simple de conexión con OpenRouter"""
    
    print("🧪 Probando conexión con OpenRouter...")
    
    if not OPENROUTER_API_KEY:
        print("❌ ERROR: OPENROUTER_API_KEY está vacía", file=sys.stderr)
        print("💡 Verifica la variable en Railway → Variables", file=sys.stderr)
        return False
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://fastapi-production-efb5.up.railway.app",
        "X-Title": "Colegio WhatsApp Bot"
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
        print(f"🔍 DEBUG: Enviando request a OpenRouter...", file=sys.stderr)
        
        response = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json=payload,
            timeout=10
        )
        
        print(f"🔍 DEBUG: Status Code: {response.status_code}", file=sys.stderr)
        
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
    result = test_openrouter()
    sys.exit(0 if result else 1)
