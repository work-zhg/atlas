# 由 acp/scripts/generate_v1.py 从官方 ACP schema（schema-v1.23.0，sha256 3c17bd6385d90cf6…）生成。
# 不要手改：升级 ACP 时更新 acp/schema/v1/ 后重新运行生成脚本。

"""ACP v1 的方法名（来自官方 meta.json）。"""

from __future__ import annotations

# client → agent
INITIALIZE = "initialize"
AUTHENTICATE = "authenticate"
SESSION_NEW = "session/new"
SESSION_LOAD = "session/load"
SESSION_SET_MODE = "session/set_mode"
SESSION_SET_CONFIG_OPTION = "session/set_config_option"
SESSION_PROMPT = "session/prompt"
SESSION_CANCEL = "session/cancel"
SESSION_LIST = "session/list"
SESSION_DELETE = "session/delete"
SESSION_RESUME = "session/resume"
SESSION_CLOSE = "session/close"
LOGOUT = "logout"

# agent → client
SESSION_REQUEST_PERMISSION = "session/request_permission"
SESSION_UPDATE = "session/update"
FS_WRITE_TEXT_FILE = "fs/write_text_file"
FS_READ_TEXT_FILE = "fs/read_text_file"
TERMINAL_CREATE = "terminal/create"
TERMINAL_OUTPUT = "terminal/output"
TERMINAL_RELEASE = "terminal/release"
TERMINAL_WAIT_FOR_EXIT = "terminal/wait_for_exit"
TERMINAL_KILL = "terminal/kill"
ELICITATION_CREATE = "elicitation/create"
ELICITATION_COMPLETE = "elicitation/complete"

# 双向
CANCEL_REQUEST = "$/cancel_request"

PROTOCOL_VERSION = 1
