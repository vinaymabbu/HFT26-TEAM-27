# Deploy: dashboard on Vercel, API on Render

Vercel cannot run the API (4.5 MB request limit, ~250 MB bundle vs ~234 MB of OpenCV+pandas, no persistent disk, short timeouts), so the dashboard and the API are deployed separately.

1. **API (Render):** New > Blueprint > pick this repo (`render.yaml`). Set `FEATHERLESS_API_KEY` (never commit it). Note the URL, e.g. `https://incident-api.onrender.com`; check `/api/health`. The starter plan is needed for the disk that stores uploads.
2. **Dashboard (Vercel):** Add New Project > this repo, **Root Directory `src/frontend`**. Environment variable `VITE_API_BASE` = the Render URL (no trailing slash). Deploy.
3. **CORS:** in Render set `CORS_ORIGINS` to the Vercel URL and redeploy.
4. Open the Vercel URL: Evidence tab > upload > Run analysis.

CLI alternative for step 2: `cd src/frontend && npx vercel --prod` (asks you to log in), then add `VITE_API_BASE` under Project Settings > Environment Variables and redeploy.
