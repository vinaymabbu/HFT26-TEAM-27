# Deploy everything on Vercel (best effort)

One Vercel project serves the dashboard (static, built from `src/frontend`) and the API (a Python function, `api/index.py`, running the FastAPI app). Cloudflare R2 is the durable store, because a Vercel function has no disk that survives and cannot receive more than 4.5 MB per request.

```
browser --(video, presigned PUT)--> R2 bucket
browser --(/api/*)--> Vercel function --(evidence, results, audit log)--> R2 bucket
```

* Large videos go browser -> R2 directly (`POST /api/evidence/presign`, PUT, `POST /api/evidence/register`). Small files and text go through the function.
* Around every request the function pulls what it needs from the bucket into `/tmp`, works on local files as usual, then pushes changes back (`app/mirror.py`).
* Analysis runs inside the request (`maxDuration` 300 s). It is not a background job; the Run button waits for it.
* Without `R2_*` variables nothing is mirrored and the backend runs on local disk as before.

## Steps

1. **R2:** Cloudflare dashboard > R2 > create a bucket. Create an API token (Object Read & Write on that bucket). Note the account id, bucket name, access key id and secret.
2. **Bucket CORS** (bucket > Settings > CORS policy), so the browser can PUT videos:
   ```json
   [{ "AllowedOrigins": ["https://YOUR-PROJECT.vercel.app"], "AllowedMethods": ["PUT"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 3600 }]
   ```
3. **Vercel:** Add New Project > import this repo, **Root Directory = repo root** (not `src/frontend`). `vercel.json` sets the build and the function.
4. **Environment variables** (Project Settings; never commit them): `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `FEATHERLESS_API_KEY`. Leave `VITE_API_BASE` unset (the dashboard calls `/api` on its own origin).
5. Deploy, open `https://YOUR-PROJECT.vercel.app/api/health` (expect `"direct_upload": true`), then Evidence > upload > Run analysis.

## Limits to know (untested until deployed on your account)

* **Bundle size:** OpenCV + pandas + numpy are about 234 MB against the roughly 250 MB function limit. If the build fails with a size error, the fallback is `docs/DEPLOY.md` (API on Render).
* **Time:** the Hobby plan caps functions at 60 s; analysis of a long video with the AI on may need Pro (300 s).
* **Cold starts** re-download the video from R2 for analysis and frames.
* **Audit log** is one object; two simultaneous writers can lose an entry.
