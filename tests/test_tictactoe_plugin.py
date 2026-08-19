#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_tictactoe_plugin.py - unit tests for plugins/tictactoe_plugin.py

Run with:
    python -m unittest tests/test_tictactoe_plugin.py -v
or from the workspace root:
    python tests/test_tictactoe_plugin.py

These tests cover:
  * Pure-function core: drop_piece, legal_moves, game_status, render_board,
    all four AI strategies, opening book.
  * Plugin lifecycle: new_game / play / board / status / reset
  * vs-human and vs-ai opponents
  * Win / draw detection across horizontal, vertical, diagonal lines
  * Edge cases: invalid columns, full columns, finishing a game.
"""

from __future__ import annotations

import copy
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

# Make the plugins/ folder importable whether we're run from the workspace
# root, from the tests/ dir, or via unittest discovery.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "plugins"))

import tictactoe_plugin as ttt
from tictactoe_plugin import (
    TicTacToePlugin,
    PLUGIN_NAME,
    DIFFICULTY_LEVELS,
    DEFAULT_ROWS,
    DEFAULT_COLS,
    DEFAULT_IN_A_ROW,
    drop_piece,
    legal_moves,
    game_status,
    render_board,
    _ai_easy,
    _ai_medium,
    _ai_hard,
    _ai_super_challenge,
)

# The plugin represents empty cells with None (no exported EMPTY constant).
EMPTY = None


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def make_empty_board(rows: int = DEFAULT_ROWS,
                     cols: int = DEFAULT_COLS) -> List[List[str]]:
    return [[EMPTY for _ in range(cols)] for _ in range(rows)]


def force_board(rows: int, cols: int, layout: List[str]) -> List[List[str]]:
    """Build a board from a list of strings, each length `cols`, '.' = empty."""
    b = []
    for row in layout:
        assert len(row) == cols, f"row {row!r} is not {cols} long"
        b.append([c if c != "." else EMPTY for c in row])
    assert len(b) == rows
    return b


# --------------------------------------------------------------------- #
# Pure-function tests
# --------------------------------------------------------------------- #

class TestDropPiece(unittest.TestCase):
    def test_drop_into_empty_column_lands_on_bottom(self):
        b = make_empty_board(6, 7)
        landed = drop_piece(b, 3, "X")
        self.assertEqual(landed, 5)  # bottom row on a 6-row board is index 5
        self.assertEqual(b[5][3], "X")
        # all other cells still empty
        self.assertEqual(sum(1 for r in b for c in r if c == "X"), 1)

    def test_drop_stacks(self):
        b = make_empty_board(6, 7)
        drop_piece(b, 0, "X")
        drop_piece(b, 0, "O")
        drop_piece(b, 0, "X")
        self.assertEqual([b[5][0], b[4][0], b[3][0]], ["X", "O", "X"])

    def test_drop_in_full_column_raises(self):
        b = make_empty_board(3, 2)
        for i in range(3):
            drop_piece(b, 0, "X" if i % 2 == 0 else "O")
        with self.assertRaises(ValueError):
            drop_piece(b, 0, "X")

    def test_drop_out_of_range_column_raises(self):
        b = make_empty_board(6, 7)
        with self.assertRaises(IndexError):
            drop_piece(b, -1, "X")
        with self.assertRaises(IndexError):
            drop_piece(b, 7, "X")


class TestLegalMoves(unittest.TestCase):
    def test_empty_board_all_cols_legal(self):
        b = make_empty_board(6, 7)
        self.assertEqual(sorted(legal_moves(b)), list(range(7)))

    def test_full_column_excluded(self):
        b = make_empty_board(3, 3)
        for r in range(3):
            b[r][1] = "X"
        self.assertNotIn(1, legal_moves(b))
        self.assertIn(0, legal_moves(b))
        self.assertIn(2, legal_moves(b))

    def test_draw_board_has_no_legal_moves(self):
        b = [
            list("XOX"),
            list("OXO"),
            list("XOX"),
        ]
        self.assertEqual(legal_moves(b), [])


class TestGameStatus(unittest.TestCase):
    def test_empty_board_is_ongoing(self):
        b = make_empty_board(6, 7)
        st = game_status(b, 4)
        self.assertEqual(st["status"], "ongoing")
        self.assertIsNone(st["winner"])

    def test_horizontal_win(self):
        b = make_empty_board(6, 7)
        for c in range(4):
            b[5][c] = "X"
        st = game_status(b, 4)
        self.assertEqual(st["status"], "won")
        self.assertEqual(st["winner"], "X")

    def test_vertical_win(self):
        b = make_empty_board(6, 7)
        for r in range(2, 6):  # rows 2,3,4,5
            b[r][0] = "O"
        st = game_status(b, 4)
        self.assertEqual(st["status"], "won")
        self.assertEqual(st["winner"], "O")

    def test_diagonal_up_right_win(self):
        # X on (5,0), (4,1), (3,2), (2,3) - rises to the right
        b = make_empty_board(6, 7)
        coords = [(5, 0), (4, 1), (3, 2), (2, 3)]
        for r, c in coords:
            b[r][c] = "X"
        st = game_status(b, 4)
        self.assertEqual(st["status"], "won")
        self.assertEqual(st["winner"], "X")

    def test_diagonal_down_right_win(self):
        # X on (2,0), (3,1), (4,2), (5,3) - falls to the right
        b = make_empty_board(6, 7)
        for r, c in [(2, 0), (3, 1), (4, 2), (5, 3)]:
            b[r][c] = "X"
        st = game_status(b, 4)
        self.assertEqual(st["status"], "won")
        self.assertEqual(st["winner"], "X")

    def test_no_false_positive_with_gap(self):
        b = make_empty_board(6, 7)
        b[5][0] = b[5][1] = b[5][3] = "X"  # gap at col 2
        st = game_status(b, 4)
        self.assertEqual(st["status"], "ongoing")
        self.assertIsNone(st["winner"])

    def test_three_in_a_row_not_a_win(self):
        b = make_empty_board(6, 7)
        for c in range(3):
            b[5][c] = "X"
        st = game_status(b, 4)
        self.assertEqual(st["status"], "ongoing")

    def test_draw_detection(self):
        b = [
            list("XOX"),
            list("OXO"),
            list("XOX"),
        ]
        st = game_status(b, 3)
        self.assertEqual(st["status"], "draw")
        self.assertIsNone(st["winner"])

    def test_draw_takes_priority_over_no_winner(self):
        # If the board is full with no winner, status is `draw`, not `ongoing`.
        b = [
            list("XXO"),
            list("OOX"),
            list("XXO"),
        ]
        st = game_status(b, 3)
        self.assertEqual(st["status"], "draw")


class TestRenderBoard(unittest.TestCase):
    def test_empty_board_renders_with_dots(self):
        b = make_empty_board(6, 7)
        out = render_board(b)
        # Should be 6 lines, each 7 chars wide
        lines = out.strip("\n").split("\n")
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertEqual(len(line), 7)
            for ch in line:
                self.assertIn(ch, (".", "X", "O"))

    def test_rendered_after_drop(self):
        b = make_empty_board(6, 7)
        drop_piece(b, 0, "X")
        out = render_board(b)
        # bottom-left cell should be X
        self.assertEqual(out.strip("\n").split("\n")[-1][0], "X")


class TestAiEasy(unittest.TestCase):
    def test_returns_legal_column(self):
        b = make_empty_board(6, 7)
        for _ in range(50):
            col = _ai_easy(b, 4, "X", "O")
            self.assertIn(col, range(7))

    def test_skips_full_columns(self):
        b = make_empty_board(3, 3)
        b[0][0] = b[1][0] = b[2][0] = "X"  # column 0 full
        for _ in range(20):
            col = _ai_easy(b, 3, "O", "X")
            self.assertNotEqual(col, 0)


class TestAiMedium(unittest.TestCase):
    def test_takes_immediate_win(self):
        # O has three in a row at the bottom of col 0/1/2. Col 3 wins.
        b = make_empty_board(6, 7)
        b[5][0] = b[5][1] = b[5][2] = "O"
        col = _ai_medium(b, 4, "O", "X")
        self.assertEqual(col, 3)

    def test_blocks_opponent_immediate_win(self):
        # X has 3-in-a-row at the bottom of cols 0/1/2; O must block col 3.
        b = make_empty_board(6, 7)
        b[5][0] = b[5][1] = b[5][2] = "X"
        col = _ai_medium(b, 4, "O", "X")
        self.assertEqual(col, 3)

    def test_returns_legal_when_no_threat(self):
        b = make_empty_board(6, 7)
        col = _ai_medium(b, 4, "O", "X")
        self.assertIn(col, range(7))


class TestAiHard(unittest.TestCase):
    def test_returns_legal_column(self):
        b = make_empty_board(6, 7)
        col = _ai_hard(b, 4, "X", "O")
        self.assertIn(col, range(7))

    def test_takes_winning_move_over_blunder(self):
        # Hard AI is O. Set up: X threatens col 3 to win horizontally,
        # O has a winning move available at col 6. O must take the win.
        b = make_empty_board(6, 7)
        # Force O's pieces to be the lowest so col 6 is playable as a win.
        b[5][0] = b[5][1] = b[5][2] = "O"
        b[5][6] = "O"  # need one more O at row 4 col 6 to win vertically? No - 4 in a row needs 4 Os
        # Simpler: vertical win for O in col 5 - three Os already, drop a fourth
        for r in range(2, 5):
            b[r][5] = "O"
        # And X threatens at row 5 col 3
        b[5][3] = "X"
        # Make a baseline X threat too: X has 3-in-a-row at col 0,1,2 row 5
        b[5][0] = b[5][1] = b[5][2] = "X"
        col = _ai_hard(b, 4, "O", "X")
        # Should pick col 5 to win vertically (4th O)
        self.assertEqual(col, 5)

    def test_no_illegal_moves_on_unusual_board(self):
        # 1x3 board, 3-in-a-row to win. Only one legal move at any time.
        b = [[EMPTY, EMPTY, EMPTY]]
        col = _ai_hard(b, 3, "X", "O")
        self.assertIn(col, (0, 1, 2))


class TestAiSuperChallenge(unittest.TestCase):
    # The plugin's super_challenge opening move on an empty Connect Four
    # board. Connect Four theory: open in the centre column.
    EXPECTED_OPENING_COL = 3

    def test_uses_opening_book_on_empty_6x7(self):
        b = make_empty_board(6, 7)
        col = _ai_super_challenge(b, 4, "X", "O")
        self.assertEqual(col, self.EXPECTED_OPENING_COL)

    def test_after_one_move_still_legal(self):
        b = make_empty_board(6, 7)
        # Opponent plays a non-center move
        drop_piece(b, 0, "O")
        col = _ai_super_challenge(b, 4, "X", "O")
        self.assertIn(col, range(7))


class TestDifficultyLevelsConst(unittest.TestCase):
    def test_all_levels_defined(self):
        for level in ("easy", "medium", "hard", "super_challenge"):
            self.assertIn(level, DIFFICULTY_LEVELS)


# --------------------------------------------------------------------- #
# Plugin-instance tests
# --------------------------------------------------------------------- #

class TestPluginMetadata(unittest.TestCase):
    def test_plugin_name_and_description(self):
        p = TicTacToePlugin()
        self.assertEqual(p.name, PLUGIN_NAME)
        self.assertIsInstance(p.description, str)
        self.assertGreater(len(p.description), 20)

    def test_default_config_has_all_keys(self):
        cfg = TicTacToePlugin().get_default_config()
        for key in ("rows", "cols", "in_a_row",
                    "difficulty", "opponent", "human_symbol"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["rows"], DEFAULT_ROWS)
        self.assertEqual(cfg["cols"], DEFAULT_COLS)
        self.assertEqual(cfg["in_a_row"], DEFAULT_IN_A_ROW)


class TestPluginNewGame(unittest.TestCase):
    def setUp(self):
        self.p = TicTacToePlugin()

    def test_new_game_ai_defaults(self):
        r = self.p.execute("new_game", {"opponent": "ai",
                                         "difficulty": "easy"})
        self.assertTrue(r.get("ok"))
        self.assertIn("game", r)
        g = r["game"]
        self.assertEqual(g["opponent"], "ai")
        self.assertEqual(g["difficulty"], "easy")
        self.assertFalse(g["finished"])
        self.assertEqual(g["rows"], DEFAULT_ROWS)
        self.assertEqual(g["cols"], DEFAULT_COLS)
        self.assertEqual(g["in_a_row"], DEFAULT_IN_A_ROW)
        # All cells empty
        self.assertEqual(sum(1 for row in g["board"] for c in row if c != EMPTY),
                         0)

    def test_new_game_human_vs_human(self):
        r = self.p.execute("new_game", {"opponent": "human"})
        self.assertTrue(r["ok"])
        g = r["game"]
        self.assertEqual(g["opponent"], "human")
        # In H-vs-H, it's still X's turn to start.
        self.assertIn(g["turn"], ("X", "O"))

    def test_new_game_custom_dimensions(self):
        r = self.p.execute("new_game", {
            "opponent": "human",
            "rows": 4,
            "cols": 5,
            "in_a_row": 3,
        })
        self.assertTrue(r["ok"])
        g = r["game"]
        self.assertEqual(g["rows"], 4)
        self.assertEqual(g["cols"], 5)
        self.assertEqual(g["in_a_row"], 3)
        self.assertEqual(len(g["board"]), 4)
        self.assertEqual(len(g["board"][0]), 5)

    def test_new_game_human_symbol_choice(self):
        r = self.p.execute("new_game", {
            "opponent": "ai",
            "difficulty": "easy",
            "human_symbol": "O",
        })
        g = r["game"]
        self.assertEqual(g["human_symbol"], "O")
        self.assertEqual(g["ai_symbol"], "X")
        # If human is O, AI (X) moves first.
        self.assertEqual(g["turn"], "ai")

    def test_new_game_rejects_bad_difficulty(self):
        r = self.p.execute("new_game", {"opponent": "ai",
                                         "difficulty": "impossible"})
        self.assertFalse(r.get("ok"))

    def test_new_game_rejects_bad_opponent(self):
        r = self.p.execute("new_game", {"opponent": "dog"})
        self.assertFalse(r.get("ok"))

    def test_reset_clears_state(self):
        self.p.execute("new_game", {"opponent": "human"})
        self.p.execute("play", {"column": 0})
        r = self.p.execute("reset", {})
        self.assertTrue(r["ok"])
        self.assertIsNone(r.get("game"))
        # Status should be back to no-game state.
        s = self.p.execute("status", {})
        self.assertFalse(s.get("finished"))
        # A fresh game afterwards works.
        r2 = self.p.execute("new_game", {"opponent": "human"})
        self.assertTrue(r2["ok"])


class TestPluginBoard(unittest.TestCase):
    def setUp(self):
        self.p = TicTacToePlugin()
        self.p.execute("new_game", {"opponent": "human"})

    def test_board_command_without_game_succeeds(self):
        p = TicTacToePlugin()
        r = p.execute("board", {})
        # Either an error or a default-shape response is fine
        # but it must NOT crash.
        self.assertIsInstance(r, dict)

    def test_board_after_move_reflects_piece(self):
        self.p.execute("play", {"column": 0})
        r = self.p.execute("board", {})
        self.assertTrue(r.get("ok"))
        b = r["board"]
        # Bottom-left cell should now be X
        self.assertEqual(b[5][0], "X")


class TestPluginPlay(unittest.TestCase):
    def setUp(self):
        self.p = TicTacToePlugin()
        self.p.execute("new_game", {"opponent": "human"})

    def test_play_alternates_in_human_vs_human(self):
        r1 = self.p.execute("play", {"column": 0})
        self.assertEqual(r1["game"]["board"][5][0], "X")
        self.assertEqual(r1["game"]["turn"], "O")

        r2 = self.p.execute("play", {"column": 0})
        self.assertEqual(r2["game"]["board"][4][0], "O")
        self.assertEqual(r2["game"]["turn"], "X")

    def test_play_rejects_invalid_column(self):
        r = self.p.execute("play", {"column": 99})
        self.assertFalse(r.get("ok"))

    def test_play_rejects_full_column(self):
        # Fill a column up on a 3-row board? Use custom dims for speed.
        self.p.execute("reset", {})
        self.p.execute("new_game", {
            "opponent": "human", "rows": 2, "cols": 3, "in_a_row": 2,
        })
        self.p.execute("play", {"column": 0})
        self.p.execute("play", {"column": 0})
        r = self.p.execute("play", {"column": 0})
        self.assertFalse(r.get("ok"))

    def test_play_with_no_game_errors_cleanly(self):
        p = TicTacToePlugin()
        r = p.execute("play", {"column": 0})
        self.assertFalse(r.get("ok"))

    def test_play_after_finish_errors(self):
        # Force a 2x3 board, 3-in-a-row needed (can't on 2 rows ->
        # easier: 3x3 board, 3-in-a-row). Build a guaranteed H-vs-H X win:
        self.p.execute("reset", {})
        self.p.execute("new_game", {
            "opponent": "human", "rows": 3, "cols": 3, "in_a_row": 3,
        })
        # Play out an X win in the bottom row.
        # But X only owns col 0/2/... we can use cols 0,1,2 with O playing
        # in cols 1 and 2 for distraction. Easier: just fill board and
        # detect draw. For "won" we just check the finished guard triggers.
        # Fill the 3x3 with alternating moves ending in a win.
        # X at (2,0), O at (2,1), X at (2,2), O at (1,2), X at (1,0)
        # -> X has (2,0),(2,2),(1,0) - not a win. Try again.
        # Cleanest: tiny board with 1-in-a-row means first move wins.
        self.p.execute("reset", {})
        self.p.execute("new_game", {
            "opponent": "human", "rows": 1, "cols": 1, "in_a_row": 1,
        })
        r = self.p.execute("play", {"column": 0})
        self.assertTrue(r["ok"])
        self.assertTrue(r["game"]["finished"])
        # Now the game is finished; further play should be refused.
        r2 = self.p.execute("play", {"column": 0})
        self.assertFalse(r2.get("ok"))

    def test_unknown_command_returns_error(self):
        r = self.p.execute("totally_made_up", {})
        self.assertFalse(r.get("ok"))


class TestPluginStatus(unittest.TestCase):
    def setUp(self):
        self.p = TicTacToePlugin()

    def test_status_no_game(self):
        s = self.p.execute("status", {})
        self.assertFalse(s.get("finished"))
        self.assertEqual(s.get("move_count", 0), 0)

    def test_status_after_move(self):
        self.p.execute("new_game", {"opponent": "human"})
        self.p.execute("play", {"column": 3})
        s = self.p.execute("status", {})
        self.assertEqual(s["move_count"], 1)
        self.assertEqual(s["cols"], DEFAULT_COLS)
        self.assertEqual(s["rows"], DEFAULT_ROWS)
        self.assertFalse(s["finished"])


class TestPluginVsAi(unittest.TestCase):
    def test_ai_replies_in_same_response(self):
        p = TicTacToePlugin()
        p.execute("new_game", {"opponent": "ai", "difficulty": "easy"})
        r = p.execute("play", {"column": 0})
        self.assertTrue(r["ok"])
        # In vs-AI mode, both the human's move AND the AI's reply are made.
        # That means the move_count should be 2 after one play call.
        self.assertEqual(r["game"]["move_count"], 2)
        # And it should be the human's turn again (since human always alternates).
        # Whether the human started or not depends on symbol choice; in
        # default config human=X and AI=O, so AI just moved -> human turn.
        self.assertEqual(r["game"]["turn"], "human")

    def test_ai_easy_does_not_crash_on_tiny_board(self):
        p = TicTacToePlugin()
        p.execute("new_game", {
            "opponent": "ai",
            "difficulty": "easy",
            "rows": 2, "cols": 2, "in_a_row": 2,
        })
        # Play out the game; with 2x2 board, 2-in-a-row, one player will win
        # within 4 moves.
        played = 0
        for _ in range(10):
            s = p.execute("status", {})
            if s.get("finished"):
                break
            r = p.execute("play", {"column": 0})
            self.assertTrue(r["ok"])
            played += 1
        self.assertGreaterEqual(played, 1)


class TestHumanVsHumanWin(unittest.TestCase):
    def test_x_can_win_horizontally(self):
        p = TicTacToePlugin()
        p.execute("new_game", {
            "opponent": "human",
            "rows": 3, "cols": 4, "in_a_row": 3,
        })
        # Build X win on the bottom row across cols 0,1,2.
        # Each X move at col 0 lands at bottom (row 2). After X's first move
        # the turn is O. We play O in col 3 each round so O doesn't get in
        # the way, except O needs an empty column. Use cols 0..2 for X and
        # place O above to not interfere.
        # Move 1: X at col 0  -> X(2,0), turn -> O
        # Move 2: O at col 0  -> O(1,0), turn -> X
        # Move 3: X at col 1  -> X(2,1), turn -> O
        # Move 4: O at col 1  -> O(1,1), turn -> X
        # Move 5: X at col 2  -> X(2,2), X wins.
        seq = [0, 0, 1, 1, 2]
        last = None
        for c in seq:
            last = p.execute("play", {"column": c})
            self.assertTrue(last.get("ok"), msg=f"play({c}) failed: {last}")
        self.assertTrue(last["game"]["finished"])
        self.assertEqual(last["game"]["result"]["status"], "won")
        self.assertEqual(last["game"]["result"]["winner"], "X")

    def test_o_can_win_vertically(self):
        p = TicTacToePlugin()
        p.execute("new_game", {
            "opponent": "human",
            "rows": 4, "cols": 3, "in_a_row": 4,
        })
        # Play so that O ends up with 4 in a row in column 0.
        # Sequence (X always plays first so we get O=X_move+1):
        # Turn order is X,O,X,O,...
        # Want: O at (3,0),(2,0),(1,0),(0,0) and X elsewhere.
        # Move 1: X col 1  -> X(3,1)
        # Move 2: O col 0  -> O(3,0)
        # Move 3: X col 1  -> X(2,1)
        # Move 4: O col 0  -> O(2,0)
        # Move 5: X col 1  -> X(1,1)
        # Move 6: O col 0  -> O(1,0)
        # Move 7: X col 1  -> X(0,1)
        # Move 8: O col 0  -> O(0,0) - O wins 4 in a row.
        seq = [1, 0, 1, 0, 1, 0, 1, 0]
        last = None
        for c in seq:
            last = p.execute("play", {"column": c})
            self.assertTrue(last.get("ok"), msg=f"play({c}) failed: {last}")
        self.assertTrue(last["game"]["finished"])
        self.assertEqual(last["game"]["result"]["status"], "won")
        self.assertEqual(last["game"]["result"]["winner"], "O")


class TestDraw(unittest.TestCase):
    def test_full_3x3_with_no_winner_is_draw(self):
        p = TicTacToePlugin()
        p.execute("new_game", {
            "opponent": "human",
            "rows": 3, "cols": 3, "in_a_row": 4,  # 4 needed: impossible -> draw
        })
        # Fill the entire board. X plays first.
        # Sequence that fills the 3x3:
        #   X(2,0) O(2,1) X(2,2)   <- bottom row alternating
        #   X(1,0) O(1,1) X(1,2)
        #   X(0,0) O(0,1) X(0,2)
        # Plan: X always at col 0 or 2 on odd moves, O at col 1 otherwise.
        # Simpler: just iterate col = move%3.
        last = None
        for i in range(9):
            col = i % 3
            last = p.execute("play", {"column": col})
            self.assertTrue(last.get("ok"),
                            msg=f"play({col}) on move {i} failed: {last}")
        self.assertTrue(last["game"]["finished"])
        self.assertEqual(last["game"]["result"]["status"], "draw")


class TestSnapshotIsolation(unittest.TestCase):
    """The board returned to the user must be a copy; mutating it
    shouldn't change the plugin's internal state."""

    def test_board_copy_isolated(self):
        p = TicTacToePlugin()
        p.execute("new_game", {"opponent": "human"})
        r = p.execute("board", {})
        b = r["board"]
        b[5][0] = "Z"   # mutate the returned copy
        # Re-fetch - it should still be empty.
        r2 = p.execute("board", {})
        self.assertEqual(r2["board"][5][0], EMPTY)


class TestHardAiMakesProgress(unittest.TestCase):
    def test_hard_ai_eventually_wins_or_draws_on_tiny_board(self):
        """On a 2x2 board with 2-in-a-row, hard AI vs a sequence of column-1
        plays (which give the AI a free diagonal win):
            X always plays col 1 -> lands at (1,1).
            AI responds optimally.
        We just check the game terminates and produces a defined result."""
        p = TicTacToePlugin()
        p.execute("new_game", {
            "opponent": "ai",
            "difficulty": "hard",
            "rows": 3, "cols": 3, "in_a_row": 3,
        })
        last = None
        for _ in range(20):
            s = p.execute("status", {})
            if s.get("finished"):
                break
            r = p.execute("play", {"column": 1})
            self.assertTrue(r["ok"], msg=str(r))
            last = r
        self.assertIsNotNone(last)
        self.assertTrue(last["game"]["finished"])
        self.assertIn(last["game"]["result"]["status"], ("won", "draw"))


# --------------------------------------------------------------------- #
# Module entry-point
# --------------------------------------------------------------------- #

if __name__ == "__main__":
    unittest.main(verbosity=2)
