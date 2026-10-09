"""Pipeline de la actualización automática del Data Map (2026-10-07): detección con cambio pendiente, reintentos, candado, reparación
del YAML y código de salida. Todo con dobles: no llama a Langfuse, Gemini ni Postgres."""
from __future__ import annotations

import json
from datetime import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "4. scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import data_map_auto_update as dmu  # noqa: E402
import data_map_log  # noqa: E402
import data_map_store as dms  # noqa: E402

CLIENT = "acme_alto"


class FakeRepository:
    """BusinessRulesRepository mínimo: `get(refresh=True)` devuelve la versión de 'Langfuse' y refresca la foto del agente."""
    langfuse = {"version": 1, "text": "regla v1"}
    cache: dict | None = None
    fail_status: int | None = None     # simula un prompt que Langfuse no entrega (404 u otro código); el agente cae a su última copia

    def __init__(self, client_config, project_root) -> None:
        self.fetch_errors = {}

    def available_rulebooks(self):
        return ("rb",)

    def _read_cache(self, key):
        return None if FakeRepository.cache is None else dict(FakeRepository.cache)

    def get(self, key, refresh=False):
        payload = {"rules_version": self.langfuse["version"], "business_scope": "ventas", "criteria_text": self.langfuse["text"]}
        if FakeRepository.fail_status is not None:
            error = RuntimeError("fallo de Langfuse")
            error.response = SimpleNamespace(status_code=FakeRepository.fail_status)
            self.fetch_errors[key] = error
            return json.dumps({**(FakeRepository.cache or payload), "source_status": "last_known_good"})
        if refresh:
            FakeRepository.cache = dict(payload)
        return json.dumps(payload)


class PipelineCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)
        self.maps = self.project / "2. clientes" / CLIENT / "data_map"
        self.maps.mkdir(parents=True)
        self.v1 = self.maps / "VI Data Map Acme V1.yaml"
        self.v1.write_text("metadata: {name: v1}\nsources: {a: {source: dashboard_v2.vw_a, fields: {x: {}}}}\n", encoding="utf-8")
        self.store = dms.LocalStore(project_root=self.project)
        FakeRepository.cache, FakeRepository.langfuse, FakeRepository.fail_status = None, {"version": 1, "text": "regla v1"}, None
        self.config = SimpleNamespace(data_map_path=self.v1, display_name="Acme", model="m", client_id="acme", tenant="Acme",
                                      business_rulebooks={"rb": SimpleNamespace(name="clientes/Acme/Ventas/checklist")})
        patcher = patch.object(dmu, "BusinessRulesRepository", FakeRepository)
        patcher.start()
        self.addCleanup(patcher.stop)


