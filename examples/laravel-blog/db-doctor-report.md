# DB Doctor — laravel-blog · 2026-10-16

**Alcance:** `app/`, `resources/views/`, `database/migrations/` · síntoma reportado: «GET /posts tarda ~4 s, creemos que es un bug del controlador».
**Motor:** PostgreSQL (`DB_CONNECTION=pgsql` en `.env.example`) · **Acceso a datos:** Eloquent + un `DB::select` crudo.
**Nivel de evidencia:** estático (🔎 verificado leyendo el código). Sin acceso a la base de datos ni a estadísticas: los tiempos no están medidos.
**No analizado:** volumen real de las tablas, `pg_stat_statements`, planes de ejecución reales, layouts Blade no incluidos (`layouts.app`, `dashboard`).

## 1. Resumen ejecutivo

El controlador `PostController@index` no tiene ningún bug lógico: el problema está en la capa de datos. La vista `posts/index` accede a `author`, `comments` y `tags` de cada post sin eager loading, así que cada página de 20 posts ejecuta **62 consultas** (1 COUNT + 1 listado + 3 × 20). Peor aún, la consulta de comentarios de cada post filtra por `comments.post_id`, una foreign key **sin índice** en PostgreSQL: cada una de esas 20 consultas recorre la tabla `comments` completa. N+1 multiplicado por un scan completo es la explicación más probable de los 4 s y empeorará a medida que crezcan los comentarios. Además hay una **inyección SQL** en la búsqueda que hay que corregir de inmediato.

| # | Problema | Dónde | Impacto | Esfuerzo | Prioridad |
|---|---|---|---|---|---|
| 1 | N+1 en el listado de posts (author, comments, tags) | `resources/views/posts/index.blade.php:8,10` | Alto | S | P0 |
| 2 | FK `comments.post_id` sin índice (cada carga de comentarios es un scan completo) | `2024_01_03_..._create_comments_table.php:13` | Alto | S | P0 |
| 3 | Inyección SQL en `search()` (`orWhereRaw` con interpolación) | `PostController.php:49` | Seguridad | S | P0 |

**Resultado esperado tras P0–P1:** `/posts` pasa de 1 + 1 + 3N = 62 a 4 consultas por página (N = 20), y la consulta de comentarios pasa de scan completo a búsqueda por índice. Tiempos reales: por medir (ver §4).

## 2. Informe de rendimiento — resultados y prioridades

### 2.1 Hallazgos

| ID | Área | Hallazgo | Ubicación | Impacto | Exposición | Evidencia | Prioridad |
|---|---|---|---|---|---|---|---|
| ORM-1 | ORM | N+1: `$post->author` en el bucle de la vista | `posts/index.blade.php:8` | Alto | Caliente | 🔎 | P0 |
| ORM-2 | ORM | N+1 + carga completa: `$post->comments->count()` | `posts/index.blade.php:8` | Alto | Caliente | 🔎 | P0 |
| ORM-3 | ORM | N+1: `@foreach ($post->tags …)` | `posts/index.blade.php:10` | Alto | Caliente | 🔎 | P0 |
| IDX-1 | Índices | FK `comments(post_id)` sin índice | `create_comments_table.php:13` | Alto | Caliente | 🔎 | P0 |
| SEC-1 | Seguridad | SQL construido con interpolación (`'%$q%'`) | `PostController.php:49` | Seguridad | Caliente | 🔎 | P0 |
| ORM-4 | ORM | Dashboard: 2 consultas por usuario (`posts()->count()` + último comentario) sobre `User::all()` | `PostController.php:27,33,34` | Alto | Periódica | 🔎 | P1 |
| IDX-2 | Índices | FK `posts(user_id)` sin índice (afecta a ORM-4 y a borrados de usuarios) | `create_posts_table.php:13` | Medio | Periódica | 🔎 | P1 |
| ORM-5 | ORM | `PostResource` lee `author` y `comments` sin `whenLoaded` → N+1 en `search()` | `PostResource.php:15,17` | Alto | Caliente | 🔎 | P1 |
| SQL-1 | SQL | Búsqueda con `LIKE '%…%'` y `lower(body)`: siempre scan completo, sin límite | `PostController.php:48-50` | Medio | Caliente | ⚠️ (depende del tamaño) | P1 |
| ORM-6 | ORM | Cron cada minuto: un `UPDATE` por post programado | `PostController.php:70-73` | Medio | Periódica | 🔎 | P2 |
| SQL-2 | SQL | `whereDate('created_at')` (no sargable) + `->get()->count()` en memoria | `PostController.php:38` | Medio | Periódica | 🔎 | P2 |
| IDX-3 | Índices | FK `post_tag(tag_id)` sin índice (borrado de tags en cascada) | `create_tags_tables.php:19` | Bajo | Rara | 🔎 | P2 |
| IDX-4 | Índices | `comments_user_idx` duplica `comments_user_id_index`; falta `(user_id, created_at)` | `add_user_indexes_to_comments.php:12-13` | Bajo | Periódica | 🔎 | P2 |
| IDX-5 | Índices | `users_email_index` duplica el UNIQUE `users_email_unique` | `create_users_table.php:20` | Bajo | — | 🔎 | P3 |
| IDX-6 | Índices | `posts_status_index` es prefijo de `(status, published_at)` | `create_posts_table.php:22` | Bajo | — | 🔎 | P3 |
| IDX-7 | Índices | Índice sobre booleano `users.is_admin` | `create_users_table.php:16` | Bajo | — | ⚠️ | P3 |
| SQL-3 | SQL | `hasComments()`: `count() > 0` en vez de `exists()` | `PostController.php:78` | Bajo | Rara | 🔎 | P3 |
| SQL-4 | SQL | `export()`: `SELECT *` de toda la tabla en memoria | `PostController.php:83` | Bajo | Rara | ⚠️ | P3 |
| GRD-1 | Guardas | Lazy loading no protegido (`preventLazyLoading` ausente) | `AppServiceProvider.php` | — | — | 🔎 | P1 |

