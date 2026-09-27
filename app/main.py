"""TDGS Chess: humans, LLMs (OpenAI-compatible endpoints) and UCI engines playing each other."""

import asyncio
import json
import os
import random
import re
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

import chess
import chess.engine
import chess.pgn
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- config

def parse_endpoints(raw: str) -> dict[str, str]:
    """ENDPOINTS="name=http://host:port/v1,other=http://..." (name optional)."""
    out = {}
    for i, item in enumerate(x.strip() for x in raw.split(",") if x.strip()):
        name, _, url = item.partition("=") if "=" in item else (f"endpoint{i + 1}", "", item)
        if not url.startswith("http"):
            url = "http://" + url
        out[name.strip()] = url.rstrip("/")
    return out


ENDPOINTS = parse_endpoints(os.environ.get("ENDPOINTS", ""))
API_KEY = os.environ.get("LLM_API_KEY", "none")
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")
LC0_UCI_TCP = os.environ.get("LC0_UCI_TCP", "")  # host:port of a UCI-over-TCP lc0 (socat)
LC0_IDLE = float(os.environ.get("LC0_IDLE", "600"))  # seconds before the shared lc0 is closed (frees VRAM)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
GAMES_DIR = DATA_DIR / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)
MAX_PLIES = int(os.environ.get("MAX_PLIES", "400"))

# ---------------------------------------------------------------- models

class PlayerSpec(BaseModel):
    type: Literal["human", "llm", "stockfish", "lc0"]
    name: Optional[str] = None
    # llm
    endpoint: Optional[str] = None
    model: Optional[str] = None
    temperature: float = 0.6
    max_tokens: int = 8192
    show_legal: bool = True
    hints: bool = True  # position facts in the prompt: material, attacked/hanging pieces, captures, checks
    vision: bool = False  # also send a PNG of the board (model must accept images)
    chat: bool = True  # post a short kid-friendly chat message with each move
    persona: str = ""  # optional character for the chat messages, e.g. "a friendly pirate"
    retries: int = 3
    on_fail: Literal["random", "forfeit"] = "random"
    extra: dict = Field(default_factory=dict)  # merged into the chat request body
    # engines
    elo: int = 0  # stockfish: 0 = full strength, else 1320..3190
    movetime: float = 0.5  # seconds per move
    nodes: int = 0  # lc0: 0 = use movetime

    def label(self) -> str:
        if self.name:
            return self.name
        if self.type == "llm":
            return f"{self.model} @ {self.endpoint}"
        if self.type == "stockfish":
            return f"Stockfish ({self.elo} Elo)" if self.elo else "Stockfish (full)"
        if self.type == "lc0":
            return f"Lc0 ({self.nodes} nodes)" if self.nodes else f"Lc0 ({self.movetime}s)"
        return "Human"


class NewGame(BaseModel):
    white: PlayerSpec
    black: PlayerSpec
    games: int = 1
    swap_colors: bool = True
    move_delay: float = 0.6  # min seconds between non-human moves (for watching)
    analysis: bool = True
    start_fen: Optional[str] = None


class HumanMove(BaseModel):
    uci: str


# ---------------------------------------------------------------- analysis

class Analyzer:
    """One shared Stockfish used only for the eval bar."""

    def __init__(self):
        self.engine = None
        self.lock = asyncio.Lock()

    async def evaluate(self, board: chess.Board):
        async with self.lock:
            try:
                if self.engine is None:
                    _, self.engine = await chess.engine.popen_uci(STOCKFISH_PATH)
                    await self.engine.configure({"Threads": 2, "Hash": 128})
                info = await self.engine.analyse(board, chess.engine.Limit(time=0.25))
            except Exception:
                self.engine = None
                return None
        score = info["score"].white()
        best = info.get("pv", [None])[0]
        if score.is_mate():
            return {"mate": score.mate(), "cp": None, "best": best.uci() if best else None}
        return {"mate": None, "cp": score.score(), "best": best.uci() if best else None}


ANALYZER = Analyzer()


