# Project 11 — Pass 2 Report

---

## 1. Objetivo y alcance

Pass 2 convirtió Notion en la fuente de contenido primaria en tiempo de
ejecución, sustituyó los cron por item de APScheduler por un único job de
polling periódico y añadió la sincronización de vuelta (la aplicación
escribe de nuevo en la fuente).

Dentro del alcance:

- lectura de la base editorial de Notion (esquema asumido, creado a mano
  por el usuario; la app no lo crea ni lo migra);
- selección de fuente con `CONTENT_SOURCE=auto|notion|yaml` y
  fallo explícito ante una configuración de Notion incompleta;
- polling periódico con `CONTENT_POLL_INTERVAL_SECONDS` (default 60);
- write-back del estado, la copia generada, la URL publicada y el error;
- terminar en `Ready` (nunca `Published`) cuando `DRY_RUN=true`.

Fuera de alcance (no implementado ni simulado): SQLite, idempotencia,
reintentos, generación de imágenes, Instagram/Facebook, web UI, webhooks
de Notion y scraping de referencias.

## 2. Arquitectura resultante

```
Notion (base editorial)  ←—— update_item: estado/copia/URL/error
      ↓  Status=Scheduled y Scheduled At <= ahora
ContentSource (NotionContentSource | YAMLContentSource)
      ↓  ContentItem
ContentScheduler (1 job de polling / cron legado YAML)
      ↓
ContentOrchestrator  → GeminiGenerator → validation → XPublisher
      ↓
ExecutionLog (log/posts.jsonl)
```

La regla de Pass 1 se mantiene: **Notion solo se habla en
`app/sources/notion_client.py` y `app/sources/notion_source.py`**. El
resto de la aplicación solo conoce `ContentItem`,
`ContentSource.update_item` y `ContentSourceError`, por lo que scheduler,
generator, validation, publisher y orchestration siguen siendo agnósticos
de la fuente. `main.py` es el único sitio que decide qué fuente se
construye.

## 3. Fuente Notion (`app/sources/notion_source.py`, 542 líneas)

Esquema asumido (validado al arrancar con `validate_schema()`):

| Propiedad | Tipo | Obligatoria |
| --- | --- | --- |
| `Topic` | Title | sí |
| `Angle` | Rich text | sí |
| `Scheduled At` | Date | sí |
| `Platform` | Select | sí |
| `Status` | Status o Select | sí |
| `References`, `Reference Notes`, `Generate Image`, `Image Brief`, `Generated Copy`, `Generated Image`, `Published URL`, `Error` | diversas | no |

- La validación nombra la propiedad que falta y el tipo esperado frente
  al recibido; si `NOTION_DATABASE_ID` apunta a una página se explica.
- Parseo defensivo: un registro roto se omite con aviso en lugar de
  romper el ciclo; los payloads inesperados producen `ContentParseError`
  con el id de la página.
- Mapeo de estados insensible a mayúsculas para los 7 valores del ciclo
  de vida; cualquier otro valor lanza `NotionStatusError` (nunca se
  inventa un estado).
- Fechas normalizadas a UTC (las naïve se tratan como UTC): nunca se
  comparan objetos naive y aware. El filtro principal va al servidor
  (`Status = Scheduled` + `Scheduled At` acotado) y se re-comprueba en
  cliente.
- Escritura: codificación por tipo de propiedad, texto partido en
  fragmentos de 2000 caracteres, resolución del nombre visible de la
  opción de `Status` contra el esquema, y omisión tolerante (con aviso)
  de columnas opcionales ausentes o de tipo no soportado.

## 4. HTTP de Notion (`app/sources/notion_client.py`, 170 líneas)

- Cliente REST propio sobre `requests` (ya añadido a
  `requirements.txt`), sin SDK de terceros: `notion-client` 3.x por
  defecto habla con la versión `2025-09-03` de la API, donde
  `databases.query` fue reemplazado por `data_sources.query`, así que se
  fija explícitamente `Notion-Version: 2022-06-28`.
- Sólo cuatro operaciones (`retrieve_database`, `query_database`,
  `retrieve_page`, `update_page`), expuestas tras el protocolo
  `NotionGateway` para poder inyectar un doble en memoria en tests.
