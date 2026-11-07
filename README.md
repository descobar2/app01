# SOAST — TEECMO

Plataforma web para consultar y correlacionar tickets del sistema propietario, monitorear SLA, administrar empresas y perfiles, gestionar turnos y coordinar sesiones remotas.

**Estado:** MVP funcional local. No reemplaza el ticket legacy. Microsoft Graph / Teams admite autenticación con certificado; requiere configurar la identidad y el buzón organizador en el servidor. No marcar como confirmada una reunión sin respuesta de Graph o enlace válido de contingencia.

## Ejecución local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Cambiar obligatoriamente la contraseña de administrador y configurar variables de entorno
set -a; source .env; set +a
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Una sola vez, inicializar la cuenta de administrador:

```bash
curl -X POST http://127.0.0.1:8000/api/bootstrap
```

Abrir `http://127.0.0.1:8000`. La documentación de la API está en `/docs`.

**Docker:** `cp .env.example .env`, definir contraseña y ejecutar `docker compose up --build -d`. La publicación se limita a `127.0.0.1` deliberadamente; utilice HTTPS mediante proxy inverso para acceso remoto.

## Importación de información (local, sin publicar datos)

Importar primero las empresas y luego el archivo de eventos:

```bash
python -m app.ingest companies /ruta/privada/DatosTeecmo.xlsx
python -m app.ingest technicians /ruta/privada/DatosTeecmo.xlsx
python -m app.ingest logs /ruta/privada/0901261.txt
```

### Jornadas por fecha (CSV mensual)

El archivo `turnos_agentes_2026-10.csv` debe permanecer **local**. Contiene
`usuario,nivel,tipo,fecha,hora_inicio,hora_fin` y permite registrar
`TURNO1`, `TURNO2_00-06`, `TURNO2_20-24` y `GUARDIA_L3`.

Primero validar sin escribir en la base de datos:

```bash
python -m app.ingest shifts /ruta/privada/turnos_agentes_2026-10.csv --dry-run
```

El importador exige que cada usuario del CSV exista como técnico y tenga el mismo
nivel. Ante una diferencia de escritura, **no modifica la base de datos** y muestra
cuántas jornadas requieren correspondencia. Tras confirmar la identidad, puede
indicarse un alias explícito, sin renombrar la cuenta existente:

```bash
python -m app.ingest shifts /ruta/privada/turnos_agentes_2026-10.csv --dry-run --alias NOMBRE_CSV=NOMBRE_REGISTRADO
python -m app.ingest shifts /ruta/privada/turnos_agentes_2026-10.csv --alias NOMBRE_CSV=NOMBRE_REGISTRADO
```

La importación es atómica e idempotente, conserva la fecha y el tipo de jornada.
Para fechas cubiertas por el archivo, los turnos fechados prevalecen sobre los
semanales; los días sin turno permanecen no disponibles. `GUARDIA_L3` se
visualiza pero **no** habilita automáticamente reservas. En la interfaz **Turnos**
pueden consultarse meses, técnicos y tipos; no se atribuyen turnos de octubre a
noviembre ni se crean reuniones retroactivas.

Las cuentas técnicas importadas están **desactivadas** y sin contraseña conocida: el administrador debe provisionarles credenciales antes de habilitarlas. Los eventos se identifican por nombre de archivo y secuencia, evitando duplicados de relectura. Registros inválidos o empresas no reconocidas quedan separados como incidencias. Para otros orígenes se necesita una regla de mapeo previamente validada.

**IMPORTANTE:** `DatosTeecmo.xlsx` y `0901261.txt` contienen información de clientes y personal. El repositorio es público. No agregarlos al control de versiones, ni tampoco `.env`, bases de datos o logs.

## Permisos y sesiones

- `externo`: solo sus tickets y sesiones de empresa.
- `tecnico`: consulta tickets de todas las empresas en solo lectura; indicadores y sesiones propias; enlace de Teams únicamente en contingencia.
- `supervisor`: coordinación, turnos, auditoría, justificación de terceros.
- `coordinador`: coordinación y consultas; no modifica turnos.
- `gerencia`: indicadores consolidados sin operación.
- `administrador`: empresas, usuarios, auditoría, salud e ingesta; sin acceso implícito a tickets.

Acceso mediante token opaco de sesión con caducidad máxima de **8 horas** o **30 minutos de inactividad**, lo que ocurra primero. Al cerrar sesión se invalida el token en el servidor. Contraseñas `scrypt` con sal aleatoria.

## Alcance de esta entrega

Incluye ingesta idempotente CSV de 13 campos, correlación `empresa + ticket`, ordenación temporal, estados y propietario oficial, cálculo de SLA 4/24 horas con pausas, paneles por rol, consultas, exportación CSV con control de acceso, historial de escalaciones y auditoría, alta de empresas y usuarios, activación/desactivación y restablecimiento de contraseñas, turnos, ausencias, búsqueda de franjas locales, reservas sin solapamiento por bloques de 15 minutos, enlace manual autorizado y creación opcional de eventos en Graph cuando se configure.

**Pendiente para producción:** reprogramación con conciliación completa, sincronización automática/reintentos de cancelación en Graph, comprobación Free/Busy corporativa y respuestas de invitación, reglas automáticas de propuesta L1→L3, mensajería Teams/WhatsApp, exportación CSV/PDF, detalle completo de histórico/escalaciones y controles de respaldo y retención de auditoría. La disponibilidad local no equivale a confirmación de Outlook.

## Variables opcionales para Teams

`SOAST_GRAPH_TENANT_ID`, `SOAST_GRAPH_CLIENT_ID`, `SOAST_GRAPH_ORGANIZER`, `SOAST_GRAPH_CERT_PATH` y `SOAST_GRAPH_KEY_PATH`: identidad y rutas a archivos PEM del certificado público y la clave privada. MSAL obtiene y reutiliza tokens mediante **client credentials** sin almacenar secretos en Git. Para Docker, monte los archivos bajo `./secrets/` (excluido de Git); esta carpeta se expone como `/run/secrets/soast/` en modo solo lectura. Se mantiene compatibilidad opcional con `SOAST_GRAPH_CLIENT_SECRET` para despliegues existentes; se recomienda certificado. Se utiliza `Calendars.ReadWrite` sobre el buzón organizador; la confirmación requiere el enlace de Teams y la cancelación usa `POST /events/{id}/cancel` para notificar a asistentes. Los errores dejan `sync_pending` o `cancel_pending` y no se reportan como confirmación. Los eventos utilizan `transactionId` estable para reducir duplicados en reintentos. El reconciliador opcional (`docker compose --profile graph up -d` o `python -m app.graph_worker`) reintenta cada 5 minutos hasta 3 veces después del intento inicial; registra en auditoría las incidencias agotadas. No sustituye la revisión humana de discrepancias sin identificador externo. Se recomienda limitar el acceso de la aplicación a un buzón dedicado.

## Pruebas

```bash
pip install pytest httpx
pytest -q
```

Para despliegue real: PostgreSQL (`SOAST_DATABASE_URL=postgresql+psycopg://...`), HTTPS, respaldo, migraciones Alembic, restricciones de acceso y programación de pruebas de integración.