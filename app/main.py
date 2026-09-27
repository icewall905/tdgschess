"""TDGS Chess: humans, LLMs (OpenAI-compatible endpoints) and UCI engines playing each other."""

import asyncio
import json
import math
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
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, PrivateAttr

from app import coach, recap

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
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
GAMES_DIR = DATA_DIR / "games"
GAMES_DIR.mkdir(parents=True, exist_ok=True)
MAX_PLIES = int(os.environ.get("MAX_PLIES", "400"))
PROFILES_FILE = DATA_DIR / "profiles.json"
START_RATING = 600
AUTO_BELOW = 100  # auto Stockfish plays this many Elo below its human opponent: a bit weaker, still a challenge

# ---------------------------------------------------------------- profiles

DEFAULT_PROFILES = [("Far", "🧔"), ("Mor", "👩"), ("Lily", "🌸"), ("Ria", "🌟")]


def load_profiles() -> dict[str, dict]:
    try:
        return json.loads(PROFILES_FILE.read_text())
    except FileNotFoundError:
        out = {}
        for name, emoji in DEFAULT_PROFILES:
            pid = uuid.uuid4().hex[:8]
            out[pid] = new_profile(pid, name, emoji, START_RATING)
        return out


def new_profile(pid: str, name: str, emoji: str, rating: int) -> dict:
    return {"id": pid, "name": name, "emoji": emoji, "rating": rating, "games": 0, "w": 0, "d": 0, "l": 0,
            "history": [{"t": time.time(), "rating": rating}], "created": time.time()}


def save_profiles():
    tmp = PROFILES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(PROFILES, ensure_ascii=False))
    tmp.replace(PROFILES_FILE)


PROFILES = load_profiles()
save_profiles()

# ---------------------------------------------------------------- LLM model ratings

LLM_RATINGS_FILE = DATA_DIR / "llm_ratings.json"
LLM_START_RATING = 600
MODEL_KEYS: dict[tuple[str, str], str] = {}  # (endpoint name, model id) -> rating key (the real model behind aliases)


def full_llm_key(model_key: str, endpoint: Optional[str]) -> str:
    """Ratings are per real model *and* endpoint: one model can be served with different settings (e.g. thinking
    on one port, no-think on another), which plays very differently."""
    return model_key if " · " in model_key or not endpoint else f"{model_key} · {endpoint}"


def new_llm_rating(key: str) -> dict:
    model, _, ep = key.partition(" · ")
    ep = ep.replace(" · ", ", ")  # "box73 · pure" -> "box73, pure"
    return {"key": key, "name": f"{model.rsplit('/', 1)[-1]}{f' ({ep})' if ep else ''}", "rating": LLM_START_RATING, "games": 0, "w": 0, "d": 0,
            "l": 0, "history": [{"t": time.time(), "rating": LLM_START_RATING}], "created": time.time()}


def save_llm_ratings():
    tmp = LLM_RATINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(LLM_RATINGS, ensure_ascii=False))
    tmp.replace(LLM_RATINGS_FILE)


try:
    LLM_RATINGS: dict[str, dict] = json.loads(LLM_RATINGS_FILE.read_text())
    LLM_BACKFILL = False
except FileNotFoundError:
    LLM_RATINGS, LLM_BACKFILL = {}, True  # built from the archived games on startup


async def probe_endpoint(name: str, url: str) -> Optional[list[str]]:
    """List an endpoint's models and remember which real model each id (e.g. the "currentmodel" alias) is."""
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(url + "/models", headers={"Authorization": f"Bearer {API_KEY}"})
            r.raise_for_status()
            models = r.json().get("data", [])
            model_path = None
            if any(not m.get("root") for m in models):
                try:  # llama.cpp / TabbyAPI style: the loaded model's path
                    pr = await c.get(url.removesuffix("/v1") + "/props", headers={"Authorization": f"Bearer {API_KEY}"})
                    model_path = pr.json().get("model_path") if pr.status_code == 200 else None
                except Exception:
                    pass
    except Exception:
        return None
    loaded = model_path.rstrip("/").rsplit("/", 1)[-1] if model_path and "/" in model_path else None
    real = [m["id"] for m in models if not m.get("root") and "current" not in m["id"].lower()]
    if not loaded and len(real) == 1:
        loaded = real[0]  # e.g. TabbyAPI behind a proxy without /props: the one real model is what's loaded
    for m in models:
        mid = m["id"]
        if m.get("root"):
            key = m["root"].rstrip("/").rsplit("/", 1)[-1]
        elif loaded and "current" in mid.lower():
            key = loaded
        else:
            key = mid
        MODEL_KEYS[(name, mid)] = key
    return [m["id"] for m in models]


def sibling_model_key(endpoint: str, mid: str) -> str:
    """An alias we couldn't resolve (e.g. a proxy port without /props): use what another endpoint on the same
    host says that alias is — typically the same server behind a different port."""
    key = MODEL_KEYS.get((endpoint, mid), mid)
    if key != mid or "current" not in mid.lower() or endpoint not in ENDPOINTS:
        return key
    host = httpx.URL(ENDPOINTS[endpoint]).host
    for (ep, m), k in MODEL_KEYS.items():
        if ep != endpoint and m == mid and k != mid and ep in ENDPOINTS and httpx.URL(ENDPOINTS[ep]).host == host:
            return k
    return key


async def resolve_model_key(endpoint: str, model: str) -> str:
    if endpoint in ENDPOINTS and ((endpoint, model) not in MODEL_KEYS or MODEL_KEYS[(endpoint, model)] == model):
        await asyncio.gather(*(probe_endpoint(n, u) for n, u in ENDPOINTS.items()))  # siblings may know the alias
    return full_llm_key(sibling_model_key(endpoint, model), endpoint)


def llm_key_of(p: dict) -> Optional[str]:
    """Rating key of an LLM player from a (possibly archived) game summary."""
    if p.get("type") != "llm":
        return None
    base = p.get("rating_key") or MODEL_KEYS.get((p.get("endpoint"), p.get("model"))) or p.get("model")
    base = base.removesuffix(" · SF hints") if base else base
    return full_llm_key(base, p.get("endpoint")) if base else None


def elo_update(rec: dict, before: float, opp: float, score: float) -> int:
    k = 40 if rec["games"] < 10 else 24
    delta = round(k * (score - 1 / (1 + 10 ** ((opp - before) / 400))))
    rec["rating"] += delta
    rec["games"] += 1
    rec["w" if score == 1 else "d" if score == 0.5 else "l"] += 1
    return delta

# ---------------------------------------------------------------- models

class PlayerSpec(BaseModel):
    type: Literal["human", "llm", "stockfish"]
    name: Optional[str] = None
    # human
    profile: Optional[str] = None  # profile id; its name and rating are used
    remote: bool = False  # plays on another device, which joins with the game's code
    # llm
    endpoint: Optional[str] = None
    model: Optional[str] = None
    temperature: float = 0.6
    max_tokens: int = 8192
    show_legal: bool = False  # the Stockfish hints below are a clearer shortlist; turn on when hints are off
    hints: bool = True  # position facts in the prompt: material, attacked/hanging pieces, captures, checks
    engine_hints: bool = True  # legacy field
    think: bool = False  # llm: allow the model's reasoning mode (much slower; Stockfish hints do the analysis anyway)
    pure: bool = False  # llm: no Stockfish hints / overview (the model's own chess); rated separately as "pure"
    vision: bool = False  # also send a PNG of the board (model must accept images)
    chat: bool = True  # post a short kid-friendly chat message with each move
    persona: str = ""  # optional character for the chat messages, e.g. "a friendly pirate"
    retries: int = 3
    on_fail: Literal["random", "forfeit"] = "random"
    extra: dict = Field(default_factory=dict)  # merged into the chat request body
    # engines
    elo: int = 0  # stockfish: 0 = full strength, else 300..3190 (below 1320 via the calibrated weak sampler)
    auto: bool = False  # stockfish: play a bit below the opponent's rating, easing off when far ahead
    rating_key: Optional[str] = None  # llm: set at game creation to the real model behind the id
    commentary: bool = False  # engines: an LLM writes a chat message for each move (uses persona/extra)
    comment_endpoint: Optional[str] = None
    comment_model: Optional[str] = None
    movetime: float = 0.5  # seconds per move
    _seat: Optional[str] = PrivateAttr(default=None)  # client id of the device playing this human side

    def label(self) -> str:
        if self.name:
            return self.name
        if self.type == "llm":
            return f"{self.model} @ {self.endpoint}"
        if self.type == "stockfish":
            if self.auto:
                return "Stockfish (auto)"
            return f"Stockfish ({self.elo} Elo)" if self.elo else "Stockfish (full)"
        return "Human"


class NewGame(BaseModel):
    white: PlayerSpec
    black: PlayerSpec
    games: int = 1
    swap_colors: bool = True
    move_delay: float = 0.6  # min seconds between non-human moves (for watching)
    analysis: bool = True
    start_fen: Optional[str] = None
    # learning mode: an LLM teacher explains Stockfish's view to the (human) learner; no rating changes
    learning: bool = False
    lang: Literal["en", "da"] = "en"
    teacher_endpoint: Optional[str] = None
    teacher_model: Optional[str] = None


class HumanMove(BaseModel):
    uci: str