- Traducción de errores: 401/403 → `NotionAuthError`, 404 →
  `NotionNotFoundError`, resto → `NotionApiError`, fallo de red →
  `NotionApiError`; body no JSON → error claro. El mensaje de la
  respuesta de Notion se incluye, **nunca** las cabeceras ni el token.
- `session` es inyectable: cualquier objeto con
  `request(method, url, headers, json, timeout)`, lo que mantiene los
  tests sin red.

## 5. Selección de fuente y configuración (`app/config.py`)

- Nuevas variables: `NOTION_TOKEN`, `NOTION_DATABASE_ID`,
  `CONTENT_SOURCE` (`auto|notion|yaml`, default `auto`),
  `CONTENT_POLL_INTERVAL_SECONDS` (default 60), `X_USERNAME`.
- `auto` = Notion **sólo** si existen token e id a la vez; una
  configuración parcial es `ConfigError` (no un fallback silencioso a
  YAML). `CONTENT_SOURCE=notion` sin credenciales también falla, y un
  intervalo no positivo o no numérico también.
- Todos los secretos siguen con `repr=False`; `.env.example` documenta
  las variables nuevas con placeholders.

## 6. Scheduler: polling en lugar de cron por item (`app/scheduler.py`)

- Un solo job (`content-poll`) con trigger `interval`,
  `max_instances=1`, `coalesce=True` y `replace_existing=True`: no hay
  un job por página, se consulta la fuente una vez por ciclo.
- `poll_due_items(now)` pide los items vencidos, descarta duplicados
  dentro del mismo ciclo y ejecuta cada uno con `try/except` propio: un
  item que falle no detiene el resto. Los errores de la fuente se
  propagan para que el arranque pueda avisar y reintentar en el siguiente
  ciclo.
- El modo YAML conserva el comportamiento legado (`schedule_items`, un
  cron diario por entrada), para no romper el flujo local de Pass 1.
- Los jobs siguen resolviendo el item por id contra la fuente: los
  payloads crudos de Notion nunca entran en el scheduler.

## 7. Sincronización de vuelta (`app/orchestration.py`)

Transiciones escritas en la fuente:

| Momento | Escritura |
| --- | --- |
| inicio del flujo | `Status = Generating` |
| texto generado | `Generated Copy` + `Status = Ready` |
| antes de publicar | `Status = Publishing` (no en DRY_RUN) |
| publicación real | `Status = Published` + `Published URL` + `Error = null` |
| cualquier fallo | `Status = Failed` + `Error` |

- Si la escritura falla, se imprime un aviso, se registra
  `publish_status = sync_error` en el audit log y el flujo continúa:
  **jamás se reintenta la publicación** (evita dobles posts). Si el
  fallo ocurre justo después de publicar, el aviso es crítico y explícito
  (`NO se reintenta automáticamente`).
- Un audit log roto dentro de ese manejo no rompe el flujo.
- `WorkflowResult.ok` ahora acepta `published` **o** `ready`, porque en
  DRY_RUN el resultado correcto es `ready`.

## 8. Semántica de DRY_RUN en Pass 2

- `DRY_RUN=true` termina el ciclo en `Status = Ready`: no se escribe
  `Publishing`, no se escribe `Published`, no se escribe `Published URL`.
- El orquestrador corrige al publisher: si un publisher reclamara
  `published` con `DRY_RUN=true`, el resultado se normaliza a
  `simulated` antes de reportarlo y de registrarlo en la auditoría.
- El audit log registra `status = simulated` y
  `workflow_status = ready`, con `external_post_id = null`.

Tres aserciones de Pass 1 que esperaban `published` en modo simulación
fueron adaptadas a `ready`: `tests/test_orchestration.py::test_simulated_publish_is_a_success`
y `tests/test_integration.py::test_yaml_to_dry_run_publish_flow`.
Es un cambio de semántica deliberado (Pass 1 marcaba `published` en
simulación), no una relajación de la prueba.

## 9. `Published URL` y `X_USERNAME`

- `XPublisher` construye `https://x.com/{handle}/status/{id}` sólo si
  `X_USERNAME` está configurado (se limpia el `@`) y existe el id del
  post; sin handle no se inventa ninguna URL: el id queda en el audit
  log. `PublishResult` y `ContentItem` ganan `url`/`published_url`.