### 2.2 Síntoma → causa raíz

- **«/posts tarda 4 s»** ← ORM-1 + ORM-2 + ORM-3 (62 consultas por página) × IDX-1 (20 de ellas recorren `comments` entera). El controlador solo lanza 2 consultas; las otras 60 las dispara la vista al tocar relaciones no cargadas.
- **Dashboard lento y cada vez más lento** ← ORM-4 (1 + 2 × nº de usuarios consultas) × IDX-2 (cada `COUNT` de posts por usuario recorre `posts`).

### 2.3 Detalle

#### ORM-1/2/3 · N+1 en `posts/index`
- **Síntoma:** la página tarda segundos aunque solo muestra 20 posts.
- **Causa raíz:** `index()` pagina `Post::where('status', 'published')` sin `with()`. En la vista:
  ```blade
  <p>By {{ $post->author->name }} · {{ $post->comments->count() }} comments</p>   {{-- línea 8 --}}
  @foreach ($post->tags as $tag)                                                   {{-- línea 10 --}}
  ```
  Cada acceso dispara una consulta por post. `comments->count()` además **carga todos los comentarios** de cada post solo para contarlos.
- **Evidencia (🔎):** `PostController.php:17-19` no hace eager loading; la única carga de `author`/`tags` está en `show()` (línea 58), que no alimenta esta vista. Cálculo: 1 (COUNT de `paginate`) + 1 (posts) + 20 (`users` por id) + 20 (`comments` por `post_id`) + 20 (`tags` ⋈ `post_tag`) = **62 consultas**.
- **Impacto:** crece linealmente con el tamaño de página; combinado con IDX-1, cada consulta de comentarios es O(tamaño de `comments`).

#### IDX-1 · `comments.post_id` sin índice
- **Causa raíz:** `$table->foreignId('post_id')->constrained()` crea la restricción FK pero en PostgreSQL **no crea índice**.
- **Evidencia (🔎):** ningún índice de `comments` empieza por `post_id` (inventario: PK `id`, `user_id` ×2). Se usa en la relación `Post::comments`, en `hasComments()` y en el `ON DELETE CASCADE` desde `posts`.
- **Impacto:** cada `WHERE post_id = ?` y cada borrado de un post recorre `comments`.

#### SEC-1 · Inyección SQL en la búsqueda
- `->orWhereRaw("lower(body) like '%$q%'")` interpola la entrada del usuario dentro del SQL. Además cada término distinto genera un SQL distinto (sin reutilización de planes).

#### ORM-4 · Dashboard
- `User::all()` + por cada usuario `$user->posts()->count()` y `Comment::where('user_id', …)->latest()->first()` → **1 + 2U consultas** (U = usuarios). Sin índice en `posts.user_id` (IDX-2), cada `COUNT` recorre `posts`.

