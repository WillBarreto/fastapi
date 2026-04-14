import os


class PromptManager:
    def __init__(self, base_path: str = "prompts"):
        self.base_path = base_path
        self.business_path = os.path.join(
            self.base_path,
            "businesses",
            "colegio_valle_filadelfia_santa_cruz"
        )

    def _read_file(self, relative_path: str) -> str:
        full_path = os.path.join(self.base_path, relative_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""
        except Exception:
            return ""

    def _detect_tono(self, mensaje_usuario: str, historial_lista: list[str]) -> str:
        msg = (mensaje_usuario or "").lower()

        if any(x in msg for x in [
            "quiero agendar", "quiero visitar", "quiero conocer",
            "me interesa mucho", "me interesa visitar",
            "quiero inscribir", "quiero inscribirlo", "quiero inscribirla",
            "sí quiero cita", "si quiero cita", "sí me interesa visitar",
            "si me interesa visitar"
        ]):
            return "alto_interes_consultivo"

        if any(x in msg for x in [
            "informes", "información", "costos", "precio",
            "colegiatura", "horarios", "ubicación",
            "inscripción", "escuela", "colegio"
        ]):
            return "interes_medio_orientador"

        return "bajo_interes_prudente"

    def _detect_tema(self, mensaje_usuario: str) -> str | None:
        msg = (mensaje_usuario or "").lower()

        if any(x in msg for x in [
            "ipad", "i pad", "pantalla", "pantallas",
            "tecnología", "tecnologia", "knotion"
        ]):
            return "pantallas_ipad"

        if any(x in msg for x in [
            "matem", "númer", "numer", "cálculo", "calculo", "aloha"
        ]):
            return "matematico"

        if any(x in msg for x in [
            "leer", "lectura", "comprensión", "comprension"
        ]):
            return "lectura"

        if any(x in msg for x in [
            "emoc", "conducta", "disciplina", "valores", "comportamiento"
        ]):
            return "inteligencia_emocional"

        if any(x in msg for x in [
            "motriz", "física", "fisica", "deporte", "judo", "gimnasia",
            "ligamentos", "articulaciones"
        ]):
            return "motriz"

        if any(x in msg for x in [
            "música", "musica", "violín", "violin",
            "arte", "artístico", "artistico", "suzuki"
        ]):
            return "artistico_musical"

        return None

    def build_prompt(self, mensaje_usuario: str, historial_lista: list[str], estado: str) -> str:
        tono = self._detect_tono(mensaje_usuario, historial_lista)
        tema = self._detect_tema(mensaje_usuario)

        bloques = []

        # CORE
        bloques.append(self._read_file("core/reglas_base.txt"))
        bloques.append(self._read_file("core/reglas_costos.txt"))
        bloques.append(self._read_file("core/embudo_inicial_maestro.txt"))

        # TONO
        bloques.append(self._read_file(f"shared/tonos/{tono}.txt"))

        # NEGOCIO
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/contexto_general.txt"))
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/propuesta_valor.txt"))
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/politicas_comerciales.txt"))
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/agenda_y_visitas.txt"))

        # TEMA OPCIONAL
        if tema:
            bloques.append(
                self._read_file(
                    f"businesses/colegio_valle_filadelfia_santa_cruz/temas/{tema}.txt"
                )
            )

        historial_texto = "\n".join(historial_lista[-5:]) if historial_lista else "Sin historial reciente."

        prompt_final = "\n\n".join([b for b in bloques if b])

        prompt_final += f"\n\nESTADO ACTUAL DE LA CONVERSACIÓN:\n{estado}"
        prompt_final += f"\n\nHISTORIAL RECIENTE:\n{historial_texto}"
        prompt_final += f"\n\nMENSAJE ACTUAL DEL USUARIO:\n{mensaje_usuario}"

        prompt_final += (
            "\n\nINSTRUCCIÓN FINAL:\n"
            "Responda estrictamente según el estado actual y el flujo definido. "
            "No improvise. No salte pasos. No reinicie la conversación. "
            "No repita preguntas ya respondidas. "
            "Use formato claro para WhatsApp, con bloques breves y fáciles de leer."
            "Priorice siempre avanzar la conversación hacia la cita presencial cuando el prospecto muestre interés. "
        )

        return prompt_final
