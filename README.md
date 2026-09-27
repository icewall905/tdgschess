# TDGS Chess

Web UI at `http://<your-host>:8765` (e.g. `http://localhost:8765`) — human / LLM / Stockfish in any combination.

- Backend: FastAPI + python-chess (`app/main.py`), Stockfish 17 bundled in the image.
- LLMs: any OpenAI-compatible `/v1` endpoint, set in `.env` as `ENDPOINTS=name=url,name=url`.
  Each move is a fresh prompt (see Prompt below); the model must end
  with `MOVE: <san|uci>`. Illegal/unparseable answers are retried with the error fed back; after
  `retries` it plays a random move or forfeits (per-player setting). Illegal counts show in Standings.
- Prompt: system prompt walks the model through threats → safety → candidates → blunder check. User
  message has PGN, last move in words, FEN, bordered ASCII board, and (toggles per player):
  - **position facts** (default on): piece list per side, material, attacked pieces flagged
    UNDEFENDED / attacked by cheaper piece, available checks/captures, mate-in-one and opponent mate threats,
    repetition / 50-move counters. Computed by python-chess, so small models stop hallucinating pieces.
  - **legal moves grouped by piece** (default on). Research is mixed: helps small models avoid illegal
    moves (LLM Chess benchmark: −10–30% without), can hurt strong ones (dynomight). Try both.
  - **board image** (default off): 512px PNG (cairosvg), White at bottom, last move highlighted, sent as
    an OpenAI `image_url` part. Only for vision models; studies find images add little over text.
  - **chat** (default on): model adds a `SAY: …` line before `MOVE:` — short, kid-friendly reaction,
    optional character ("a friendly pirate"). Shown in the 💬 Chat tab next to Model log, with
    optional read-aloud (browser speech). Chat text is ignored when parsing the move.
  Request errors (e.g. 502) back off and retry up to 6× without using an illegal-move retry.
  The first prompt of each move is logged (Model log → show reasoning → "prompt").
- Extra request JSON per LLM player is merged into the chat request, e.g.
  `{"chat_template_kwargs": {"enable_thinking": false}}` to turn off reasoning on llama.cpp/Qwen.
- Engine chat: Stockfish can have "Chat comments by an LLM" — after each engine move an LLM (endpoint +
  model + optional character) writes a kid-friendly chat line from move facts, material and the eval bar's
  score. It runs in the background, so the engine never waits for it; the comment appears when it arrives.
- Players (👪): profiles with an emoji and a rating (start 600, stored in `data/profiles.json`). After each
  finished game a profile's rating moves by Elo (K=40 for the first 10 games, then 24) against the opponent's
  strength: another profile's rating, Stockfish's Elo (auto: its average effective Elo that game).
  Guests are unrated. LLM models are rated the same way (`data/llm_ratings.json`, new models start at 600),
  keyed by the real model behind the id *and* the endpoint (so a thinking and a no-think port of the same
  model are rated separately): vLLM's `root`, or for "currentmodel"-style aliases the loaded model
  from `/props` (or the endpoint's only real model). The rating shows when the model is picked in setup, in
  the player bar, Standings and on the Players page (🤖 Models). Self-play games don't count. On the first
  start the ratings are rebuilt from the archived games. Auto Stockfish plays even with a rated LLM.
- Stockfish manual strength goes down to 300 (below 1320 via the same calibrated sampler as auto).
- Auto Stockfish (default): plays `AUTO_BELOW` (100) Elo under its human opponent's rating and eases off
  mid-game when far ahead (up to −350 Elo at +7.5 pawns). At ≥1320 it uses Stockfish's own `UCI_Elo`; below
  that it samples among its top 8 moves with a softmax whose temperature grows as the rating drops, and
  searches shallower. The mapping is calibrated by engine-vs-engine matches against `UCI_Elo` 1320.
  Against LLMs/engines auto uses the slider value.
- Network play: set a human side to "📱 Another device". The game gets a 4-digit code; the other device taps
  🔑 Join, picks who they are, and taps the game (games waiting for a player are listed automatically and
  shown in a banner) or types the code. Each browser has a client id, so only the device holding a seat can
  move/resign for it, and reopening the page on that device rejoins. The host can "🔓 Free seat" if the kid
  switches devices. Game pages long-poll, so moves show on the other device instantly. Players can send emoji
  reactions and short messages in the chat. Home network only — there is no login.
- Matches: "Games" > 1 plays a series, alternating colours; results aggregate in Standings.
- Finished games are saved as JSON (incl. PGN + full model log) in `./data/games/`.
  Games still running are lost if the container restarts.

```
cp .env.example .env              # first time: set your endpoints / key
docker compose up -d --build     # after editing app/ or .env
```

Autostart (systemd): `docker-compose-chess-arena.service`. Everything runs on the CPU — no GPU needed.

Lc0 (GPU) was removed on 2026-09-27: on a 3090 shared with TabbyAPI it only drew with CPU Stockfish while
taking ~1.3 GB of VRAM. `docker-compose-lc0.service` is disabled; old Lc0 games still show in Games/Standings.
