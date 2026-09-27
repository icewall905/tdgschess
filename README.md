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
  - **legal moves grouped by piece** (default off since Stockfish hints are on; on when hints are off). Research is mixed: helps small models avoid illegal
    moves (LLM Chess benchmark: −10–30% without), can hurt strong ones (dynomight). Try both.
  - **board image** (default off): 512px PNG (cairosvg), White at bottom, last move highlighted, sent as
    an OpenAI `image_url` part. Only for vision models; studies find images add little over text.
  - **chat** (default on): model adds a `SAY: …` line before `MOVE:` — short, kid-friendly reaction,
    optional character ("a friendly pirate"). Shown in the 💬 Chat tab next to Model log, with
    optional read-aloud (browser speech). Chat text is ignored when parsing the move.
  - **Stockfish hints** (always on): Stockfish's top 3 moves from a shallow depth-3 MultiPV search (eval in
    words + a 4-ply line) plus a game overview (phase, last 6 moves in words). Time limits were too strong
    and load-dependent: 0.5 s (depth ~20) and 50 ms (depth ~12) both let Gemma E4B win every game up to
    Stockfish ~1650; 10 ms still reaches depth ~9.
  Request errors (e.g. 502) back off and retry up to 6× without using an illegal-move retry.
  The first prompt of each move is logged (Model log → show reasoning → "prompt").
- Extra request JSON per LLM player is merged into the chat request, e.g.
  `{"chat_template_kwargs": {"enable_thinking": false}}` to turn off reasoning on llama.cpp/Qwen.
- Engine chat: Stockfish can have "Chat comments by an LLM" — after each engine move an LLM (endpoint +
  model + optional character) writes a kid-friendly chat line from move facts, material and the eval bar's
  score. It runs in the background, so the engine never waits for it; the comment appears when it arrives.
- 🎓 Learning mode (preset "Learn with a teacher", or the Learning mode box): the kid plays auto Stockfish and
  an LLM teacher (endpoint/model picked in setup, thinking switched off) explains each turn (the kid's move +
  the computer's reply) in 2-3 sentences. A newer turn replaces an explanation that hasn't finished, so fast
  players don't queue up LLM calls; if the endpoint is down, explanations are skipped and hints/questions get
  a friendly "teacher is on a break" message.
  All chess facts come from Stockfish (`app/coach.py`): evaluation, top moves + plans, threats (null-move search),
  pieces in danger, and a review of each of the kid's moves (win-probability loss: blunder/mistake/inaccuracy,
  what it allows, forks, missed mates, better moves). A blunder pauses the game before the engine replies and
  shows arrows (red = the move, orange = the punishment, green = better moves) with "Take it back" / "Keep it".
  The Teacher tab has 💡 Hint (engine-backed suggestions with arrows), ❓ Explain, and free questions — moves the
  kid mentions ("what if Nf3?", Danish letters too) are checked by Stockfish first. A "🏹 arrows" switch in the
  Teacher tab turns the board arrows/highlights on or off (remembered per device). Learning games don't change
  ratings and are left out of Standings.
- Language: English by default; pick 🇩🇰 Dansk per game for LLM chat lines, engine comments, chat replies and the
  teacher. The 🇬🇧/🇩🇰 switch next to the game's tabs changes it mid-game (players and the host device only).
- Each device remembers the player it last picked (profile) and uses it for presets, new Human sides and Join.
- LLM chat replies: when a player writes in the chat, the LLM players with chat on (and engines with an LLM
  commentator) answer within a few seconds in a separate background call — the game never waits. A burst of
  messages gets one reply; messages arriving while it types are answered once more afterwards. Replies stay on
  the game (off-topic questions get a friendly redirect) and teach one Stockfish fact: opening, plan, threat,
  why a move was good or a mistake. Asked for help ("what should I do?"), they give a hint from the player's own
  Stockfish facts and explain why it works, so the player learns the idea; unasked, they don't give moves away.
- Thinking: LLM players run with the model's reasoning mode off (`enable_thinking: false`) — with Stockfish
  hints it isn't needed, and Gemma 12B took 9-22 s and often lost its chat line to the token budget (2.8 s
  off). "🧠 Let it think" turns it on (rated separately, "…, think").
- Pure LLM toggle: no Stockfish hints/overview (legal-move list on); rated separately as "…, pure".
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

Live games with a human that nobody has looked at for `HIBERNATE_AFTER` seconds (default 3600) are saved as
"hibernated" and stopped; so are all running games when the server shuts down. They show a ▶ Resume button
(Games list and game page) that rebuilds the game at the same position, with the original devices' seats.

Throttling: optional LLM talk (engine commentary, the teacher's per-turn explanations) waits for
`QUIET_BEFORE_LLM` seconds (2.5) without a new move; if the players move on, it is replaced before any LLM call
is made, so 12 book moves in 12 s cost nothing and one comment arrives once the game pauses. In a known opening
line the commentator only talks on a capture, check or a new opening name. LLM players still write their chat
line in the same call that picks their move; chat replies batch bursts of messages.

Chat voice: LLM chat lines and engine comments sound like a real opponent, not a cheerleader. Each message gets
one ANGLE picked (weighted) from what Stockfish says actually happened — admit an own blunder, pounce on the
other player's mistake, react to a capture, tease a check or an attacked piece, worry about a threat, cheeky
confidence or a comeback — plus the last 3 lines to avoid repeats. Praise only for moves the engine rates good
or best; the opening is named once (and again when it changes). The commentator skips about half of the moves
where nothing notable happened. Danish messages get the Danish piece names. Worn-out phrases
("super strong move", "Godt spillet", "Wow"...) are banned. When the game ends, every talking LLM sends a closing
message: a good-game in character, the turning point (largest eval swing) and one tip. Hint answers list the
computer's reply to each suggested move, so questions like "won't you just take my rook?" get a true answer.

Explaining *why* (`coach.move_reasons`): for the best moves, python-chess works out the concrete consequences —
what it takes (and whether for free), checks, forks, pins, attacks, rescuing or protecting a piece, developing,
castling, the centre, open files, promotion — plus what happens next in Stockfish's line ("if they answer X, you
continue with Y (it takes the queen)"), a tempting capture/check that goes wrong ("Tempting but bad"), and 1-2
kid-friendly chess principles that fit. Questions with "why/hvorfor/forklar" get a 4-5 sentence child-level
explanation (move, what it does, what happens next, rule of thumb, what to avoid); help questions get the move
plus its main reason. Chat suggestions draw arrows on the board (green move, orange reply, blue follow-up;
click the 🏹 message to show them; the 🏹 arrows switch applies).

Chat lines are "chess-smart": LLM players and the engines' LLM commentator get Chat facts from Stockfish — the
opening name (Lichess CC0 opening list, `app/data/openings.tsv`), the engine's verdict on the opponent's last
move and how to punish it, the evaluation, Stockfish's plan and threats — and are told to use one real idea
while staying in character. The teacher's overview includes the opening name too.

Stockfish processes (each game's opponent, and the shared eval-bar, LLM-hint and teacher engines) start on
demand and shut down after `ENGINE_IDLE` seconds (default 300) without use, so games waiting for a human don't
hold memory; the next move restarts them. The container is limited to 3 GB.

Autostart (systemd): `docker-compose-chess-arena.service`. Everything runs on the CPU — no GPU needed.

Lc0 (GPU) was removed on 2026-09-27: on a 3090 shared with TabbyAPI it only drew with CPU Stockfish while
taking ~1.3 GB of VRAM. `docker-compose-lc0.service` is disabled; old Lc0 games still show in Games/Standings.
