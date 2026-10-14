# Alternatives: what to use when something breaks, without lowering quality

The doctor reads this before every diagnosis, fix and answer. A fallback is acceptable only if it keeps the channel's
identity (same voice gender and style, same format, same facts standard). When the only alternative would lower
quality, hold the publish and alert instead: a late Short costs less than a weak one at YPP review.

## general

- **Script writers (LLM).** Ladder: Gemini models discovered and ranked at run time (Pro/Flash first, Flash-Lite
  last; 429/503 retried in rounds) -> local Gemma 4 12B on the runner (llama.cpp) -> local Qwen3.5-35B-A3B
  (`FALLBACKS` in each `llm.py`; Qwen is picked automatically when Gemma can't be downloaded or won't start). Scout
  bench 2026-10-13: Qwen has Gemma's gate passes and runs 2.2x faster on CPU; its Hindi is rougher, so it stays behind
  Gemma for Hindi lanes. Gemini's free quota resets at 07:00 UTC (midnight Pacific). Never relax a gate to pass a
  weaker model's draft: make the writer repair with the gate's exact message instead.
- **The doctor's own brain.** Gemini (`DOCTOR_GEMINI_API_KEY`) -> local Qwen3.5-35B-A3B in heal/improve jobs
  (`DOCTOR_LOCAL=1`, about 13 GB, 10-15 min a diagnosis).
- **Narration.** Gemini TTS voice per channel -> the channel's Chatterbox clone of that same voice (reference clip
  in `tts/voices/`), checked by Whisper. Never switch to a different voice or gender.
- **Pictures.** Licensed real photos (Wikimedia Commons, CC/PD, credited) for real people and events; Z-Image-Turbo
  (stable-diffusion.cpp, CPU) only for fiction and mythology; museum paintings as Mool Katha's fallback. Never
  generate a real person's face or a real incident.
- **YouTube upload quota** (`quotaExceeded`, `uploadLimitExceeded`): each channel has its own GCP project and 10,000
  units a day; an upload costs about 1,600. Wait for the Pacific-midnight reset; keep the finished video in the
  buffer (draft release or state) and publish it in the next slot. Never move a channel to another channel's project.
- **YouTube feeds** (watch): channel feed -> uploads playlist feed (UC.. -> UU..) -> retry with pauses.
- **News and story sources.** Many feeds per lane (`outlets.py`); a 403 on GitHub runners (Indian Express) is a
  runner-IP block, not an outage. Wire copy counts as one owner. A refused source is skipped, never forced through.
- **GitHub Actions.** Public repos have free minutes; private repos stop when billing fails. Never ask the owner to
  pay: the free fix is to make the repo public after scanning its whole history for credentials and personal data
  and scrubbing any with git-filter-repo (done for universe-receipts on 2026-10-14). Every active channel repo is
  public. Transient runner errors are re-run up to 3 attempts. "No space left on device":
  delete caches or model files before the next download, never skip a quality step.
- **Credentials.** `invalid_grant` or a revoked token needs the owner to sign in again; the doctor never reads,
  copies or prints secrets.
- **Models that vanish (404).** Every lane discovers models at run time; a retired model is skipped automatically.

## kyun-hua

Hindi news explainers (hindi-news-desk). Writer: Gemini Flash -> Gemma 4 12B -> Qwen3.5-35B-A3B. Voice: Gemini TTS
Sulafat -> Chatterbox clone of Sulafat. Pictures: Commons photos per beat. Timing is the channel: publish trending
stories while they trend; a story past its moment is dropped, never published late.

## orbitwire

World news in 3D/photo stories (rebuilt-in-3d). Writer: local Gemma 4 12B (`STUDIO_LLM`) -> Qwen3.5-35B-A3B. Voices:
`india.wav` / `world.wav` Chatterbox clones. Photos from Commons; quakes keep the 3D terrain lane. Every name and
number must be in the source article plus a second fact-check pass.

## kahaani-chaos

Hindi magical twist stories. Writer: Gemini -> Gemma 4 12B (`KC_LLM`) -> Qwen3.5-35B-A3B, with code gates and an
editor pass; a repair keeps earlier scenes (`writer.carry`). Narrator: Gemini TTS Charon -> Chatterbox clone of
Charon. Paintings: Z-Image-Turbo paint matrix, vision-checked; a beat without people says so. Buffer of draft
releases covers a failed production; publish-only crons fill slots from it.

## pangaea

Long-form deep-time sleep documentaries plus Shorts. Daily build 16:10 UTC, premiere 00:30 UTC; `promote.yml`
publishes a passed dry run from its artifacts without a rebuild. Pictures: FLUX.2 klein / Z-Image / Qwen-Image
benchmarks in `bench-images.yml`.

## atlas

Data Shorts (Modal render, `history-last-hours-control/atlas-publish.yml` publishes via Buffer). Numbers come only
from the downloaded World Bank data (extremes audited). Modal credit or Buffer failures need the owner.

## mool-katha

Hindi scripture stories (mool-katha-control lite). Paintings: Z-Image-Turbo -> museum paintings automatically
(`LITE_AI_ART=off` switches AI art off). Published through Buffer to YouTube and Instagram.

## scam-autopsy

FTC-sourced scam explainers. Research: a refused draft is rewritten once with the checker's reason; a source that
still fails is skipped (no new script), and the health check alerts only when the ready queue runs low.

## universe

Hands-off for content: never change its scripts, uploads, titles or channel; watch and report only (no lab, no
caller). Studio: Modal runs it four times a day; the repo's `studio.yml` is the backup when Modal's last run failed
or is over 8 h old, and `watchdog.yml` checks Modal, Buffer, Cloudinary and the feed every 3 h. Billing: the repo
was made public on 2026-10-14 (the phone number scrubbed from history first) because its private Actions minutes
had stopped on GitHub billing; public repos run free, so billing is never the fix. If Modal's credit runs out, the
backup studio on GitHub takes over by itself.