class ProfileIn(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    rating: Optional[int] = None


class JoinReq(BaseModel):
    code: str
    profile: Optional[str] = None
    name: Optional[str] = None  # guest name when no profile


class SayReq(BaseModel):
    text: str


# ---------------------------------------------------------------- analysis

ENGINE_IDLE = float(os.environ.get("ENGINE_IDLE", "300"))
HIBERNATE_AFTER = float(os.environ.get("HIBERNATE_AFTER", "3600"))
QUIET_BEFORE_LLM = float(os.environ.get("QUIET_BEFORE_LLM", "2.5"))  # s without a new move before optional LLM talk  # s without any player viewing a live game  # seconds without use before a Stockfish is shut down


class LazyEngine:
    """A Stockfish process started on demand and shut down after ENGINE_IDLE seconds without use, so games left
    waiting for a human (and idle shared engines) don't hold memory. Restarts transparently, same options."""

    def __init__(self, options: dict):
        self.options = options
        self.engine = None
        self.idle_task: Optional[asyncio.Task] = None

    async def acquire(self):
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        if self.engine is not None and self.engine.returncode.done():
            self.engine = None
        if self.engine is None:
            _, self.engine = await chess.engine.popen_uci(STOCKFISH_PATH)
            await self.engine.configure(self.options)
        return self.engine

    def release(self):
        if self.idle_task:
            self.idle_task.cancel()
        self.idle_task = asyncio.create_task(self._close_later())

    async def _close_later(self):
        await asyncio.sleep(ENGINE_IDLE)
        self.idle_task = None
        await self.close()

    async def close(self):
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        engine, self.engine = self.engine, None
        if engine:
            try:
                await asyncio.wait_for(engine.quit(), 5)
            except Exception:
                pass


class Analyzer:
    """One shared Stockfish used only for the eval bar."""

    def __init__(self):
        self.lazy = LazyEngine({"Threads": 2, "Hash": 32})
        self.lock = asyncio.Lock()

    async def evaluate(self, board: chess.Board):
        async with self.lock:
            try:
                engine = await self.lazy.acquire()
                info = await engine.analyse(board, chess.engine.Limit(time=0.5))  # also the 💡 hint arrow
                self.lazy.release()
            except Exception:
                await self.lazy.close()
                return None
        score = info["score"].white()
        best = info.get("pv", [None])[0]
        if score.is_mate():
            return {"mate": score.mate(), "cp": None, "best": best.uci() if best else None}
        return {"mate": None, "cp": score.score(), "best": best.uci() if best else None}


ANALYZER = Analyzer()


# Depth, not time: time limits vary with CPU load, and even 10-50 ms reaches depth 9-12 (~2000+ play).
# 0.5 s and 50 ms hints both let Gemma E4B win every game up to Stockfish ~1650.
HINT_DEPTH = 3


class Advisor:
    """Stockfish's top moves for LLM players with engine hints (separate from the eval-bar engine)."""

    def __init__(self):
        self.lazy = LazyEngine({"Threads": 2, "Hash": 32})
        self.lock = asyncio.Lock()

    async def top_moves(self, board: chess.Board, n: int = 3, depth: int = HINT_DEPTH) -> list[dict]:
        async with self.lock:
            try:
                engine = await self.lazy.acquire()
                infos = await engine.analyse(board, chess.engine.Limit(depth=depth),
                                             multipv=min(n, board.legal_moves.count()))
                self.lazy.release()
            except Exception:
                await self.lazy.close()
                return []
        out = []
        for info in infos:
            pv = info.get("pv") or []
            if not pv:
                continue
            b = board.copy()
            line = []
            for mv in pv[:4]:
                line.append(b.san(mv))
                b.push(mv)
            score = info["score"].pov(board.turn)
            out.append({"move": line[0], "line": line, "cp": score.score(mate_score=100000), "mate": score.mate()})
        return out


ADVISOR = Advisor()


def eval_words(cp: int, mate: Optional[int]) -> str:
    if mate is not None:
        return f"you can force checkmate in {mate}" if mate > 0 else f"you get checkmated in {-mate} with best play"
    pawns = cp / 100
    mood = ("you are winning" if cp > 300 else "you are better" if cp > 80 else "roughly equal" if cp > -80
            else "you are worse" if cp > -300 else "you are losing")
    return f"{pawns:+.1f} ({mood})"


OPENINGS: dict[str, tuple[str, str]] = {}  # position (EPD) -> (ECO code, name), from the Lichess CC0 data set


def load_openings():
    try:
        lines = (Path(__file__).parent / "data" / "openings.tsv").read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        if line.startswith(("#", "eco\t")):
            continue
        eco, name, pgn = line.split("\t")
        b = chess.Board()
        try:
            for tok in pgn.split():
                if not tok[0].isdigit():
                    b.push_san(tok)
        except ValueError:
            continue
        OPENINGS[b.epd()] = (eco, name)


load_openings()


def opening_of(board: chess.Board) -> Optional[str]:
    """Name of the opening the game is in (the latest known position, so transpositions count too)."""
    b = board.copy()
    for _ in range(min(len(b.move_stack), 40) + 1):
        hit = OPENINGS.get(b.epd())
        if hit:
            return f"{hit[1]} ({hit[0]})"
        if not b.move_stack:
            break
        b.pop()
    return None


def game_overview(board: chess.Board, color: bool) -> str:
    n = sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values() if p.piece_type != chess.PAWN)
    phase = "opening" if board.fullmove_number <= 10 and n > 50 else "endgame" if n <= 26 else "middlegame"
    lines = [f"Move {board.fullmove_number}, {phase}."]
    opening = opening_of(board)
    if opening:
        lines.append(f"Opening: {opening}")
    b = board.copy()
    recent = []
    for _ in range(min(6, len(b.move_stack))):
        mv = b.pop()
        who = "You" if b.turn == color else "Opponent"
        recent.append(f"{who}: {move_words(b, mv)}")
    if recent:
        lines.append("Recent moves (oldest first):\n" + "\n".join(f"- {r}" for r in reversed(recent)))
    return "\n".join(lines)


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

Be concise: keep your whole reply short (at most ~120 words of analysis) - no long essays, no restating the
position, no listing every legal move.

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


VOICE = """Sound like a real, lively chess opponent sitting across the board - not a cheerleader. React like a \
person would: surprise ("Hey, sneaky!"), worry ("Uh-oh, my rook is in trouble"), cheeky confidence ("My knight is \
coming for you!"), jokes, bragging about a plan, and admitting your own mistakes ("Oops, I just gave away my \
bishop!"). Only praise the other player's move when the facts say it was good or the best - never call a move \
strong just to be nice. Mention the opening only when the facts say a new opening was reached. Follow the ANGLE \
given with the facts. Never use these worn-out phrases: "Wow", "That was a super strong move", "Great move", \
"You're playing really well", "Det var et super stærkt træk", "Godt spillet", "Hold da op, for et godt træk", \
"Du spiller virkelig godt". Start every message differently and don't repeat your recent lines. \
1-2 short sentences, at most two emojis, in character, kid-friendly (never mean, scary or rude)."""


