# TicTacToePlugin
The TicTacToePlugin implements a configurable m-in-a-row game that can be played against the user or human-vs-human.

### Commands
* `new_game`: Starts a fresh game.
* `play`: Drops a piece in a column. With an AI opponent, the AI reply is included in the same response.
* `board`: Shows the current board.
* `reset`: Ends the current game.

### Usage
To use the TicTacToePlugin, simply call the desired command with the relevant arguments. For example:
* To start a fresh game of Tic Tac Toe, call `new_game` with the argument "rows": 6, "cols": 7, and "in_a_row": 4.
* To drop a piece in column 3, call `play` with the argument "column": 3.