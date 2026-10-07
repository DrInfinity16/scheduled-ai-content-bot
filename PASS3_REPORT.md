# Project 11 — Pass 3 Reliability Report

---

## 1. Objetivo y alcance

Pass 3 añade la capa de confiabilidad sobre el sistema del Pass 2: estado
técnico durable en SQLite, idempotencia de publicación (nunca repetir un
post ya conocido como publicado), reintentos acotados ante fallos
transitorios y recuperación desde interrupciones del proceso.

Dentro del alcance:

- tabla SQLite `executions` (identidad `content_item_id + platform`) con
  transiciones protegidas y creación idempotente del esquema;
- gate de idempotencia en el orquestador **antes** de cualquier efecto
  externo;
- orden durable: `mark_publishing` → publicar en X → `mark_published` en
  SQLite → recién entonces `Published` en Notion;
- caso crítico "X publicó y la escritura final de Notion falló": queda
  registrado, se avisa en forma visible y **sólo** se reintenta la
  sincronización, nunca la publicación;
- recuperación: generación interrumpida (re-ejecutable), publicación
  interrumpida (revisión manual, jamás republicada), DRY_RUN
  (`simulated`) y reinicios de proceso;
- reconciliación automática al inicio de cada ciclo de polling;
- reintentos acotados con backoff exponencial en los tres límites de
  integración (Notion, X, Gemini), configurables por `.env`;
- validación controlada de todo lo anterior sin red.

Fuera de alcance (no implementado ni simulado): generación de imágenes,
medios en X, Instagram/Facebook, web UI, Docker/Redis/Celery/webhooks,
migraciones de esquema, coordinación entre múltiples procesos y
exactly-once (imposible de garantizar entre un sistema externo y una base
local; se ofrece *best-effort at-most-once* con idempotencia durable).

## 2. Diseño de confiabilidad (decisiones)

1. **Dos verdades separadas.** Notion sigue siendo la fuente *editorial*
   (tema, ángulo, fechas, copia, estado del calendario). SQLite es el
   estado *técnico* (¿ya se publicó?, ¿qué id devolvió X?, ¿cuántos
   intentos?, ¿Notion está al día?). El audit JSONL queda como traza
   append-only y **no** participa en decisiones de idempotencia.
2. **Identidad.** La clave durable es `content_item_id + platform`
   (única por constraint). `content_hash` (SHA-256 de identidad + copia)
   es sólo detección defensiva de cambios: si alguien edita la copia
   publicada, el hash avisa pero **no** desbloquea una nueva
   publicación.
3. **Orden irreducible.** El único punto en que se pierde información es
   entre la respuesta de X y la escritura de SQLite; todo lo demás se
   ordena para que la pérdida sea detectable y recuperable:
   `mark_publishing` (si falla, se aborta antes de tocar X) → `publish`
   → `mark_published` (crítico) → escritura `Published` en Notion →
   `mark_synced` / `mark_sync_pending`.
4. **Conservador ante la duda.** Un resultado externo desconocido nunca
   se re-ejecuta: se convierte en `manual_review` (terminal) con mensaje
   de revisión humana. Una base ilegible no se interpreta como "nunca se
   publicó": el ciclo falla (`store_error`) sin tocar X.
5. **Reintento en los bordes.** `app/retry.py` no importa nada de la
   aplicación (sin ciclos); el orquestador nunca duerme. Los reintentos
   viven en `NotionApiClient._request`, `XPublisher.publish` y
   `GeminiGenerator.generate`, con la misma política configurada desde
   `.env`.
6. **Reconciliación como ciclo perdido.** Cualquier escritura fallida a
   Notion queda como deuda (`notion_sync_pending`) y se salda en el
   próximo ciclo con `reconcile_pending_syncs()`, que sólo escribe en la
   fuente y jamás publica.

## 3. Estado técnico durable en SQLite (`app/storage.py`, 436 líneas)

Esquema creado con `CREATE TABLE IF NOT EXISTS` + índice sobre
`notion_sync_pending`; sin framework de migraciones (documentado como
límite). Toda sentencia es parametrizada y las columnas escribibles
salen de un whitelist fijo (`_WRITABLE_COLUMNS`): ninguna contenido de
usuario forma parte del SQL.

