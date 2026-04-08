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

    def _detect_fase(self, mensaje_usuario: str, historial_lista: list[str]) -> str:
        msg = (mensaje_usuario or "").lower()

        if any(x in msg for x in [
            "visita", "agendar", "agendo", "cita", "quiero ir",
            "quiero conocer", "informes presenciales"
        ]):
            return "cita"

        if any(x in msg for x in [
            "ya fui", "ya asistí", "ya asistimos", "inscripción",
            "inscribir", "inscribirlo", "inscribirla", "control escolar"
        ]):
            return "post_visita"

        if len(historial_lista) >= 4:
            return "seguimiento"

        if any(x in msg for x in [
            "costo", "costos", "precio", "precios",
            "colegiatura", "colegiaturas", "inscripción"
        ]):
            return "nutricion"

        return "calificacion"

    def _detect_tono(self, mensaje_usuario: str, historial_lista: list[str]) -> str:
        msg = (mensaje_usuario or "").lower()

        if any(x in msg for x in [
            "quiero agendar", "quiero visitar", "quiero conocer",
            "me interesa mucho", "me interesa visitar",
            "quiero inscribir", "quiero inscribirlo", "quiero inscribirla"
        ]):
            return "alto_interes_consultivo"

        if any(x in msg for x in [
            "informes", "información", "costos", "precio",
            "colegiatura", "horarios", "ubicación"
        ]):
            return "interes_medio_orientador"

        return "bajo_interes_prudente"

    def _detect_tema(self, mensaje_usuario: str) -> str | None:
        msg = (mensaje_usuario or "").lower()

        if any(x in msg for x in ["ipad", "i pad", "pantalla", "pantallas", "tecnología", "tecnologia", "knotion"]):
            return "pantallas_ipad"

        if any(x in msg for x in ["matem", "númer", "numer", "cálculo", "calculo", "aloha"]):
            return "matematico"

        if any(x in msg for x in ["leer", "lectura", "comprensión", "comprension"]):
            return "lectura"

        if any(x in msg for x in ["emoc", "conducta", "disciplina", "valores", "comportamiento"]):
            return "inteligencia_emocional"

        if any(x in msg for x in ["motriz", "física", "fisica", "deporte", "judo", "gimnasia"]):
            return "motriz"

        if any(x in msg for x in ["música", "musica", "violín", "violin", "arte", "artístico", "artistico"]):
            return "artistico_musical"

        return None

    def _detect_contexto_temporal(self, historial_lista: list[str]) -> str:
        if len(historial_lista) >= 6:
            return "reingreso_tardio"
        return "nuevo"

    def build_prompt(self, mensaje_usuario: str, historial_lista: list[str]) -> str:
        fase = self._detect_fase(mensaje_usuario, historial_lista)
        tono = self._detect_tono(mensaje_usuario, historial_lista)
        tema = self._detect_tema(mensaje_usuario)
        contexto_temporal = self._detect_contexto_temporal(historial_lista)

        bloques = []

        # CORE
        bloques.append(self._read_file("core/reglas_base.txt"))
        bloques.append(self._read_file("core/estrategia_conversacional.txt"))
        bloques.append(self._read_file("core/reglas_costos.txt"))

        # SHARED
        bloques.append(self._read_file(f"shared/fases/{fase}.txt"))
        bloques.append(self._read_file(f"shared/tonos/{tono}.txt"))
        bloques.append(self._read_file(f"shared/contextos_temporales/{contexto_temporal}.txt"))

        # NEGOCIO
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/contexto_general.txt"))
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/propuesta_valor.txt"))
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/politicas_comerciales.txt"))
        bloques.append(self._read_file("businesses/colegio_valle_filadelfia_santa_cruz/negocio/agenda_y_visitas.txt"))

        # ACCIÓN OPERATIVA
        if fase == "cita":
            bloques.append(self._read_file("shared/acciones/consultar_disponibilidad.txt"))

        # TEMA OPCIONAL
        if tema:
            bloques.append(self._read_file(
                f"businesses/colegio_valle_filadelfia_santa_cruz/temas/{tema}.txt"
            ))

        historial_texto = "\n".join(historial_lista[-5:]) if historial_lista else "Sin historial reciente."

        prompt_final = "\n\n".join([b for b in bloques if b])

        prompt_final += f"\n\nHISTORIAL RECIENTE:\n{historial_texto}"
        prompt_final += f"\n\nMENSAJE ACTUAL DEL USUARIO:\n{mensaje_usuario}"
        prompt_final += (
            "\n\nINSTRUCCIÓN FINAL:\n"
            "Responda en español, en un solo bloque claro, con trato de usted, "
            "sin contradecir el historial, y cerrando con una pregunta si corresponde "
            "para avanzar la conversación."
        )

        return prompt_final