CHAT_PROMPT = """

You are also chatting with the audience, who are children watching the game. Just before the MOVE line, write one
line in exactly this format:
SAY: <your message>
The message (at most 25 words) reacts to the game and/or your move, built on one real idea from the "Chat facts".
Speak in character{persona}. {lang}
""" + VOICE


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
            sp += CHAT_PROMPT.format(persona=f" as {persona}" if persona else " as a cheerful chess buddy",
                                     lang=chat_lang_line(self.game))
        return sp

    def build_prompt(self, board: chess.Board, rejected: list[str], advice: Optional[list[dict]] = None) -> list[dict]:
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
        if not self.spec.pure:
            parts.append("Game overview:\n" + game_overview(board, self.color))
        if self.spec.hints:
            parts.append("Position facts (plain facts computed from the board, no evaluation - you must still choose the move yourself):\n" + position_facts(board, self.color))
        if advice:
            rows = [f"{i + 1}. {a['move']}  eval {eval_words(a['cp'], a['mate'])}  line: {' '.join(a['line'])}"
                    for i, a in enumerate(advice)]
            parts.append("Engine suggestions (quick Stockfish search, best first; eval from your side):\n" + "\n".join(rows)
                         + "\nThese are strong. Normally play one of them; only deviate for a clear reason.")
        if self.spec.show_legal:
            parts.append("Legal moves, by piece:\n" + legal_by_piece(board))
        if self.spec.chat and getattr(self, "chat_facts", None):
            parts.append("Chat facts (from Stockfish, for your SAY line):\n" + "\n".join(f"- {f}" for f in self.chat_facts))
        if self.spec.chat and getattr(self, "unread", None):
            parts.append("The players wrote in the chat since your last move - answer them (kindly, briefly) in your "
                         "SAY line:\n" + "\n".join(f"- {m}" for m in self.unread))
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
        advice = None if self.spec.pure else await ADVISOR.top_moves(board)
        self.unread = unread_chat(self.game, "white" if self.color else "black") if self.spec.chat else []
        self.chat_facts = (await chat_insight(self.game, "white" if self.color else "black", board)
                           if self.spec.chat else [])
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
            while attempt <= self.spec.retries:
                messages = self.build_prompt(board, rejected, advice)
                body = {
                    "model": self.spec.model,
                    "messages": messages,
                    "temperature": self.spec.temperature,
                    "max_tokens": self.spec.max_tokens,
                    "chat_template_kwargs": {"enable_thinking": self.spec.think},
                    **self.spec.extra,  # e.g. its own chat_template_kwargs override the above
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


# Measured by engine-vs-engine matches (40 games per pair, 0.1 s/move for UCI_Elo), chained down from
# Stockfish's own UCI_Elo 1320: (Elo, softmax temperature in centipawns, search depth).
WEAK_TABLE = [(1410, 100, 5), (1183, 125, 5), (950, 150, 4), (760, 200, 3), (600, 250, 2), (370, 350, 1), (290, 600, 1)]


def weak_params(elo: float) -> tuple[float, int]:
    """Below Stockfish's own UCI_Elo floor (1320): sample among its top moves with a softmax whose temperature
    grows as the rating drops, searching shallower too. Interpolated from WEAK_TABLE; ~290 is the weakest."""
    if elo >= WEAK_TABLE[0][0]:
        return WEAK_TABLE[0][1], WEAK_TABLE[0][2]
    for (e1, t1, d1), (e0, t0, d0) in zip(WEAK_TABLE, WEAK_TABLE[1:]):
        if elo >= e0:
            f = (elo - e0) / (e1 - e0)
            return t0 + (t1 - t0) * f, round(d0 + (d1 - d0) * f)
    return WEAK_TABLE[-1][1], WEAK_TABLE[-1][2]


def soften(elo: float, advantage_cp: int) -> float:
    """Mid-game softening: the further the engine is ahead, the more it eases off (up to 350 Elo)."""
    if advantage_cp <= 150:
        return elo
    return elo - min(1.0, (advantage_cp - 150) / 600) * 350


async def chat_context(board: chess.Board, color: bool) -> list[str]:
    """Stockfish facts that let even a small model chat like a chess expert: the opening, how good the opponent's
    last move was and what it allows, the best plan now and threats. `board` is before `color`'s move."""
    facts = []
    opening = opening_of(board)
    if opening:
        facts.append(f"Opening: {opening}")
    if board.move_stack:
        prev = board.copy()
        last = prev.pop()
        mine = prev.turn == color
        try:
            r = await coach.review(prev, last, prev.turn)
            verdict = {"best": "the best move", "good": "a good move", "inaccuracy": "a small inaccuracy",
                       "mistake": "a mistake", "blunder": "a big blunder"}[r["cls"]]
            whose = "Your" if mine else "The opponent's"
            line = f"{whose} last move {r['words']} was {verdict} (engine)"
            if r["cls"] in ("mistake", "blunder") and r["reply"]:
                line += (f"; it allows {r['reply']['words']}" if mine else f"; you can punish it with {r['reply']['words']}")
            if r["motifs"]:
                line += "; " + "; ".join(r["motifs"])
            facts.append(line)
        except Exception:
            pass
    try:
        a = await coach.analyse(board, color)
        facts.append(f"The position for you: {a['eval']}")
        if a.get("opponent_plan"):
            facts.append(f"What Stockfish expects the opponent to play: {' '.join(a['opponent_plan'])}")
        if a["top"]:
            facts.append(f"Stockfish's plan for you: {' '.join(a['top'][0]['line'])} (starting with {a['top'][0]['words']})")
        if a["threat"]:
            facts.append(f"The opponent threatens {a['threat']['words']}")
        if a["targets"]:
            facts.append("Opponent pieces you can attack or win: " + ", ".join(f"{d['piece']} on {d['square']}" for d in a["targets"][:2]))
    except Exception:
        pass
    return facts


EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")
WORD_RE = re.compile(r"[^\W\d_]+", re.U)
STOP = set("the a an and to of in is it my your you i me we og at en et er det din min mig du jeg den til på for med så nu "
           "har kan skal vil ikke men om som".split())


def recent_lines(game, side: str, n: int = 6) -> list[str]:
    return [e["say"] for e in game.log if e.get("side") == side and e.get("say") and e.get("kind") != "ask"][-n:]


def first_word(text: str) -> str:
    words = WORD_RE.findall(text.lower())
    return words[0] if words else ""


def phrases(text: str) -> set[tuple]:
    words = WORD_RE.findall(text.lower())
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2) if sum(w not in STOP for w in words[i:i + 3]) >= 2}


def style_guard(game, side: str) -> list[str]:
    """Tell the model exactly what it has been repeating: openers, phrases and emojis of its last lines."""
    lines = recent_lines(game, side)
    if not lines:
        return []
    out = []
    starts = sorted({first_word(l) for l in lines[-4:] if first_word(l)})
    if starts:
        out.append("Your recent messages started with: " + ", ".join(f'"{w}"' for w in starts) + " - start differently")
    counts: dict = {}
    for l in lines:
        for ph in phrases(l):
            counts[ph] = counts.get(ph, 0) + 1
    used = [" ".join(ph) for ph, _ in sorted(counts.items(), key=lambda x: -x[1])[:8]]
    if used:
        out.append("Phrases you already used - do NOT use them again: " + "; ".join(f'"{u}"' for u in used))
    emojis = sorted({e for l in lines[-3:] for e in EMOJI_RE.findall(l)})
    if emojis:
        out.append(f"Emojis you just used: {' '.join(emojis)} - pick different ones, or none")
    return out


def too_similar(text: str, lines: list[str]) -> bool:
    if not lines:
        return False
    fw = first_word(text)
    if fw and fw in {first_word(l) for l in lines[-3:]}:
        return True
    mine = phrases(text)
    return any(mine & phrases(l) for l in lines[-4:])


async def talk(base: str, body: dict, lines: list[str]) -> Optional[str]:
    """One chat line from the LLM; if it sounds like a recent line, ask once more for something different."""
    text = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
        for attempt in range(2):
            try:
                r = await client.post(base.rstrip("/") + "/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {API_KEY}"})
                r.raise_for_status()
                draft = r.json()["choices"][0]["message"].get("content") or ""
            except Exception:
                return text or None
            draft = THINK_RE.sub("", draft).strip()
            draft = re.sub(r"^\s*(SAY|MOVE)\s*[:：]\s*", "", draft, flags=re.I).strip().strip("\"'`*").strip()
            if not draft:
                continue
            text = draft
            if not too_similar(draft, lines):
                break
            body = {**body, "temperature": 1.0, "messages": body["messages"] + [
                {"role": "assistant", "content": draft},
                {"role": "user", "content": "That sounds too much like your earlier messages (same start or the same "
                                            "phrases). Write a completely different message: different first word, "
                                            "different words, different idea. Reply with only the message."}]}
    return text or None


async def chat_insight(game, side: str, board: chess.Board, own: Optional[chess.Move] = None,
                       optional: bool = False) -> Optional[list[str]]:
    """Facts for a chat line plus one ANGLE picked from what actually happened, so messages vary like a real
    opponent's: admit an own blunder, pounce on theirs, react to captures, tease threats, brag about a plan...
    `board` is the position before `side`'s move `own` (None: the move isn't known yet, e.g. an LLM player)."""
    color = side == "white"
    facts, angles = [], []  # (weight, angle)
    after = board.copy()
    if own:
        after.push(own)
    opening = opening_of(after)
    if opening and game.said_opening.get(side) != opening:
        facts.append(f"New opening reached: {opening}")
        angles.append((2, f"mention that we are now in the {opening.split(' (')[0]}"))
        game.said_opening[side] = opening
    if board.move_stack:
        prev = board.copy()
        last = prev.pop()
        try:
            r = await coach.review(prev, last, prev.turn)
            took = prev.piece_at(last.to_square)
            facts.append(f"The other player's last move: {r['words']} - engine verdict: {r['cls']}")
            if r["cls"] in ("blunder", "mistake"):
                how = f" ({r['reply']['words']})" if r["reply"] else ""
                facts.append(f"It lets you punish it{how}" + (f"; {r['motifs'][0]}" if r["motifs"] else ""))
                angles.append((5, "pounce on their mistake - cheeky and excited, but kind"))
            elif took:
                angles.append((4, f"react to losing your {PIECE_NAMES[took.piece_type]} - dramatic, funny"))
            elif r["cls"] == "best":
                angles.append((2, "give an honest, impressed compliment for their move"))
        except Exception:
            pass
    if own:
        try:
            mine = await coach.review(board, own, color)
            facts.append(f"Your move: {mine['words']} - engine verdict: {mine['cls']}")
            if mine["cls"] in ("blunder", "mistake"):
                if mine["motifs"]:
                    facts.append(f"Your move's problem: {mine['motifs'][0]}")
                angles.append((6, "admit your own mistake in a funny way (\"oops...\") - you are only human"))
            elif board.is_capture(own):
                angles.append((4, "brag a little about the piece you just captured"))
            if after.is_check():
                angles.append((4, "tease that you are giving check"))
        except Exception:
            pass
    try:
        a = await coach.analyse(after, color)
        facts.append(f"Score for you: {a['eval']}")
        if a["threat"]:
            facts.append(f"The other player threatens {a['threat']['words']}")
            angles.append((3, "sound a bit worried about their threat"))
        if a["targets"]:
            t = a["targets"][0]
            facts.append(f"You are attacking their {t['piece']} on {t['square']}")
            angles.append((3, f"tease that you are after their {t['piece']}"))
        if (a["cp"] or 0) > 300:
            angles.append((2, "cheeky confidence - you are ahead"))
        elif (a["cp"] or 0) < -300:
            angles.append((2, "determined to make a comeback - you are behind"))
        plan = a["top"][0]["line"] if a["top"] else a.get("opponent_plan")
        if plan:
            facts.append(f"Stockfish's expected line: {' '.join(plan)}")
    except Exception:
        pass
    angles += [(1, "tell them about your plan for your pieces"), (1, "friendly banter about how the game is going")]
    weight, angle = random.choices(angles, [w * w for w, _ in angles])[0]
    if optional and weight <= 2 and random.random() < 0.5:
        return None  # nothing notable happened: a real opponent doesn't talk after every move either
    facts += style_guard(game, side)
    facts.append(f"ANGLE for this message: {angle}")
    return facts


def unread_chat(game, side: str) -> list[str]:
    """Players' chat messages the LLM on `side` hasn't answered yet (it answers once, with its next move)."""
    seen = game.chat_seen.get(side, 0)
    game.chat_seen[side] = len(game.log)
    return [f"{game.spec_for(e['side'] == 'white').label()}: {e['say']}"
            for e in game.log[seen:] if e.get("kind") == "chat" and e.get("side") != side and not e.get("bot")]


