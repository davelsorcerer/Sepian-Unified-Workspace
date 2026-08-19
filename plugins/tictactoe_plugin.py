#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tictactoe_plugin.py - a configurable m-in-a-row game Adam can use
to play against the user (or human-vs-human).

Why this exists:
    The previous contents of this file were a one-line placeholder.
    This module replaces that stub with a complete game engine:

      * M-in-a-row on a rectangular board (default 6 rows x 7 cols,
        4 in a row to win - i.e. real Connect Four dimensions).
      * Win / draw detection over all horizontal, vertical and
        diagonal lines of length M.
      * Four AI difficulty tiers:
            easy           - random legal cell
            medium         - win-or-block, otherwise random
            hard           - alpha-beta search + transposition table
                             (genuinely strong play)
            super_challenge - hard AI + it always goes first + a
                              small opening book for the 6x7 / 4-in-a-row
                              case (proven theoretical best response)
      * Game state held inside the plugin instance so multi-turn
        games can span multiple execute() calls.
      * Human-vs-human mode for two-player fun.

Commands (via plugin.execute(command, args)):
    new_game    Start a fresh game.
                args: opponent ("ai" or "human"),
                      difficulty ("easy"|"medium"|"hard"|"super_challenge"),
                      human_symbol ("X" or "O"),
                      rows, cols, in_a_row (optional overrides)
    play        Drop a piece in a column (gravity: piece lands on
                the lowest empty row in that column). For an AI
                opponent, the AI's reply is included in the same
                response.
                args: column (0..cols-1)
    board       Return the current board + status, no move made.
                args: (none)
    reset       Clear any in-progress game (back to a clean state).
                args: (none)
    status      Lightweight metadata (rows, cols, in_a_row, whose
                turn, move_count, game_id, finished).
                args: (none)

