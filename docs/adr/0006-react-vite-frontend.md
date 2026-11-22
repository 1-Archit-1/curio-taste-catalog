# 6. Control-plane frontend: React, Vite, and OpenAPI codegen

Status: Accepted

## Context

The initial control-plane UI (`src/curio/ui/index.html`) was a single hand-built HTML file with inline CSS and vanilla DOM scripting. It was enough for the Settings tab in #2, but it does not scale to upcoming client-heavy tabs — starting with Agent Traces (#9), which needs interactive filtering, live progress updates, and structured run inspection. Building DOM strings with `innerHTML` also makes escaping mistakes easy to introduce.

At the same time, FastAPI already knows the shape of every `/api/*` request and response once control-plane routes declare Pydantic models (ADR 0005, rule 6). Generating TypeScript types directly from that OpenAPI schema prevents backend changes from silently breaking the UI.

## Decision

Build the control-plane frontend as a React + TypeScript single-page application under `frontend/`, bundled with Vite and served by FastAPI from the same origin.

1. **React + Vite + TypeScript.** Use React 19, TypeScript (strict), and Vite, managed with `npm` and a committed `package-lock.json`.
2. **Routing.** Use `react-router` (`BrowserRouter` with `basename="/app"`) so the login screen, Settings tab, and future tabs share one app shell.
3. **OpenAPI codegen.** Put Pydantic request and response models in `src/curio/api/schemas/<domain>.py` for every `/api/*` route, mapped explicitly from repository/service results (never returning ORM models). Generate `frontend/src/api/schema.d.ts` from `create_app().openapi()` (filtered to `/api/*` routes, excluding Stremio addon routes and `/healthz`) using `openapi-typescript`, and call endpoints through `openapi-fetch` (a thin, zero-dependency typed wrapper over native `fetch`). Commit the generated file and check in CI that regenerating it produces no diff.
4. **Styling.** Use plain CSS with CSS custom properties (`tokens.css`) and Vite's built-in CSS Modules. No CSS-in-JS runtime, no utility framework, and no component kit.
5. **Small dependency set.** Keep runtime dependencies to `react`, `react-dom`, `react-router`, and `openapi-fetch`. Do not add a state manager, data-fetching cache (e.g. TanStack Query), or component library unless a later tab's requirements justify it in an ADR update. Testing, linting, and formatting live in `devDependencies`: `vitest`, `@testing-library/react`, `@testing-library/user-event`, `@testing-library/jest-dom`, `jsdom`, and `@biomejs/biome`.
6. **Same-origin auth and `/app/` prefix.** Serve the built SPA under `/app/` with an SPA fallback for `/app/{path:path}`, and redirect `/` to `/app/`. Keep `/api/*`, `/healthz`, and `/{addon_token}/...` unshadowed; only addon routes send CORS `*`. Auth stays unchanged: same-origin HttpOnly `SameSite=Lax` session cookie, no tokens or API keys in `localStorage`/`sessionStorage`, and no `dangerouslySetInnerHTML`.
7. **No Node at runtime.** Use a multi-stage `Dockerfile`: a `node:22-slim` builder stage runs `npm ci && npm run build`, and the Python runtime image copies only the compiled `dist/` directory. In local development, Vite proxies `/api` to uvicorn so cookies remain same-origin.

## Alternatives considered

- **HTMX + Jinja templates.** Avoids a JS build step, but becomes awkward for client-heavy state and live streaming in Agent Traces (#9) and gives up compile-time type checking between FastAPI and the UI.
- **Keep hand-written vanilla JS (`index.html`).** Zero dependencies, but hard to maintain past one tab and error-prone for HTML escaping.
- **Tailwind CSS or a component kit (MUI, Chakra, shadcn).** Adds configuration and dependency churn that a focused, self-hosted control plane does not need; CSS custom properties + CSS Modules cover our styling needs natively in Vite.
- **Heavier OpenAPI generators (`openapi-generator`, Orval).** Generate runtime boilerplate and pull in larger dependencies than `openapi-typescript` + `openapi-fetch`.

## Consequences

- Frontend builds and tests require Node (v22+) in development and CI, but the production container image remains Python-only.
- Every `/api/*` route must declare explicit Pydantic response schemas; backend contract changes require regenerating `frontend/src/api/schema.d.ts` (enforced by CI).
- React's default text escaping removes the `innerHTML` XSS risk; Biome (`security/noDangerouslySetInnerHtml`) and code review forbid `dangerouslySetInnerHTML`.
- New control-plane tabs (#9 and later) plug into the router and reuse the typed client and auth guard without re-implementing layout or session handling.