DEEP_Q = re.compile(r"hvorfor|why|forklar|explain|hvordan|how come|hvad gør", re.I)
HELP_Q = re.compile(r"hvad skal|what should|anbefal|recommend|foresl|suggest|tip|hjælp|help|hint|"
                    r"næste træk|next move|hvad nu|what now|best move|bedste træk", re.I)


def has_unread_chat(game, side: str) -> bool:
    return any(e.get("kind") == "chat" and e.get("side") != side and not e.get("bot")
               for e in game.log[game.chat_seen.get(side, 0):])


CHAT_REPLY_PROMPT = """You are {persona}, playing {color} in a chess game that children are watching and playing. \
A player just wrote in the game chat - most players are children (about 6-12), so use simple words and short \
sentences a child understands; if you use a chess word like fork, pin or castle, explain it in a few words. \
Reply in 1-3 short sentences, kind and fun, in character. Answer the \
question directly; don't start every reply with a greeting or their name, and avoid empty words like "super \
strong" - say concretely what a move does (what it attacks, takes, defends or threatens, and what the answer is).
Always keep the conversation on THIS chess game: if they ask about something else, answer in a few friendly words \
and bring it right back to the board. Be eager to teach: explain one real idea from the "Chess facts" (they come \
from the Stockfish engine and are true) - the opening's name, a plan, a threat, why a move was good or a mistake. \
If a player asks for help or a hint, help them gladly: suggest one of THEIR best moves from the "Facts for the \
player" and explain simply WHY it is good (what it attacks, defends or prepares), so they learn the idea - not just \
the move. Use the listed computer answers to be honest ("if you take on f7, my king takes your rook back").
Don't give away their best moves when they haven't asked.
Never invent moves or pieces that are not in the facts. Kid-friendly: never mean, scary or rude. {lang}"""


def chat_responders(game) -> list[tuple[str, PlayerSpec]]:
    """LLMs that talk in this game: LLM players with chat on, and engines with an LLM commentator."""
    out = []
    for side, sp in (("white", game.white), ("black", game.black)):
        if (sp.type == "llm" and sp.chat and sp.endpoint and sp.model) or \
                (sp.type == "stockfish" and sp.commentary and sp.comment_endpoint and sp.comment_model):
            out.append((side, sp))
    return out


async def chat_reply(game, side: str, spec: PlayerSpec):
    """Answer the players' chat right away (without waiting for the next move), about the game."""
    color = side == "white"
    board = game.board.copy()
    facts = await chat_context(board, color)
    unread = unread_chat(game, side)
    if not unread:
        return
    convo = [e for e in game.log if e.get("kind") == "chat"][-8:]
    history = "\n".join(f"{'You' if e.get('bot') and e['side'] == side else game.spec_for(e['side'] == 'white').label()}: {e['say']}"
                        for e in convo)
    endpoint, model = (spec.endpoint, spec.model) if spec.type == "llm" else (spec.comment_endpoint, spec.comment_model)
    base = ENDPOINTS.get(endpoint or "", endpoint or "")
    persona = spec.persona.strip() or "a cheerful chess buddy"
    helping, arrows = "", []
    opp = game.spec_for(not color)
    asked = " ".join(unread).lower()
    deep = bool(DEEP_Q.search(asked))
    wants_help = deep or bool(HELP_Q.search(asked))
    if opp.type == "human":  # the player's own side, in case they ask for a hint
        try:
            a = await coach.analyse(board, not color)
            if wants_help and a["top"]:
                a["mistake"] = await coach.tempting_mistake(board, not color, a["top"])
                pv = a["top"][0].get("pv") or [a["top"][0]["move"]]
                arrows = [{"from": chess.square_name(m.from_square), "to": chess.square_name(m.to_square), "color": c}
                          for m, c in zip(pv[:3], ("green", "orange", "blue"))]
            helping = f"\n\nFacts for the player {opp.label()} (only use if they ask for help):\n" + coach.facts_text(a, opp.label())
        except Exception:
            pass
    guard = style_guard(game, side)
    user = ("Chess facts (from Stockfish, from your side):\n" + "\n".join(f"- {f}" for f in facts + guard) + helping
            + f"\n\nRecent chat:\n{history}\n\nNew messages to answer:\n" + "\n".join(f"- {m}" for m in unread)
            + ("\n\nThey want to understand WHY. Explain it like to a child, in 4-5 short, simple sentences: the move "
               "to play, what it concretely does (from 'why it is good' - a picture they can see on the board), what "
               "will probably happen next, and the chess rule of thumb behind it; if listed, one tempting move to "
               "avoid and why." if deep else
               "\n\nThey ask for help: name the move, then its most important concrete reason and what happens "
               "next, in 2-3 short sentences." if wants_help else "")
            + "\n\nReply with only your chat message.")
    body = {"model": model, "temperature": 0.7, "max_tokens": 600 if deep else 350,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": CHAT_REPLY_PROMPT.format(
                persona=persona, color="White" if color else "Black", lang=chat_lang_line(game))},
                {"role": "user", "content": user}]}
    text = await talk(base, body, recent_lines(game, side))
    if text:
        game.add_log({"ply": len(game.board.move_stack), "side": side, "kind": "chat", "bot": True,
                      "say": text[:900], "arrows": arrows, "t": time.time()})


CLOSING_PROMPT = """You are {persona}, and a chess game you played as {color} against {opp} just ended. Write the \
closing message a friendly human opponent would say (2-3 short sentences): a warm "good game" that fits the \
result (congratulate them if they won, be a good sport; if you won, be kind and encouraging), what decided the \
game (the key moment from the facts), and ONE concrete tip that fits the result: if they won, name what worked or \
a next step to get even better; if they lost, what to watch for next time. Only name moves that appear in \
the facts - never invent moves. In character, kid-friendly, at most two emojis. Reply with only the message. {lang}"""


def key_moment(game) -> Optional[str]:
    """The move that swung the game most, from the eval bar's scores."""
    ev = game.evals
    if len(ev) < 3:
        return None
    replay = chess.Board(game.opts.start_fen) if game.opts.start_fen else chess.Board()
    best, when = 0.0, None
    frac = lambda e: 0.5 if not e else (1.0 if (e.get("mate") or 0) > 0 else 0.0) if e.get("mate") is not None \
        else 1 / (1 + math.exp(-(e.get("cp") or 0) / 250))
    for i, mv in enumerate(game.board.move_stack):
        if i + 1 >= len(ev):
            break
        swing = abs(frac(ev[i + 1]) - frac(ev[i]))
        if swing > best:
            best, when = swing, (replay.fullmove_number, replay.turn, move_words(replay, mv))
        replay.push(mv)
    if not when or best < 0.2:
        return None
    n, turn, words = when
    return f"The turning point was move {n}: {'White' if turn else 'Black'} played {words}"


async def closing_chat(game, side: str, spec: PlayerSpec):
    color = side == "white"
    win = "1-0" if color else "0-1"
    outcome = "you won" if game.result == win else "it was a draw" if game.result == "1/2-1/2" else "you lost"
    facts = [f"Result: {game.result} ({game.termination}) - {outcome}"]
    if game.board.move_stack:
        b = game.board.copy()
        last = b.pop()
        facts.append(f"The final move: {'White' if b.turn else 'Black'} played {move_words(b, last)}")
    km = key_moment(game)
    if km:
        facts.append(km)
    opening = opening_of(game.board)
    if opening:
        facts.append(f"The opening was the {opening}")
    other = "black" if color else "white"
    r = (game.ratings or {}).get(other)
    if r and r.get("delta") is not None:
        facts.append(f"The other player's rating went {r['before']} -> {r['after']}")
    endpoint, model = (spec.endpoint, spec.model) if spec.type == "llm" else (spec.comment_endpoint, spec.comment_model)
    base = ENDPOINTS.get(endpoint or "", endpoint or "")
    body = {"model": model, "temperature": 0.8, "max_tokens": 300, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": CLOSING_PROMPT.format(
                persona=spec.persona.strip() or "a cheerful chess buddy", color="White" if color else "Black",
                opp=game.spec_for(not color).label(), lang=chat_lang_line(game))},
                {"role": "user", "content": "\n".join(f"- {f}" for f in facts)}]}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            resp = await client.post(base.rstrip("/") + "/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {API_KEY}"})
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"].get("content") or ""
    except Exception:
        return
    text = THINK_RE.sub("", text).strip().strip("\"'`*").strip()
    if text:
        game.add_log({"ply": len(game.board.move_stack), "side": side, "kind": "chat", "bot": True, "closing": True,
                      "say": text[:500], "t": time.time()})
        game.save()  # finished games are archived from disk: keep the goodbye


def schedule_chat_replies(game):
    """One reply task per talking LLM: waits a moment to gather a burst of messages, answers, and answers again
    only if new messages arrived meanwhile."""
    for side, spec in chat_responders(game):
        task = game.reply_tasks.get(side)
        if task and not task.done():
            continue  # the running task picks up the new messages when it finishes

        async def loop(side=side, spec=spec):
            await asyncio.sleep(1.5)
            while has_unread_chat(game, side):
                await chat_reply(game, side, spec)

        game.reply_tasks[side] = coach.background(loop())