| `ExecutionStatus` | Significado / regla |
| --- | --- |
| `pending` | fila creada, sin ejecutar |
| `generating`, `ready` | fases intermedias; re-ejecutables sin restricción |
| `publishing` | intención registrada **antes** de tocar X |
| `published` | publicación real conocida (con `external_post_id`) |
| `sync_pending` | publicado; Notion aún no confirmado |
| `failed` | intento fallido; reintentable si el item se reprograma |
| `simulated` | DRY_RUN; nunca hubo publicación real |
| `manual_review` | terminal; el resultado en X es desconocido |

Protecciones (`_can_transition`):

- `manual_review` no admite ninguna transición automática;
- `published` / `sync_pending` sólo aceptan avanzar hacia
  `sync_pending`/`published` (resolución de sincronización); cualquier
  intento de degradación conserva la fila e imprime
  `... no se degrada ... (protección anti-duplicado)`;
- `mark_published` sobre una fila ya publicada es una re-afirmación
  inofensiva (mismo id o vacío no pisa un id existente).

Otros detalles: conexión única con `RLock` (`check_same_thread=False`,
el polling corre en un worker de APScheduler), `created_at`/`updated_at`
en UTC ISO, `attempt_count` incremental por ciclo, snapshot defensivo de
`generated_content` para el hash, `__del__` best-effort que cierra el
handle (evita `ResourceWarning`), gestor de contexto y `close()`
explotables. El directorio del archivo se crea en `__init__`.

## 4. Idempotencia durable (el gate en `app/orchestration.py`)

`ContentOrchestrator.run()` consulta SQLite **antes** de cualquier efecto
externo:

| Fila encontrada | Comportamiento |
| --- | --- |
| ilegible (excepción) | falla conservadora `store_error`: sin publicar, sin tocar Notion, reintentable en el próximo ciclo |
| `is_published` (id externo o estado de la familia publicada) | `_already_published`: cero llamadas a X; si Notion está atrasado se reconcilia; audit `duplicate_blocked`; resultado `PUBLISHED` con el id conocido |
| `manual_review` | bloqueo permanente: Notion pasa a `Failed` con el motivo, audit `error` |
| `publishing` sin `external_post_id` | `_interrupted_publishing`: la fila pasa a `manual_review` **antes** de nada y el item queda bloqueado |
| `pending`/`generating`/`ready`/`failed`/`simulated` | se ejecuta normalmente |

Evidencia de la defensa en profundidad: con `publishing` + id perdido
(falla de escritura de `mark_published` tras un post exitoso), el estado
`published` de la fila **sola** ya bloquea la siguiente ejecución; el id
vacío sólo degrada la calidad del mensaje (`post externo desconocido`),
nunca abre una segunda publicación. Una copia editada en la fuente
(hash distinto) imprime el aviso correspondiente y sigue bloqueada.

`DRY_RUN` mantiene su semántica: la fila queda `simulated`, sin id
externo y sin `notion_sync_pending`; re-ejecutar una `simulated`
programada de nuevo es permitido (Case C: el operador re-lanza el item)
y en modo real publica una única vez.

## 5. Caso crítico: X publicó, Notion falló

Flujo real (validado en `scripts/validate_recovery.py`, escenario S1):

1. `mark_publishing` OK → X devuelve `post_id` → `mark_published`
   escribe `external_post_id` y `notion_sync_pending = 1` **antes** de
   cualquier escritura en Notion.
2. La escritura final `Status = Published` falla → `_sync_detailed`
   registra `publish_status = sync_error` en el audit, la fila queda
   `sync_pending` y se imprime:
   `AVISO CRÍTICO: ... NO se reintentará la publicación: el próximo ciclo
   sólo reintentará la sincronización.` El resultado del workflow sigue
   siendo `PUBLISHED` (la publicación ocurrió; ocultarla sería mentir).
3. Ciclos siguientes: `reconcile_pending_syncs()` reintenta **sólo** el
   write-back (contador de rechazos crece, número de posts constante).
4. Cuando Notion sana: la fuente queda `Published` (+ URL si es
   reconstruible vía `build_post_url`), la fila vuelve a `published` sin
   pendiente y el audit anota `reconciled`.
5. Reinicio del proceso entre medio: la fila durable manda; la
   reprogramación del item choca contra el gate (`duplicate_blocked`).

Cadena de seguridad completa: **X → SQLite → Notion**. Notion nunca es
prerequisito para recordar una publicación.

## 6. Recuperación desde interrupciones

