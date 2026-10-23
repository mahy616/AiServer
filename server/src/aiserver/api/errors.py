class ApiError(Exception):
    def __init__(self, status, code, details=None):
        super().__init__(code)
        self.status, self.code, self.details = status, code, details or {}

def safe_error(code, details=None):
    return {"code": code, "message": code.replace("_", " ").capitalize(),
            "retryable": code in {"STORAGE_UNAVAILABLE", "SERVICE_NOT_READY", "ENGINE_RECOVERING",
                                 "QUEUE_FULL", "RATE_LIMITED"},
            "details": details or {}}
