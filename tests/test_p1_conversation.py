"""Anonymized scenarios reconstructed from audit IDs 395/397; no real PII."""
import unittest
from types import SimpleNamespace
from offline_main import load_main


class ConversationRules(unittest.TestCase):
    def setUp(self):
        self.n = load_main()
        self.contact = SimpleNamespace(id=1, status="EN_CALIFICACION", notes="")
        self.context = {"zona_interes": "Santa Cruz Atizapán", "alumnos": [],
                        "hitos_comerciales": ["RECIBIO_PRESENTACION_VALOR", "RECIBIO_EXPLICACION_METODO"]}

    def decide(self, analysis, message, context=None):
        return self.n["aplicar_reglas_negocio_estructuradas"](
            analysis, self.contact, message, self.context if context is None else context)

    def test_known_zone_pre_kinder_does_not_restart_zone(self):
        d = self.decide({"fecha_nacimiento_iso": "2024-06-15", "edad_alumno": 2,
                         "nivel": "Kínder", "pide_costos": True}, "¿Y la colegiatura?")
        self.assertNotIn(d["accion"], {"ORIENTAR_PRE_KINDER", "PEDIR_ZONA", "PEDIR_FECHA_NACIMIENTO"})

    def test_pre_kinder_inferred_level_is_available_for_tuition(self):
        d = self.decide({"fecha_nacimiento_iso": "2024-06-15", "pide_costos": True}, "¿Y la colegiatura?")
        self.assertEqual(d["accion"], "RESPONDER_COSTOS")
        self.assertEqual(self.n["obtener_niveles_costos_solicitados"]({}, d), ["Kínder"])

    def test_single_child_birthdate_is_reused(self):
        self.context["alumnos"] = [{"nombre": "Alumno A", "nivel_interes": "Kínder", "fecha_nacimiento": "2024-06-15"}]
        d = self.decide({"edad_alumno": 2, "requiere_validar_pre_kinder": True}, "Ya le di la fecha, ¿qué sigue?")
        self.assertNotEqual(d["accion"], "PEDIR_FECHA_NACIMIENTO")

    def test_another_child_must_not_inherit_birthdate(self):
        self.context["alumnos"] = [{"nombre": "Alumno A", "nivel_interes": "Kínder", "fecha_nacimiento": "2024-06-15"}]
        d = self.decide({"edad_alumno": 2, "requiere_validar_pre_kinder": True}, "Es para mi otra hija de dos años")
        self.assertEqual(d["accion"], "PEDIR_FECHA_NACIMIENTO")

    def test_multiple_children_must_not_guess_birthdate(self):
        self.context["alumnos"] = [{"nombre": "Alumno A", "fecha_nacimiento": "2024-06-15"},
                                   {"nombre": "Alumno B", "fecha_nacimiento": "2023-06-15"}]
        d = self.decide({"edad_alumno": 2, "requiere_validar_pre_kinder": True}, "Tiene dos años")
        self.assertEqual(d["accion"], "PEDIR_FECHA_NACIMIENTO")

    def test_unknown_zone_pre_kinder_still_requires_zone(self):
        d = self.decide({"fecha_nacimiento_iso": "2024-06-15", "edad_alumno": 2}, "Tiene dos años", {})
        self.assertEqual(d["accion"], "ORIENTAR_PRE_KINDER")
        self.assertFalse(d["puede_compartir_costos"])

    def test_human_request_must_not_disappear_into_pause(self):
        d = self.decide({"requiere_admin_contextual": True, "pausa_conversacion": True},
                        "Espero a que una persona me atienda, por favor")
        self.assertTrue(d["requiere_admin"])
        self.assertEqual(d["accion"], "CONSULTAR_ADMIN")

    def test_plain_pause_is_not_an_admin_request(self):
        d = self.decide({"pausa_conversacion": True}, "Voy a revisar mi agenda y les escribo")
        self.assertEqual(d["accion"], "SEGUIMIENTO")
        self.assertFalse(d["requiere_admin"])

    def test_decline_does_not_create_an_admin_task(self):
        d = self.decide({"cierre_comercial_sin_seguimiento": True}, "Elegimos otra escuela, gracias")
        self.assertEqual(d["accion"], "CIERRE_COMERCIAL")
        self.assertFalse(d["requiere_admin"])

    def test_tuition_reference_is_short_and_authorized(self):
        d = {"accion": "RESPONDER_COSTOS", "datos_detectados": {}, "puede_compartir_costos": True}
        r = self.n["generar_respuesta_final_estructurada"]("¿Cuánto cuesta primaria?", {"nivel": "Primaria"}, d, {})
        self.assertTrue(r["generada"])
        price = self.n["cargar_configuracion_precios"]()["colegiaturas_mensuales"]["Primaria"]
        self.assertIn(f"${price:,.0f}", r["respuesta"])
        self.assertIn("aproximadamente", r["respuesta"])
        self.assertLess(len(r["respuesta"]), 500)
        self.assertNotIn("semanal", r["respuesta"])

    def test_only_requested_levels_are_quoted(self):
        d = {"accion": "RESPONDER_COSTOS", "datos_detectados": {}, "puede_compartir_costos": True}
        r = self.n["generar_respuesta_final_estructurada"]("Para primaria y kínder", {"alumnos": [{"nivel": "Primaria"}, {"nivel": "Kínder"}]}, d, {})
        self.assertIn("Primaria", r["respuesta"])
        self.assertIn("Preescolar", r["respuesta"])
        self.assertNotIn("Secundaria", r["respuesta"])

    def test_extras_after_price_do_not_repeat_tuition(self):
        self.context["hitos_comerciales"].append("RECIBIO_COSTOS")
        a = {"pide_costos": True, "alcance_consulta_costos": "DETALLE", "nivel": "Primaria"}
        d = self.decide(a, "¿Cuáles son los gastos adicionales?")
        self.assertEqual(d["accion"], "RESPONDER_TEMA")
        p = self.n["construir_plan_respuesta_estructurada"](a, d)
        r = self.n["generar_respuesta_final_estructurada"]("¿Cuáles son los gastos adicionales?", a, d, p)
        self.assertTrue(r["generada"])
        self.assertNotIn("$", r["respuesta"])
        self.assertIn("visita", r["respuesta"])

    def test_new_semantic_field_survives_normalization(self):
        a = self.n["normalizar_analisis_mensaje_ia"]({"alcance_consulta_costos": "detalle"})
        self.assertEqual(a.get("alcance_consulta_costos"), "DETALLE")

    def test_invalid_semantic_field_uses_compatible_default(self):
        a = self.n["normalizar_analisis_mensaje_ia"]({"alcance_consulta_costos": None})
        self.assertEqual(a.get("alcance_consulta_costos"), "COLEGIATURA")

    def test_first_price_question_still_presents_context(self):
        c = {"zona_interes": "Santa Cruz Atizapán", "hitos_comerciales": []}
        d = self.decide({"pide_costos": True, "nivel": "Primaria"}, "¿Cuánto cuesta?", c)
        self.assertEqual(d["accion"], "PRESENTAR_PROPUESTA_VALOR")
        self.assertFalse(d["puede_compartir_costos"])

    def test_unknown_zone_still_blocks_price(self):
        d = self.decide({"pide_costos": True, "nivel": "Primaria"}, "¿Cuánto cuesta?", {})
        self.assertEqual(d["accion"], "PEDIR_ZONA")
        self.assertFalse(d["puede_compartir_costos"])

    def test_ambiguous_new_birthdate_is_not_replaced(self):
        self.context["alumnos"] = [{"nombre": "Alumno A", "fecha_nacimiento": "2024-06-15"}]
        d = self.decide({"edad_alumno": 2, "fecha_nacimiento_texto": "No, nació el 5/6", "requiere_validar_pre_kinder": True}, "No, nació el 5/6")
        self.assertEqual(d["accion"], "PEDIR_FECHA_NACIMIENTO")

    def test_new_named_child_does_not_inherit_birthdate(self):
        self.context["alumnos"] = [{"nombre": "Alumno A", "fecha_nacimiento": "2024-06-15"}]
        d = self.decide({"edad_alumno": 2, "nombre_alumno": "Alumno B", "requiere_validar_pre_kinder": True}, "Alumno B tiene dos años")
        self.assertEqual(d["accion"], "PEDIR_FECHA_NACIMIENTO")

    def test_structured_different_level_does_not_inherit_birthdate(self):
        self.context["alumnos"] = [{"nombre": "Alumno A", "nivel_interes": "Primaria", "fecha_nacimiento": "2018-06-15"}]
        d = self.decide({"edad_alumno": 2, "alumnos": [{"nivel": "Kínder"}], "requiere_validar_pre_kinder": True}, "Tiene dos años")
        self.assertEqual(d["accion"], "PEDIR_FECHA_NACIMIENTO")

    def test_confirmed_visit_does_not_get_new_previsit_restriction(self):
        self.context["hitos_comerciales"].extend(["RECIBIO_COSTOS", "VISITA_REALIZADA"])
        self.context["estado_comercial"] = "VISITA_REALIZADA"
        d = self.decide({"pide_costos": True, "alcance_consulta_costos": "DETALLE", "nivel": "Primaria"}, "¿Y la inscripción?")
        self.assertFalse(d["datos_detectados"].get("detalle_costos_reservado_visita"))

    def test_human_request_keeps_commercial_stage_without_inventing_appointment(self):
        a = {"requiere_admin_contextual": True, "pausa_conversacion": True}
        d = self.decide(a, "Espero a que alguien me atienda")
        c = {"estado_comercial": "PROSPECTO_INFORMADO", "etapa_conversacional": "EXPLICACION_METODO",
             "objetivo_pendiente": "OBTENER_AREA_INTERES", "hitos_comerciales": ["RECIBIO_EXPLICACION_METODO"]}
        t = self.n["calcular_transicion_comercial_post_envio"]({"analisis": a, "decision": d}, c)
        self.assertEqual(t["estado_comercial"], c["estado_comercial"])
        self.assertNotIn("CITA_SOLICITADA", t["hitos_comerciales"])
        self.assertEqual(t["objetivo_pendiente"], "ESPERAR_CONFIRMACION_ADMIN")

    def test_human_request_dry_run_requires_escalation_without_sending(self):
        a = {"requiere_admin_contextual": True, "pausa_conversacion": True}
        d = self.decide(a, "Espero a que alguien me atienda")
        result = self.n["procesar_escalacion_admin_estructurada"](
            None, self.contact, "Espero a que alguien me atienda", "", {"analisis": a, "decision": d})
        self.assertTrue(result["requiere_escalacion"])
        self.assertFalse(result["ejecutada"])
        self.assertIsNone(result["tarea_id"])

    def test_missing_tariff_does_not_invent_an_amount(self):
        self.n["obtener_colegiatura_autorizada"] = lambda nivel: None
        d = {"accion": "RESPONDER_COSTOS", "datos_detectados": {}}
        r = self.n["generar_respuesta_final_estructurada"]("¿Cuánto cuesta?", {"nivel": "Primaria"}, d, {})
        self.assertEqual(r["error"], "PRECIO_AUTORIZADO_NO_DISPONIBLE")
        self.assertNotIn("$", r["respuesta"])


if __name__ == "__main__":
    unittest.main()
