"""Entrypoint sin espacios para Streamlit Community Cloud.

Streamlit Cloud tropieza con espacios en el "Main file path" (los rompe por espacio al armar
la instalación de dependencias -ver el log de deploy real: "Failed to parse: 'Intelligence/4.'").
Este archivo sólo ejecuta el streamlit_app.py real, que sigue viviendo en su carpeta original
("experimental_proyects/Vera Intelligence/4. scripts/") sin cambiar nada de esa estructura -el
resto del código del proyecto referencia esos nombres de carpeta como strings literales, así que
renombrarlas ahí habría roto imports y rutas en todo el proyecto.
"""
import runpy
from pathlib import Path

_REAL_APP = (
    Path(__file__).resolve().parent
    / "experimental_proyects"
    / "Vera Intelligence"
    / "4. scripts"
    / "streamlit_app.py"
)

runpy.run_path(str(_REAL_APP), run_name="__main__")