def chat_lang_line(game) -> str:
    """The game's language for chat lines (the MOVE line stays in chess notation)."""
    if getattr(game, "opts", None) and game.opts.lang == "da":
        return ("Write the chat message in Danish (dansk), with the right Danish chess words: bonde (pawn), springer "
                "(knight), løber (bishop), tårn (rook), dronning (queen), konge (king), skak (check), skakmat "
                "(checkmate), rokade (castling). Only Danish words: no English words, no English words in "
                "parentheses, no English exclamations like Uh-oh, Whoa, Wow or Oops. Any MOVE line stays in "
                "standard English chess notation.")
    return "Write the chat message in English."


COMMENT_PROMPT = """You are {persona}, playing {color} against {opp} in a game children are playing and watching. \
You just made your move. Write ONE chat message (at most 25 words) built on one real idea from the "Chat facts". \
Reply with only the message. {lang}
""" + VOICE

COMMENT_TASKS: set = set()  # keep references so fire-and-forget tasks aren't garbage collected


def move_words(board: chess.Board, mv: chess.Move) -> str:
    p = board.piece_at(mv.from_square)
    txt = f"{board.san(mv)} ({PIECE_NAMES[p.piece_type]} {chess.square_name(mv.from_square)} to {chess.square_name(mv.to_square)}"
    victim = board.piece_at(mv.to_square) or (chess.Piece(chess.PAWN, not p.color) if board.is_en_passant(mv) else None)
    if victim:
        txt += f", capturing a {PIECE_NAMES[victim.piece_type]}"
    after = board.copy(stack=False)
    after.push(mv)
    txt += ", checkmate!" if after.is_checkmate() else ", check" if after.is_check() else ""
    return txt + ")"


def engine_comment(game: "Game", spec: PlayerSpec, color: bool, board: chess.Board, mv: chess.Move):
    """Ask an LLM for a chat line about the engine's move, without delaying the game."""
    if not (spec.commentary and spec.comment_model):
        return
    base = ENDPOINTS.get(spec.comment_endpoint or "", spec.comment_endpoint or "")
    if not base:
        return
    side = "white" if color else "black"
    ply = len(board.move_stack)
    facts = []
    if board.move_stack:
        prev = board.copy()
        last = prev.pop()
        facts.append(f"Opponent's last move: {move_words(prev, last)}")
    facts.append(f"Your move: {move_words(board, mv)}")
    mat = {c: sum(PIECE_VALUES[p.piece_type] for p in board.piece_map().values() if p.color == c) for c in (True, False)}
    after = board.copy(stack=False)
    after.push(mv)
    mat_after = {c: sum(PIECE_VALUES[p.piece_type] for p in after.piece_map().values() if p.color == c) for c in (True, False)}
    diff = mat_after[color] - mat_after[not color]
    facts.append("Material is even." if diff == 0 else f"You are {abs(diff)} points of material {'ahead' if diff > 0 else 'behind'}.")
    if after.is_checkmate():
        facts.append("This move wins the game!")
    opp = game.spec_for(not color).label()
    persona = spec.persona.strip() or "a cheerful chess buddy"
    body = {
        "model": spec.comment_model,
        "messages": [
            {"role": "system", "content": COMMENT_PROMPT.format(persona=persona, engine=spec.label(), lang=chat_lang_line(game),
                                                                color="White" if color else "Black", opp=opp)},
            {"role": "user", "content": "\n".join(facts)},
        ],
        "temperature": 0.8, "max_tokens": 600, "chat_template_kwargs": {"enable_thinking": False}, **spec.extra,
    }

    async def run():
        # throttle: only talk once the game pauses (fast book moves get no comment at all), and in a known
        # opening line only when something happens (capture, check, a new opening name)
        await asyncio.sleep(QUIET_BEFORE_LLM)
        if len(game.board.move_stack) > ply + 1 or game.status != "running":
            return  # the players already moved on; a later comment covers the newer position
        task.talking = True  # from here on a newer move doesn't cancel it: the comment is being written
        book = OPENINGS.get(after.epd())  # exactly on a known opening line (not just "somewhere after one")
        notable = board.is_capture(mv) or after.is_check() or after.is_checkmate()
        if book and not notable and game.comment_opening.get(side) == book[1].split(":")[0]:
            return  # still following well-known theory of the same opening: nothing worth saying
        game.comment_opening[side] = book[1].split(":")[0] if book else None
        # expert context from Stockfish (opening, verdict on the opponent's move, threats, what comes next)
        extra = await chat_insight(game, side, board, mv, optional=True)
        if extra is None:
            return
        if extra:
            body["messages"][1]["content"] += "\nChat facts (from Stockfish):\n" + "\n".join(f"- {f}" for f in extra)
        text = await talk(base, body, recent_lines(game, side))
        if text and text not in recent_lines(game, side, 3):  # two comments written at once can come out identical
            game.add_log({"ply": ply, "side": side, "move": board.san(mv), "say": text[:300], "kind": "comment"})

    old = game.comment_tasks.get(side)
    if old and not old.done() and not getattr(old, "talking", False):
        old.cancel()  # a newer move replaces a comment that is still waiting for a pause
    task = asyncio.create_task(run())
    game.comment_tasks[side] = task
    COMMENT_TASKS.add(task)
    task.add_done_callback(COMMENT_TASKS.discard)