class Lc0Pool:
    """One shared lc0 connection for all games: each lc0 process holds ~1.4 GB of VRAM next to the
    LLM, so parallel games queue on the lock instead of spawning more. Closed after LC0_IDLE seconds."""

    def __init__(self):
        self.engine = None
        self.lock = asyncio.Lock()
        self.idle_task = None

    async def _connect(self):
        # the engine side serves one connection at a time and re-listens between them, so retry briefly;
        # a plain failure otherwise surfaces as a cryptic closed-transport error
        for attempt in range(10):
            try:
                _, self.engine = await asyncio.wait_for(
                    chess.engine.popen_uci(["socat", "-", f"TCP:{LC0_UCI_TCP}"]), 30)
                return
            except Exception as e:
                err = e
                await asyncio.sleep(0.5)
        raise RuntimeError(f"Lc0 unreachable at {LC0_UCI_TCP} ({type(err).__name__}: {err}) — "
                           "is ../lc0/chess-engine running?")

    async def _drop(self):
        if self.engine:
            try:
                await asyncio.wait_for(self.engine.quit(), 5)
            except Exception:
                pass
        self.engine = None

    async def _close_when_idle(self):
        await asyncio.sleep(LC0_IDLE)
        async with self.lock:
            await self._drop()

    async def play(self, board: chess.Board, limit: chess.engine.Limit, game_id: str) -> chess.Move:
        async with self.lock:
            if self.idle_task:
                self.idle_task.cancel()
            try:
                if self.engine is not None and self.engine.returncode.done():
                    self.engine = None  # engine side closed it (idle timeout / restart)
                if self.engine is None:
                    await self._connect()
                # lc0 prints "error CUDA error: out of memory" and never answers, so don't wait forever
                timeout = (limit.time or 0) * 3 + (limit.nodes or 0) / 20 + 60
                try:
                    res = await asyncio.wait_for(self.engine.play(board, limit, game=game_id), timeout)
                except asyncio.TimeoutError:
                    raise RuntimeError(f"Lc0 gave no move within {timeout:.0f}s "
                                       "(out of GPU memory? see docker logs chess-engine-lc0-1)") from None
            except BaseException:
                await self._drop()
                raise
            finally:
                self.idle_task = asyncio.create_task(self._close_when_idle())
            return res.move


LC0 = Lc0Pool()

# ---------------------------------------------------------------- players

THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
MOVE_RE = re.compile(r"MOVE\s*[:：]\s*[*`\"']*\s*([A-Za-z0-9=+#\-]+)", re.I)
SAY_RE = re.compile(r"^[\s*_>#-]*SAY\s*[:：]\s*(.+?)\s*$", re.I | re.M)

# Prompt design (see README): the position is the ground truth, not the model's memory of the move list;
# python-chess supplies the facts small models get wrong (where pieces are, what is attacked/hanging), and
# the model is walked through a short threat -> candidates -> blunder-check routine before answering.
SYSTEM_PROMPT = """You are a chess grandmaster playing {color} in a standard game of chess. Play the strongest move you can.

Think it through in this order, briefly:
1. Threats: what did the opponent's last move attack or threaten (checks, captures, mate, attacks on your pieces)?
2. Safety: which of your pieces are attacked or undefended? Don't leave material hanging.
3. Candidates: pick 2-4 candidate moves. Always look at checks, captures and threats first, for both sides.
4. Blunder check: for each candidate, find the opponent's best reply. Reject moves that lose material or allow mate.
5. Choose the best candidate. If you are clearly winning, trade down and push toward checkmate; avoid repeating positions.

Use only the position given (board, FEN, piece list) as ground truth - verify that the piece you move is really on
that square and that the move is in the legal move list when one is given. Do not invent pieces or squares.

End your reply with a final line in exactly this format:
MOVE: <move>
where <move> is in Standard Algebraic Notation (e.g. e4, Nf3, O-O, exd5, e8=Q)."""

PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
               chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}
PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def board_diagram(board: chess.Board) -> str:
    rows = ["  +-----------------+"]
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            p = board.piece_at(chess.square(file, rank))
            cells.append(p.symbol() if p else ".")
        rows.append(f"{rank + 1} | " + " ".join(cells) + " |")
    rows.append("  +-----------------+")
    rows.append("    a b c d e f g h")
    return "\n".join(rows)


def board_png(board: chess.Board) -> str:
    """Board image as a data URI (White at the bottom, like the text diagram), last move and check highlighted."""
    import base64

    import cairosvg
    import chess.svg

    last = board.peek() if board.move_stack else None
    check = board.king(board.turn) if board.is_check() else None
    svg = chess.svg.board(board, lastmove=last, check=check, size=512, coordinates=True)
    return "data:image/png;base64," + base64.b64encode(cairosvg.svg2png(bytestring=svg.encode())).decode()