| Interrupción | Estado resultante | Comportamiento tras reiniciar |
| --- | --- | --- |
| durante generación (`generating`) | re-ejecutable | se vuelve a generar y publicar normalmente |
| falla normal de generación/publicación | `failed` | reintentable si el item se reprograma; `attempt_count` conserva el histórico |
| tras `mark_publishing`, antes/durante X | `manual_review` | bloqueado para siempre: `ITEM BLOQUEADO` + Notion `Failed` con "revisión manual requerida (verificar en X antes de reprogramar)"; cero publicaciones automáticas |
| tras X, antes de `mark_published` | fila en `publishing` sin id | la próxima ejecución la detecta y la convierte en `manual_review` (mismo tratamiento conservador) |
| tras SQLite, con Notion atrasado | `sync_pending` o `published` sin confirmar | reconciliación en el próximo ciclo; nunca republica |
| DRY_RUN (`simulated`) | `simulated` | permitido re-ejecutar si el operador reprograma; en modo real publica una sola vez |

Los mensajes de bloqueo sobreviven reinicios porque viven en
`last_error` de la fila; el audit JSONL registra cada bloqueo.

## 7. Reintentos acotados (`app/retry.py`, 145 líneas)

API: `RetryPolicy(max_attempts, base_delay, max_delay)` con
`delay_for(intento)` = `min(base_delay * 2^(n-1), max_delay)` (1 s, 2 s,
4 s... tope 30 s), `is_transient(exc)` y
`with_retries(op, policy, retry_if, sleep, on_retry)`.

Clasificación (defensiva, con tipos explorados en vez de imports):

- códigos de estado **transitorios**: 408, 425, 429, 500, 502, 503, 504;
  cualquier otro 4xx es permanente;
- excepciones de red (`ConnectionError`, `TimeoutError`) y nombres
  conocidos (`*timeout*`, `*unavailable*`, `*rate*limit*`, ...);
- recorre `__cause__`/`__context__` (profundidad 5): un
  `GenerationError` envolviendo un error 503 de Gemini se reintenta, uno
  envolviendo 401 no;
- atributo `.code`/`.status_code` estilo SDK de Google;
- **desconocido = permanente** (no arriesgar efectos externos en bucle).

`on_retry` imprime la observabilidad (`↻ ... reintento N en Xs`) y
`sleep` no se invoca con retardo 0 (tests y `RETRY_BASE_DELAY_SECONDS=0`
no esperan reloj). El reintento nunca ocurre dentro del orquestador: si
los intentos se agotan, la excepción sube y el workflow marca `failed`.

Dónde vive (límites de integración, políticas inyectables para tests):

- `NotionApiClient._request` → traduce a `NotionApiError` con
  `status_code` (los 4xx/404 siguen siendo no-reintentables);
- `XPublisher.publish` alrededor de `create_tweet` (en DRY_RUN no entra
  en la ruta de retry);
- `GeminiGenerator.generate` alrededor de `generate_content` (sigue
  envolviendo en `GenerationError`).

## 8. Reconciliación en el ciclo de polling (`app/scheduler.py`)

`poll_due_items()` ahora ejecuta primero
`reconcile_pending_syncs()` (1.ª fase) y sólo después consulta los items
vencidos (2.ª fase):

- el hook se busca por `getattr`: dobles u orquestadores sin el método
  (modo YAML, tests legacy) lo omiten sin cambios;
- una excepción de reconciliación se imprime y **no** detiene el ciclo;
- la reconciliación usa `pending_syncs()` (`notion_sync_pending = 1` o
  estado `sync_pending`), resuelve la página por id en la fuente
  (ausente o ilegible ⇒ se mantiene pendiente con aviso) y escribe
  `Published` + URL si es posible;
- `reconciled` en el audit identifica cada saneamiento.

Resultado: una página cuyo ciclo murió entre X y Notion se cura aunque
su `Status` ya no sea `Scheduled` (no depende de que el calendario la
vuelva a entregar).

## 9. Configuración, composición y DRY_RUN

Nuevas variables (con validación y defaults en `app/config.py`):

| Variable | Default | Validación |
| --- | --- | --- |
| `DATABASE_PATH` | `content_bot.db` | no vacío (vacío en `.env` ⇒ default) |
| `RETRY_MAX_ATTEMPTS` | `3` | entero `>= 1`; no numérico ⇒ `ConfigError` |
| `RETRY_BASE_DELAY_SECONDS` | `1.0` | `>= 0`; no numérico ⇒ `ConfigError` |