- La URL se escribe en la propiedad `Published URL` sólo en la
  publicación real.

## 10. Validación ejecutada

**Tests:** `208 passed`, 1 warning preexistente (DeprecationWarning del
SDK de Gemini), **cobertura 100% sobre `app/` (932 statements)**.

Distribución de los tests nuevos: `test_notion_source.py` 63,
`test_notion_client.py` 18, `test_orchestration_sync.py` 16,
`test_polling.py` 13; ampliados `test_config.py` (20) y
`test_publisher.py` (18). Ninguna prueba abre un socket.

**Validación controlada DRY_RUN** (`scripts/validate_dry_run.py`,
exit 0): clases de producción reales (Config, NotionContentSource,
GeminiGenerator, XPublisher, ExecutionLog, ContentOrchestrator,
ContentScheduler) con transportes falsos. 9 puntos, todos OK:

1. descubrimiento: el polling procesa sólo el item vencido;
2. parseo: página → `ContentItem` con topic/platform/estado;
3. generación: `GeminiGenerator` llamado una vez;
4. `Generated Copy` escrito en la fuente;
5. transiciones `Generating → Ready`, sin `Publishing/Published`;
6. cero llamadas a `create_tweet`;
7. estado final `Ready` y sin `Published URL`;
8. auditoría `simulated` / `ready` / `external_post_id = null`;
9. el segundo ciclo no reprocesa el item.

**Limitación honesta:** la validación en vivo contra Notion y Gemini no
fue posible en este entorno: `.env` no contiene `NOTION_TOKEN` ni
`NOTION_DATABASE_ID` y `GEMINI_API_KEY` está vacía. Por eso la
validación se hizo con transportes falsos y sin red. Con credenciales
reales, el único paso pendiente es arrancar `python main.py` y observar
un ciclo de polling de extremo a extremo.

## 11. Seguridad

- `.env` está en `.gitignore`; `.env.example` sólo lleva placeholders.
- `Config` oculta con `repr=False` `gemini_api_key`, credenciales de X y
  `notion_token`; ningún `print`/log emite secretos (verificado por
  búsqueda en todo `app/` y `main.py`).
- Los errores de Notion incluyen status HTTP y el mensaje de la API,
  jamás las cabeceras ni el token (`test_auth_failures_are_translated`
  asegura que el token no aparece en la excepción).
- La auditoría guarda contenido publicado e ids, nunca credenciales.

## 12. Cambios realizados

Ficheros nuevos:

- `app/sources/notion_client.py`, `app/sources/notion_source.py`;
- `tests/test_notion_source.py`, `tests/test_notion_client.py`,
  `tests/test_polling.py`, `tests/test_orchestration_sync.py`;
- `scripts/validate_dry_run.py`.

Ficheros modificados: `app/config.py`, `app/models.py`
(`published_url`, `error`, `PublishResult.url`), `app/sources/base.py`
(campos actualizables, `filter_by_range` en UTC), `app/sources/__init__.py`,
`app/orchestration.py`, `app/scheduler.py`, `app/publishers/x.py`,
`main.py`, `requirements.txt` (+`requests`), `.env.example`, `README.md`,
`tests/conftest.py` (dobles de Notion) y las dos aserciones adaptadas del
apartado 8.

## 13. Limitaciones, deuda y próximos pasos

- **Escrituras fallidas:** se detectan, se avisan y se registran, pero no
  se reconcilian ni se reintentan (reconciliación queda para SQLite).
- **Sin idempotencia ni bloqueo distribuido:** un solo proceso se da por
  supuesto; dos instancias duplicarían publicaciones.
- **Sin webhooks de Notion:** el polling es la única vía de descubrimiento
  (latencia = `CONTENT_POLL_INTERVAL_SECONDS`).
- **Imágenes y demás plataformas:** campos presentes, sin implementar.
- **Validación en vivo pendiente** por falta de credenciales (apartado 10).

Próximos pasos: SQLite para estado técnico/idempotencia/reintentos,
reconciliación de escrituras fallidas, generación opcional de imágenes y
nuevos publishers registrados en `create_publisher`.