def sq_piece(board: chess.Board, sq: int) -> str:
    p = board.piece_at(sq)
    return f"{p.symbol().upper()}{chess.square_name(sq)}" if p.piece_type != chess.PAWN else f"pawn {chess.square_name(sq)}"


def position_facts(board: chess.Board, me: bool) -> str:
    """Plain-language facts computed by python-chess, so the model doesn't have to reconstruct them."""
    out = []
    for color in (chess.WHITE, chess.BLACK):
        pieces = []
        for pt in (chess.KING, chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT, chess.PAWN):
            sqs = [chess.square_name(s) for s in board.pieces(pt, color)]
            if sqs:
                pieces.append(f"{PIECE_NAMES[pt]}{'s' if len(sqs) > 1 else ''} {', '.join(sqs)}")
        who = ("White" if color else "Black") + (" (you)" if color == me else "")
        out.append(f"{who}: " + "; ".join(pieces))

    mat = {c: sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values() if p.color == c) for c in (True, False)}
    diff = mat[me] - mat[not me]
    balance = "even" if diff == 0 else f"you are {abs(diff)} {'up' if diff > 0 else 'down'}"
    out.append(f"Material: you {mat[me]}, opponent {mat[not me]} ({balance})")

    def danger(color):
        lines = []
        for sq, p in board.piece_map().items():
            if p.color != color or p.piece_type == chess.KING:
                continue
            attackers = board.attackers(not color, sq)
            if not attackers:
                continue
            defended = bool(board.attackers(color, sq))
            cheapest = min(PIECE_VALUES[board.piece_type_at(a)] or 100 for a in attackers)
            tag = "UNDEFENDED" if not defended else ("attacked by a cheaper piece" if cheapest < PIECE_VALUES[p.piece_type] else "defended")
            lines.append(f"{sq_piece(board, sq)} attacked by {', '.join(sq_piece(board, a) for a in attackers)} ({tag})")
        return lines

    mine = danger(me)
    out.append("Your pieces under attack: " + ("; ".join(mine) if mine else "none"))
    theirs = danger(not me)
    out.append("Opponent pieces you attack: " + ("; ".join(theirs) if theirs else "none"))

    checks = [board.san(m) for m in board.legal_moves if board.gives_check(m)]
    captures = [board.san(m) for m in board.legal_moves if board.is_capture(m)]
    mates = []
    for m in board.legal_moves:
        board.push(m)
        if board.is_checkmate():
            mates.append(board.peek())
        board.pop()
    if mates:
        out.append("Checkmate in one available: " + ", ".join(board.san(m) for m in mates))
    out.append("Your checks: " + (", ".join(checks) if checks else "none"))
    out.append("Your captures: " + (", ".join(captures) if captures else "none"))

    # does the opponent threaten mate in one if it were their move?
    if not board.is_check():
        nb = board.copy(stack=False)
        nb.turn = not me
        nb.ep_square = None
        threats = []
        for m in nb.legal_moves:
            nb.push(m)
            if nb.is_checkmate():
                threats.append(m)
            nb.pop()
        if threats:
            out.append("WARNING: opponent threatens checkmate with " + ", ".join(nb.san(m) for m in threats))

    rights = []
    if board.has_kingside_castling_rights(me):
        rights.append("O-O")
    if board.has_queenside_castling_rights(me):
        rights.append("O-O-O")
    out.append("Your castling rights: " + (", ".join(rights) if rights else "none"))
    reps = board_repetitions(board)
    if reps > 1:
        out.append(f"This position has occurred {reps} times (3 times = draw).")
    if board.halfmove_clock >= 60:
        out.append(f"{board.halfmove_clock} half-moves without a capture or pawn move (100 = draw).")
    return "\n".join(out)


def board_repetitions(board: chess.Board) -> int:
    key = board._transposition_key()
    b = board.copy()
    n = 1
    while b.move_stack:
        b.pop()
        if b._transposition_key() == key:
            n += 1
    return n


def legal_by_piece(board: chess.Board) -> str:
    groups: dict[int, list[str]] = {}
    for m in board.legal_moves:
        groups.setdefault(m.from_square, []).append(board.san(m))
    order = sorted(groups, key=lambda s: (-PIECE_VALUES[board.piece_type_at(s)] if board.piece_type_at(s) != chess.KING else -100, s))
    return "\n".join(f"{PIECE_NAMES[board.piece_type_at(s)]} {chess.square_name(s)}: {' '.join(sorted(groups[s]))}"
                     for s in order)