### 2.4 Salud de índices

| Acción | Índice | Por qué | Lo bueno | Lo malo / coste |
|---|---|---|---|---|
| Crear | `comments (post_id)` | FK sin índice, usada en cada listado y en cascadas | búsqueda por índice en vez de scan; `withCount` y borrados de posts baratos | +1 índice en cada escritura de `comments`; espacio ≈ filas × ~20 B |
| Crear | `posts (user_id)` | FK sin índice, usada por `posts()->count()` y borrados de usuarios | conteos por usuario por índice | +1 índice en escrituras de `posts` |
| Crear (sustituye 2) | `comments (user_id, created_at)` | sirve `latest()->first()` por usuario y `latestOfMany` | top-1 por usuario sin ordenar | +1 índice, pero se eliminan los 2 duplicados → neto: −1 índice |
| Crear | `post_tag (tag_id)` | FK sin índice (cascada al borrar tags, `Tag::posts`) | borrado de tags sin scan | coste de escritura en la tabla pivote |
| Eliminar | `comments_user_idx`, `comments_user_id_index` | duplicados entre sí; sustituidos por el compuesto | menos escritura y espacio | ninguno tras crear el compuesto |
| Eliminar | `users_email_index` | duplica `users_email_unique` | menos escritura | ninguno |
| Eliminar | `posts_status_index` | prefijo de `(status, published_at)` | menos escritura | ninguno salvo que las estadísticas digan lo contrario |
| Revisar | `users_is_admin_index` | booleano: rara vez se usa | — | se paga en cada escritura de `users` |
| Mantener | `posts_status_published_at_index` | sirve el listado (`status = ? ORDER BY published_at DESC`) | | |

## 3. Plan de optimización

### Fase 1 — Quick wins (bajo riesgo, alto retorno)

#### Acción 1 — Eager loading en el listado · cierra ORM-1, ORM-2, ORM-3 · esfuerzo S
- **Cambio:**
  ```php
  // PostController@index
  $posts = Post::with(['author:id,name', 'tags'])
      ->withCount('comments')
      ->where('status', 'published')
      ->orderByDesc('published_at')
      ->paginate(20);
  ```
  ```blade
  <p>By {{ $post->author->name }} · {{ $post->comments_count }} comments</p>
  ```
- **SQL resultante (aprox.):**
  ```sql
  select count(*) as aggregate from posts where status = 'published' and deleted_at is null;
  select posts.*, (select count(*) from comments where posts.id = comments.post_id) as comments_count
  from posts where status = 'published' and deleted_at is null
  order by published_at desc limit 20 offset 0;
  select id, name from users where id in (…);
  select tags.*, post_tag.post_id as pivot_post_id, post_tag.tag_id as pivot_tag_id
  from tags inner join post_tag on tags.id = post_tag.tag_id where post_tag.post_id in (…);
  ```
- **Efecto esperado:** 62 → 4 consultas por página, y sin cargar los comentarios en memoria.
- **Riesgos:** el subselect de `withCount` se ejecuta por fila → **depende de la Acción 2** (sin índice serían 20 scans dentro de una consulta). Si se restringen columnas (`author:id,name`) hay que incluir siempre la clave (`id`).
- **Validación:** test de número de consultas (abajo) + Debugbar/Telescope antes/después en `/posts`.
- **Rollback:** revertir el commit.

#### Acción 2 — Índice en `comments(post_id)` · cierra IDX-1 · esfuerzo S
- **Migración (PostgreSQL, sin bloquear escrituras):**
  ```php
  use Illuminate\Database\Migrations\Migration;
  use Illuminate\Support\Facades\DB;

  return new class extends Migration
  {
      public $withinTransaction = false; // CONCURRENTLY no puede ir dentro de una transacción

      public function up(): void
      {
          DB::statement('SET lock_timeout = \'5s\'');
          DB::statement('CREATE INDEX CONCURRENTLY IF NOT EXISTS comments_post_id_index ON comments (post_id)');
      }

      public function down(): void
      {
          DB::statement('DROP INDEX CONCURRENTLY IF EXISTS comments_post_id_index');
      }
  };
  ```
  (Nombre igual al que generaría Laravel, para que un futuro `$table->dropIndex(['post_id'])` funcione.)