`Config.retry_policy` construye la política perezosamente (import
diferido). `main.py` ahora compone: `ExecutionStore(config.database_path)`
→ pasa `config.retry_policy` a generator, publisher y fuente Notion →
cierre del store en un `finally` tras arrancar el scheduler, y anuncia la
ruta del estado técnico al arrancar. El stdout de `main.py` ya se
reconfiguraba a UTF-8 (los `✓/⚠/✗` no rompen en consolas cp1252); los
scripts de validación ahora hacen lo mismo.

`DRY_RUN=true` conserva exactamente la semántica del Pass 2 (termina en
`Ready`, cero `create_tweet`, audit `simulated`, fila `simulated` sin id
externo); lo único nuevo es el registro técnico durable de esa
simulación.

## 10. Tests

**342 tests, 100 % de cobertura sobre `app/` (1337 statements), 1 único
warning preexistente** (DeprecationWarning del SDK `google.genai`).
Ninguna prueba abre red: Gemini, Tweepy y el HTTP de Notion siguen con
dobles; los tests nuevos usan SQLite real en directorios temporales.

| Archivo | Tests | Qué cubre |
| --- | ---: | --- |
| `test_storage.py` (nuevo) | 26 | esquema, identidad, transiciones, protecciones, durabilidad tras reopen, hash, SQL hostil, whitelist |
| `test_retry.py` (nuevo) | 44 | backoff, clasificación (status codes, red, causa, `.code`), `with_retries`, reintento/no-reintento en X, Gemini y Notion |
| `test_idempotency.py` (nuevo) | 11 | gate: segundo ciclo, reinicio, copia editada, Notion atrasado, `store_error`, identidad por plataforma, audit `duplicate_blocked` |
| `test_critical_failure.py` (nuevo) | 8 | X ok + Notion falla: orden SQLite-antes-de-Notion, reintentos de sólo-sync, convergencia, reinicio, reconciliación |
| `test_recovery.py` (nuevo) | 12 | `manual_review` permanente, generación interrumpida, Case C, reconciliación con página ausente/ilegible/caída |
| `test_store_failures.py` (nuevo) | 6 | aborte antes de X si `mark_publishing` falla, aviso crítico si `mark_published` falla (y bloqueo posterior), fallo no crítico, audit roto, URL reconstruida/URL falla |
| `test_dry_run_store.py` (nuevo) | 4 | fila `simulated`, cero afirmación de publicación, re-ejecución segura, fallo en modo simulado |
| `test_main_wiring.py` (nuevo) | 6 | `build_app`: store, políticas de retry, fuente YAML, durabilidad |
| `test_polling.py` (+5) | 18 | reconcile primero, también sin items, error tolerado, sin hook, flujo completo sanea sin publicar |
| `test_config.py` (+11) | 31 | defaults, `retry_policy`, carga desde `.env`, rechazos |
| `test_notion_source.py` (+1) | 64 | gateway por defecto + política inyectada |

Un test existente se adaptó con transparencia:
`test_notion_client.py::test_network_failure_is_translated` ahora inyecta
`RetryPolicy(base_delay=0)` (misma aserción, tres intentos reales, sin
espera de reloj): sin eso la suite tardaba +3 s por la política por
defecto. La suite completa corre en ~4 s.

Todos los tests del Pass 2 (208) siguen en pie y en verde: 134 tests
nuevos/extendidos se sumaron sin romper el contrato anterior.

## 11. Validación controlada ejecutada

Ambos scripts corren clases **de producción** con transportes falsos,
imprimen cada checkpoint y devuelven `exit 0` sólo si todo pasa:

```
python scripts/validate_dry_run.py     → 13/13 OK (exit 0)
python scripts/validate_recovery.py    → 10/10 OK (exit 0)
```

`validate_dry_run.py` (Pass 2 ampliado): los 9 puntos originales del
ciclo DRY_RUN + 4 nuevos:

1. fila `simulated` con `attempt_count = 1` y sin `external_post_id`;
2. `content_hash` SHA-256 y snapshot de la copia en SQLite;
3. durabilidad: cerrar y reabrir el archivo conserva la fila (simulación
   de reinicio dentro del propio orquestador);
4. reprogramar una `simulated` vuelve a ejecutarla (`attempt_count = 2`)
   siguiendo sin `create_tweet`.

