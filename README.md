# The Long Ago — production pipeline

Mostly unattended pipeline for an English "daily life in history" documentary channel, built on free tiers.
Setup guide (Turkish): [KURULUM.md](KURULUM.md)

- Script: Gemini free tier with model fallback chains (`config.yaml` → `models`, `"auto"` finds the newest Flash).
- Voice: Kokoro-82M on the Actions CPU (ElevenLabs / Gemini TTS optional).
- Images: Cloudflare Workers AI FLUX.2 klein → FLUX.1 schnell → Gemini (optional, paid); Gemini vision QC redraws defects.
- Render: ffmpeg Ken Burns, per-chapter cross-dissolves, grain/vignette, chapter titles, captions, thumbnail, 2 Shorts.

Workflows: `topics` (Mon) → `produce` (Tue/Thu) → review issue → `/onay` / `/iptal` or silent auto-approve after 48 h
(`auto_approve`, daily, 1 upload/day for YouTube quota) → scheduled publish. `compile` (monthly sleep compilation),
`check` (smoke test of all keys). Uploads need `UPLOAD_MODE=api` (repo variable) once the YouTube API audit passes.

Offline test: `MOCK=1 python -m pipeline selftest` (needs ffmpeg).
