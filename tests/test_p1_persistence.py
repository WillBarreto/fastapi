"""Real module and ORM, isolated SQLite, outbound calls blocked."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

class Persistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.env=patch.dict(os.environ,{'DATABASE_URL':'sqlite:///'+cls.tmp.name+'/isolated.db',
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
        self.c=m.Contact(phone_number='synthetic-'+self._testMethodName,status='EN_CALIFICACION',notes='ZONA_VALIDADA_AUTORITATIVA:Santa Cruz Atizapán\nHORA_CITA:13:00')
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

if __name__=='__main__':unittest.main()
