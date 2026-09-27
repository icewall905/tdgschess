# TDGS Chess

Web UI at `http://<your-host>:8765` (e.g. `http://localhost:8765`) — human / LLM / Stockfish / Lc0 in any combination.

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
- Lc0: talks UCI over TCP to `../lc0/chess-engine` (socat, e.g. `LC0_UCI_TCP=lc0-host:4001`).
  That container must be running to use it.
- Matches: "Games" > 1 plays a series, alternating colours; results aggregate in Standings.
- Finished games are saved as JSON (incl. PGN + full model log) in `./data/games/`.
  Games still running are lost if the container restarts.

```
cp .env.example .env              # first time: set your endpoints / key
docker compose up -d --build     # after editing app/ or .env
```

Autostart (systemd): `docker-compose-chess-arena.service` and `docker-compose-lc0.service`. TabbyAPI
autostarts via its own docker restart policy (whatever stack `switch-llm.sh` left up). Lc0 (small
`t1-256x10` net, ~1.4 GB VRAM) waits for the active LLM stack's GPU container to be healthy
(`../lc0/chess-engine/wait-llm-healthy.sh`); its container has `restart: "no"` so dockerd can't start it first.

Only one lc0 process ever runs: the engine side's socat doesn't fork, and the app shares one lc0
connection between all games (moves queue on a lock; parallel Lc0 games and Lc0 vs Lc0 are fine).
It's closed after `LC0_IDLE` seconds idle (default 600) to free the VRAM; the engine side also kills lc0 after
`IDLE_TIMEOUT` s (900) without traffic, in case a client vanished. The app reconnects automatically. If lc0 gives no move in time
(e.g. `CUDA error: out of memory`), the game ends with an error instead of hanging.