- **Efecto esperado:** `WHERE post_id = ?` / `IN (…)` y el subselect de `withCount` pasan a `Index Scan`/`Index Only Scan`.
- **Riesgos:** la construcción concurrente tarda más y, si falla, deja un índice `INVALID` (borrarlo y reintentar). Coste extra en cada INSERT/DELETE de `comments`.
- **Validación:** `EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM comments WHERE post_id = 123;` antes (Seq Scan) y después (Index Only Scan); `idx_scan` creciendo en `pg_stat_user_indexes`.
- **Rollback:** `DROP INDEX CONCURRENTLY comments_post_id_index;`

#### Acción 3 — Cerrar la inyección SQL de `search()` · cierra SEC-1 (parcialmente SQL-1) · esfuerzo S
- **Cambio:**
  ```php
  $term = '%' . mb_strtolower((string) $request->input('q', '')) . '%';

  $posts = Post::with('author:id,name')
      ->withCount('comments')
      ->where(fn ($w) => $w->whereRaw('lower(title) like ?', [$term])
                           ->orWhereRaw('lower(body) like ?', [$term]))
      ->limit(50)
      ->get();
  ```
- **Riesgos:** ninguno funcional; el rendimiento de la búsqueda sigue siendo de scan completo hasta la Acción 7.
- **Validación:** test con `q = "x' OR '1'='1"` no devuelve todo.

#### Acción 4 — Guarda contra lazy loading · cierra GRD-1 · esfuerzo S
  ```php
  // AppServiceProvider::boot()
  Model::preventLazyLoading(! app()->isProduction());
  ```
- **Riesgo:** otros puntos con N+1 empezarán a lanzar excepciones en desarrollo/tests — es justo lo que queremos detectar.

### Fase 2 — Cambios planificados

#### Acción 5 — Dashboard en 3 consultas · cierra ORM-4, IDX-2, IDX-4 · esfuerzo M
- **Cambio:**
  ```php
  // User.php
  public function latestComment(): HasOne
  {
      return $this->hasOne(Comment::class)->latestOfMany('created_at'); // por defecto usaría max(id)
  }

  // PostController@dashboard
  $users = User::select('id', 'name')->withCount('posts')->with('latestComment')->get();
  $todayPosts = Post::where('created_at', '>=', today())->where('created_at', '<', today()->addDay())->count();
  ```
- **SQL propuesta (índices):**
  ```sql
  CREATE INDEX CONCURRENTLY IF NOT EXISTS posts_user_id_index ON posts (user_id);
  CREATE INDEX CONCURRENTLY IF NOT EXISTS comments_user_id_created_at_index ON comments (user_id, created_at);
  -- tras desplegar y comprobar que el compuesto se usa:
  DROP INDEX CONCURRENTLY IF EXISTS comments_user_idx;
  DROP INDEX CONCURRENTLY IF EXISTS comments_user_id_index;
  ```
- **Efecto esperado:** 1 + 2U → 3 consultas; conteos y «último comentario» resueltos por índice.
- **Riesgos:** con muchos usuarios, `->get()` sigue cargando todos → paginar el dashboard si U es grande. Borrar los índices antiguos solo después de verificar el compuesto.
- **Validación:** número de consultas del dashboard constante con 1 y con 50 usuarios; EXPLAIN de `latestOfMany`.
- **Rollback:** recrear `comments_user_id_index` (sentencia guardada) y revertir el código.

#### Acción 6 — Publicación programada en una sola sentencia · cierra ORM-6 · esfuerzo S
  ```php
  Post::where('status', 'scheduled')->where('published_at', '<=', now())
      ->update(['status' => 'published']);
  ```
- **Riesgo:** un `update()` masivo **no dispara eventos de modelo** (observers, `saved`/`updated`). Si hay listeners (notificaciones al publicar), mantener el bucle pero usar `->each()` sobre `lazyById()` o disparar los eventos explícitamente.
- **Validación:** el job ejecuta 2 consultas en vez de 1 + N.

#### Acción 7 — Búsqueda indexable · cierra SQL-1 · esfuerzo M
- **SQL propuesta (PostgreSQL):**
  ```sql
  CREATE EXTENSION IF NOT EXISTS pg_trgm;
  CREATE INDEX CONCURRENTLY posts_title_trgm_idx ON posts USING gin (lower(title) gin_trgm_ops);
  CREATE INDEX CONCURRENTLY posts_body_trgm_idx  ON posts USING gin (lower(body)  gin_trgm_ops);
  ```
