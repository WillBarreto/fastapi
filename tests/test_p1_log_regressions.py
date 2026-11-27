"""Synthetic variants of Nov 24 log; no prospect identifiers or transcripts."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from offline_main import load_main

class LogRegressions(unittest.TestCase):
    def setUp(self):
        self.n = load_main()
        self.contact = NS(id=1, status='EN_CALIFICACION', notes='ZONA_VALIDADA_AUTORITATIVA:Buen Suceso\nHORA_CITA:13:00')
        self.context = {'zona_interes':'Buen Suceso', 'referencia_colegio':'Internet',
                        'hitos_comerciales':['RECIBIO_PRESENTACION_VALOR','RECIBIO_EXPLICACION_METODO'],
                        'objetivo_pendiente':'OBTENER_HORA_CITA'}
    def decide(self, a, message='Para el jueves'):
        return self.n['aplicar_reglas_negocio_estructuradas'](a,self.contact,message,self.context)
    def test_historical_campus_does_not_reject_local_interest(self):
        d=self.decide({'campus_mencionado':'Santa Cruz Atizapán','zona_mencionada':'Buen Suceso'},
                      'Vi Metepec en internet, pero busco Santa Cruz Atizapán')
        self.assertNotEqual(d['accion'],'RECHAZAR_CAMPUS')
        zone=d['datos_detectados']['clasificacion_zona_determinista']
        self.assertTrue(zone['es_zona_validada']);self.assertFalse(zone['es_zona_externa'])
    def test_actual_external_campus_is_still_rejected(self):
        d=self.decide({'campus_mencionado':'Metepec','campus_externo':True},'Quiero informes del campus Metepec')
        self.assertEqual(d['accion'],'RECHAZAR_CAMPUS')
    def test_external_location_without_fields_still_detected(self):
        d=self.n['clasificar_zona_determinista']('Estoy en Metepec')
        self.assertTrue(d['es_campus_externo'] or d['es_zona_externa'])
    def test_validated_zone_clears_conflicting_external_flag(self):
        original=self.n['clasificar_zona_determinista']
        def contradictory(**kw):
            r=original(**kw);r['es_zona_externa']=True;return r
        self.n['clasificar_zona_determinista']=contradictory
        d=self.decide({'zona_mencionada':'Buen Suceso'})
        self.assertFalse(d['datos_detectados']['clasificacion_zona_determinista']['es_zona_externa'])
    def appointment(self,**extra):
        return self.decide(dict(pide_cita=True,fecha_cita_iso='2030-11-28',fecha_cita_texto='jueves',**extra))
    def test_retains_hour_when_selecting_day(self):
        d=self.appointment()
        self.assertEqual(d['accion'],'CONFIRMAR_FECHA_CITA')
        self.assertEqual(d['datos_detectados']['hora_cita_confirmar'],'13:00')
        self.assertFalse(d['requiere_admin'])
    def test_new_hour_overrides_stored_hour(self):
        d=self.appointment(hora_cita_24h='12:00')
        self.assertEqual(d['datos_detectados']['hora_cita_confirmar'],'12:00')
    def test_ambiguous_new_hour_is_not_replaced(self):
        self.assertEqual(self.appointment(hora_cita_texto='más tarde')['accion'],'PEDIR_HORA_CITA')
    def test_inactive_negotiation_does_not_inherit_hour(self):
        self.context['objetivo_pendiente']=''
        self.assertEqual(self.appointment()['accion'],'PEDIR_HORA_CITA')
    def test_changed_appointment_does_not_inherit_hour(self):
        self.assertEqual(self.appointment(cambio_fecha_cita=True)['accion'],'PEDIR_HORA_CITA')
    def test_explicit_cancellation_still_closes(self):
        d=self.decide({'cancelacion_cita':True},'Cancelo la visita')
        self.assertNotIn(d['accion'],{'CONSULTAR_ADMIN','CONFIRMAR_FECHA_CITA'})
    def test_retains_date_when_selecting_hour(self):
        self.contact.notes+='\nFECHA_CITA:2030-11-28'
        d=self.decide({'pide_cita':True,'hora_cita_24h':'12:00'},'A las doce')
        self.assertEqual(d['accion'],'CONSULTAR_ADMIN')
        self.assertTrue(d['requiere_admin'])
    def test_past_date_is_not_confirmed(self):
        self.contact.notes+='\nFECHA_CITA:2020-11-26'
        d=self.decide({'pide_cita':True,'hora_cita_24h':'12:00'})
        self.assertEqual(d['accion'],'PEDIR_FECHA_CITA')
    def test_no_references_is_an_answer(self):
        self.context['referencia_colegio']=''
        self.context['hitos_comerciales']=[]
        d=self.decide({'intencion_principal':'PEDIR_INFORMES'},'No tengo referencias')
        self.assertNotEqual(d['accion'],'PEDIR_REFERENCIA')
        self.assertTrue(d['datos_detectados']['referencia_ya_respondida'])
    def generate(self,text,flag=False):
        self.n['os']=NS(getenv=lambda key:'offline-placeholder')
        self.n['genai']=NS(configure=lambda **kw:None, types=NS(GenerationConfig=lambda **kw:kw))
        self.n['generar_con_gemini_con_fallback']=lambda *a,**kw:(NS(text=text),'stub')
        return self.n['generar_respuesta_final_estructurada']('Necesito orientación',{},
            {'accion':'RESPONDER_TEMA','datos_detectados':{'referencia_ya_respondida':flag}}, {})
    def test_complete_correspondiente_ending_is_accepted(self):
        r=self.generate('Con gusto podemos brindarle la atención correspondiente.')
        self.assertFalse(r['uso_fallback_seguro'],r)
    def test_incomplete_ending_is_still_rejected(self):
        r=self.generate('Con gusto podemos brindarle información sobre')
        self.assertTrue(any('RESPUESTA_TERMINA_INCOMPLETA' in e for e in r['errores_validacion']))
    def test_repeated_reference_question_is_rejected(self):
        r=self.generate('Con gusto le orientamos. ¿Cómo se enteró del colegio?',True)
        self.assertTrue(any('REPITE_REFERENCIA_YA_RESPONDIDA' in e for e in r['errores_validacion']))

class ConnectionRecovery(unittest.TestCase):
    def test_stale_pooled_connection_is_replaced_before_query(self):
        # Execute the real engine construction with its actual options, but
        # substitute a local SQLite transport. No PostgreSQL/network access.
        import os
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import QueuePool
        source=Path(os.environ.get('WHAPSBOT_TEST_SOURCE', Path(__file__).parents[1]/'main.py'))
        tree=ast.parse(source.read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign)
                  and isinstance(n.value,ast.Call)
                  and isinstance(n.value.func,ast.Name) and n.value.func.id=='create_engine'
                  and not any(k.arg=='connect_args' for k in n.value.keywords))
        env={'DATABASE_URL':'sqlite://',
             'create_engine':lambda url,**kw:create_engine(url,poolclass=QueuePool,**kw)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),env)
        engine=env['engine']
        try:
            with engine.connect() as c:
                raw=c.connection.driver_connection
                self.assertEqual(c.execute(text('SELECT 1')).scalar(),1)
            raw.close()  # server-side disconnect while pooled
            with engine.connect() as c:
                self.assertIsNot(c.connection.driver_connection,raw)
                self.assertEqual(c.execute(text('SELECT 1')).scalar(),1)
        finally:engine.dispose()

if __name__=='__main__':unittest.main()
