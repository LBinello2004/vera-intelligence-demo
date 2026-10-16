"""Rescate cualitativo (2026-10-16): si la verificación de cifras falla del todo, se conserva la parte sin cifras en vez de un mensaje sin salida."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "4. scripts"))

import answer_verification as av  # noqa: E402

CONSEJO = (
    "### Qué hacer cuando un cliente pide la baja\n\n"
    "Primero preguntá el motivo real antes de ofrecer cualquier descuento: muchas veces el cliente no se va por el precio sino por una mala experiencia previa, "
    "y escucharlo con calma baja la tensión de la conversación.\n\n"
    "Después ofrecé una alternativa que no sea un descuento, como ajustar el plan a su uso o resolver el problema técnico que lo molesta, y recién "
    "si no alcanza, negociá un beneficio de menor a mayor.\n\n"
    "Si el cliente mantiene su decisión, radicá la cancelación en el momento y entregá el número de radicado; demorarla daña la confianza y suma un reclamo.\n"
)
CON_CIFRAS = (
    "**Resultados:**\n"
    "- La retención exitosa fue de 23,10% (6.315 de 27.334 casos).\n"
    "1. **Factura alta:** 39,45% de los retiros\n"
    "| Región | Tasa |\n|---|---|\n| Sur | 14,44% |\n\n"
    "```vera-chart\n{\"type\":\"bar\",\"title\":\"x\",\"labels\":[\"a\"],\"values\":[1]}\n```\n"
    "```vera-suggestions\n[\"¿Y por región?\"]\n```\n"
)


class SalvageTests(unittest.TestCase):
    def test_se_conserva_lo_cualitativo_y_se_quitan_las_cifras_los_graficos_y_las_tablas(self):
        out = av.salvage_qualitative(CONSEJO + "\n" + CON_CIFRAS, {})
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith(av.SALVAGE_NOTE))
        self.assertIn("preguntá el motivo real", out)
        self.assertIn("radicá la cancelación", out)
        for gone in ("23,10", "6.315", "39,45", "14,44", "vera-chart", "vera-suggestions", "| Región"):
            self.assertNotIn(gone, out)

    def test_una_lista_numerada_sin_cifras_conserva_sus_items(self):
        text = "Los pasos recomendados son:\n1. Escuchá el motivo sin interrumpir al cliente y reconocé su molestia con tus palabras.\n2. Ofrecé una alternativa antes de un descuento y explicá el beneficio en concreto.\n3. Cerrá confirmando qué va a pasar y cuándo, y dejá constancia de la gestión realizada con el cliente en la conversación." * 2
        out = av.salvage_qualitative(text, {})
        self.assertIsNotNone(out)
        self.assertIn("1. Escuchá el motivo", out)

    def test_fechas_y_anios_no_hacen_perder_la_linea(self):
        text = CONSEJO + "\nEste análisis cubre el período del 16 de julio de 2026 al 15 de octubre de 2026 y no cambia la recomendación anterior sobre el trato con el cliente.\n"
        out = av.salvage_qualitative(text, {})
        self.assertIsNotNone(out)
        self.assertIn("16 de julio de 2026", out)

    def test_si_queda_muy_poco_devuelve_none_y_se_usa_el_mensaje_de_siempre(self):
        self.assertIsNone(av.salvage_qualitative(CON_CIFRAS, {}))
        self.assertIsNone(av.salvage_qualitative("La tasa fue 23,10%.", {}))
        self.assertIsNone(av.salvage_qualitative("", {}))
        self.assertIsNone(av.salvage_qualitative(None, {}))

    def test_se_quitan_las_frases_de_certeza_y_el_lenguaje_de_frecuencia_en_los_patrones(self):
        text = (CONSEJO + "\n**En conversaciones reales:** en las interacciones revisadas se observa que el asesor informa el costo vigente y deriva el trámite sin explorar alternativas.\n"
                "Los asesores suelen limitarse a informar los requisitos de la cancelación sin ofrecer un ajuste.\n"
                "Esto garantiza que el cliente se va.\n")
        out = av.salvage_qualitative(text, {})
        self.assertIsNotNone(out)
        self.assertNotIn("suelen", out)
        self.assertNotIn("garantiza", out)
        self.assertIn("informa el costo vigente", out)
        self.assertFalse(av.verify_answer(out, {}).errors)

    def test_no_deja_encabezados_huerfanos(self):
        text = "### Resultados\n- Tasa: 23,10%\n\n### Recomendaciones\n" + CONSEJO.split("\n\n", 1)[1]
        out = av.salvage_qualitative(text, {})
        self.assertIsNotNone(out)
        self.assertNotIn("### Resultados", out)
        self.assertIn("### Recomendaciones", out)

    def test_el_resultado_siempre_pasa_la_verificacion(self):
        for text in (CONSEJO, CONSEJO + CON_CIFRAS):
            out = av.salvage_qualitative(text, {})
            self.assertIsNotNone(out)
            self.assertFalse(av.verify_answer(out, {}).errors)


if __name__ == "__main__":
    unittest.main()
