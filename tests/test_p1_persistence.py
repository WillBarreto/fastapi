"""Real module and ORM, isolated SQLite, outbound calls blocked."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

class Persistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        database_url=os.environ.get('P1_TEST_DATABASE_URL','sqlite:///'+cls.tmp.name+'/isolated.db')
        if not database_url.startswith('sqlite:///'):
            from sqlalchemy.engine import make_url
            target=make_url(database_url)
            if target.host != '127.0.0.1' or target.port != 55439 or not target.database.startswith('whaps_test'):
                raise ValueError('PostgreSQL tests require isolated loopback database whaps_test*, port 55439')
        cls.env=patch.dict(os.environ,{'DATABASE_URL':database_url,
            'FOLLOWUP_AUTOMATION_ENABLED':'false','GOOGLE_AI_API_KEY':'', 'GEMINI_API_KEY':''})
        cls.env.start()
        cls.block=patch.object(socket.socket,'connect',side_effect=AssertionError('Network disabled in persistence tests'))
        cls.block.start()
        sys.path.insert(0,str(Path(__file__).parents[1]))
        import main
        cls.m=main
    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose();cls.block.stop();cls.env.stop();cls.tmp.cleanup()
    def setUp(self):
        m=self.m
        self.db=m.SessionLocal()
        self.c=m.Contact(phone_number='synthetic-'+uuid.uuid4().hex,status='EN_CALIFICACION',notes='ZONA_VALIDADA_AUTORITATIVA:Santa Cruz Atizapán\nHORA_CITA:13:00')
        self.db.add(self.c);self.db.commit()
    def tearDown(self):self.db.close()
    def test_appointment_data_survives_new_session(self):
        m=self.m
        result={'analisis':{'fecha_cita_iso':'2030-11-28'},'decision':{'accion':'CONFIRMAR_FECHA_CITA','datos_detectados':{'fecha_cita_iso':'2030-11-28','hora_cita_24h':'13:00'}}}
        out=m.persistir_resultado_estructurado(self.db,self.c,result)
        self.assertTrue(out['persistido'],out)
        ident=self.c.id;self.db.close();self.db=m.SessionLocal();c=self.db.get(m.Contact,ident)
        self.assertEqual(m.get_note_value(c,'HORA_CITA'),'13:00')
        self.assertEqual(m.get_note_value(c,'FECHA_CITA'),'2030-11-28')
    def test_pending_task_reused_on_retry(self):
        m=self.m
        one=m.crear_tarea_admin_pendiente(self.db,self.c,'Solicito visita','Consultamos disponibilidad')
        two=m.crear_tarea_admin_pendiente(self.db,self.c,'Solicito visita','Consultamos disponibilidad')
        self.assertEqual(one.id,two.id)
        self.assertEqual(self.db.query(m.AdminPendingTask).filter_by(contact_id=self.c.id).count(),1)
    def orchestrate(self, analysis, message, context):
        m=self.m
        analysis=m.normalizar_analisis_mensaje_ia(dict(analysis,confianza=0.99))
        with patch.object(m,'analizar_mensaje_prospecto_con_ia',return_value=analysis), patch.object(
                m,'generar_respuesta_final_estructurada',return_value={'generada':True,'respuesta':'Respuesta de prueba.'}):
            result=m.procesar_mensaje_prospecto_estructurado(message,self.c,[],context)
        self.assertTrue(result['procesado'],result)
        return result
    def test_arbitration_preserves_human_request(self):
        r=self.orchestrate({'requiere_admin_contextual':True,'pausa_conversacion':True},
            'Esperaré a que me atienda una persona',{'zona_interes':'Santa Cruz Atizapán'})
        self.assertEqual(r['decision']['accion'],'CONSULTAR_ADMIN')
        self.assertTrue(r['decision']['requiere_admin'])
    def test_arbitration_preserves_known_hour(self):
        r=self.orchestrate({'pide_cita':True,'fecha_cita_iso':'2030-11-28','fecha_cita_texto':'jueves'},
            'El jueves',{'zona_interes':'Santa Cruz Atizapán','objetivo_pendiente':'OBTENER_HORA_CITA'})
        self.assertEqual(r['decision']['accion'],'CONFIRMAR_FECHA_CITA')
        self.assertEqual(r['decision']['datos_detectados']['hora_cita_confirmar'],'13:00')
    def test_arbitration_preserves_local_campus(self):
        r=self.orchestrate({'campus_mencionado':'Santa Cruz Atizapán','zona_mencionada':'Santa Cruz Atizapán'},
            'Vi Metepec pero busco Santa Cruz Atizapán',{'zona_interes':'Santa Cruz Atizapán'})
        self.assertNotEqual(r['decision']['accion'],'RECHAZAR_CAMPUS')

    def test_dry_run_has_no_task_or_message(self):
        m=self.m
        r=m.procesar_escalacion_admin_estructurada(self.db,self.c,'Necesito ayuda','Consultamos',
            {'decision':{'accion':'CONSULTAR_ADMIN','requiere_admin':True}},ejecutar_envio=False)
        self.assertTrue(r['requiere_escalacion']);self.assertFalse(r['ejecutada'])
        self.assertEqual(self.db.query(m.AdminPendingTask).filter_by(contact_id=self.c.id).count(),0)
        self.assertEqual(self.db.query(m.Message).filter_by(contact_id=self.c.id).count(),0)

    def incoming(self):
        row=self.m.Message(contact_id=self.c.id,direction='incoming',content='Solicito atención')
        self.db.add(row);self.db.commit()
        return row.id
    def escalate(self, ident):
        return self.m.procesar_escalacion_admin_estructurada(
            self.db,self.c,'Solicito atención','Consultamos',
            {'decision':{'accion':'CONSULTAR_ADMIN','requiere_admin':True}},
            ejecutar_envio=True,source_message_id=ident)
    def test_same_event_is_not_sent_twice_across_sessions(self):
        ident=self.incoming();cid=self.c.id
        with patch.object(self.m,'enviar_alerta_admin_whatsapp',return_value='✅ Mensaje enviado. SID: SM'+'a'*32) as send:
            first=self.escalate(ident)
            self.db.close();self.db=self.m.SessionLocal();self.c=self.db.get(self.m.Contact,cid)
            second=self.escalate(ident)
        self.assertTrue(first['ejecutada']);self.assertTrue(second['duplicada'])
        self.assertEqual(send.call_count,1)
        self.assertEqual(second['estado_envio'],'ACEPTADO_PROVEEDOR')
    def test_new_event_same_text_can_alert_again(self):
        with patch.object(self.m,'enviar_alerta_admin_whatsapp',return_value='✅ Template admin enviado. SID: SM'+'b'*32) as send:
            first=self.escalate(self.incoming());second=self.escalate(self.incoming())
        self.assertTrue(second['ejecutada']);self.assertEqual(send.call_count,2)
        self.assertEqual(first['tarea_id'],second['tarea_id'])
    def test_failure_not_executed_and_not_retried_blindly(self):
        ident=self.incoming()
        with patch.object(self.m,'enviar_alerta_admin_whatsapp',return_value='❌ Error Twilio: simulated') as send:
            first=self.escalate(ident);second=self.escalate(ident)
        self.assertFalse(first['ejecutada']);self.assertTrue(first['error'])
        self.assertEqual(second['estado_envio'],'NO_CONFIRMADO')
        self.assertEqual(send.call_count,1)
    def test_exception_leaves_claim_for_manual_reconciliation(self):
        ident=self.incoming()
        with patch.object(self.m,'enviar_alerta_admin_whatsapp',side_effect=TimeoutError('unknown provider outcome')) as send:
            first=self.escalate(ident);second=self.escalate(ident)
        self.assertFalse(first['ejecutada']);self.assertFalse(second['ejecutada'])
        self.assertEqual(send.call_count,1)
        self.assertEqual(second['estado_envio'],'INTENTANDO')
    def test_missing_source_has_no_side_effect(self):
        with patch.object(self.m,'enviar_alerta_admin_whatsapp') as send:
            result=self.escalate(None)
        send.assert_not_called()
        self.assertEqual(result['error'],'EVENTO_ENTRANTE_REQUERIDO')
        self.assertEqual(self.db.query(self.m.AdminPendingTask).filter_by(contact_id=self.c.id).count(),0)

    def test_concurrent_workers_claim_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        ident=self.incoming();cid=self.c.id;barrier=Barrier(2);m=self.m
        def run():
            db=m.SessionLocal()
            try:
                contact=db.get(m.Contact,cid)
                barrier.wait(timeout=5)
                return m.procesar_escalacion_admin_estructurada(db,contact,'Ayuda','Consultamos',
                    {'decision':{'requiere_admin':True}},ejecutar_envio=True,source_message_id=ident)
            finally: db.close()
        with patch.object(m,'enviar_alerta_admin_whatsapp',return_value='✅ Mensaje enviado. SID: SM'+'c'*32) as send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:run(),range(2)))
        self.assertEqual(send.call_count,1)
        self.assertEqual(sum(r['duplicada'] for r in results),1)
        self.assertEqual(self.db.query(m.AdminAlertAttempt).filter_by(contact_id=cid).count(),1)

if __name__=='__main__':unittest.main()