class StockfishPlayer:
    def __init__(self, spec: PlayerSpec, color: bool, game: "Game"):
        self.spec, self.color, self.game = spec, color, game
        self.side = "white" if color else "black"
        self.engine = None
        opts = {"Threads": 2, "Hash": 32}
        if spec.elo >= 1320 and not spec.auto:
            opts.update(UCI_LimitStrength=True, UCI_Elo=min(3190, spec.elo))
        self.lazy = LazyEngine(opts)  # hibernates while the game waits for a human

    async def start(self):
        await self.lazy.acquire()  # fail early if Stockfish can't start
        self.lazy.release()

    async def close(self):
        await self.lazy.close()

    async def choose(self, board: chess.Board) -> chess.Move:
        self.engine = await self.lazy.acquire()
        try:
            return await self._choose(board)
        finally:
            self.lazy.release()

    async def _choose(self, board: chess.Board) -> chess.Move:
        t0 = time.time()
        if self.spec.auto:
            mv = await self.weak_move(board, self.game.auto_target(self.color), soften_ok=True)
        elif 0 < self.spec.elo < 1320:
            mv = await self.weak_move(board, max(300, self.spec.elo), soften_ok=False)
        else:
            mv = (await self.engine.play(board, chess.engine.Limit(time=self.spec.movetime))).move
        self.game.stats[self.side]["seconds"] += time.time() - t0
        engine_comment(self.game, self.spec, self.color, board, mv)
        return mv

    async def weak_move(self, board: chess.Board, base: float, soften_ok: bool) -> chess.Move:
        if base >= 3190:
            self.game.note_engine_elo(self.side, 3190)
            return (await self.engine.play(board, chess.engine.Limit(time=self.spec.movetime))).move
        T, depth = weak_params(base)
        multipv = min(8, board.legal_moves.count()) if base < 1320 else 1
        infos = await self.engine.analyse(board, chess.engine.Limit(depth=max(depth, 2)), multipv=multipv)
        cand = [(i["pv"][0], i["score"].pov(self.color).score(mate_score=10000)) for i in infos if i.get("pv")]
        best = max(c for _, c in cand)
        elo = max(200, soften(base, best)) if soften_ok else base
        self.game.note_engine_elo(self.side, elo)
        if elo >= 1320:
            limit = chess.engine.Limit(time=self.spec.movetime)
            return (await self.engine.play(board, limit, options={"UCI_LimitStrength": True, "UCI_Elo": round(elo)})).move
        if multipv == 1:  # softened below the UCI floor on this move: widen the search
            infos = await self.engine.analyse(board, chess.engine.Limit(depth=weak_params(elo)[1]),
                                              multipv=min(8, board.legal_moves.count()))
            cand = [(i["pv"][0], i["score"].pov(self.color).score(mate_score=10000)) for i in infos if i.get("pv")]
            best = max(c for _, c in cand)
        T = weak_params(elo)[0]
        cand = [(m, c) for m, c in cand if best - c <= max(200, 3 * T)]
        return random.choices([m for m, _ in cand], [math.exp((c - best) / T) for _, c in cand])[0]


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
    return {"human": HumanPlayer, "llm": LLMPlayer, "stockfish": StockfishPlayer}[spec.type](spec, color, game)


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
        self.changed = asyncio.Event()
        self.task: Optional[asyncio.Task] = None
        self.code: Optional[str] = None  # 4-digit join code, shared by the games of a match
        self.host: Optional[str] = None  # client id of the device that created it
        self.ratings: dict[str, dict] = {}  # side -> {"profile", "before", "after", "delta"}
        self.engine_elo: dict[str, list[float]] = {"white": [], "black": []}
        self.review: Optional[dict] = None  # learning mode: a blunder waiting for "undo" / "keep"
        self.last_seen = time.time()  # last time any device looked at or played in this game
        self.last_move_at = time.time()
        self.hibernating = False
        self.teach_task: Optional[asyncio.Task] = None  # learning: the running per-turn explanation
        self.teach_busy = False
        self.teach_kid: Optional[tuple] = None  # learner's last move, explained together with the reply
        self.chat_seen: dict[str, int] = {}
        self.reply_tasks: dict[str, asyncio.Task] = {}
        self.comment_tasks: dict[str, asyncio.Task] = {}
        self.comment_opening: dict[str, Optional[str]] = {}
        self.said_opening: dict[str, Optional[str]] = {}  # last opening name each side's chat mentioned  # side -> log length when its LLM last read the players' chat
        self.review_decisions: asyncio.Queue = asyncio.Queue()

    def touch(self):
        self.version += 1
        self.changed.set()
        self.changed = asyncio.Event()

    # ---- ratings / auto strength
    def snapshot_rating(self, side: str):
        spec = self.white if side == "white" else self.black
        if spec.type == "human" and spec.profile in PROFILES:
            self.ratings[side] = {"profile": spec.profile, "before": PROFILES[spec.profile]["rating"]}
        elif spec.type == "llm" and spec.rating_key:
            rec = LLM_RATINGS.setdefault(spec.rating_key, new_llm_rating(spec.rating_key))
            self.ratings[side] = {"llm": spec.rating_key, "before": rec["rating"]}
        else:
            self.ratings.pop(side, None)

    def human_rating(self, side: str) -> Optional[float]:
        spec = self.white if side == "white" else self.black
        if side in self.ratings:
            return self.ratings[side]["before"]
        return START_RATING if spec.type == "human" else None

    def auto_target(self, color: bool) -> float:
        """Base strength for an auto Stockfish on `color`: a bit below a human opponent, else its slider."""
        other = "black" if color else "white"
        opp = self.spec_for(not color)
        if opp.type == "human":
            return max(200, self.human_rating(other) - AUTO_BELOW)
        if opp.type == "llm" and other in self.ratings:
            return max(300, self.ratings[other]["before"])  # rated models get an even game
        return self.spec_for(color).elo or 3190

    def note_engine_elo(self, side: str, elo: float):
        self.engine_elo[side].append(elo)

    def opp_strength(self, side: str) -> Optional[float]:
        """Rating of `side` as an opponent, for the other side's rating update (None = unrated, e.g. LLMs)."""
        spec = self.white if side == "white" else self.black
        if spec.type in ("human", "llm"):
            return self.ratings[side]["before"] if side in self.ratings else None
        if spec.type == "stockfish":
            if spec.auto:
                e = self.engine_elo[side]
                return sum(e) / len(e) if e else None
            return spec.elo or 3200
        return None

    def apply_ratings(self):
        if self.opts.learning:
            return  # take-backs and hints: learning games never change ratings
        touched = set()
        w, b = self.ratings.get("white", {}), self.ratings.get("black", {})
        if w.get("llm") and w.get("llm") == b.get("llm"):
            return  # a model playing itself says nothing about its strength
        for side, other, win in (("white", "black", "1-0"), ("black", "white", "0-1")):
            r = self.ratings.get(side)
            opp = self.opp_strength(other)
            if not r or opp is None:
                continue
            rec = PROFILES.get(r["profile"]) if "profile" in r else LLM_RATINGS.get(r.get("llm"))
            if not rec:
                continue
            score = 1.0 if self.result == win else 0.5 if self.result == "1/2-1/2" else 0.0
            delta = elo_update(rec, r["before"], opp, score)
            opp_label = (self.black if side == "white" else self.white).label()
            rec["history"].append({"t": time.time(), "rating": rec["rating"], "delta": delta, "game": self.id,
                                   "opp": opp_label, "score": score})
            r.update(after=rec["rating"], delta=delta, opp=round(opp))
            touched.add("profile" if "profile" in r else "llm")
        if "profile" in touched:
            save_profiles()
        if "llm" in touched:
            save_llm_ratings()

    def seat_open(self, side: str) -> bool:
        spec = self.white if side == "white" else self.black
        return spec.type == "human" and spec.remote and spec._seat is None

    def can_move(self, cid: Optional[str], color: bool) -> bool:
        spec = self.spec_for(color)
        if spec._seat is None:
            return not spec.remote  # an open remote seat waits for the joining device
        return spec._seat == cid

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
        self.apply_ratings()
        self.touch()
        self.save()
        for side, spec in chat_responders(self):
            coach.background(closing_chat(self, side, spec))

    def save(self):
        (GAMES_DIR / f"{self.id}.json").write_text(json.dumps(self.state(full=True)))

    async def run(self):
        self.status = "running"
        for side in ("white", "black"):
            if side not in self.ratings:  # a resumed game keeps its original "before" ratings
                self.snapshot_rating(side)
        self.touch()
        players = {chess.WHITE: make_player(self.white, chess.WHITE, self),
                   chess.BLACK: make_player(self.black, chess.BLACK, self)}
        try:
            for p in players.values():
                await p.start()
            if self.opts.analysis and not self.evals:
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
                rev = None
                if self.opts.learning and spec.type == "human":
                    rev = await self.check_learner_move(b, mv)
                    if rev == "undo":
                        continue  # the learner moves again
                before = b.copy()
                b.push(mv)
                self.last_move_at = time.time()
                self.thinking_since = None
                self.touch()
                recap.on_move(self)
                if self.opts.learning:
                    self.queue_explanation(before, mv, rev if isinstance(rev, dict) else None)
                if self.opts.analysis:
                    self.evals.append(await ANALYZER.evaluate(b))
                    self.touch()
        except asyncio.CancelledError:
            if self.hibernating:
                raise  # saved as "hibernated" already; it can be resumed
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

    def queue_explanation(self, before: chess.Board, mv: chess.Move, rev: Optional[dict]):
        """Learning mode: explain once per turn (the learner's move + the reply), dropping stale explanations."""
        learner_color, _ = coach.learner(self)
        if before.turn == learner_color:
            already = bool(rev and rev.get("explained"))  # a paused blunder was explained on the card already
            self.teach_kid = None if already else (before, mv, rev)
            if self.board.is_game_over() or self.spec_for(not learner_color).type == "human":
                self.flush_explanation()  # no reply coming (game over / two humans): explain right away
            return
        kid, self.teach_kid = self.teach_kid, None
        coach.explain_latest(self, coach.explain_turn(self, kid, (before, mv)))

    def flush_explanation(self):
        if self.teach_kid:
            kid, self.teach_kid = self.teach_kid, None
            coach.explain_latest(self, coach.explain_turn(self, kid, None))

    async def check_learner_move(self, b: chess.Board, mv: chess.Move):
        """Learning mode: pause on a blunder so the learner can take it back. Returns the review, or "undo"."""
        try:
            rev = await coach.review(b, mv, b.turn)
        except Exception:
            return None
        if rev["cls"] != "blunder" or (rev["cp_before"] or 0) < -600:
            return rev  # not a blunder, or already lost: no point interrupting
        lang = self.opts.lang if self.opts.lang in coach.TEXT else "en"
        self.review = {**rev, "ply": len(b.move_stack), "uci": mv.uci(), "text": None,
                       "side": "white" if b.turn else "black"}
        self.thinking_since = time.time()
        self.touch()
        coach.background(coach.explain_blunder(self, b, rev))
        decision = await self.review_decisions.get()
        text = self.review.get("text") if self.review else None
        self.review = None
        if text:  # decided before the explanation arrived: the kept move still gets its normal explanation
            coach.add_teach(self, text, rev["arrows"], rev["squares"], about=rev["san"], cls="blunder",
                            mover="kid", blunder=True, decision=decision)
        coach.add_teach(self, coach.TEXT[lang]["undone" if decision == "undo" else "kept"], note=True)
        return "undo" if decision == "undo" else {**rev, "explained": bool(text)}

    def state(self, full: bool = True, cid: Optional[str] = None) -> dict:
        b = self.board
        human_turn = self.status == "running" and self.awaiting_human and self.can_move(cid, b.turn)
        s = {
            "id": self.id, "match_id": self.match_id, "index": self.index,
            "white": {"label": self.white.label(), **self.white.model_dump()},
            "black": {"label": self.black.label(), **self.black.model_dump()},
            "status": self.status, "result": self.result, "termination": self.termination,
            "plies": len(b.move_stack), "created": self.created, "version": self.version,
            "stats": self.stats, "ratings": self.ratings, "code": self.code,
            "open_seats": [side for side in ("white", "black") if self.seat_open(side)],
            "remote_sides": [side for side, sp in (("white", self.white), ("black", self.black))
                             if sp.type == "human" and sp.remote],
            "learning": self.opts.learning,
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
            my_sides=[side for side, sp in (("white", self.white), ("black", self.black))
                      if sp.type == "human" and cid and sp._seat == cid],
            is_host=bool(cid and cid == self.host),
            engine_now={side: round(e[-1]) for side, e in self.engine_elo.items() if e},
            lang=self.opts.lang,
            review=dict(self.review) if self.review else None,
            teach_busy=self.teach_busy,
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


def migrate_llm_keys():
    """Old ratings were per model only; attach the endpoint the model was played on (from the archive).
    Stockfish hints were once optional and rated as "… · SF hints"; now they are the only mode, so that
    rating becomes the model's rating (replacing the old no-hints one)."""
    changed = False
    for key in [k for k in LLM_RATINGS if k.endswith(" · SF hints")]:
        base = key.removesuffix(" · SF hints")
        rec = LLM_RATINGS.pop(key)
        rec["key"], rec["name"] = base, new_llm_rating(base)["name"]
        LLM_RATINGS[base] = rec
        for d in ARCHIVE.values():
            for r in (d.get("ratings") or {}).values():
                if r.get("llm") == key:
                    r["llm"] = base
        changed = True
    for key in [k for k in LLM_RATINGS if " · " not in k]:
        eps = {d[s].get("endpoint") for d in ARCHIVE.values() for s in ("white", "black")
               if d[s].get("type") == "llm" and d[s].get("rating_key") == key}
        eps.discard(None)
        if len(eps) != 1:
            continue
        new = full_llm_key(key, eps.pop())
        rec = LLM_RATINGS.pop(key)
        rec["key"], rec["name"] = new, new_llm_rating(new)["name"]
        LLM_RATINGS[new] = rec
        for d in ARCHIVE.values():  # keep archived snapshots pointing at the renamed rating
            for r in (d.get("ratings") or {}).values():
                if r.get("llm") == key:
                    r["llm"] = new
        changed = True
    if changed:
        save_llm_ratings()


migrate_llm_keys()


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
    results = await asyncio.gather(*(probe_endpoint(n, u) for n, u in ENDPOINTS.items()))

    def info(name, mid):
        key = full_llm_key(sibling_model_key(name, mid), name)
        rec = LLM_RATINGS.get(key)
        return {"key": key, "name": rec["name"] if rec else key, "rating": rec["rating"] if rec else LLM_START_RATING,
                "games": rec["games"] if rec else 0}

    return {
        "endpoints": [{"name": n, "url": u, "models": m, "online": m is not None,
                       "model_info": {mid: info(n, mid) for mid in (m or [])}}
                      for (n, u), m in zip(ENDPOINTS.items(), results)],
        "max_plies": MAX_PLIES,
    }


def new_code() -> str:
    used = {g.code for g in GAMES.values() if g.status in ("running", "queued")}
    while True:
        code = f"{random.randint(1000, 9999)}"
        if code not in used:
            return code


def apply_profile(spec: PlayerSpec):
    if spec.type == "human" and spec.profile:
        if spec.profile not in PROFILES:
            raise HTTPException(400, "unknown profile")
        spec.name = PROFILES[spec.profile]["name"]


@app.post("/api/games")
async def create(req: NewGame, x_client_id: Optional[str] = Header(None)):
    for spec in (req.white, req.black):
        apply_profile(spec)
        if spec.type == "llm" and spec.endpoint and spec.model:
            spec.rating_key = (await resolve_model_key(spec.endpoint, spec.model) + (" · pure" if spec.pure else "")
                               + (" · think" if spec.think else ""))
        if spec.type == "human" and not spec.remote:
            spec._seat = x_client_id
        if spec.type == "llm" and (not spec.endpoint or not spec.model):
            raise HTTPException(400, "LLM players need an endpoint and a model")
    if req.start_fen:
        try:
            chess.Board(req.start_fen)
        except ValueError:
            raise HTTPException(400, "invalid FEN")
    if req.learning:
        if "human" not in (req.white.type, req.black.type):
            raise HTTPException(400, "learning mode needs a human player")
        req.teacher_endpoint = req.teacher_endpoint or next(iter(ENDPOINTS), None)
        if not req.teacher_endpoint or req.teacher_endpoint not in ENDPOINTS:
            raise HTTPException(400, "learning mode needs a teacher LLM endpoint")
    n = max(1, min(100, req.games))
    match_id = uuid.uuid4().hex[:8]
    code = new_code() if "human" in (req.white.type, req.black.type) else None
    games = []
    for i in range(n):
        w, b = (req.black, req.white) if (req.swap_colors and i % 2) else (req.white, req.black)
        g = Game(w, b, req, match_id, i + 1)
        g.code, g.host = code, x_client_id
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
            "plies", "created", "stats", "learning")
    return sorted(({k: d.get(k) for k in keys} for d in out.values()),
                  key=lambda d: d["created"], reverse=True)


