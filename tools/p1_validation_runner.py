"""One-shot synthetic model validation. Never starts webhooks or workers."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

if os.getenv('P1_VALIDATION_ONLY') != 'true':
    raise SystemExit('Explicit validation mode required')
if any(os.getenv(k) for k in ('TWILIO_ACCOUNT_SID','TWILIO_API_KEY','TWILIO_API_SECRET','TWILIO_WHATSAPP_NUMBER')):
    raise SystemExit('Twilio credentials must be absent')
if not os.getenv('DATABASE_URL','').startswith('sqlite:'):
    raise SystemExit('Only disposable SQLite allowed')
os.environ['FOLLOWUP_AUTOMATION_ENABLED']='false'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main as m

def denied(*a,**kw):raise RuntimeError('Outbound WhatsApp disabled')
m.enviar_respuesta_twilio=denied
m.enviar_alerta_admin_whatsapp=denied
m.enviar_template_alerta_admin_whatsapp=denied
cases=[
 ('local_campus','Vi Metepec en internet, pero quiero informes de Santa Cruz Atizapán. Vivo en Santa Cruz Atizapán y busco primaria.',{}),
 ('external_campus','Vivo en Santa Cruz Atizapán, pero busco el campus de Metepec.',{}),
 ('human','Quiero que una persona me atienda; esperaré su respuesta.',{}),
 ('cost_detail','Ya me dio la colegiatura, ¿cuáles son los gastos adicionales?',{'hitos_comerciales':['RECIBIO_COSTOS','RECIBIO_PRESENTACION_VALOR']})
]
results=[]
for name,message,extra in cases:
 ctx={'zona_interes':'Santa Cruz Atizapán','referencia_colegio':'Internet',**extra}
 c=SimpleNamespace(id=None,status='EN_CALIFICACION',notes='ZONA_VALIDADA_AUTORITATIVA:Santa Cruz Atizapán')
 result=m.procesar_mensaje_prospecto_estructurado(message,c,[],ctx)
 a=result.get('analisis',{});d=result.get('decision',{})
 checks={
 'local_campus':d.get('accion')!='RECHAZAR_CAMPUS' and a.get('campus_externo') is False,
 'external_campus':d.get('accion')=='RECHAZAR_CAMPUS',
 'human':d.get('accion')=='CONSULTAR_ADMIN' and d.get('requiere_admin') is True,
 'cost_detail':a.get('alcance_consulta_costos')=='DETALLE' and d.get('datos_detectados',{}).get('detalle_costos_reservado_visita') is True
 }
 row={'case':name,'passed':bool(result.get('procesado') and checks[name]),'action':d.get('accion'),'campus':a.get('campus_mencionado'),'cost_scope':a.get('alcance_consulta_costos'),'human':a.get('requiere_admin_contextual'),'response':result.get('respuesta_generada'),'error':result.get('error')}
 results.append(row);print('P1_LIVE_CASE '+json.dumps(row,ensure_ascii=False),flush=True)
print('P1_LIVE_SUMMARY '+json.dumps({'passed':sum(r['passed'] for r in results),'total':len(results),'whatsapp_sends':0,'production_writes':0}),flush=True)
m.engine.dispose()
sys.exit(0 if all(r['passed'] for r in results) else 1)
