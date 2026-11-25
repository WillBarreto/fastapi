# P1 · Primer candidato de correcciones conversacionales

Base: `c76066593a4f03953efddd2c53d6fdc6e7e0d1ae`, rama productiva
`refactor-ia-estructurada`. Preparado el 24 de noviembre de 2026.

## Cambio de comportamiento

- Pre-kínder conserva su clasificación, pero deja de forzar la pregunta de
  localidad cuando la zona ya está validada. El nivel inferido queda disponible
  para la referencia de colegiatura.
- Se reutiliza fecha de nacimiento válida de un único alumno persistido cuando
  no hay corrección nueva, cambio de nivel o señal de otro alumno. No se elige
  entre hermanos ni se pisa una fecha nueva ambigua.
- La necesidad contextual de atención administrativa tiene prioridad sobre
  una pausa. Se reutiliza CONSULTAR_ADMIN y la escalación existente. La pausa
  normal sigue siendo SEGUIMIENTO; el cierre explícito conserva su prioridad.
- La referencia de colegiatura usa los mismos montos y niveles autorizados,
  pero omite el catálogo de becas, descuentos y medios de pago.
- El contrato de interpretación añade `alcance_consulta_costos` con valores
  COLEGIATURA, DETALLE o AMBOS. Por compatibilidad, un valor ausente/inválido
  utiliza COLEGIATURA. Después del hito RECIBIO_COSTOS, una pregunta DETALLE
  pre-visita responde sobre el límite de información presencial sin repetir
  la colegiatura. No aplica la nueva restricción a una visita ya realizada
  según estado, etapa o hito.

## Conservación

Sin cambios a config/precios.json, Twilio, modelos de base de datos,
scheduler, moderación, empleo, rutas del panel o motor legacy. No se borraron
las cuatro funciones candidatas a depuración. La búsqueda en los archivos
versionados fuera de main no encontró referencias textuales a ellas, pero
todavía falta descartar uso operativo/dinámico antes de retirarlas.

El bloque retirado es la construcción extensa de la respuesta comercial de
colegiaturas, sustituida por la referencia breve acordada. Las opciones
comerciales permanecen en la configuración.

## Verificación reproducible

```bash
python -m unittest discover -s tests -p 'test_p1*.py' -v
python -m py_compile main.py tests/offline_main.py tests/test_p1_conversation.py
git diff --check
```

23 pruebas pasan con Python 3.12 y Pydantic 2.5.0, versión fijada en
requirements.txt. También pasaron con Pydantic 2.13.4 del entorno inicial.
Sobre el main original, el mismo conjunto termina con 10 aserciones fallidas
y cero errores de ejecución: son comprobaciones del comportamiento esperado,
no diez incidentes comerciales independientes.

Para contrastar con el original, guardar `git show
c76066593a4f03953efddd2c53d6fdc6e7e0d1ae:main.py` en un archivo temporal y
establecer `WHAPSBOT_TEST_SOURCE` a su ruta al ejecutar unittest. Se espera
fallo en el original.

El arnés carga por AST funciones reales y contratos de main, sin ejecutar
imports del módulo, clientes, base de datos, eventos startup o endpoints.
Las llamadas de envío, creación de tareas y validación Google están bloqueadas.
Las entradas semánticas se suministran como fixtures anónimos reconstruidos
desde la auditoría; no son una nueva transcripción ni contienen datos reales.
La lectura del tarifario se hace desde el archivo real versionado.

## Límites y puertas antes de producción

Esto verifica reglas y generación determinista; no certifica integración
FastAPI, persistencia PostgreSQL, interpretación real de Gemini o entrega de
notificaciones. La ejecución de escalación se comprobó en modo diagnóstico,
sin crear tareas ni enviar WhatsApp.

Pendiente antes de integrar/desplegar:

1. Probar la salida real del intérprete, incluyendo el campo nuevo de costos,
   con ejemplos anonimizados y sin enviar respuestas a prospectos.
2. Revisar arbitraje/orquestación completo para confirmar que no sustituya las
   decisiones corregidas; validar persistencia y tarea/aviso mediante dobles
   o entorno de prueba.
3. Verificar éxito/fallo de notificación: la función existente puede marcar
   ejecutada una llamada sin acreditar entrega efectiva. Este candidato
   corrige la decisión de escalar, no ese comprobante de entrega.
4. Validar casos de varias intenciones y las prioridades anteriores a pausa:
   campus externo, edad y cita siguen teniendo reglas previas. No se declara
   resuelto todo caso posible de solicitud humana.
5. Validar interpretación del ciclo escolar siguiente: este bloque conserva
   la función de cálculo del ciclo; no implementa una selección nueva de ciclo.
6. Cerrar los paquetes CRM, pausa manual y seguimientos del plan maestro antes
   de considerarlos implementados. La regla precio/silencio sin seguimiento
   todavía requiere su paquete P2; este cambio no la activa.
7. Verificar configuración vigente y recuperación de datos antes de cualquier
   migración o despliegue. No hubo migraciones en este bloque.

El caso posterior a visita solo se comprueba para evitar aplicar la nueva
restricción pre-visita; no certifica que el flujo post-visita anterior esté
resuelto. La detección semántica de preguntas sobre extras puede fallar si
Gemini no proporciona el campo; el valor por defecto mantiene compatibilidad.

Estado: candidato para revisión e integración de pruebas, sin despliegue.

## Ampliación del 25 de noviembre: hallazgos del log

Se añaden 17 pruebas (40 en total) y correcciones sobre el candidato previo:

- Cuando el intérprete aporta campus, la clasificación utiliza campus y
  localidad extraídos para evitar rechazar una referencia histórica a otra
  sede. Si no aporta campus se conserva la detección textual. Una localidad
  previamente validada deja de quedar marcada simultáneamente como externa;
  se conserva el rechazo de campus externo explícito. La calidad de extracción
  semántica continúa siendo una dependencia que requiere validación real.
- En objetivos OBTENER_FECHA_CITA/OBTENER_HORA_CITA se recupera el dato
  complementario persistido. Una corrección nueva, dato ambiguo, cancelación
  o negociación inactiva no hereda el horario. Las validaciones de fecha
  futura y autorización administrativa siguen aplicándose.
- La referencia ya contestada se pasa a redacción y al validador; se acepta
  también la respuesta explícita de no tener referencias. Se rechazan
  preguntas reconocibles que vuelven a pedirlas. No es una garantía de
  detección de todas las paráfrasis ni sustituye la persistencia de memoria.
- Se elimina “correspondiente” de la lista de terminaciones incompletas;
  las frases realmente incompletas siguen siendo rechazadas.
- PostgreSQL utiliza pool_pre_ping. Se comprueba una conexión del pool antes
  de usarla; no hay reintentos de transacciones ni envíos. Una conexión que
  cae durante una transacción aún puede fallar y requiere nueva ejecución
  del ciclo existente. No se certifica que desaparezcan todos los errores EOF.

Validación: 40 pruebas pasan con Pydantic 2.5.0 y SQLAlchemy 2.0.23,
compilación sintáctica y git diff --check correctos. Las respuestas del modelo
se sustituyen por dobles en las pruebas de redacción. La prueba de conexión
usa la construcción real del engine y simula una conexión cerrada en SQLite
local; no conecta a PostgreSQL productivo ni inicia el worker.

El log aportado es un fragmento de operación, no una nueva auditoría de todas
las conversaciones. No se incorporan teléfonos, nombres ni transcripciones
reales al repositorio. No se cambian reglas de precios ni se despliega.