- **Lo bueno:** `lower(col) LIKE '%término%'` deja de ser scan completo (términos de 3+ caracteres).
- **Lo malo:** índices GIN grandes (a menudo más que la propia columna `body`) y escrituras más lentas en `posts`; alternativa: búsqueda full-text (`tsvector`) o Laravel Scout si la búsqueda es una funcionalidad importante.
- **Validación:** EXPLAIN muestra `Bitmap Index Scan on posts_title_trgm_idx`.

### Fase 3 — Higiene
- **Acción 8:** borrar `users_email_index` y `posts_status_index` (IDX-5, IDX-6) — `DROP INDEX CONCURRENTLY …` tras confirmar con `pg_stat_user_indexes` en primaria y réplicas.
- **Acción 9:** revisar `users_is_admin_index` (IDX-7): si solo se consulta `is_admin = true`, sustituir por `CREATE INDEX CONCURRENTLY users_admins_idx ON users (id) WHERE is_admin;` o eliminarlo.
- **Acción 10:** `post_tag (tag_id)` (IDX-3); `hasComments()` → `$post->comments()->exists()` (SQL-3); `export()` → `Post::select([...])->lazyById(1000)` con respuesta en streaming (SQL-4).
- **Acción 11:** `PostResource` → `'author' => $this->whenLoaded('author', fn () => $this->author->name)`, `'comments_count' => $this->whenCounted('comments')` (ORM-5).

### Orden de ejecución
1. Acción 3 (seguridad) y Acción 2 (índice) — el índice **antes** que la Acción 1, porque `withCount` depende de él.
2. Acción 1 y Acción 11 (mismo PR), Acción 4.
3. Fase 2: índices de la Acción 5 → código del dashboard → borrar índices viejos una semana después.
4. Fase 3 cuando haya estadísticas de uso.

## 4. Plan de validación
- **Línea base (antes de tocar nada):** tiempo de `/posts` y `/dashboard` (p50/p95), número de consultas por petición (Debugbar/Telescope), `SELECT count(*)` de `posts`, `comments`, `users`, y `EXPLAIN (ANALYZE, BUFFERS)` de `select count(*) from comments where post_id = <id real>`.
- **Test de regresión del N+1:**
  ```php
  public function test_posts_index_runs_constant_queries(): void
  {
      Post::factory()->count(20)->hasComments(3)->hasTags(2)->create(['status' => 'published', 'published_at' => now()]);

      DB::enableQueryLog();
      $this->get('/posts')->assertOk();

      $this->assertLessThanOrEqual(5, count(DB::getQueryLog()));
  }
  ```
- **Tras cada fase:** mismas medidas; criterio de éxito: `/posts` ≤ 5 consultas y sin `Seq Scan` sobre `comments` en el plan.

## 5. Riesgos
| Cambio | Riesgo | Mitigación |
|---|---|---|
| Índices nuevos | bloqueo o fallo en la construcción, coste de escritura | `CONCURRENTLY`, `lock_timeout`, fuera de hora punta, comprobar `indisvalid` |
| Borrar índices | alguna consulta no vista dependía de ellos | estadísticas en primaria y réplicas, guardar el `CREATE INDEX` para revertir |
| `update()` masivo en el cron | se saltan eventos de modelo | revisar observers antes |
| `preventLazyLoading` | excepciones nuevas en dev/tests | es la detección deseada; corregir cada una |

## 6. Limitaciones y datos que confirmarían los ⚠️
- Número de filas de `posts`, `comments`, `users` y `post_tag` (convierte las estimaciones en impacto real).
- `EXPLAIN (ANALYZE, BUFFERS)` del subselect de comentarios y de la búsqueda.
- `pg_stat_statements` top 20 por `total_exec_time` y por `calls` (confirmaría el N+1 en producción).
- `pg_stat_user_indexes` de primaria y réplicas antes de borrar índices.

## Apéndice — salida de los scripts
- `scan_orm.py`: 11 hallazgos (7 high) · `scan_sql.py`: 7 (1 high, seguridad) · `analyze_indexes.py`: 11 (3 high: FKs sin índice).
- Inventario: 5 tablas, 14 índices, 5 FKs (`analyze_indexes.py --inventory`).