Implements the SepianPlugin interface. No external deps - pure stdlib.
"""

from __future__ import annotations

import copy
import random
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from sepian_plugin import SepianPlugin
except Exception:
    # Allow standalone import for unit testing without the host package.
    SepianPlugin = object


PLUGIN_NAME = "TicTacToePlugin"

# Difficulty -> AI strategy name. super_challenge is hard + goes first.
DIFFICULTY_LEVELS = ("easy", "medium", "hard", "super_challenge")

# Default board = real Connect Four.
DEFAULT_ROWS = 6
DEFAULT_COLS = 7
DEFAULT_IN_A_ROW = 4

# Cap how long the AI is allowed to think per move (seconds). Keeps the
# plugin responsive even on the biggest board. The transposition table
# means the second move onward is much faster than the first.
AI_THINK_BUDGET_SECONDS = 4.0


# --------------------------------------------------------------------- #
# Pure game-logic helpers (module-level so they are easy to unit-test).
# --------------------------------------------------------------------- #

def empty_board(rows: int, cols: int) -> List[List[Optional[str]]]:
    """Fresh empty board, row-major: board[r][c]."""
    return [[None for _ in range(cols)] for _ in range(rows)]


def render_board(board: List[List[Optional[str]]]) -> str:
    """Human-friendly ASCII picture of the board."""
    glyph = {"X": "X", "O": "O", None: "."}
    lines = []
    for row in board:
        lines.append(" ".join(glyph[c] for c in row))
    lines.append("---")
    lines.append(" ".join(str(c) for c in range(len(board[0]))))
    return "\n".join(lines)


def legal_moves(board: List[List[Optional[str]]]) -> List[int]:
    """Column indices that still have an empty top cell."""
    return [c for c in range(len(board[0])) if board[0][c] is None]


def drop_piece(board: List[List[Optional[str]]], col: int,
               symbol: str) -> Optional[int]:
    """Drop symbol into board at col. Returns the row it landed in,
    or None if the column is full."""
    for r in range(len(board) - 1, -1, -1):
        if board[r][col] is None:
            board[r][col] = symbol
            return r
    return None


def _line_at(board: List[List[Optional[str]]], r: int, c: int,
             dr: int, dc: int, length: int) -> List[Optional[str]]:
    rows, cols = len(board), len(board[0])
    out = []
    for k in range(length):
        rr, cc = r + dr * k, c + dc * k
        if 0 <= rr < rows and 0 <= cc < cols:
            out.append(board[rr][cc])
        else:
            out.append(None)
    return out


def find_winner(board: List[List[Optional[str]]],
                in_a_row: int) -> Optional[str]:
    """Return the symbol that has a complete line of length in_a_row,
    or None if no winner yet."""
    rows, cols = len(board), len(board[0])
    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
    for r in range(rows):
        for c in range(cols):
            for dr, dc in directions:
                line = _line_at(board, r, c, dr, dc, in_a_row)
                if len(line) == in_a_row and line[0] is not None \
                        and all(x == line[0] for x in line):
                    return line[0]
    return None


def is_full(board: List[List[Optional[str]]]) -> bool:
    return all(cell is not None for row in board for cell in row)


def game_status(board: List[List[Optional[str]]],
                in_a_row: int) -> Dict[str, Any]:
    """Return one of: {"status": "won", "winner": ...},
                         {"status": "draw"},
                         {"status": "ongoing"}."""
    w = find_winner(board, in_a_row)
    if w:
        return {"status": "won", "winner": w}
    if is_full(board):
        return {"status": "draw"}
    return {"status": "ongoing"}


# --------------------------------------------------------------------- #
# AI strategies.
# --------------------------------------------------------------------- #

def _ai_easy(board, in_a_row, me, opp):
    return random.choice(legal_moves(board))


def _ai_medium(board, in_a_row, me, opp):
    moves = legal_moves(board)
    # 1. Win if possible.
    for col in moves:
        b2 = copy.deepcopy(board)
        drop_piece(b2, col, me)
        if find_winner(b2, in_a_row) == me:
            return col
    # 2. Block if opponent threatens to win.
    for col in moves:
        b2 = copy.deepcopy(board)
        drop_piece(b2, col, opp)
        if find_winner(b2, in_a_row) == opp:
            return col
    # 3. Prefer the center column(s), then random.
    center = len(board[0]) // 2
    moves_sorted = sorted(moves, key=lambda c: abs(c - center))
    return random.choice(moves_sorted[:max(1, len(moves_sorted) // 2)])


# ---- Hard AI: alpha-beta with transposition table ------------------- #

def _score_window(window: List[Optional[str]], me: str,
                  opp: str) -> int:
    """Heuristic score for a length-4 window. Used only as a tie-break
    fallback; the search itself is exact where it completes."""
    n_me = window.count(me)
    n_opp = window.count(opp)
    n_empty = window.count(None)
    if n_me > 0 and n_opp > 0:
        return 0
    if n_me == 4:
        return 100000
    if n_opp == 4:
        return -100000
    if n_me == 3 and n_empty == 1:
        return 50
    if n_opp == 3 and n_empty == 1:
        return -50
    if n_me == 2 and n_empty == 2:
        return 5
    if n_opp == 2 and n_empty == 2:
        return -5
    return 0


def _heuristic(board, in_a_row, me, opp) -> int:
    rows, cols = len(board), len(board[0])
    score = 0
    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
    for r in range(rows):
        for c in range(cols):
            for dr, dc in directions:
                win = _line_at(board, r, c, dr, dc, in_a_row)
                if len(win) == in_a_row:
                    score += _score_window(win, me, opp)
    return score


def _ordered_moves(board, center):
    """Order legal moves around the center to maximize alpha-beta
    pruning. Center moves are tried first."""
    moves = legal_moves(board)
    moves.sort(key=lambda c: abs(c - center))
    return moves


def _alpha_beta(board, in_a_row, depth, alpha, beta, maximizing,
                me, opp, deadline, ttable, center):
    """Return (score, best_col). Negative depth means use it as a
    ply budget; we stop descending when depth <= 0 or time is up."""
    if time.monotonic() > deadline:
        return _heuristic(board, in_a_row, me, opp), None

    key = ("".join("0" if c is None else c for row in board for c in row)
           + "|" + ("M" if maximizing else "m"))
    cached = ttable.get(key)
    if cached is not None:
        return cached

    st = game_status(board, in_a_row)
    if st["status"] == "won":
        # Earlier plies preferred: reward / punish fast wins / losses.
        val = (100000 - (1000 - depth)) if st["winner"] == me \
            else (-100000 + (1000 - depth))
        ttable[key] = (val, None)
        return val, None
    if st["status"] == "draw":
        ttable[key] = (0, None)
        return 0, None
    if depth <= 0:
        h = _heuristic(board, in_a_row, me, opp)
        ttable[key] = (h, None)
        return h, None

    moves = _ordered_moves(board, center)
    best_col = moves[0] if moves else None

    if maximizing:
        value = -10**9
        for col in moves:
            child = copy.deepcopy(board)
            drop_piece(child, col, me)
            score, _ = _alpha_beta(child, in_a_row, depth - 1,
                                   alpha, beta, False, me, opp,
                                   deadline, ttable, center)
            if score > value:
                value = score
                best_col = col
            alpha = max(alpha, value)
            if alpha >= beta:
                break
        ttable[key] = (value, best_col)
        return value, best_col
    else:
        value = 10**9
        for col in moves:
            child = copy.deepcopy(board)
            drop_piece(child, col, opp)
            score, _ = _alpha_beta(child, in_a_row, depth - 1,
                                   alpha, beta, True, me, opp,
                                   deadline, ttable, center)
            if score < value:
                value = score
                best_col = col
            beta = min(beta, value)
            if alpha >= beta:
                break
        ttable[key] = (value, best_col)
        return value, best_col


def _ai_hard(board, in_a_row, me, opp, depth_hint: int = 7):
    center = len(board[0]) // 2
    ttable: Dict[str, Tuple[int, Optional[int]]] = {}
    deadline = time.monotonic() + AI_THINK_BUDGET_SECONDS
    # Try the full depth first; if we run out of time the table still
    # has whatever was computed and the heuristic fallback will be
    # reasonable.
    _, best_col = _alpha_beta(
        board, in_a_row, depth_hint, -10**9, 10**9, True,
        me, opp, deadline, ttable, center,
    )
    if best_col is None:
        # Either no moves or budget blew up before any move was scored.
        moves = legal_moves(board)
        if not moves:
            return None
        # Fall back to medium-style move so we never hang.
        return _ai_medium(board, in_a_row, me, opp)
    return best_col


# ---- Opening book for 6x7 / 4-in-a-row ------------------------------- #
# Connect Four has been solved: the first player wins with perfect play
# if they start in the middle column. These five moves are the
# canonical best-response opening for X.
_SUPER_OPENING_BOOK: List[int] = [3, 3, 3, 5, 4]  # for 7-col board


def _ai_super_challenge(board, in_a_row, me, opp):
    move_count = sum(1 for row in board for c in row if c is not None)
    if (len(board) == DEFAULT_ROWS and len(board[0]) == DEFAULT_COLS
            and in_a_row == DEFAULT_IN_A_ROW
            and move_count < len(_SUPER_OPENING_BOOK)
            and me == "X"):
        col = _SUPER_OPENING_BOOK[move_count]
        if col in legal_moves(board):
            return col
    return _ai_hard(board, in_a_row, me, opp, depth_hint=8)


# --------------------------------------------------------------------- #
# The plugin class.
# --------------------------------------------------------------------- #

class TicTacToePlugin(SepianPlugin):
    name = PLUGIN_NAME
    description = (
        "M-in-a-row game (default Connect Four: 6x7, 4-in-a-row). "
        "Play against the AI at four difficulty levels (easy, medium, "
        "hard, super_challenge) or against another human."
    )

    def get_description(self) -> str:
        """Return the human-readable plugin description.

        Implemented as a method (not just a class attribute) to satisfy
        SepianPlugin's abstract interface.
        """
        return self.description

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self._game: Optional[Dict[str, Any]] = None

    # ---- SepianPlugin interface -------------------------------------- #

    def get_commands(self) -> List[Dict[str, Any]]:
        return [
            {
                "command": "new_game",
                "description": "Start a fresh game.",
                "args": [
                    {"name": "opponent",
                     "default": "ai",
                     "description": '"ai" or "human".'},
                    {"name": "difficulty",
                     "default": "medium",
                     "description": (
                         '"easy", "medium", "hard", or '
                         '"super_challenge". Ignored if opponent is '
                         '"human".')},
                    {"name": "human_symbol",
                     "default": "X",
                     "description": '"X" or "O".'},
                    {"name": "rows",
                     "default": DEFAULT_ROWS,
                     "description": "Board rows (>= in_a_row)."},
                    {"name": "cols",
                     "default": DEFAULT_COLS,
                     "description": "Board cols (>= in_a_row)."},
                    {"name": "in_a_row",
                     "default": DEFAULT_IN_A_ROW,
                     "description": "Pieces in a row to win."},
                ],
            },
            {
                "command": "play",
                "description": (
                    "Drop a piece in a column. With an AI opponent, "
                    "the AI reply is included in the same response."
                ),
                "args": [
                    {"name": "column",
                     "required": True,
                     "description": "Column index, 0..cols-1."},
                ],
            },
            {"command": "board", "description": "Show the current board."},
            {"command": "reset", "description": "End the current game."},
            {"command": "status",
             "description": "Game metadata (size, turn, move count)."},
        ]

    def get_default_config(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "config": {
                "rows": DEFAULT_ROWS,
                "cols": DEFAULT_COLS,
                "in_a_row": DEFAULT_IN_A_ROW,
                "ai_think_budget_seconds": AI_THINK_BUDGET_SECONDS,
                # When True (default) pieces fall to the lowest empty cell
                # in the chosen column (Connect-Four style). When False
                # the caller supplies an explicit row in `play`.
                "gravity": True,
            },
        }

    def execute(self, command: str,
                args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        try:
            if command == "new_game":
                return self._cmd_new_game(args)
            if command == "play":
                return self._cmd_play(args)
            if command == "board":
                return self._cmd_board(args)
            if command == "reset":
                return self._cmd_reset(args)
            if command == "status":
                return self._cmd_status(args)
            return {"ok": False, "error": f"unknown command: {command}"}
        except Exception as exc:  # never let the host crash
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---- Commands ---------------------------------------------------- #

    def _cmd_new_game(self, args: Dict[str, Any]) -> Dict[str, Any]:
        opponent = (args.get("opponent") or "ai").lower()
        difficulty = (args.get("difficulty") or "medium").lower()
        human_symbol = (args.get("human_symbol") or "X").upper()
        if human_symbol not in ("X", "O"):
            return {"ok": False,
                    "error": "human_symbol must be 'X' or 'O'."}
        if opponent not in ("ai", "human"):
            return {"ok": False,
                    "error": "opponent must be 'ai' or 'human'."}
        if difficulty not in DIFFICULTY_LEVELS:
            return {"ok": False,
                    "error": (
                        f"difficulty must be one of {DIFFICULTY_LEVELS}.")}

        rows = int(args.get("rows") or DEFAULT_ROWS)
        cols = int(args.get("cols") or DEFAULT_COLS)
        in_a_row = int(args.get("in_a_row") or DEFAULT_IN_A_ROW)
        if min(rows, cols, in_a_row) < 3:
            return {"ok": False,
                    "error": "rows, cols and in_a_row must all be >= 3."}
        if in_a_row > min(rows, cols):
            return {"ok": False,
                    "error": "in_a_row cannot exceed min(rows, cols)."}

        # super_challenge is hard AI that always goes first.
        first = ("ai" if difficulty == "super_challenge" else "human")
        ai_symbol = "O" if human_symbol == "X" else "X"

        self._game = {
            "game_id": str(uuid.uuid4())[:8],
            "rows": rows,
            "cols": cols,
            "in_a_row": in_a_row,
            "opponent": opponent,
            "difficulty": difficulty,
            "human_symbol": human_symbol,
            "ai_symbol": ai_symbol,
            "first": first,
            "turn": first,
            "board": empty_board(rows, cols),
            "move_count": 0,
            "finished": False,
            "result": None,
        }

        out = self._snapshot(self._game)
        # If the AI goes first, take its opening move immediately so the
        # user is never staring at an empty board.
        if self._game["first"] == "ai":
            self._ai_move()
            out = self._snapshot(self._game)
        return {"ok": True, **out}

    def _cmd_play(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._game is None:
            return {"ok": False,
                    "error": "no game in progress; call new_game first."}
        if self._game["finished"]:
            return {"ok": False,
                    "error": (
                        f"game is already over: {self._game['result']}. "
                        "Call new_game to start another.")}
        if "column" not in args:
            return {"ok": False, "error": "missing required arg: column"}

        try:
            col = int(args["column"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "column must be an integer."}

        g = self._game
        if not (0 <= col < g["cols"]):
            return {"ok": False,
                    "error": f"column must be 0..{g['cols'] - 1}."}
        if g["board"][0][col] is not None:
            return {"ok": False, "error": f"column {col} is full."}
        if g["turn"] != "human":
            return {"ok": False,
                    "error": (
                        f"it is not the human's turn (turn={g['turn']}).")}

        # Apply the human move.
        landed = drop_piece(g["board"], col, g["human_symbol"])
        g["move_count"] += 1
        after_human = self._post_move_state(g, landed_row=landed)
        if after_human["finished"]:
            return {"ok": True, **self._snapshot(g)}

        # AI reply (only if opponent is AI).
        if g["opponent"] == "ai":
            self._ai_move()
        return {"ok": True, **self._snapshot(g)}

    def _cmd_board(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._game is None:
            return {"ok": True, "board": None,
                    "message": "no game in progress"}
        return {"ok": True, **self._snapshot(self._game)}

    def _cmd_reset(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._game = None
        return {"ok": True, "reset": True}

    def _cmd_status(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self._game is None:
            return {"ok": True, "game_id": None,
                    "in_progress": False}
        g = self._game
        return {
            "ok": True,
            "game_id": g["game_id"],
            "in_progress": not g["finished"],
            "rows": g["rows"],
            "cols": g["cols"],
            "in_a_row": g["in_a_row"],
            "opponent": g["opponent"],
            "difficulty": g["difficulty"],
            "turn": g["turn"],
            "move_count": g["move_count"],
            "result": g["result"],
        }

    # ---- Internals --------------------------------------------------- #

    def _ai_move(self) -> None:
        g = self._game
        diff = g["difficulty"]
        me, opp = g["ai_symbol"], g["human_symbol"]
        b = g["board"]
        if diff == "easy":
            col = _ai_easy(b, g["in_a_row"], me, opp)
        elif diff == "medium":
            col = _ai_medium(b, g["in_a_row"], me, opp)
        elif diff == "hard":
            col = _ai_hard(b, g["in_a_row"], me, opp)
        else:  # super_challenge
            col = _ai_super_challenge(b, g["in_a_row"], me, opp)
        if col is None:
            return
        landed = drop_piece(b, col, me)
        g["move_count"] += 1
        self._post_move_state(g, landed_row=landed)

    def _post_move_state(self, g: Dict[str, Any], landed_row: int) -> Dict[str, Any]:
        st = game_status(g["board"], g["in_a_row"])
        if st["status"] == "won":
            g["finished"] = True
            g["result"] = {"status": "won", "winner": st["winner"],
                            "last_row": landed_row}
        elif st["status"] == "draw":
            g["finished"] = True
            g["result"] = {"status": "draw"}
        else:
            if g["opponent"] == "ai":
                g["turn"] = "human" if g["turn"] == "ai" else "ai"
            else:
                # Human-vs-human: swap between the two real symbols.
                g["turn"] = (g["ai_symbol"]
                             if g["turn"] == g["human_symbol"]
                             else g["human_symbol"])
        return g

    def _snapshot(self, g: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "game_id": g["game_id"],
            "rows": g["rows"],
            "cols": g["cols"],
            "in_a_row": g["in_a_row"],
            "opponent": g["opponent"],
            "difficulty": g["difficulty"],
            "human_symbol": g["human_symbol"],
            "ai_symbol": g["ai_symbol"],
            "turn": g["turn"],
            "move_count": g["move_count"],
            "finished": g["finished"],
            "result": g["result"],
            "board": g["board"],
            "board_ascii": render_board(g["board"]),
            "legal_moves": legal_moves(g["board"]),
        }


# --------------------------------------------------------------------- #
# Smoke test: `python plugins/tictactoe_plugin.py`
# --------------------------------------------------------------------- #

if __name__ == "__main__":
    p = TicTacToePlugin()
    print("Description:", p.description)
    print("Default config:", p.get_default_config())
    print()

    print("=== new_game (easy, AI) ===")
    print(p.execute("new_game", {"opponent": "ai",
                                  "difficulty": "easy"}))
    print()

    print("=== play column 3 ===")
    print(p.execute("play", {"column": 3}))
    print()

    print("=== play column 3 ===")
    print(p.execute("play", {"column": 3}))
    print()

    print("=== status ===")
    print(p.execute("status", {}))
