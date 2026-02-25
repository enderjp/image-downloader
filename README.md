# FB Image Lab

Herramienta mínima para experimentar con la extracción de imágenes desde enlaces públicos de Facebook (especialmente `share`/`sharer`). El objetivo es comparar fácilmente distintos enfoques fuera del proyecto principal.

## Características

- Descarga la página usando múltiples combinaciones de URL (`www`, `m`, `mbasic`, `/share/`).
- Imita agentes de usuario comunes (navegador móvil y `facebookexternalhit`).
- Permite configurar un proxy HTTP/S (ideal para IP residencial) mediante la variable `FACEBOOK_PROXY_URL`.
- Devuelve las URLs encontradas en tags `<img>`, `meta og:image`, `twitter:image` y `link rel="image_src"`.
- Expone la lógica vía FastAPI (`POST /scrape`) para integrarla en otros servicios o desplegarla en Render.

## Requisitos

- Python 3.11+
- Dependencias en `requirements.txt`: instálalas con `pip install -r requirements.txt` (recomendado usar un virtualenv).
  - Incluye FastAPI y Uvicorn para correr la API HTTP.

## Uso rápido

### CLI

```bash
cd fb-image-lab
python main.py --url "https://www.facebook.com/share/p/1G9m7NqsRF/"
```

Opciones útiles:

- `--proxy http://usuario:pass@host:puerto` para forzar un proxy solo para esa ejecución.
- `--no-mobile` para omitir las variantes móviles.
- `--max-depth 3` para limitar cuántas URLs derivadas se prueban.

El resultado se imprime en JSON, incluyendo la URL original, las variantes visitadas, los códigos HTTP y la lista deduplicada de imágenes.

### API FastAPI

Inicia el servidor:

```bash
uvicorn main:app --reload
```

Ejemplo de request:

```bash
curl -X POST http://127.0.0.1:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.facebook.com/share/p/1G9m7NqsRF/",
    "proxy": "http://usuario:pass@host:puerto"
  }'
```

El endpoint devuelve la misma estructura JSON que el CLI (`original_url`, `variants_tried`, `images`). También hay un `GET /healthz` simple para checks.

## Próximos pasos sugeridos

- Integrar Playwright para obtener HTML renderizado cuando Facebook sirva placeholders.
- Añadir pruebas automatizadas con mocks de HTML.
- Implementar autenticación o rate limiting básico en la API si se expone públicamente.