def movetext(board: chess.Board) -> str:
    game = chess.pgn.Game.from_board(board)
    txt = game.accept(chess.pgn.StringExporter(headers=False, variations=False, comments=False))
    return txt.replace("*", "").strip()


def try_parse(board: chess.Board, token: str):
    tok = token.strip().strip(".,;:!?*`\"'()[]")
    tok = tok.replace("0-0-0", "O-O-O").replace("0-0", "O-O")
    if not tok:
        return None, "empty move"
    try:
        return board.parse_san(tok), None
    except chess.IllegalMoveError:
        return None, f"'{tok}' is illegal in this position"
    except chess.AmbiguousMoveError:
        return None, f"'{tok}' is ambiguous — specify the file or rank of the piece"
    except ValueError:
        pass
    try:
        mv = chess.Move.from_uci(tok.lower())
        if mv in board.legal_moves:
            return mv, None
        return None, f"'{tok}' is illegal in this position"
    except ValueError:
        return None, f"'{tok}' is not valid SAN or UCI notation"


CHAT_PROMPT = """

You are also chatting with the audience, who are children watching the game. Just before the MOVE line, write one
line in exactly this format:
SAY: <your message>
The message is 1-2 short sentences (at most 25 words) that react to the opponent's last move and/or announce your
own move in a cute, funny, kind way that fits the position (proud of a capture, "oops!" after losing a piece,
excited about a check, a friendly compliment for a good opponent move). Emojis are welcome. Be sporting and
kid-friendly: never mean, scary or rude. Speak in character{persona}."""


def parse_say(text: str) -> str:
    found = SAY_RE.findall(THINK_RE.sub("", text or ""))
    if not found:
        return ""
    say = re.split(r"\bMOVE\s*[:：]", found[-1], flags=re.I)[0]  # in case both landed on one line
    return say.strip().strip("*_\"'`").strip()[:300]


def parse_llm_move(board: chess.Board, text: str):
    text = THINK_RE.sub("", text or "")
    found = MOVE_RE.findall(text)
    if found:
        return try_parse(board, found[-1])
    text = SAY_RE.sub("", text)  # chat text can mention squares like "e4"; keep it out of the fallback
    # fallback: last token in the tail that is a legal move
    for tok in reversed(re.findall(r"[A-Za-z0-9=+#\-]+", text[-400:])):
        if len(tok) >= 2:
            mv, _ = try_parse(board, tok)
            if mv:
                return mv, None
    return None, "no 'MOVE: <move>' line found in reply"


