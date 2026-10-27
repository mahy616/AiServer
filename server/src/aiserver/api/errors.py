class ApiError(Exception):
    def __init__(self, status, code, details=None):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details or {}

def safe_error(code, details=None):
    message = code.replace("_", " ").capitalize()
    if code == "OUTPUT_TRUNCATED":
        message = "模型输出达到本次上限，已截断。思考和正文共用输出预算；请提高输出上限、关闭思考或分批生成。"
    return {"code": code, "message": message,
            "retryable": code in {"STORAGE_UNAVAILABLE", "SERVICE_NOT_READY", "ENGINE_RECOVERING",
                                 "QUEUE_FULL", "RATE_LIMITED"},
            "details": details or {}}
