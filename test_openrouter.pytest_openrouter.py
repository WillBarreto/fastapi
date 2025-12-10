import os
from tu_codigo_actual import llamar_openrouter

# Configurar API Key directamente para prueba
os.environ['OPENROUTER_API_KEY'] = 'sk-or-v1-a56...if7'  # Tu key aquí

# Probar
respuesta = llamar_openrouter("Hola, quisiera información sobre el colegio")
print("🤖 Respuesta:", respuesta)