class LLMPlayer:
    def __init__(self, spec: PlayerSpec, color: bool, game: "Game"):
        self.spec, self.color, self.game = spec, color, game
        base = ENDPOINTS.get(spec.endpoint or "", spec.endpoint or "")
        if not base:
            raise ValueError(f"unknown endpoint {spec.endpoint!r}")
        self.url = base.rstrip("/") + "/chat/completions"

    async def start(self):
        pass

    async def close(self):
        pass

    def system_prompt(self, cname: str) -> str:
        sp = SYSTEM_PROMPT.format(color=cname)
        if self.spec.chat:
            persona = self.spec.persona.strip()
            sp += CHAT_PROMPT.format(persona=f" as {persona}" if persona else " as a cheerful chess buddy")
        return sp

    def build_prompt(self, board: chess.Board, rejected: list[str]) -> list[dict]:
        cname = "White" if self.color else "Black"
        parts = []
        mt = movetext(board)
        parts.append(f"Game so far (PGN): {mt}" if mt else "No moves have been played yet.")
        if board.move_stack:
            last = board.copy()
            mv = last.pop()
            p = last.piece_at(mv.from_square)
            parts.append(f"Opponent's last move: {last.san(mv)} ({PIECE_NAMES[p.piece_type]} "
                         f"{chess.square_name(mv.from_square)} to {chess.square_name(mv.to_square)})")
        parts.append(f"Current position (FEN): {board.fen()}")
        parts.append(f"Board (White uppercase at the bottom, Black lowercase at the top; {cname} to move):\n"
                     f"{board_diagram(board)}")
        if board.is_check():
            parts.append("You are in CHECK - you must get out of check.")
        if self.spec.hints:
            parts.append("Position facts (plain facts computed from the board, no evaluation - you must still choose the move yourself):\n" + position_facts(board, self.color))
        if self.spec.show_legal:
            parts.append("Legal moves, by piece:\n" + legal_by_piece(board))
        if rejected:
            parts.append("Your previous attempt(s) were rejected:\n" + "\n".join(f"- {r}" for r in rejected)
                         + "\nChoose a different, legal move" + (" from the list above." if self.spec.show_legal else "."))
        ending = "the SAY: line, then the line MOVE: <move>" if self.spec.chat else "the line MOVE: <move>"
        parts.append(f"What is your move as {cname}? Finish with {ending}.")
        text = "\n\n".join(parts)
        user: object = text
        if self.spec.vision:
            user = [{"type": "image_url", "image_url": {"url": board_png(board)}},
                    {"type": "text", "text": "The image shows the current board (White at the bottom, last move "
                                             "highlighted).\n\n" + text}]
        return [
            {"role": "system", "content": self.system_prompt(cname)},
            {"role": "user", "content": user},
        ]

    async def choose(self, board: chess.Board) -> Optional[chess.Move]:
        rejected: list[str] = []
        side = "white" if self.color else "black"
        stats = self.game.stats[side]
        attempt, net_fails = 0, 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
            while attempt <= self.spec.retries:
                messages = self.build_prompt(board, rejected)
                body = {
                    "model": self.spec.model,
                    "messages": messages,
                    "temperature": self.spec.temperature,
                    "max_tokens": self.spec.max_tokens,
                    **self.spec.extra,
                }
                t0 = time.time()
                entry = {"ply": len(board.move_stack), "side": side, "attempt": attempt + 1}
                if attempt == 0:  # the prompt for retries only adds the rejection list
                    u = messages[1]["content"]
                    entry["prompt"] = u if isinstance(u, str) else "[board image]\n" + u[-1]["text"]
                try:
                    r = await client.post(self.url, json=body, headers={"Authorization": f"Bearer {API_KEY}"})
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    detail = ""
                    if isinstance(e, httpx.HTTPStatusError):
                        detail = " — " + e.response.text[:300]
                    entry.update(error=f"request failed: {str(e).splitlines()[0]}{detail}",
                                 seconds=round(time.time() - t0, 1))
                    self.game.add_log(entry)
                    stats["errors"] += 1
                    # server trouble isn't the model's fault: back off and retry without using up an attempt
                    net_fails += 1
                    if net_fails > 6:
                        break
                    await asyncio.sleep(min(60, 3 * 2 ** (net_fails - 1)))
                    continue
                attempt += 1
                choice = data["choices"][0]
                msg = choice.get("message", {})
                content = msg.get("content") or ""
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                m = re.search(r"<think>(.*?)</think>", content, re.S | re.I)
                if m and not reasoning:
                    reasoning = m.group(1)
                usage = data.get("usage") or {}
                stats["llm_calls"] += 1
                stats["tokens"] += usage.get("completion_tokens", 0)
                stats["seconds"] += time.time() - t0
                mv, err = parse_llm_move(board, content)
                if not content.strip() and choice.get("finish_reason") == "length":
                    err = "empty reply (hit max_tokens while reasoning) — raise max_tokens"
                entry.update(
                    content=THINK_RE.sub("", content).strip()[-3000:],
                    reasoning=reasoning.strip()[-6000:],
                    tokens=usage.get("completion_tokens"),
                    seconds=round(time.time() - t0, 1),
                )
                if self.spec.chat:
                    entry["say"] = parse_say(content)
                if mv:
                    entry["move"] = board.san(mv)
                    self.game.add_log(entry)
                    return mv
                entry["error"] = err
                self.game.add_log(entry)
                stats["illegal"] += 1
                rejected.append(err)
        if self.spec.on_fail == "forfeit":
            return None
        mv = random.choice(list(board.legal_moves))
        stats["random_moves"] += 1
        self.game.add_log({"ply": len(board.move_stack), "side": side,
                           "error": f"out of retries — random move {board.san(mv)} played"})
        return mv