`validate_recovery.py` (nuevo):

- **S1 (5 checkpoints):** X publicó y SQLite lo registró antes de tocar
  Notion; la escritura final falló en forma visible (`NO se reintentará
  la publicación`); ciclo 2 reintenta sólo la sync (posts = 1, rechazos
  = 2); Notion saneado ⇒ reconcile converge sin duplicar; reinicio +
  reprogramación ⇒ `ya publicado` con el mismo `external_post_id`.
- **S2 (3):** fila en `publishing` sin id ⇒ `manual_review`, cero
  posts, Notion `Failed`; reprogramado sigue bloqueado.
- **S3 (1):** 2 fallos transitorios de X ⇒ exactamente 3 intentos,
  `reintento` visible, publicación OK.
- **S4 (1):** fallo permanente ⇒ 1 intento (sin `reintento`), fila
  `failed`, Notion `Failed`.

**Por qué no hay validación en vivo:** `.env` contiene únicamente
placeholders (verificado: `GEMINI_API_KEY=your-gemini-api-key`,
`NOTION_TOKEN=secret_xxx`) y no existen credenciales reales en este
entorno; no se pidió ni se usó ningún secreto. Toda la evidencia de este
pass proviene de transportes falsos sobre el código de producción, y así
se reporta sin adornos. La validación en vivo queda como paso manual
para quien posea credenciales: ejecutar ambos scripts (ya sin red falsa
no cambian) y después `python main.py` apuntando a una base Notion de
pruebas con `DRY_RUN=true`.

## 12. Seguridad

- `.env` y `content_bot.db` están en `.gitignore`; el directorio no es
  un repositorio git, pero la higiene de archivos es la correcta.
- **Credenciales nunca persistidas ni impresas:** grep sistemático
  muestra que token/keys sólo aparecen en `config.py` (lectura, con
  `repr=False`), en las cabeceras de `notion_client.py`, en la
  construcción del cliente de Tweepy y en `genai.Client`. No hay
  interpolación de secretos en mensajes de error, en la consola, en el
  audit JSONL ni en SQLite (el esquema guarda estado técnico, hashes y
  un snapshot de la copia generada).
- Los errores de Notion se derivan únicamente del campo `message` del
  cuerpo de respuesta ("never the request headers or token"); la URL no
  lleva el token.
- SQL siempre parametrizado; los nombres de columna provienen de un
  whitelist fijo; los identificadores hostiles (`page'; DROP TABLE ...`)
  quedan como datos (test incluido).
- El archivo de estado usa los permisos por defecto del sistema y no
  contiene secretos; se recomienda tratarlo como dato operacional.
- **Hallazgo corregido:** `.env.example` llegó a este pass duplicado
  (55 líneas con dos copias del contenido) y con valores con apariencia
  de secreto reales (un `GEMINI_API_KEY=AQ.Ab8...` y tokens
  `ntn_...`). Se reescribió con placeholders consistentes y sin
  duplicados, y se añadieron `DATABASE_PATH` / `RETRY_*`. Se verificó
  que `.env` (el archivo real) también contiene sólo placeholders, lo
  que confirma que no hubo exposición de secretos reales y explica la
  imposibilidad de validación en vivo.

## 13. Limitaciones y riesgos residuales

1. **No hay exactly-once.** Se entrega *best-effort at-most-once* con
   idempotencia durable. La ventana perdible es "X respondió y el
   proceso murió antes de `mark_published`": se detecta como
   `publishing` sin id y escala a revisión manual; el precio es una
   falsa alarma humana posible, nunca un duplicado automático.
2. **Si SQLite pierde `mark_published`** con el post exitoso, se emite
   `AVISO CRÍTICO` y el gate aún bloquea (por estado `published`), pero
   `external_post_id` queda vacío: el mensaje de bloqueo dirá "post
   externo desconocido" y habrá que verificarlo a mano.
3. **Reconciliación best-effort por ciclo:** mientras Notion siga
   caído, la fila permanece `sync_pending`; no hay backoff propio de
   reconciliación (depende del intervalo de polling).
4. **Sin coordinación multiproceso:** dos procesos con el mismo
   `DATABASE_PATH` no están lockados entre sí (el file se abre con el
   busy timeout de SQLite, pero no hay lease de candidato único).
5. **Sin migraciones de esquema:** `CREATE TABLE IF NOT EXISTS` cubre la
   primera versión; cambios futuros de columnas requerirán un plan.