class DetectPendingChangesTests(PipelineCase):
    def detect(self):
        return dmu.detect_pending_changes(self.config, self.store, CLIENT)

    def test_la_primera_vez_no_es_un_cambio_y_devuelve_la_version_actual(self) -> None:
        changes, current = self.detect()
        self.assertEqual((changes, current), ([], {"rb": 1}))

    def test_un_cambio_respecto_de_la_foto_del_agente_se_detecta_con_el_texto_viejo(self) -> None:
        self.detect()                                           # el agente queda con la v1
        FakeRepository.langfuse = {"version": 2, "text": "regla v2"}
        changes, current = self.detect()
        self.assertEqual([(c.key, c.old_version, c.new_version, c.old_text, c.new_text) for c in changes],
                         [("rb", 1, 2, "regla v1", "regla v2")])
        self.assertEqual(current, {"rb": 2})
        self.assertEqual(self.store.pending(CLIENT)["rb"]["old_text"], "regla v1")

    def test_un_cambio_no_promovido_sigue_pendiente_aunque_el_agente_ya_refresco_su_foto(self) -> None:
        self.detect()
        self.store.set_processed(CLIENT, {"rb": 1})              # lo último que se procesó (y promovió)
        FakeRepository.langfuse = {"version": 2, "text": "regla v2"}
        self.detect()                                            # detecta y refresca la foto del agente a v2
        changes, _ = self.detect()                               # segunda corrida: la foto ya dice v2, pero v2 nunca se promovió
        self.assertEqual([(c.old_version, c.new_version, c.old_text) for c in changes], [(1, 2, "regla v1")])

    def test_un_prompt_que_da_404_pide_revision_de_inmediato_y_no_es_un_cambio(self) -> None:
        self.detect()                                           # el agente ya tiene su copia
        FakeRepository.fail_status = 404
        problems: list = []
        changes, current = dmu.detect_pending_changes(self.config, self.store, CLIENT, problems=problems)
        self.assertEqual((changes, current), ([], {}))
        self.assertEqual(len(problems), 1)
        self.assertEqual((problems[0]["http_status"], problems[0]["needs_human"], problems[0]["prompt"]),
                         (404, True, "clientes/Acme/Ventas/checklist"))

    def test_un_error_que_no_es_404_solo_pide_revision_si_se_repite_y_se_olvida_al_recuperarse(self) -> None:
        self.detect()
        FakeRepository.fail_status = 503
        flags = []
        for _ in range(dmu.UNAVAILABLE_RUNS_BEFORE_ALERT):
            problems: list = []
            dmu.detect_pending_changes(self.config, self.store, CLIENT, problems=problems)
            flags.append(problems[0]["needs_human"])
        self.assertEqual(flags, [False] * (dmu.UNAVAILABLE_RUNS_BEFORE_ALERT - 1) + [True])
        FakeRepository.fail_status = None
        recovered: list = []
        dmu.detect_pending_changes(self.config, self.store, CLIENT, problems=recovered)
        self.assertEqual((recovered, self.store.unavailable(CLIENT)), ([], {}))
        FakeRepository.fail_status = 503
        again: list = []
        dmu.detect_pending_changes(self.config, self.store, CLIENT, problems=again)
        self.assertEqual((again[0]["consecutive_runs"], again[0]["needs_human"]), (1, False))     # el conteo vuelve a empezar

    def test_un_rulebook_local_tiene_version_texto_y_no_rompe_ni_cuenta_como_cambio(self) -> None:
        # Caso real (2026-10-07): coaching_playbook es local y su rules_version es la cadena "local"; un int() la tumbaba.
        FakeRepository.langfuse = {"version": "local", "text": "playbook"}
        changes, current = self.detect()
        self.assertEqual((changes, current), ([], {"rb": "local"}))
        self.store.set_processed(CLIENT, current)
        self.assertEqual(self.store.processed_versions(CLIENT), {"rb": "local"})
        self.assertEqual(self.detect()[0], [])

    def test_la_primera_vez_visto_se_conserva_mientras_la_version_siga_igual_y_se_reinicia_con_otra(self) -> None:
        self.detect()
        FakeRepository.langfuse = {"version": 2, "text": "regla v2"}
        with patch.object(dmu.time, "time", return_value=1000.0):
            self.detect()
        with patch.object(dmu.time, "time", return_value=5000.0):
            self.detect()
        self.assertEqual(self.store.pending(CLIENT)["rb"]["first_seen_at"], 1000.0)     # sigue siendo la v2: no se reinicia
        FakeRepository.langfuse = {"version": 3, "text": "regla v3"}
        with patch.object(dmu.time, "time", return_value=9000.0):
            self.detect()
        self.assertEqual(self.store.pending(CLIENT)["rb"]["first_seen_at"], 9000.0)     # una versión nueva reinicia la espera

    def test_lo_promovido_deja_de_ser_un_cambio(self) -> None:
        self.detect()
        FakeRepository.langfuse = {"version": 2, "text": "regla v2"}
        changes, current = self.detect()
        self.store.promote(CLIENT, self.v1, rules_versions=current)
        self.assertEqual(self.detect()[0], [])


def fake_regeneration(path: Path | None, error: str | None = None):
    return dmu.RegenerationResult(candidate_path=path, changelog="cambió algo", tool_calls=[], error=error)