class EnginePlayer:
    def __init__(self, spec: PlayerSpec, color: bool, game: "Game"):
        self.spec, self.color, self.game = spec, color, game
        self.engine = None

    async def start(self):
        if self.spec.type == "stockfish":
            _, self.engine = await chess.engine.popen_uci(STOCKFISH_PATH)
            opts = {"Threads": 2, "Hash": 64}
            if self.spec.elo:
                opts.update(UCI_LimitStrength=True, UCI_Elo=max(1320, min(3190, self.spec.elo)))
            await self.engine.configure(opts)
        else:
            if not LC0_UCI_TCP:
                raise ValueError("LC0_UCI_TCP not configured")

    async def close(self):
        if self.engine:
            try:
                await asyncio.wait_for(self.engine.quit(), 5)
            except Exception:
                pass

    async def choose(self, board: chess.Board) -> chess.Move:
        limit = (chess.engine.Limit(nodes=self.spec.nodes) if self.spec.type == "lc0" and self.spec.nodes
                 else chess.engine.Limit(time=self.spec.movetime))
        t0 = time.time()
        if self.spec.type == "lc0":
            mv = await LC0.play(board, limit, self.game.id)
        else:
            mv = (await self.engine.play(board, limit)).move
        side = "white" if self.color else "black"
        self.game.stats[side]["seconds"] += time.time() - t0
        return mv


class HumanPlayer:
    def __init__(self, spec, color, game: "Game"):
        self.game = game

    async def start(self):
        pass

    async def close(self):
        pass

    async def choose(self, board: chess.Board) -> Optional[chess.Move]:
        self.game.awaiting_human = True
        self.game.touch()
        try:
            return await self.game.human_moves.get()  # None = resign
        finally:
            self.game.awaiting_human = False


def make_player(spec: PlayerSpec, color: bool, game):
    return {"human": HumanPlayer, "llm": LLMPlayer}.get(spec.type, EnginePlayer)(spec, color, game)


# ---------------------------------------------------------------- game

def new_stats():
    return {"illegal": 0, "random_moves": 0, "errors": 0, "llm_calls": 0, "tokens": 0, "seconds": 0.0}


class Game:
    def __init__(self, white: PlayerSpec, black: PlayerSpec, opts: NewGame, match_id: str, index: int):
        self.id = uuid.uuid4().hex[:10]
        self.match_id, self.index = match_id, index
        self.white, self.black, self.opts = white, black, opts
        self.board = chess.Board(opts.start_fen) if opts.start_fen else chess.Board()
        self.status = "queued"
        self.result, self.termination = "*", ""
        self.log: list[dict] = []
        self.stats = {"white": new_stats(), "black": new_stats()}
        self.evals: list = []
        self.thinking_since: Optional[float] = None
        self.human_moves: asyncio.Queue = asyncio.Queue()
        self.awaiting_human = False
        self.created = time.time()
        self.version = 0
        self.task: Optional[asyncio.Task] = None

    def touch(self):
        self.version += 1

    def add_log(self, entry: dict):
        self.log.append(entry)
        self.touch()

    def spec_for(self, color: bool) -> PlayerSpec:
        return self.white if color else self.black

    def pgn(self) -> str:
        g = chess.pgn.Game.from_board(self.board)
        g.headers.update(Event="TDGS Chess", Site="local", White=self.white.label(),
                         Black=self.black.label(), Result=self.result,
                         Date=time.strftime("%Y.%m.%d", time.localtime(self.created)))
        if self.termination:
            g.headers["Termination"] = self.termination
        return str(g)

    def finish(self, result: str, termination: str):
        self.result, self.termination = result, termination
        self.status = "finished"
        self.thinking_since = None
        self.touch()
        self.save()

    def save(self):
        (GAMES_DIR / f"{self.id}.json").write_text(json.dumps(self.state(full=True)))

    async def run(self):
        self.status = "running"
        self.touch()
        players = {chess.WHITE: make_player(self.white, chess.WHITE, self),
                   chess.BLACK: make_player(self.black, chess.BLACK, self)}
        try:
            for p in players.values():
                await p.start()
            if self.opts.analysis:
                self.evals.append(await ANALYZER.evaluate(self.board))
            while True:
                b = self.board
                outcome = b.outcome(claim_draw=True)
                if outcome:
                    reason = outcome.termination.name.replace("_", " ").lower()
                    return self.finish(outcome.result(), reason)
                if len(b.move_stack) >= MAX_PLIES:
                    return self.finish("1/2-1/2", f"adjudicated draw after {MAX_PLIES} plies")
                spec = self.spec_for(b.turn)
                self.thinking_since = time.time()
                self.touch()
                t0 = time.time()
                mv = await players[b.turn].choose(b.copy())
                if mv is None:
                    loser = "White" if b.turn else "Black"
                    why = "resigned" if spec.type == "human" else "forfeit (no legal move after retries)"
                    return self.finish("0-1" if b.turn else "1-0", f"{loser} {why}")
                if spec.type != "human":
                    await asyncio.sleep(max(0, self.opts.move_delay - (time.time() - t0)))
                b.push(mv)
                self.thinking_since = None
                self.touch()
                if self.opts.analysis:
                    self.evals.append(await ANALYZER.evaluate(b))
                    self.touch()
        except asyncio.CancelledError:
            self.status, self.termination = "aborted", "aborted"
            self.thinking_since = None
            self.touch()
            self.save()
            raise
        except Exception as e:
            self.status, self.termination = "error", f"error: {e}"
            self.thinking_since = None
            self.touch()
            self.save()
        finally:
            for p in players.values():
                await p.close()

    def state(self, full: bool = True) -> dict:
        b = self.board
        human_turn = self.status == "running" and self.awaiting_human
        s = {
            "id": self.id, "match_id": self.match_id, "index": self.index,
            "white": {"label": self.white.label(), **self.white.model_dump()},
            "black": {"label": self.black.label(), **self.black.model_dump()},
            "status": self.status, "result": self.result, "termination": self.termination,
            "plies": len(b.move_stack), "created": self.created, "version": self.version,
            "stats": self.stats,
        }
        if not full:
            return s
        replay = chess.Board(self.opts.start_fen) if self.opts.start_fen else chess.Board()
        sans, fens = [], [replay.fen()]
        for mv in b.move_stack:
            sans.append(replay.san(mv))
            replay.push(mv)
            fens.append(replay.fen())
        s.update(
            fen=b.fen(), turn="white" if b.turn else "black",
            uci=[m.uci() for m in b.move_stack], san=sans, fens=fens,
            check=b.is_check(), human_turn=human_turn,
            legal=[m.uci() for m in b.legal_moves] if human_turn else [],
            thinking_for=round(time.time() - self.thinking_since, 1) if self.thinking_since else None,
            log=self.log, evals=self.evals, pgn=self.pgn(), opts=self.opts.model_dump(),
        )
        return s