@app.get("/api/games")
async def list_games():
    return all_summaries()


def engine_settings(p: dict) -> str:
    """Strength settings shown under an engine's name in the standings (they also separate its rows)."""
    if p.get("type") == "stockfish":
        if p.get("auto"):
            return f"auto: {AUTO_BELOW} below a human's rating"
        elo = f"{p['elo']} Elo" if p.get("elo") else "full strength"
        return f"{elo} · {p.get('movetime', 0.5):g}s/move"
    if p.get("type") == "lc0":  # games from before Lc0 was removed
        return f"{p['nodes']} nodes" if p.get("nodes") else f"{p.get('movetime', 1):g}s/move"
    return ""


@app.get("/api/standings")
async def standings():
    table: dict[tuple, dict] = {}
    for d in all_summaries():
        if d["status"] != "finished" or d.get("learning"):
            continue
        for side, other, win in (("white", "black", "1-0"), ("black", "white", "0-1")):
            p = d[side]
            settings = engine_settings(p)
            pid = p.get("profile") if p.get("profile") in PROFILES else None
            lkey = llm_key_of(p)
            lrec = LLM_RATINGS.get(lkey) if lkey else None
            if pid:
                key, label, rating, emoji = ("profile", pid), PROFILES[pid]["name"], PROFILES[pid]["rating"], PROFILES[pid]["emoji"]
            elif lrec:
                key, label, rating, emoji = ("llm", lkey), lrec["name"], lrec["rating"], "🤖"
                settings = settings or (f"as {p['label']}" if p["label"] != lrec["name"] else "")
            else:
                key, label, rating, emoji = (p["label"], settings), p["label"], None, None
            row = table.setdefault(key, {"label": label, "type": p["type"], "settings": settings,
                                         "profile": pid, "rating": rating, "emoji": emoji,
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
    GAMES[gid].last_seen = time.time()
    return GAMES[gid]


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "resume"}  # resume data holds the devices' seat ids


# ---------------------------------------------------------------- hibernation

def hibernate(g: Game, reason: str):
    """Park a live game on disk (it can be resumed later) and stop its task and engines."""
    g.hibernating = True
    d = g.state(full=True)
    d.update(status="hibernated", termination=reason, thinking_for=None, review=None, human_turn=False, legal=[],
             resume={"host": g.host, "seats": {side: sp._seat for side, sp in (("white", g.white), ("black", g.black))}})
    (GAMES_DIR / f"{g.id}.json").write_text(json.dumps(d))
    ARCHIVE[g.id] = d
    GAMES.pop(g.id, None)
    if g.task and not g.task.done():
        g.task.cancel()
    for other in GAMES.values():  # later games of the same match won't start on their own
        if other.match_id == g.match_id and other.status == "queued":
            other.status, other.termination = "aborted", "aborted"
            other.touch()


async def hibernate_idle_games():
    while True:
        await asyncio.sleep(min(60, HIBERNATE_AFTER / 2))
        now = time.time()
        for g in list(GAMES.values()):
            if (g.status == "running" and now - g.last_seen > HIBERNATE_AFTER
                    and "human" in (g.white.type, g.black.type)):
                hibernate(g, f"hibernated: no players for {round(HIBERNATE_AFTER / 60)} min")


@app.on_event("startup")
async def start_hibernator():
    asyncio.create_task(hibernate_idle_games())


@app.on_event("shutdown")
async def hibernate_on_shutdown():
    for g in list(GAMES.values()):
        if g.status == "running":
            hibernate(g, "hibernated: the server restarted")


@app.post("/api/games/{gid}/resume")
async def resume(gid: str, x_client_id: Optional[str] = Header(None)):
    d = ARCHIVE.get(gid)
    if gid in GAMES:
        return {"id": gid}
    if not d or d.get("status") != "hibernated":
        raise HTTPException(409, "that game is not hibernated")
    fields = PlayerSpec.model_fields
    white = PlayerSpec(**{k: v for k, v in d["white"].items() if k in fields})
    black = PlayerSpec(**{k: v for k, v in d["black"].items() if k in fields})
    opts = NewGame(**d["opts"])
    g = Game(white, black, opts, d["match_id"], d["index"])
    g.id, g.created = gid, d["created"]
    for u in d["uci"]:
        g.board.push_uci(u)
    g.log, g.stats, g.evals, g.ratings = d["log"], d["stats"], d.get("evals") or [], d.get("ratings") or {}
    res = d.get("resume") or {}
    g.host = res.get("host") or x_client_id
    for side, sp in (("white", white), ("black", black)):
        sp._seat = (res.get("seats") or {}).get(side)
        if sp.type == "human" and not sp.remote and sp._seat is None:
            sp._seat = x_client_id
    if "human" in (white.type, black.type):
        g.code = new_code()
    ARCHIVE.pop(gid, None)
    GAMES[gid] = g
    g.task = asyncio.create_task(run_match([g]))
    return {"id": gid}


@app.get("/api/games/{gid}")
async def game_state(gid: str, since: int = -1, x_client_id: Optional[str] = Header(None)):
    if gid in GAMES:
        g = GAMES[gid]
        g.last_seen = time.time()
        if since == g.version:
            # long poll: answer as soon as something changes, so the other device sees moves instantly
            try:
                await asyncio.wait_for(g.changed.wait(), 25)
            except asyncio.TimeoutError:
                return {"unchanged": True, "version": g.version}
        return g.state(cid=x_client_id)
    if gid in ARCHIVE:
        return public(ARCHIVE[gid])
    raise HTTPException(404, "game not found")


@app.get("/api/games/{gid}/pgn", response_class=PlainTextResponse)
async def game_pgn(gid: str):
    if gid in GAMES:
        return GAMES[gid].pgn()
    if gid in ARCHIVE:
        return ARCHIVE[gid]["pgn"]
    raise HTTPException(404)


@app.post("/api/games/{gid}/move")
async def human_move(gid: str, m: HumanMove, x_client_id: Optional[str] = Header(None)):
    g = get_game(gid)
    if not (g.status == "running" and g.awaiting_human and g.can_move(x_client_id, g.board.turn)):
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
async def resign(gid: str, x_client_id: Optional[str] = Header(None)):
    g = get_game(gid)
    if g.status == "running" and g.awaiting_human and g.can_move(x_client_id, g.board.turn):
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
    for lang in ("en", "da"):
        recap.recap_path(gid, lang).unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------- network play

def live_match_game(code: str) -> Optional[Game]:
    live = [g for g in GAMES.values() if g.code == code and g.status in ("running", "queued")]
    return min(live, key=lambda g: g.index) if live else None


@app.get("/api/open")
async def open_games(x_client_id: Optional[str] = Header(None)):
    """Live games with a human seat waiting for another device (the Join screen lists these)."""
    out = []
    for g in GAMES.values():
        if g.status not in ("running", "queued") or not g.code:
            continue
        if live_match_game(g.code) is not g:
            continue
        for side in ("white", "black"):
            if g.seat_open(side):
                other = g.black if side == "white" else g.white
                out.append({"id": g.id, "code": g.code, "side": side, "opponent": other.label(),
                            "opponent_type": other.type, "created": g.created, "mine": g.host == x_client_id})
    return sorted(out, key=lambda x: -x["created"])


@app.post("/api/join")
async def join(req: JoinReq, x_client_id: Optional[str] = Header(None)):
    if not x_client_id:
        raise HTTPException(400, "missing client id")
    g = live_match_game(req.code.strip())
    if not g:
        raise HTTPException(404, "no game with that code")
    specs = {"white": g.white, "black": g.black}
    side = next((s for s, sp in specs.items() if sp.type == "human" and sp._seat == x_client_id and sp.remote), None)
    side = side or next((s for s in specs if g.seat_open(s)), None)
    if not side:
        raise HTTPException(409, "that game has no free seat")
    spec = specs[side]
    spec._seat = x_client_id
    if req.profile:
        if req.profile not in PROFILES:
            raise HTTPException(400, "unknown profile")
        spec.profile, spec.name = req.profile, PROFILES[req.profile]["name"]
    elif req.name:
        spec.profile, spec.name = None, req.name.strip()[:40] or None
    # the seat's spec is shared by every game of the match; refresh ratings of the ones already running
    for other in GAMES.values():
        if other.match_id == g.match_id and other.status == "running":
            other.snapshot_rating("white" if other.white is spec else "black")
            other.touch()
    g.touch()
    return {"id": g.id, "side": side}


@app.post("/api/games/{gid}/free/{side}")
async def free_seat(gid: str, side: Literal["white", "black"], x_client_id: Optional[str] = Header(None)):
    """Host only: open a remote seat again (e.g. the kid switched phones)."""
    g = get_game(gid)
    if not x_client_id or x_client_id != g.host:
        raise HTTPException(403, "only the device that started the game can do that")
    spec = g.white if side == "white" else g.black
    if spec.type != "human":
        raise HTTPException(400, "not a human seat")
    spec.remote, spec._seat = True, None
    g.touch()
    return {"ok": True}


@app.post("/api/games/{gid}/say")
async def human_say(gid: str, req: SayReq, x_client_id: Optional[str] = Header(None)):
    g = get_game(gid)
    sides = [s for s, sp in (("white", g.white), ("black", g.black)) if sp.type == "human" and x_client_id and sp._seat == x_client_id]
    text = req.text.strip()[:140]
    if not sides or not text:
        raise HTTPException(403, "only players can chat")
    side = sides[0] if len(sides) == 1 else ("white" if g.board.turn else "black")
    g.add_log({"ply": len(g.board.move_stack), "side": side, "kind": "chat", "say": text, "t": time.time()})
    schedule_chat_replies(g)
    return {"ok": True}


# ---------------------------------------------------------------- learning mode api

class ReviewReq(BaseModel):
    decision: Literal["undo", "keep"]


class AskReq(BaseModel):
    text: str


class LangReq(BaseModel):
    lang: Literal["en", "da"]


def learning_game(gid: str, cid: Optional[str]) -> Game:
    g = get_game(gid)
    if not g.opts.learning:
        raise HTTPException(400, "not a learning game")
    color, _ = coach.learner(g)
    spec = g.spec_for(color)
    if spec._seat is not None and spec._seat != cid:
        raise HTTPException(403, "only the learner's device can do that")
    return g


@app.post("/api/games/{gid}/review")
async def review_decision(gid: str, req: ReviewReq, x_client_id: Optional[str] = Header(None)):
    g = learning_game(gid, x_client_id)
    if not g.review:
        raise HTTPException(409, "nothing to review")
    await g.review_decisions.put(req.decision)
    return {"ok": True}


@app.post("/api/games/{gid}/hint")
async def learning_hint(gid: str, x_client_id: Optional[str] = Header(None)):
    g = learning_game(gid, x_client_id)
    coach.background(coach.hint(g))
    return {"ok": True}


@app.post("/api/games/{gid}/ask")
async def learning_ask(gid: str, req: AskReq, x_client_id: Optional[str] = Header(None)):
    g = learning_game(gid, x_client_id)
    text = req.text.strip()[:300]
    if not text:
        raise HTTPException(400, "empty question")
    coach.background(coach.answer(g, text))
    return {"ok": True}


@app.post("/api/games/{gid}/lang")
async def game_lang(gid: str, req: LangReq, x_client_id: Optional[str] = Header(None)):
    """Switch the game's language (LLM chat lines, engine comments, chat replies, teacher) mid-game."""
    g = get_game(gid)
    players = [sp._seat for sp in (g.white, g.black) if sp.type == "human"]
    if x_client_id != g.host and x_client_id not in players:
        raise HTTPException(403, "only the players can change the language")
    g.opts.lang = req.lang
    g.touch()
    return {"ok": True}


# ---------------------------------------------------------------- recap api

class RecapReq(BaseModel):
    lang: Literal["en", "da"] = "en"


@app.post("/api/games/{gid}/recap")
async def recap_start(gid: str, req: RecapReq):
    d = recap.game_data(gid)
    if not d:
        raise HTTPException(404, "game not found")
    if d["status"] in ("running", "queued"):
        raise HTTPException(409, "the recap is available when the game is over")
    if not d.get("uci"):
        raise HTTPException(400, "no moves to review")
    return recap.start(gid, req.lang)


@app.get("/api/games/{gid}/recap")
async def recap_get(gid: str, lang: Literal["en", "da"] = "en"):
    data = recap.load(gid, lang)
    if not data:
        raise HTTPException(404, "no recap yet")
    return data


# ---------------------------------------------------------------- llm ratings api

@app.get("/api/llm-ratings")
async def list_llm_ratings():
    return sorted(LLM_RATINGS.values(), key=lambda r: -r["rating"])


class LlmRatingIn(BaseModel):
    key: str
    rating: Optional[int] = None
    name: Optional[str] = None
    delete: bool = False


@app.post("/api/llm-ratings/update")
async def update_llm_rating(p: LlmRatingIn):
    if p.key not in LLM_RATINGS:
        raise HTTPException(404)
    if p.delete:
        LLM_RATINGS.pop(p.key)
    else:
        rec = LLM_RATINGS[p.key]
        if p.name and p.name.strip():
            rec["name"] = p.name.strip()[:60]
        if p.rating is not None and p.rating != rec["rating"]:
            rec["rating"] = max(100, min(3000, p.rating))
            rec["history"].append({"t": time.time(), "rating": rec["rating"], "manual": True})
    save_llm_ratings()
    return {"ok": True}


@app.on_event("startup")
async def backfill_llm_ratings():
    """First start with LLM ratings: replay the archived games so models start from their past results."""
    if not LLM_BACKFILL:
        return
    await asyncio.gather(*(probe_endpoint(n, u) for n, u in ENDPOINTS.items()))

    def strength(p: dict, side: str, d: dict) -> Optional[float]:
        if p["type"] == "stockfish" and not p.get("auto"):
            return p.get("elo") or 3200
        if p["type"] == "lc0":
            return 3000
        if p["type"] == "llm":
            k = llm_key_of(p)
            return LLM_RATINGS[k]["rating"] if k in LLM_RATINGS else LLM_START_RATING
        r = (d.get("ratings") or {}).get(side)
        return r["before"] if r and "before" in r else None

    for d in sorted(ARCHIVE.values(), key=lambda d: d["created"]):
        if d.get("status") != "finished":
            continue
        snap = {side: strength(d[side], side, d) for side in ("white", "black")}
        if llm_key_of(d["white"]) and llm_key_of(d["white"]) == llm_key_of(d["black"]):
            continue  # self-play
        for side, other, win in (("white", "black", "1-0"), ("black", "white", "0-1")):
            key = llm_key_of(d[side])
            if not key or snap[other] is None:
                continue
            rec = LLM_RATINGS.setdefault(key, new_llm_rating(key))
            score = 1.0 if d["result"] == win else 0.5 if d["result"] == "1/2-1/2" else 0.0
            delta = elo_update(rec, snap[side], snap[other], score)
            rec["history"].append({"t": d["created"], "rating": rec["rating"], "delta": delta, "game": d["id"],
                                   "opp": d[other]["label"], "score": score, "backfill": True})
    save_llm_ratings()


# ---------------------------------------------------------------- profiles api

@app.get("/api/profiles")
async def list_profiles():
    return sorted(PROFILES.values(), key=lambda p: p["created"])


@app.post("/api/profiles")
async def create_profile(p: ProfileIn):
    if not (p.name or "").strip():
        raise HTTPException(400, "name required")
    pid = uuid.uuid4().hex[:8]
    PROFILES[pid] = new_profile(pid, p.name.strip()[:40], (p.emoji or "🙂").strip()[:8],
                                p.rating if p.rating else START_RATING)
    save_profiles()
    return PROFILES[pid]


@app.patch("/api/profiles/{pid}")
async def update_profile(pid: str, p: ProfileIn):
    if pid not in PROFILES:
        raise HTTPException(404)
    prof = PROFILES[pid]
    if p.name and p.name.strip():
        prof["name"] = p.name.strip()[:40]
    if p.emoji and p.emoji.strip():
        prof["emoji"] = p.emoji.strip()[:8]
    if p.rating is not None and p.rating != prof["rating"]:
        prof["rating"] = max(100, min(3000, p.rating))
        prof["history"].append({"t": time.time(), "rating": prof["rating"], "manual": True})
    save_profiles()
    return prof


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    PROFILES.pop(pid, None)
    save_profiles()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