class RunForClientTests(PipelineCase):
    def setUp(self) -> None:
        super().setUp()
        self.v2 = self.maps / "VI Data Map Acme V2.yaml"
        self.v2.write_text("metadata: {name: v2}\nsources: {a: {source: dashboard_v2.vw_a, fields: {x: {}}}}\n", encoding="utf-8")
        self.change = dmu.RulebookChange("rb", "ventas", 1, 2, "viejo", "nuevo")
        self.calls = {"regenerate": 0, "promote": 0}
        self.models: list = []
        self.feedbacks: list = []
        self.gate_detail: dict = {}
        self.regenerated_changes: list = []
        self.gate_passes = True
        self.regen_error = None
        for target, value in (
            (dmu.vi_agent, ("configure_client", lambda *a, **k: None)),
            (dmu.vi_agent, ("load_environment", lambda *a, **k: None)),
            (dmu.vi_agent, ("CLIENT_CONFIG", self.config)),
            (dmu, ("_lint_bank_for_client", lambda folder: [])),
            (dmu, ("detect_pending_changes", lambda cfg, store, folder, problems=None: ([self.change], {"rb": 2}))),
            (dmu, ("regenerate_data_map", self._regenerate)),
            (dmu, ("_evaluate_candidate", self._evaluate)),
            (dmu, ("promote", self._promote)),
        ):
            patcher = patch.object(target, value[0], value[1])
            patcher.start()
            self.addCleanup(patcher.stop)

    def _regenerate(self, cfg, changes, *, store=None, client_folder=None, model=None, feedback=None, tool_budget=None, soft_deadline_seconds=None):
        self.calls["regenerate"] += 1
        if getattr(self, "raises", None) is not None:
            raise self.raises
        self.feedbacks.append(feedback)
        self.models.append(model)
        self.regenerated_changes.append(list(changes))
        return fake_regeneration(None if self.regen_error else self.v2, self.regen_error)

    def _evaluate(self, cfg, regeneration, folder, gate_mode):
        if self.gate_passes:
            return {"passed": True, "status": "promovido", "detail": {"gate": gate_mode, "gate_passed": True}}
        return {"passed": False, "status": "gate_fallo_no_promovido", "detail": {"gate": gate_mode, "gate_passed": False, **self.gate_detail}}

    def _promote(self, candidate, folder):
        self.calls["promote"] += 1

    def run_client(self, **kwargs):
        kwargs.setdefault("settle", 0)      # la espera de estabilidad se prueba aparte
        return dmu.run_for_client(CLIENT, store=self.store, **kwargs)

    # ---- camino feliz
    def test_promueve_deja_la_version_activa_lo_procesado_y_suelta_el_candado(self) -> None:
        summary = self.run_client()
        self.assertEqual(summary["status"], "promovido")
        self.assertEqual(self.store.active(CLIENT)["version"], 2)
        self.assertEqual(self.store.processed_versions(CLIENT), {"rb": 2})
        self.assertIsNone(self.store.lock_info(CLIENT))
        self.assertEqual(self.calls["promote"], 1)
        self.assertIsNotNone(self.store.last_check(CLIENT))

    def test_si_config_yaml_no_se_puede_escribir_igual_se_promueve_en_el_almacen(self) -> None:
        def boom(candidate, folder):
            raise ValueError("config.yaml de solo lectura")

        with patch.object(dmu, "promote", boom):
            summary = self.run_client()
        self.assertEqual(summary["status"], "promovido")
        self.assertFalse(summary["config_yaml_actualizado"])
        self.assertEqual(self.store.active(CLIENT)["version"], 2)

    # ---- reintentos automáticos
    def test_un_gate_que_falla_deja_el_cambio_pendiente_y_se_reintenta_en_la_proxima_corrida(self) -> None:
        self.gate_passes = False
        first = self.run_client()
        self.assertEqual(first["status"], "reintento_pendiente")       # todavía no requiere a nadie
        self.assertEqual(first["status_del_intento"], "gate_fallo_no_promovido")
        self.assertEqual(first["attempts"], dmu.RUN_ATTEMPTS)
        self.assertEqual(self.store.processed_versions(CLIENT), {})    # lo procesado NO avanzó
        self.assertIsNone(self.store.active(CLIENT))
        self.gate_passes = True                                        # en la próxima corrida el gate pasa
        second = self.run_client()
        self.assertEqual(second["status"], "promovido")
        self.assertEqual(self.store.processed_versions(CLIENT), {"rb": 2})

    def test_agotados_los_intentos_pide_revision_humana_y_deja_de_gastar(self) -> None:
        self.gate_passes = False
        self.run_client()                                              # intentos 1-2
        second = self.run_client()                                     # intentos 3-4: se agotan
        self.assertEqual(second["status"], "gate_fallo_no_promovido")
        self.assertIn(second["status"], dmu.NEEDS_HUMAN_STATUSES)
        regenerations = self.calls["regenerate"]
        third = self.run_client()                                      # ya no regenera
        self.assertEqual(third["status"], "reintentos_agotados")
        self.assertIn(third["status"], dmu.NEEDS_HUMAN_STATUSES)
        self.assertEqual(self.calls["regenerate"], regenerations)
        self.assertEqual(regenerations, dmu.MAX_ATTEMPTS_PER_VERSION)

    def test_un_error_de_regeneracion_tambien_se_reintenta_y_luego_pide_revision(self) -> None:
        self.regen_error = "YAML inválido"
        first = self.run_client()
        self.assertEqual((first["status"], first["status_del_intento"]), ("reintento_pendiente", "error_regeneracion"))
        second = self.run_client()
        self.assertEqual(second["status"], "error_regeneracion")
        self.assertIn(second["status"], dmu.NEEDS_HUMAN_STATUSES)

    def test_un_cambio_de_prompt_nuevo_tiene_sus_propios_intentos(self) -> None:
        self.gate_passes = False
        self.run_client()
        self.run_client()
        self.change = dmu.RulebookChange("rb", "ventas", 2, 3, "v2", "v3")   # otra versión del prompt
        third = self.run_client()
        self.assertEqual(third["status"], "reintento_pendiente")

    # ---- otros caminos
    def test_sin_cambios_siembra_la_linea_de_base(self) -> None:
        with patch.object(dmu, "detect_pending_changes", lambda cfg, store, folder, problems=None: ([], {"rb": 5})):
            summary = self.run_client()
        self.assertEqual(summary["status"], "sin_cambios")
        self.assertEqual(self.store.processed_versions(CLIENT), {"rb": 5})
        self.assertEqual(self.calls["regenerate"], 0)

    def test_dry_run_genera_la_candidata_y_no_evalua_ni_promueve(self) -> None:
        summary = self.run_client(dry_run=True)
        self.assertEqual(summary["status"], "candidata_generada_sin_gate_dry_run")
        self.assertIsNone(self.store.active(CLIENT))

    def test_si_otro_proceso_tiene_el_candado_no_hace_nada(self) -> None:
        self.store.acquire_lock(CLIENT, "otro", 3600)
        summary = self.run_client()
        self.assertEqual(summary["status"], "en_curso_por_otro_proceso")
        self.assertEqual(self.calls["regenerate"], 0)
        self.assertEqual(self.store.lock_info(CLIENT)["owner"], "otro")    # no se lo pisó

    def test_con_lock_owner_no_vuelve_a_tomar_el_candado_y_lo_suelta_al_terminar(self) -> None:
        self.store.acquire_lock(CLIENT, "padre", 3600)
        summary = self.run_client(lock_owner="padre")
        self.assertEqual(summary["status"], "promovido")
        self.assertIsNone(self.store.lock_info(CLIENT))

    def test_el_candado_se_suelta_aunque_la_corrida_falle(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("Postgres caído")

        with patch.object(dmu, "regenerate_data_map", boom):
            with self.assertRaises(RuntimeError):
                self.run_client()
        self.assertIsNone(self.store.lock_info(CLIENT))

    def test_cada_corrida_deja_un_registro_auditable(self) -> None:
        self.run_client()
        runs = list((self.project / ".runtime" / "data_map_updates" / CLIENT).glob("*.json"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(json.loads(runs[0].read_text(encoding="utf-8"))["status"], "promovido")

    def test_sin_cambios_pero_con_un_prompt_inaccesible_no_se_informa_sin_cambios(self) -> None:
        problem = {"key": "rb", "prompt": "clientes/Acme/Ventas/checklist", "http_status": 404, "error": "x", "consecutive_runs": 1, "needs_human": True}

        def detect(cfg, store, folder, problems=None):
            problems.append(problem)
            return [], {}

        with patch.object(dmu, "detect_pending_changes", detect):
            summary = self.run_client()
        self.assertEqual(summary["status"], "prompt_no_disponible")
        self.assertEqual(summary["rulebook_problems"], [problem])
        self.assertIn("prompt_no_disponible", dmu.NEEDS_HUMAN_STATUSES)

    def test_un_problema_que_aun_no_pide_revision_se_registra_pero_sigue_siendo_sin_cambios(self) -> None:
        problem = {"key": "rb", "prompt": "p", "http_status": 503, "error": "x", "consecutive_runs": 1, "needs_human": False}

        def detect(cfg, store, folder, problems=None):
            problems.append(problem)
            return [], {}

        with patch.object(dmu, "detect_pending_changes", detect):
            summary = self.run_client()
        self.assertEqual((summary["status"], summary["rulebook_problems"]), ("sin_cambios", [problem]))

    # ---- cambios cosméticos, espera de estabilidad, cascada de modelo y registro de cambios
    def test_un_cambio_cosmetico_se_acepta_sin_regenerar_ni_gastar(self) -> None:
        self.change = dmu.RulebookChange("rb", "ventas", 1, 2, "Si el cliente PIDE descuento, ofrecer cuotas.", "si el cliente pide  descuento ofrecer cuotas")
        summary = self.run_client()
        self.assertEqual(summary["status"], "cambio_cosmetico_sin_regenerar")
        self.assertEqual(self.calls["regenerate"], 0)
        self.assertEqual(self.store.processed_versions(CLIENT), {"rb": 2})      # queda al día: no se vuelve a evaluar
        self.assertNotIn(summary["status"], dmu.NEEDS_HUMAN_STATUSES)

    def test_el_segundo_intento_recibe_por_que_fallo_el_primero(self) -> None:
        self.gate_passes = False
        self.gate_detail = {"gate_detail": [{"id": "q03", "regression": True, "missing_numbers": ["93"], "answer": "Hubo casos."}]}
        self.run_client()
        self.assertEqual(self.feedbacks[0], None)
        self.assertIn("q03", self.feedbacks[1])
        self.assertIn("93", self.feedbacks[1])

    def test_gate_feedback_junta_estructura_campos_perdidos_regresiones_y_deriva(self) -> None:
        text = dmu.gate_feedback({
            "gate_structural_problems": ["fuentes eliminadas: x"],
            "gate_detail": [{"id": "q1", "dropped_fields": ["v: faltan en el candidato a"]},
                            {"id": "q2", "regression": True, "missing_numbers": ["10"], "answer": "nada"},
                            {"id": "q3", "error": "timeout"}],
            "column_drift": ["campo inexistente z"]})
        for expected in ("fuentes eliminadas: x", "q1", "faltan en el candidato a", "q2", "10", "q3", "timeout", "campo inexistente z"):
            self.assertIn(expected, text)
        self.assertEqual(dmu.gate_feedback({}), "")

    def test_un_cambio_real_nunca_se_toma_por_cosmetico(self) -> None:
        self.change = dmu.RulebookChange("rb", "ventas", 1, 2, "ofrecer cuotas", "ofrecer cuotas sin interés los martes")
        self.assertEqual(self.run_client()["status"], "promovido")
        self.assertEqual(self.calls["regenerate"], 1)

    def test_sin_texto_viejo_no_se_puede_afirmar_que_es_cosmetico(self) -> None:
        self.assertFalse(dmu.is_cosmetic_change(dmu.RulebookChange("rb", "v", 1, 2, None, "texto")))

    def test_si_algunos_cambios_son_cosmeticos_solo_se_regenera_por_los_reales(self) -> None:
        cosmetic = dmu.RulebookChange("a", "x", 1, 2, "Regla A.", "regla a")
        real = dmu.RulebookChange("b", "y", 3, 4, "regla b", "regla b modificada")
        with patch.object(dmu, "detect_pending_changes", lambda cfg, store, folder, problems=None: ([cosmetic, real], {"a": 2, "b": 4})):
            summary = self.run_client()
        self.assertEqual(summary["status"], "promovido")
        self.assertEqual([c.key for c in self.regenerated_changes[0]], ["b"])
        self.assertEqual(self.store.processed_versions(CLIENT), {"a": 2, "b": 4})   # los dos quedan al día

    def test_un_cambio_recien_visto_espera_antes_de_regenerar(self) -> None:
        import time as _time
        now = _time.time()
        self.store.put_pending(CLIENT, {"rb": {"old_version": 1, "new_version": 2, "old_text": "viejo", "first_seen_at": now - 3600}})
        waiting = self.run_client(settle=18)
        self.assertEqual(waiting["status"], "esperando_estabilidad")
        self.assertEqual((self.calls["regenerate"], self.store.attempts(CLIENT, "rb@2")), (0, 0))   # no gasta ni cuenta intentos
        self.assertNotIn(waiting["status"], dmu.NEEDS_HUMAN_STATUSES)
        self.store.put_pending(CLIENT, {"rb": {"old_version": 1, "new_version": 2, "old_text": "viejo", "first_seen_at": now - 19 * 3600}})
        self.assertEqual(self.run_client(settle=18)["status"], "promovido")

    def test_la_espera_se_puede_desactivar_y_no_aplica_al_dry_run(self) -> None:
        self.store.put_pending(CLIENT, {"rb": {"old_version": 1, "new_version": 2, "old_text": "viejo", "first_seen_at": __import__("time").time()}})
        self.assertEqual(self.run_client(settle=0)["status"], "promovido")

    def test_el_primer_intento_usa_el_modelo_barato_y_los_siguientes_el_del_cliente(self) -> None:
        self.gate_passes = False
        with patch.dict("os.environ", {"VI_REGEN_CHEAP_MODEL": "barato"}):
            self.run_client()                                          # 2 intentos en la corrida
        self.assertEqual(self.models, ["barato", "m"])

    def test_sin_modelo_barato_configurado_siempre_usa_el_del_cliente(self) -> None:
        with patch.dict("os.environ", {"VI_REGEN_CHEAP_MODEL": ""}):
            self.run_client()
        self.assertEqual(self.models, ["m"])

    def test_si_el_modelo_barato_pasa_el_gate_se_promueve_y_el_registro_dice_con_cual(self) -> None:
        with patch.dict("os.environ", {"VI_REGEN_CHEAP_MODEL": "barato"}):
            summary = self.run_client()
        self.assertEqual((summary["status"], summary["regeneration_model"]), ("promovido", "barato"))
        entries = data_map_log.read_entries(self.project / "2. clientes", CLIENT)
        self.assertEqual(entries[0]["modelo_regeneracion"], "barato")

    def test_la_promocion_deja_un_registro_con_versiones_resumen_y_como_revertir(self) -> None:
        summary = self.run_client()
        self.assertEqual((summary["version_anterior"], summary["version_nueva"]), (1, 2))
        entries = data_map_log.read_entries(self.project / "2. clientes", CLIENT)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry["tipo"], entry["version_anterior"], entry["version_nueva"]), ("promovido", 1, 2))
        self.assertEqual(entry["prompts"], [{"key": "rb", "old_version": 1, "new_version": 2}])
        self.assertIn("--client acme_alto --to 1", entry["revertir_con"])
        md = Path(summary["registro_de_cambios"]).read_text(encoding="utf-8")
        self.assertIn("Promovido: V1 → V2", md)
        self.assertIn("cambió algo", md)

    def test_si_falla_el_registro_igual_se_promueve(self) -> None:
        with patch.object(dmu.data_map_log, "append_entry", side_effect=OSError("disco lleno")):
            summary = self.run_client()
        self.assertEqual(summary["status"], "promovido")
        self.assertIn("registro_de_cambios_error", summary)

    def test_un_gate_que_falla_no_deja_registro_de_cambios(self) -> None:
        self.gate_passes = False
        self.run_client()
        self.assertEqual(data_map_log.read_entries(self.project / "2. clientes", CLIENT), [])


class InfrastructureFailureTests(RunForClientTests):
    """Una caída de Gemini, de la base o de la red no es un rechazo del candidato: no gasta intentos y solo pide revisión si se repite."""

    def setUp(self) -> None:
        super().setUp()
        from google.genai import errors as genai_errors
        self.errors = genai_errors
        self.raises = None

    def server_error(self, code=504):
        return self.errors.ServerError(code, {"error": {"message": "Deadline expired", "status": "DEADLINE_EXCEEDED"}})

    def test_un_504_de_gemini_no_gasta_intentos_ni_pide_revision_el_primer_dia(self) -> None:
        self.raises = self.server_error()
        summary = self.run_client()
        self.assertEqual((summary["status"], summary["kind"], summary["consecutive_days"], summary["needs_human"]),
                         (dmu.INFRA_STATUS, "transitorio", 1, False))
        self.assertEqual(self.store.attempts(CLIENT, dmu._change_signature([self.change])), 0)      # el intento se devolvió
        self.assertEqual(self.store.processed_versions(CLIENT), {})                                  # el cambio sigue sin procesar

    def test_el_mismo_dia_no_suma_a_la_racha_y_a_los_3_dias_pide_revision(self) -> None:
        self.raises = self.server_error()
        for _ in range(3):
            summary = self.run_client()
        self.assertEqual((summary["consecutive_days"], summary["needs_human"]), (1, False))
        for day in ("2026-10-10", "2026-10-11"):
            with patch.object(dmu, "datetime") as fake_dt:
                fake_dt.now.return_value = datetime.fromisoformat(day + "T11:00:00+00:00")
                summary = self.run_client()
        self.assertEqual((summary["consecutive_days"], summary["needs_human"]), (3, True))

    def test_un_403_o_un_modelo_inexistente_es_de_configuracion_y_pide_revision_de_inmediato(self) -> None:
        self.raises = self.errors.ClientError(403, {"error": {"message": "API key not valid", "status": "PERMISSION_DENIED"}})
        summary = self.run_client()
        self.assertEqual((summary["kind"], summary["needs_human"]), ("configuracion", True))

    def test_el_intento_en_curso_queda_marcado_y_se_limpia_al_terminar_con_un_resultado(self) -> None:
        signature = dmu._change_signature([self.change])
        self.raises = KeyError("bug")
        with self.assertRaises(KeyError):
            self.run_client()
        self.assertEqual(self.store._read(CLIENT, "inflight.json")["signature"], signature)      # el proceso murió con un intento en curso
        self.raises = None
        self.assertEqual(self.run_client()["status"], "promovido")
        self.assertIsNone(self.store._read(CLIENT, "inflight.json"))

    def test_al_empezar_se_descarta_un_en_curso_viejo(self) -> None:
        self.store.set_inflight(CLIENT, "rb@1")
        self.store.add_attempt(CLIENT, "rb@1")
        self.gate_passes = False                                       # un rechazo no limpia los intentos (una promoción sí)
        self.run_client()
        self.assertEqual(self.store.attempts(CLIENT, "rb@1"), 1)       # nunca se devolvió: el en curso viejo se borró al empezar
        self.assertIsNone(self.store._read(CLIENT, "inflight.json"))

    def test_un_bug_nuestro_no_se_disfraza_de_caida_y_rompe(self) -> None:
        self.raises = KeyError("campo")
        with self.assertRaises(KeyError):
            self.run_client()

    def test_un_resultado_normal_corta_la_racha(self) -> None:
        self.raises = self.server_error()
        self.run_client()
        self.assertEqual(self.store.infra_failures(CLIENT)["count"], 1)
        self.raises = None
        self.assertEqual(self.run_client()["status"], "promovido")
        self.assertEqual(self.store.infra_failures(CLIENT), {})

    def test_si_en_el_gate_solo_fallo_gemini_tampoco_se_gastan_intentos(self) -> None:
        with patch.object(dmu, "_evaluate_candidate", lambda *a, **k: {"passed": False, "status": dmu.INFRA_STATUS, "error": "504 DEADLINE", "detail": {}}):
            summary = self.run_client()
        self.assertEqual((summary["status"], summary["error"]), (dmu.INFRA_STATUS, "504 DEADLINE"))
        self.assertEqual(self.store.attempts(CLIENT, dmu._change_signature([self.change])), 0)

    def test_clasificacion_de_errores(self) -> None:
        self.assertEqual(dmu.classify_infrastructure_error(self.server_error(503)), "transitorio")
        self.assertEqual(dmu.classify_infrastructure_error(self.errors.ClientError(429, {"error": {"message": "cuota"}})), "transitorio")
        self.assertEqual(dmu.classify_infrastructure_error(self.errors.ClientError(400, {"error": {"message": "mal"}})), "configuracion")
        self.assertEqual(dmu.classify_infrastructure_error(ConnectionError("red")), "transitorio")
        self.assertIsNone(dmu.classify_infrastructure_error(ValueError("bug")))


class NextVersionTests(PipelineCase):
    def test_la_siguiente_version_va_despues_de_la_mas_alta_que_exista(self) -> None:
        # tras volver atrás (vigente V1) con una V5 ya escrita, la próxima candidata no puede pisar a la V5
        (self.maps / "VI Data Map Acme V5.yaml").write_text("x", encoding="utf-8")
        path, label, semver = dmu._next_data_map_path(self.v1)
        self.assertEqual((path.name, label, semver), ("VI Data Map Acme V6.yaml", "V6", "6.0.0"))

    def test_sin_versiones_mas_altas_es_la_siguiente_de_siempre(self) -> None:
        self.assertEqual(dmu._next_data_map_path(self.v1)[0].name, "VI Data Map Acme V2.yaml")


class RegenerationRepairTests(PipelineCase):
    VALID = ("===DATA_MAP_YAML===\nmetadata:\n  name: v2\nsources:\n  a:\n    source: dashboard_v2.vw_a\n===END_DATA_MAP_YAML===\n"
             "===CHANGELOG===\ncambió algo\n===END_CHANGELOG===")
    BROKEN = ("===DATA_MAP_YAML===\nmetadata:\n  name: algo: roto\nsources: {}\n===END_DATA_MAP_YAML===\n"
              "===CHANGELOG===\nx\n===END_CHANGELOG===")

    def regenerate(self, scripted):
        sent = []

        def send(chat, message, debug=False):
            sent.append(message)
            return SimpleNamespace(function_calls=[], text=scripted.pop(0))

        class FakeClient:
            def __init__(self, *a, **k):
                self.chats = SimpleNamespace(create=lambda **kw: object())

        change = dmu.RulebookChange("rb", "ventas", 1, 2, "viejo", "nuevo")
        with patch.dict("os.environ", {"VERA_AI_API_KEY": "x"}), patch.object(dmu.vi_agent.genai, "Client", FakeClient), \
                patch.object(dmu.vi_agent, "_send_message_with_retry", send):
            result = dmu.regenerate_data_map(self.config, [change], store=self.store, client_folder=CLIENT)
        return result, sent

    def test_un_yaml_invalido_se_le_devuelve_a_gemini_y_se_acepta_la_correccion(self) -> None:
        result, sent = self.regenerate([self.BROKEN, self.VALID])
        self.assertIsNone(result.error)
        self.assertEqual(len(sent), 2)
        self.assertIn("no se pudo usar", sent[1])          # la 2.ª llamada le pasa el error
        self.assertTrue(result.candidate_path.is_file())
        self.assertEqual(result.candidate_path.name, "VI Data Map Acme V2.yaml")

    def test_si_sigue_invalido_tras_las_reparaciones_devuelve_el_error(self) -> None:
        result, sent = self.regenerate([self.BROKEN] * (dmu.REGENERATION_REPAIRS + 1))
        self.assertIsNone(result.candidate_path)
        self.assertIn("inválida", result.error)
        self.assertEqual(len(sent), dmu.REGENERATION_REPAIRS + 1)

    def test_una_respuesta_valida_a_la_primera_no_pide_reparaciones(self) -> None:
        result, sent = self.regenerate([self.VALID])
        self.assertIsNone(result.error)
        self.assertEqual(len(sent), 1)


class MainExitCodeTests(unittest.TestCase):
    def run_main(self, status: str) -> int:
        with patch.object(dmu, "run_for_client", lambda *a, **k: {"client_id": "x", "status": status}), \
                patch.object(sys, "argv", ["data_map_auto_update.py", "--client", "x"]):
            with self.assertRaises(SystemExit) as ctx:
                dmu.main()
        return ctx.exception.code

    def test_sale_con_cero_salvo_que_alguien_tenga_que_mirar(self) -> None:
        for status in ("sin_cambios", "promovido", "reintento_pendiente", "en_curso_por_otro_proceso"):
            self.assertEqual(self.run_main(status), 0, status)
        for status in ("gate_fallo_no_promovido", "error_regeneracion", "deriva_de_columnas_no_promovido", "reintentos_agotados"):
            self.assertEqual(self.run_main(status), 1, status)


if __name__ == "__main__":
    unittest.main()
