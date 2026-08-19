# ApprovedShellPlugin
The ApprovedShellPlugin allows execution of shell commands within the Sepian workspace, but requires user approval before running.

### Commands
* `run_command`: Executes a shell command with approval required before running.

### Usage
To use the ApprovedShellPlugin, simply call `run_command` with the desired shell command as an argument. For example:
* To execute the command "git status", call `run_command` with the argument "cmd": "git status".