# ---------------------------------------------------------------- registry

GAMES: dict[str, Game] = {}
ARCHIVE: dict[str, dict] = {}  # finished games from disk (summary + full)

for f in sorted(GAMES_DIR.glob("*.json")):
    try:
        d = json.loads(f.read_text())
        ARCHIVE[d["id"]] = d
    except Exception:
        pass


async def run_match(games: list[Game]):
    for g in games:
        if g.status == "queued":
            g.task = asyncio.current_task()
            await g.run()


# ---------------------------------------------------------------- api

app = FastAPI(title="TDGS Chess")
STATIC = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def config():
    async def probe(url):
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.get(url + "/models", headers={"Authorization": f"Bearer {API_KEY}"})
                r.raise_for_status()
                return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            return None

    results = await asyncio.gather(*(probe(u) for u in ENDPOINTS.values()))
    return {
        "endpoints": [{"name": n, "url": u, "models": m, "online": m is not None}
                      for (n, u), m in zip(ENDPOINTS.items(), results)],
        "lc0": bool(LC0_UCI_TCP),
        "max_plies": MAX_PLIES,
    }


@app.post("/api/games")
async def create(req: NewGame):
    for spec in (req.white, req.black):
        if spec.type == "llm" and (not spec.endpoint or not spec.model):
            raise HTTPException(400, "LLM players need an endpoint and a model")
        if spec.type == "lc0" and not LC0_UCI_TCP:
            raise HTTPException(400, "Lc0 is not configured (LC0_UCI_TCP)")
    if req.start_fen:
        try:
            chess.Board(req.start_fen)
        except ValueError:
            raise HTTPException(400, "invalid FEN")
    n = max(1, min(100, req.games))
    match_id = uuid.uuid4().hex[:8]
    games = []
    for i in range(n):
        w, b = (req.black, req.white) if (req.swap_colors and i % 2) else (req.white, req.black)
        g = Game(w, b, req, match_id, i + 1)
        GAMES[g.id] = g
        games.append(g)
    task = asyncio.create_task(run_match(games))
    for g in games:
        g.task = task
    return {"ids": [g.id for g in games]}