6. **Live no validado** (sección 11): hasta que un operador con
   credenciales ejecute el flujo real.
7. La copia generada se guarda en SQLite (necesaria para el hash): es
   contenido editorial, no secreto, pero el archivo debe protegerse como
   cualquier dato de producción.

## 14. Cambios realizados

Nuevos:

- `app/storage.py` (436 líneas): `ExecutionStore`, `ExecutionRecord`,
  `ExecutionStatus`, protecciones, `compute_content_hash`.
- `app/retry.py` (145): `RetryPolicy`, `is_transient`, `with_retries`.
- `scripts/validate_recovery.py` (352): 10 checkpoints de
  recuperación/idempotencia.
- Tests nuevos: `test_storage.py`, `test_retry.py`,
  `test_idempotency.py`, `test_critical_failure.py`,
  `test_recovery.py`, `test_store_failures.py`,
  `test_dry_run_store.py`, `test_main_wiring.py` (117 tests).
- `PASS3_REPORT.md` (este documento).

Modificados:

- `app/orchestration.py`: gate, orden durable, rutas de bloqueo,
  `reconcile_pending_syncs`, manejo de fallos de store/audit.
- `app/scheduler.py`: barrido de reconciliación al inicio de cada
  ciclo + hook tolerante.
- `app/config.py`: `DATABASE_PATH`, `RETRY_*`, `_env_float`,
  `retry_policy`, validaciones.
- `main.py`: store + políticas de retry + cierre en `finally`.
- `app/sources/notion_client.py`: `status_code` en errores + reintentos.
- `app/sources/notion_source.py`: inyección de política.
- `app/publishers/x.py`, `app/publishers/__init__.py`,
  `app/generator.py`: reintentos con política inyectable.
- `scripts/validate_dry_run.py`: 4 checkpoints SQLite + UTF-8.
- `.env.example` (reescritura: placeholders, sin duplicados, `RETRY_*`),
  `.gitignore` (`content_bot.db`), `README.md` (arquitectura Pass 3,
  tabla de estados técnicos, límites conocidos, 342 tests).
- Tests existentes ampliados: `test_config.py` (+11), `test_polling.py`
  (+5), `test_notion_source.py` (+1); adaptado
  `test_notion_client::test_network_failure_is_translated` (política de
  retardo 0, mismas aserciones).

Verificación final: `pytest` → 342 en verde (~4 s);
`pytest --cov=app` → **100 % (1337 stmts)**; los dos scripts de
validación → `TODOS LOS PUNTOS OK`, exit 0.

## 15. Recomendación del siguiente pass

**Recomendado: Pass 4 — generación opcional de imagen + publicación de
medios en X** (único pass recomendado; no implementado en este pass).

Justificación: es la funcionalidad de producto con mayor valor
pendiente y los cimientos ya existen (`generate_image`, `image_brief`,
`generated_image` en `ContentItem` y en el esquema de Notion; registro
de publishers en `create_publisher`); la capa de confiabilidad de Pass 3
es precisamente lo que permite añadir un efecto externo adicional con
idempotencia y reintentos sin reescribir el flujo.

Criterios de aceptación propuestos:

1. `generate_image=true` produce una imagen (proveedor configurable,
   doble falso en tests, sin claves duras) y la adjunta al post de X
   cuando la plataforma lo soporta; sin imagen ⇒ comportamiento actual
   intacto.
2. La imagen generada queda registrada en `Generated Image` (Notion) y
   su generación obedece la misma política de reintentos acotados;
   un fallo de imagen nunca convierte una publicación exitosa en
   fallida (modo *best-effort* documentado).
3. La identidad en SQLite sigue siendo `content_item_id + platform`;
   agregar medios no añade una segunda vía de publicación.
4. Suite completa en verde sin borrar cobertura (mantener 100 % sobre
   `app/`), sin llamadas reales a APIs en tests y con una validación
   controlada extendida (scripts, exit 0).
5. README y report del pass actualizados; `.env.example` documenta
   cualquier clave nueva con placeholders.

Alternativas futuras (no recomendadas todavía): más publishers
(Instagram/Facebook), que requieren antes decidir modelo de identidad
por plataforma y alcance del gate; infraestructura (Docker, webhooks,
multi-proceso) sigue fuera de alcance hasta que el núcleo editorial
estabilice.
