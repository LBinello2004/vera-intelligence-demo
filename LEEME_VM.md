# Refresco automático del Data Map (VM)

Guía completa de cómo armar la VM que actualiza solo los Data Maps y cómo esos cambios llegan a la app:
**`experimental_proyects/Vera Intelligence/10. documentos/REFRESCO_DIARIO_VM.md`**

Resumen: un cron en la VM corre `run_daily_refresh.py` todos los días hábiles; si cambió algún prompt de negocio regenera el Data Map del cliente, lo valida con un gate y,
solo si pasa, lo publica con un commit y un push a `main`, que Streamlit Cloud toma solo. Si algo necesita revisión humana, lo escribe en `.runtime/daily_refresh/<fecha>.md`
y, si se configura `VI_NOTIFY_WEBHOOK`, lo avisa a ese canal.