def all_summaries():
    out = {gid: d for gid, d in ARCHIVE.items()}
    for gid, g in GAMES.items():
        out[gid] = g.state(full=False)
    keys = ("id", "match_id", "index", "white", "black", "status", "result", "termination",
            "plies", "created", "stats")
    return sorted(({k: d.get(k) for k in keys} for d in out.values()),
                  key=lambda d: d["created"], reverse=True)


@app.get("/api/games")
async def list_games():
    return all_summaries()


def engine_settings(p: dict) -> str:
    """Strength settings shown under an engine's name in the standings (they also separate its rows)."""
    if p.get("type") == "stockfish":
        elo = f"{p['elo']} Elo" if p.get("elo") else "full strength"
        return f"{elo} · {p.get('movetime', 0.5):g}s/move"
    if p.get("type") == "lc0":
        return f"{p['nodes']} nodes" if p.get("nodes") else f"{p.get('movetime', 1):g}s/move"
    return ""


@app.get("/api/standings")
async def standings():
    table: dict[tuple, dict] = {}
    for d in all_summaries():
        if d["status"] != "finished":
            continue
        for side, other, win in (("white", "black", "1-0"), ("black", "white", "0-1")):
            p = d[side]
            settings = engine_settings(p)
            row = table.setdefault((p["label"], settings), {"label": p["label"], "type": p["type"], "settings": settings,
                                                            "games": 0, "w": 0, "d": 0, "l": 0, "illegal": 0,
                                                            "random_moves": 0})
            row["games"] += 1
            row["illegal"] += d["stats"][side]["illegal"]
            row["random_moves"] += d["stats"][side]["random_moves"]
            if d["result"] == win:
                row["w"] += 1
            elif d["result"] == "1/2-1/2":
                row["d"] += 1
            else:
                row["l"] += 1
    rows = list(table.values())
    for r in rows:
        r["points"] = r["w"] + r["d"] / 2
        r["score"] = round(100 * r["points"] / r["games"], 1)
    return sorted(rows, key=lambda r: (-r["score"], -r["games"]))


def get_game(gid) -> Game:
    if gid not in GAMES:
        raise HTTPException(404, "game not found or not live")
    return GAMES[gid]


@app.get("/api/games/{gid}")
async def game_state(gid: str, since: int = -1):
    if gid in GAMES:
        g = GAMES[gid]
        if since == g.version and g.thinking_since is None:
            return {"unchanged": True, "version": g.version}
        return g.state()
    if gid in ARCHIVE:
        return ARCHIVE[gid]
    raise HTTPException(404, "game not found")


@app.get("/api/games/{gid}/pgn", response_class=PlainTextResponse)
async def game_pgn(gid: str):
    if gid in GAMES:
        return GAMES[gid].pgn()
    if gid in ARCHIVE:
        return ARCHIVE[gid]["pgn"]
    raise HTTPException(404)


@app.post("/api/games/{gid}/move")
async def human_move(gid: str, m: HumanMove):
    g = get_game(gid)
    if not (g.status == "running" and g.awaiting_human):
        raise HTTPException(409, "not your turn")
    try:
        mv = chess.Move.from_uci(m.uci)
    except ValueError:
        raise HTTPException(400, "bad move")
    if mv not in g.board.legal_moves:
        raise HTTPException(400, "illegal move")
    g.awaiting_human = False
    await g.human_moves.put(mv)
    return {"ok": True}


@app.post("/api/games/{gid}/resign")
async def resign(gid: str):
    g = get_game(gid)
    if g.status == "running" and g.awaiting_human:
        g.awaiting_human = False
        await g.human_moves.put(None)
        return {"ok": True}
    raise HTTPException(409, "you can resign on your own turn")


@app.post("/api/games/{gid}/abort")
async def abort(gid: str):
    g = get_game(gid)
    if g.task and not g.task.done():
        # abort the running match task; the rest of the queued games in the match are aborted too
        g.task.cancel()
    for other in GAMES.values():
        if other.match_id == g.match_id and other.status == "queued":
            other.status, other.termination = "aborted", "aborted"
            other.touch()
    return {"ok": True}


@app.delete("/api/games/{gid}")
async def delete(gid: str):
    g = GAMES.get(gid)
    if g and g.status in ("running", "queued"):
        raise HTTPException(409, "abort it first")
    GAMES.pop(gid, None)
    ARCHIVE.pop(gid, None)
    (GAMES_DIR / f"{gid}.json").unlink(missing_ok=True)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
